"""N7B paper-runner tests: sweep chaining, feed evidence, double-gate
wiring, edge decay, restart-durable risk state (N7-BLOCKER-001/002)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from event_arbitrage_lab.collector import parse_sweep, serialize_sweep
from event_arbitrage_lab.empirical import summarize
from event_arbitrage_lab.ledger import EvidenceLedger
from event_arbitrage_lab.paper_runner import (
    PaperProcessor,
    cumulative_counts,
    read_checkpoint,
    restore_gate_from_checkpoint,
    run_paper_validation,
    run_service,
    write_checkpoint,
)
from event_arbitrage_lab.risk import RiskGate, RiskPolicy
from event_arbitrage_lab.schemas import BookLevel, Market, OrderBook, Outcome

T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
VENUE = "polymarket"
MID = "mkt-1"


def market():
    return Market(
        venue=VENUE, market_id=MID, event_id="evt-1",
        outcomes=(Outcome(outcome_id="0", label="Up"),
                  Outcome(outcome_id="1", label="Down")),
        mutually_exclusive=True, exhaustive=True,
        resolution_source="test oracle", payout_per_unit=Decimal(1),
    )


def book(outcome, bid, ask, ts, bid_size=100, ask_size=100):
    return OrderBook(
        venue=VENUE, market_id=MID, outcome_id=outcome,
        bids=(BookLevel(price=Decimal(bid), size=Decimal(bid_size)),),
        asks=(BookLevel(price=Decimal(ask), size=Decimal(ask_size)),),
        timestamp=ts,
    )


def sweep(t, yes, no, fee_rate="0"):
    """One collector-shaped sweep frame for MID (parsed shape: t is
    datetime, books are OrderBook — what the processor consumes)."""
    return {
        "v": 1,
        "t": t,
        "markets": [{
            "market": market(),
            "fee": {"enabled": True, "rate": Decimal(fee_rate),
                    "exponent": 1, "taker_only": True},
            "category": None,
            "yes": yes, "no": no, "error": None,
        }],
    }


def bad_sweep(t, error="ConnectionError: boom"):
    return {"v": 1, "t": t,
            "markets": [{"market": market(), "fee": {}, "category": None,
                         "yes": None, "no": None, "error": error}]}


def policy(**over):
    base = dict(
        max_staleness=timedelta(seconds=45),
        min_net_profit=Decimal("0.01"),
        min_net_edge_per_share=Decimal("0.0001"),
        min_capacity=Decimal(1),
        max_exposure_per_asset=Decimal(10000),
        max_total_exposure=Decimal(100000),
        max_leg_risk_shares=Decimal(10000),
        max_spread=Decimal("0.2"),
        max_consecutive_failures=5,
    )
    base.update(over)
    return RiskPolicy(**base)


def run(sweeps):
    ledger = EvidenceLedger()
    gate = RiskGate(policy(), ledger)
    result = run_paper_validation(sweeps, policy=policy(), size=Decimal(50),
                                  ledger=ledger, risk_gate=gate)
    return result, ledger, gate


def good_pair(t0, n, step_s=10):
    """n sweeps of a persistent 0.97-sum pair starting at t0."""
    return [
        sweep(t0 + timedelta(seconds=step_s * i),
              book("0", "0.45", "0.48", t0 + timedelta(seconds=step_s * i)),
              book("1", "0.46", "0.49", t0 + timedelta(seconds=step_s * i)))
        for i in range(n)
    ]


class TestChaining:
    def test_consecutive_sweeps_chain_exec_is_next_scan(self):
        result, ledger, _ = run(good_pair(T0, 2))
        assert result.report.ticks == 1  # last frame has no successor
        tick = next(r for r in ledger.records if r.kind == "TICK_SCANNED")
        p = json.loads(tick.payload_json)
        assert p["scan_t"] == T0.isoformat()
        assert p["exec_t"] == (T0 + timedelta(seconds=10)).isoformat()
        assert p["latency_interval_s"] == 10.0

    def test_feed_gap_breaks_chain_and_is_evidence(self):
        s1 = sweep(T0, book("0", "0.45", "0.48", T0),
                   book("1", "0.46", "0.49", T0))
        s3t = T0 + timedelta(seconds=20)
        s4t = T0 + timedelta(seconds=30)
        sweeps = [s1, bad_sweep(T0 + timedelta(seconds=10)),
                  sweep(s3t, book("0", "0.45", "0.48", s3t),
                        book("1", "0.46", "0.49", s3t)),
                  sweep(s4t, book("0", "0.45", "0.48", s4t),
                        book("1", "0.46", "0.49", s4t))]
        result, ledger, _ = run(sweeps)
        # chain (s1,bad) broken; (s3,s4) chains -> 1 tick
        assert result.report.ticks == 1
        assert result.feed_failures == 1
        assert result.feed_restores == 1
        frames = [json.loads(r.payload_json) for r in ledger.records
                  if r.kind == "MARKET_FRAME"]
        assert len(frames) == 4
        assert sum(1 for f in frames if not f["ok"]) == 1
        restored = next(r for r in ledger.records if r.kind == "FEED_RESTORED")
        p = json.loads(restored.payload_json)
        assert p["failed_frames"] == 1
        assert p["duration_s"] == 10.0  # T0+10 -> T0+20

    def test_absent_market_is_feed_failure(self):
        s1 = sweep(T0, book("0", "0.45", "0.48", T0),
                   book("1", "0.46", "0.49", T0))
        empty = {"v": 1, "t": T0 + timedelta(seconds=10), "markets": []}
        result, ledger, _ = run([s1, empty])
        frames = [json.loads(r.payload_json) for r in ledger.records
                  if r.kind == "MARKET_FRAME"]
        assert [f["ok"] for f in frames] == [True, False]
        assert frames[1]["error"] == "absent from sweep"


class TestRetirement:
    """Permanently-gone markets (closed/delisted, e.g. venue 404) must
    not accrue absence-failure frames forever: after retire_after
    consecutive absent sweeps the runner emits MARKET_RETIRED and stops
    tracking — matching the collector's 404-drop on the runner side."""

    def run_with_retire(self, sweeps, retire_after):
        ledger = EvidenceLedger()
        gate = RiskGate(policy(), ledger)
        result = run_paper_validation(
            sweeps, policy=policy(), size=Decimal(50),
            retire_after=retire_after, ledger=ledger, risk_gate=gate)
        return result, ledger, gate

    def test_absent_market_retires_then_silent(self):
        s1 = sweep(T0, book("0", "0.45", "0.48", T0),
                   book("1", "0.46", "0.49", T0))
        empties = [{"v": 1, "t": T0 + timedelta(seconds=10 * i),
                    "markets": []} for i in range(1, 7)]  # 6 absences
        result, ledger, _ = self.run_with_retire([s1] + empties, 3)
        frames = [json.loads(r.payload_json) for r in ledger.records
                  if r.kind == "MARKET_FRAME"]
        # 1 ok + exactly retire_after absence failures — then silence
        assert [f["ok"] for f in frames] == [True, False, False, False]
        assert result.retired == 1
        retired = [json.loads(r.payload_json) for r in ledger.records
                   if r.kind == "MARKET_RETIRED"]
        assert len(retired) == 1
        assert retired[0]["market"] == MID
        assert retired[0]["consecutive_failed_sweeps"] == 3
        assert retired[0]["down_from"] == (
            T0 + timedelta(seconds=10)).isoformat()
        assert cumulative_counts(ledger)["markets_retired"] == 1

    def test_retired_market_reappears_as_fresh_chain(self):
        s1 = sweep(T0, book("0", "0.45", "0.48", T0),
                   book("1", "0.46", "0.49", T0))
        gap = [{"v": 1, "t": T0 + timedelta(seconds=10 * i), "markets": []}
               for i in range(1, 6)]  # 5 absences -> retired at retire_after=3
        back1t = T0 + timedelta(seconds=60)
        back2t = T0 + timedelta(seconds=70)
        back = [sweep(back1t, book("0", "0.45", "0.48", back1t),
                      book("1", "0.46", "0.49", back1t)),
                sweep(back2t, book("0", "0.45", "0.48", back2t),
                      book("1", "0.46", "0.49", back2t))]
        result, ledger, _ = self.run_with_retire([s1] + gap + back, 3)
        assert result.retired == 1
        # retirement ends the gap: reappearance is NOT a feed restore
        assert not [r for r in ledger.records if r.kind == "FEED_RESTORED"]
        # fresh chain: back1 has no predecessor -> exactly one tick (back1->back2)
        assert result.report.ticks == 1
        tick = next(r for r in ledger.records if r.kind == "TICK_SCANNED")
        p = json.loads(tick.payload_json)
        assert p["scan_t"] == back1t.isoformat()
        assert p["exec_t"] == back2t.isoformat()
        summary = summarize(ledger.records)
        assert summary["feed"]["retired"] == 1
        assert summary["per_market"][MID]["retired"] is True

    def test_return_before_threshold_still_restores(self):
        s1 = sweep(T0, book("0", "0.45", "0.48", T0),
                   book("1", "0.46", "0.49", T0))
        gap = [{"v": 1, "t": T0 + timedelta(seconds=10 * i), "markets": []}
               for i in range(1, 3)]  # 2 absences < retire_after=3
        backt = T0 + timedelta(seconds=30)
        back = [sweep(backt, book("0", "0.45", "0.48", backt),
                      book("1", "0.46", "0.49", backt))]
        result, ledger, _ = self.run_with_retire([s1] + gap + back, 3)
        assert result.retired == 0
        assert result.feed_restores == 1
        restored = next(r for r in ledger.records if r.kind == "FEED_RESTORED")
        assert json.loads(restored.payload_json)["failed_frames"] == 2

    def test_retire_after_must_be_positive(self):
        ledger = EvidenceLedger()
        with pytest.raises(ValueError, match="retire_after"):
            PaperProcessor(policy=policy(), size=Decimal(50),
                           retire_after=0, ledger=ledger,
                           risk_gate=RiskGate(policy(), ledger))


class TestProductionPath:
    def test_profitable_pair_executes_with_double_gate_evidence(self):
        result, ledger, gate = run(good_pair(T0, 2))
        assert result.report.executions == 1
        assert result.report.clean_hedged == 1
        assert result.report.total_net_pnl == Decimal("1.500000")  # 50 x 0.03
        kinds = [r.kind for r in ledger.records]
        assert kinds == [
            "MARKET_FRAME",  # sweep 1
            "MARKET_FRAME",  # sweep 2
            "TICK_SCANNED", "OPPORTUNITY",
            "RISK_DECISION",  # PRE_TRADE
            "RISK_DECISION",  # EXECUTION_TIME
            "EXECUTION_SNAPSHOT", "EXECUTION", "RISK_STATE",
        ]
        decs = [r for r in ledger.records if r.kind == "RISK_DECISION"]
        assert '"phase":"PRE_TRADE"' in decs[0].payload_json
        assert '"phase":"EXECUTION_TIME"' in decs[1].payload_json
        # frame evidence pins books by hash, not by payload duplication
        frame = json.loads(ledger.records[0].payload_json)
        assert frame["ok"] is True
        assert len(frame["yes"]["sha256"]) == 64
        assert frame["yes"]["best_ask"] == "0.48"

    def test_edge_decay_blocks_at_execution_time(self):
        # scan t: asks sum 0.97 (edge 0.03); exec t+1: yes ask jumped to
        # 0.52 -> sum 1.01, edge VANISHED -> EXECUTION_TIME gate blocks
        s1 = sweep(T0, book("0", "0.45", "0.48", T0),
                   book("1", "0.46", "0.49", T0))
        t1 = T0 + timedelta(seconds=10)
        s2 = sweep(t1, book("0", "0.50", "0.52", t1),
                   book("1", "0.46", "0.49", t1))
        result, ledger, _ = run([s1, s2])
        assert result.report.executions == 0
        assert result.report.risk_rejected == 1
        exe = [r for r in ledger.records
               if r.kind == "RISK_DECISION"
               and '"phase":"EXECUTION_TIME"' in r.payload_json]
        assert len(exe) == 1
        assert "EDGE_BELOW_THRESHOLD" in exe[0].payload_json
        assert not [r for r in ledger.records if r.kind == "EXECUTION"]

    def test_no_edge_no_opportunity(self):
        t1 = T0 + timedelta(seconds=10)
        sweeps = [sweep(T0, book("0", "0.50", "0.52", T0),
                        book("1", "0.50", "0.52", T0)),
                  sweep(t1, book("0", "0.50", "0.52", t1),
                        book("1", "0.50", "0.52", t1))]
        result, ledger, _ = run(sweeps)
        assert result.report.opportunities == 0
        assert result.report.executions == 0
        tick = next(r for r in ledger.records if r.kind == "TICK_SCANNED")
        assert '"found":0' in tick.payload_json

    def test_determinism_same_sweeps_same_root(self):
        lines = [serialize_sweep(s) for s in good_pair(T0, 3)]
        sweeps = [parse_sweep(l) for l in lines]
        r1, l1, _ = run(sweeps)
        r2, l2, _ = run(sweeps)
        assert r1.report == r2.report
        assert l1.root_hash == l2.root_hash
        assert l1.records == l2.records


class TestEmpirical:
    def test_summary_rebuildable_from_ledger_alone(self):
        sweeps = good_pair(T0, 3)
        # 4th sweep: edge gone (yes ask 0.52) -> exec gate blocks
        t3 = T0 + timedelta(seconds=30)
        sweeps.append(sweep(t3, book("0", "0.50", "0.52", t3),
                            book("1", "0.46", "0.49", t3)))
        result, ledger, _ = run(sweeps)
        rep = result.report
        assert rep.ticks == 3 and rep.executions == 2
        assert rep.risk_rejected == 1
        s = summarize(ledger.records)
        assert s["ticks_scanned"] == 3
        assert s["opportunities"] == 3
        assert s["gates"]["pre_trade"]["approved"] == 3
        assert s["gates"]["execution_time"]["approved"] == 2
        assert s["gates"]["execution_time"]["rejected"] == 1
        assert "EXECUTION_TIME:EDGE_BELOW_THRESHOLD" in s["block_reasons"]
        assert s["executions"]["clean"] == 2
        assert s["executions"]["fill_rate_pct"] == 100.0
        assert s["edge_decay"]["pairs"] == 3
        # pair 3 decayed: scan net 1.5 -> exec leg B unfillable within
        # limit -> paired 0 -> exec_net 0 -> decay 1.5
        assert s["edge_decay"]["max_decay"] == 1.5
        assert s["edge_decay"]["mean_decay"] == 0.5
        assert s["edge_decay"]["vanished_at_execution_pct"] == round(100 / 3, 2)
        assert s["pnl"]["net_total"] == "3.000000"
        assert s["feed"]["frames"] == 4
        assert s["feed"]["availability_pct"] == 100.0
        assert s["per_market"][MID]["scans"] == 3
        assert s["per_market"][MID]["execs"] == 2

    def test_feed_stats_from_ledger_alone(self):
        t = [T0 + timedelta(seconds=10 * i) for i in range(5)]
        sweeps = [
            sweep(t[0], book("0", "0.45", "0.48", t[0]),
                  book("1", "0.46", "0.49", t[0])),
            bad_sweep(t[1]),
            bad_sweep(t[2], error="Timeout: slow"),
            sweep(t[3], book("0", "0.45", "0.48", t[3]),
                  book("1", "0.46", "0.49", t[3])),
            sweep(t[4], book("0", "0.45", "0.48", t[4]),
                  book("1", "0.46", "0.49", t[4])),
        ]
        result, ledger, _ = run(sweeps)
        assert result.feed_failures == 2
        s = summarize(ledger.records)
        assert s["feed"]["frames"] == 5
        assert s["feed"]["failed"] == 2
        assert s["feed"]["availability_pct"] == 60.0
        assert s["feed"]["failure_reasons"] == {"ConnectionError": 1,
                                                "Timeout": 1}
        assert s["feed"]["gaps"] == 1
        assert s["feed"]["gap_duration_s"]["total"] == 20.0  # t1 -> t3
        assert s["per_market"][MID]["frames_failed"] == 2
        assert s["per_market"][MID]["gap_count"] == 1
        assert s["per_market"][MID]["downtime_s"] == 20.0


class TestRestartDurability:
    """N7-BLOCKER-001: a restart must never grant a clean risk state."""

    def _degraded_result(self):
        from event_arbitrage_lab.execution import (
            LegOutcome,
            LegStatus,
            PaperExecutionResult,
            PairState,
        )
        from event_arbitrage_lab.schemas import Side
        leg = LegOutcome(Side.ASK, LegStatus.FILLED, ())
        return PaperExecutionResult(
            final_state=PairState.CLOSED, paired_shares=Decimal(50),
            residual_shares=Decimal(20), leg_a=leg, leg_b=leg, unwind=leg,
            paired_expected_pnl=Decimal("1.5"),
            unwind_realized_pnl=Decimal("-0.6"),
            total_fees=Decimal(0), net_pnl=Decimal("0.9"),
            events=(), residual_cost_basis=Decimal("9.6"),
        )

    def _opp(self):
        from event_arbitrage_lab.scanner import ScanParams, scan_market
        from event_arbitrage_lab.fees import ZeroFee
        from event_arbitrage_lab.scanner import BinaryMarketBooks
        books = BinaryMarketBooks(
            market=market(),
            yes=book("0", "0.45", "0.48", T0),
            no=book("1", "0.46", "0.49", T0),
        )
        params = ScanParams(size=Decimal(50), now=T0,
                            max_staleness=timedelta(seconds=45))
        return scan_market(books, ZeroFee(), params).opportunities[0]

    def test_checkpoint_roundtrip_identical_state(self, tmp_path):
        ledger = EvidenceLedger()
        gate = RiskGate(policy(max_consecutive_failures=2), ledger)
        opp = self._opp()
        # one degraded execution: exposure 20 + streak 1; second: trip
        gate.record_execution(opp, self._degraded_result())
        gate.record_execution(opp, self._degraded_result())
        assert gate.tripped
        path = tmp_path / "risk-checkpoint.json"
        write_checkpoint(path, gate, ledger.root_hash)

        # --- restart: new gate, restore from durable checkpoint ---
        gate2 = RiskGate(policy(max_consecutive_failures=2), EvidenceLedger())
        state = restore_gate_from_checkpoint(path, gate2, gate2.ledger)
        assert state is not None
        assert gate2.tripped == gate.tripped
        assert gate2.consecutive_failures == gate.consecutive_failures
        assert gate2._trip_reason == gate._trip_reason
        exp = gate2.exposure_of(VENUE, MID, "0")
        assert exp.shares == Decimal(40) and exp.basis == Decimal("19.2")
        # restored breaker still blocks (state is functional, not cosmetic)
        decision = gate2.evaluate(opp, {}, T0)
        assert not decision.approved
        assert any(r.reason.value == "CIRCUIT_BREAKER_TRIPPED"
                   for r in decision.rejections)
        # the restore itself is evidence
        restored = [r for r in gate2.ledger.records
                    if r.kind == "RISK_STATE_RESTORED"]
        assert len(restored) == 1
        assert '"consecutive_failures":2' in restored[0].payload_json

    def test_tampered_checkpoint_fails_closed(self, tmp_path):
        gate = RiskGate(policy())
        gate.record_execution(self._opp(), self._degraded_result())
        path = tmp_path / "risk-checkpoint.json"
        write_checkpoint(path, gate, "root")
        payload = json.loads(path.read_text())
        payload["state"]["consecutive_failures"] = 0  # attacker cleans state
        path.write_text(json.dumps(payload, sort_keys=True))
        with pytest.raises(ValueError, match="integrity failure"):
            restore_gate_from_checkpoint(path, RiskGate(policy()), None)

    @pytest.mark.parametrize("field,mutate", [
        ("state", lambda p: p["state"].update(consecutive_failures=0)),
        ("ledger_root", lambda p: p.update(ledger_root="0" * 64)),
        ("sweep_file", lambda p: p.update(sweep_file="/etc/passwd")),
        ("sweep_offset", lambda p: p.update(sweep_offset=0)),
        ("v", lambda p: p.update(v=1)),
    ])
    def test_envelope_field_tamper_fails_closed(self, tmp_path,
                                                field, mutate):
        """N7D-OBS-001 gate: the envelope hash covers version, state,
        ledger_root, sweep_file AND sweep_offset — editing any one of
        them must fail closed (no silent clean state, no evidence
        rewind, no divergence masking)."""
        gate = RiskGate(policy())
        gate.record_execution(self._opp(), self._degraded_result())
        path = tmp_path / "risk-checkpoint.json"
        write_checkpoint(path, gate, "a" * 64,
                         sweep_file="/data/sweeps-d1.jsonl",
                         sweep_offset=12345)
        payload = json.loads(path.read_text())
        mutate(payload)
        path.write_text(json.dumps(payload, sort_keys=True))
        with pytest.raises(ValueError,
                           match="integrity failure|unsupported checkpoint"):
            read_checkpoint(path)

    def test_envelope_structure_tamper_fails_closed(self, tmp_path):
        gate = RiskGate(policy())
        path = tmp_path / "risk-checkpoint.json"
        write_checkpoint(path, gate, "a" * 64)
        good = json.loads(path.read_text())
        # stripped integrity field
        stripped = {k: v for k, v in good.items() if k != "sha256"}
        path.write_text(json.dumps(stripped, sort_keys=True))
        with pytest.raises(ValueError, match="malformed"):
            read_checkpoint(path)
        # injected extra field
        path.write_text(json.dumps({**good, "evil": 1}, sort_keys=True))
        with pytest.raises(ValueError, match="malformed"):
            read_checkpoint(path)
        # non-object payload
        path.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError, match="malformed"):
            read_checkpoint(path)

    def test_legacy_v1_envelope_rejected(self, tmp_path):
        """Pre-hardening envelopes (state-only hash) must fail closed,
        not silently downgrade-verify."""
        path = tmp_path / "risk-checkpoint.json"
        gate = RiskGate(policy())
        state = gate.checkpoint()
        from event_arbitrage_lab.ledger import canonical_json
        import hashlib
        legacy = {
            "v": 1, "state": state, "ledger_root": "a" * 64,
            "sweep_file": None, "sweep_offset": None,
            "sha256": hashlib.sha256(
                canonical_json(state).encode()).hexdigest(),
        }
        path.write_text(json.dumps(legacy, sort_keys=True))
        with pytest.raises(ValueError, match="unsupported checkpoint"):
            read_checkpoint(path)

    def test_envelope_roundtrip_preserves_all_fields(self, tmp_path):
        gate = RiskGate(policy())
        gate.record_execution(self._opp(), self._degraded_result())
        path = tmp_path / "risk-checkpoint.json"
        write_checkpoint(path, gate, "b" * 64,
                         sweep_file="/data/sweeps-d1.jsonl",
                         sweep_offset=999)
        payload = read_checkpoint(path)
        assert payload["v"] == 2
        assert payload["ledger_root"] == "b" * 64
        assert payload["sweep_file"] == "/data/sweeps-d1.jsonl"
        assert payload["sweep_offset"] == 999
        assert payload["state"]["consecutive_failures"] == 1

    def test_missing_checkpoint_is_legitimate_first_start(self, tmp_path):
        gate = RiskGate(policy())
        assert restore_gate_from_checkpoint(
            tmp_path / "nope.json", gate, None) is None
        assert not gate.tripped

    def test_service_restart_preserves_state_and_cumulative(self, tmp_path):
        # batch 1 via processor A; simulate restart: new gate + processor B
        # over the SAME ledger + checkpoint; cumulative counts from ledger
        out = tmp_path
        ledger = EvidenceLedger()
        gate_a = RiskGate(policy(max_consecutive_failures=1))
        proc_a = PaperProcessor(policy=policy(), size=Decimal(50),
                                ledger=ledger, risk_gate=gate_a)
        proc_a.process_sweeps(good_pair(T0, 2))
        # degrade the gate externally (e.g. execution accounting)
        gate_a.record_execution(self._opp(), self._degraded_result())
        assert gate_a.tripped
        ledger.save(out / "ledger-d1.jsonl")
        write_checkpoint(out / "risk-checkpoint.json", gate_a, ledger.root_hash)

        # --- restart ---
        ledger2 = EvidenceLedger.load(out / "ledger-d1.jsonl")
        gate_b = RiskGate(policy(max_consecutive_failures=1), ledger2)
        restore_gate_from_checkpoint(out / "risk-checkpoint.json",
                                     gate_b, ledger2)
        assert gate_b.tripped  # NOT a clean state
        proc_b = PaperProcessor(policy=policy(), size=Decimal(50),
                                ledger=ledger2, risk_gate=gate_b)
        t2, t3 = T0 + timedelta(seconds=20), T0 + timedelta(seconds=30)
        # new opportunity arrives after restart: breaker still blocks it
        proc_b.process_sweeps([
            sweep(t2, book("0", "0.45", "0.48", t2),
                  book("1", "0.46", "0.49", t2)),
            sweep(t3, book("0", "0.45", "0.48", t3),
                  book("1", "0.46", "0.49", t3)),
        ])
        decs = [r for r in ledger2.records if r.kind == "RISK_DECISION"]
        assert any("CIRCUIT_BREAKER_TRIPPED" in d.payload_json for d in decs)
        # cumulative counters rebuild identically from the reloaded ledger
        c = cumulative_counts(ledger2)
        assert c["ticks_scanned"] == 2
        assert c["executions"] == 1
        assert c["risk_state_restores"] == 1
        assert c["market_frames"] == 4


class TestServiceResume:
    """Restart with frames written while down: resume must process
    EXACTLY the new frames — no duplicates, no skips."""

    def test_restart_resumes_at_checkpoint_offset(self, tmp_path):
        data, out = tmp_path / "d", tmp_path / "o"
        data.mkdir()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        f = data / f"sweeps-{day}.jsonl"
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        f.write_text(
            "\n".join(serialize_sweep(s) for s in good_pair(t0, 3)) + "\n"
        )
        run_service(data_dir=data, out_dir=out, policy=policy(),
                    size=Decimal(50), poll=0.01, max_iterations=2)
        led1 = EvidenceLedger.load(out / f"ledger-{day}.jsonl")
        frames1 = sum(1 for r in led1.records if r.kind == "MARKET_FRAME")
        assert frames1 == 3
        root1 = led1.root_hash

        # service down; collector keeps writing
        more = good_pair(t0 + timedelta(seconds=30), 2)
        with f.open("a") as fh:
            fh.write("\n".join(serialize_sweep(s) for s in more) + "\n")

        # restart: resumes at checkpoint offset, processes exactly the
        # two new frames
        run_service(data_dir=data, out_dir=out, policy=policy(),
                    size=Decimal(50), poll=0.01, max_iterations=2)
        led2 = EvidenceLedger.load(out / f"ledger-{day}.jsonl")
        assert led2.verify()
        frames2 = sum(1 for r in led2.records if r.kind == "MARKET_FRAME")
        assert frames2 == 5  # 3 old + 2 new, zero duplication
        restores = [r for r in led2.records
                    if r.kind == "RISK_STATE_RESTORED"]
        assert len(restores) == 1
        assert json.loads(restores[0].payload_json)[
            "ledger_root_at_checkpoint"] == root1

    def test_checkpoint_ledger_divergence_fails_closed(self, tmp_path):
        data, out = tmp_path / "d", tmp_path / "o"
        data.mkdir()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        f = data / f"sweeps-{day}.jsonl"
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        f.write_text(
            "\n".join(serialize_sweep(s) for s in good_pair(t0, 2)) + "\n"
        )
        run_service(data_dir=data, out_dir=out, policy=policy(),
                    size=Decimal(50), poll=0.01, max_iterations=2)
        # simulate a torn write: ledger advanced past the checkpoint
        led = EvidenceLedger.load(out / f"ledger-{day}.jsonl")
        led.append("EXECUTION_SKIPPED", {"reason": "simulated torn write"})
        led.save(out / f"ledger-{day}.jsonl")
        with pytest.raises(RuntimeError, match="divergence"):
            run_service(data_dir=data, out_dir=out, policy=policy(),
                        size=Decimal(50), poll=0.01, max_iterations=1)
