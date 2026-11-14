"""N6 adversarial tests: attack the risk gate as a SYSTEM layer.

Focus (per Verifier guidance): invariants, identity, provenance,
replayability — not test count. Key questions:
- Is the gate really on the production path (replay), or just unit-tested?
- Does exposure accounting respect full asset identity?
- Is every decision (approve AND reject) auditable evidence?
- Does a gated replay persist and reproduce byte-identical evidence?
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from event_arbitrage_lab.execution import TakerFillPolicy
from event_arbitrage_lab.fees import ZeroFee
from event_arbitrage_lab.ledger import EvidenceLedger
from event_arbitrage_lab.replay import (
    PairTick,
    load_ticks,
    run_replay,
    save_ticks,
)
from event_arbitrage_lab.risk import RiskGate, RiskPolicy, RiskReason
from event_arbitrage_lab.scanner import BinaryMarketBooks

from conftest import (
    MARKET_A,
    MARKET_B,
    NO,
    NOW,
    VENUE_A,
    VENUE_B,
    YES,
    make_book,
    make_mapping,
    make_market,
)

ZERO = ZeroFee()
TAKER = TakerFillPolicy()


def policy(**over) -> RiskPolicy:
    base = dict(
        max_staleness=timedelta(seconds=5),
        min_net_profit=Decimal("0.01"),
        min_net_edge_per_share=Decimal("0.0001"),
        min_capacity=Decimal(1),
        max_exposure_per_asset=Decimal(500),
        max_total_exposure=Decimal(10000),
        max_leg_risk_shares=Decimal(500),
        max_spread=Decimal("0.2"),
        max_consecutive_failures=2,
    )
    base.update(over)
    return RiskPolicy(**base)


def side_a(ts=NOW):
    return BinaryMarketBooks(
        market=make_market(),
        yes=make_book(VENUE_A, MARKET_A, YES,
                      bids=[("0.45", 100)], asks=[("0.48", 100)], ts=ts),
        no=make_book(VENUE_A, MARKET_A, NO,
                     bids=[("0.46", 100)], asks=[("0.52", 100)], ts=ts),
    )


def side_b(no_ask="0.49", ts=NOW):
    return BinaryMarketBooks(
        market=make_market(venue=VENUE_B, market_id=MARKET_B),
        yes=make_book(VENUE_B, MARKET_B, YES,
                      bids=[("0.48", 100)], asks=[("0.51", 100)], ts=ts),
        no=make_book(VENUE_B, MARKET_B, NO,
                     bids=[("0.46", 100)], asks=[(no_ask, 100)], ts=ts),
    )


def thin_bids_a(ts=NOW):
    """Exec-time leg-A book: entry side intact (asks 0.48x100, passes
    both gate phases) but exit side nearly gone (bids 0.45x20) — so a
    partial leg-B fill leaves a residual that can only be unwound at a
    loss. Gate-passing books, venue-side degradation."""
    return make_book(VENUE_A, MARKET_A, YES,
                     bids=[("0.45", 20)], asks=[("0.48", 100)], ts=ts)


def degraded_tick(ts=NOW):
    """Scan books and exec books BOTH healthy; degradation comes only
    from a partial-fill policy at execution (depth_ratio < 1), which no
    gate can predict. This is the honest N6 semantics: approved ticks
    execute, and degraded outcomes arrive via venue-side fills."""
    return PairTick(side_a(ts), side_b(ts=ts),
                    exec_books=(thin_bids_a(ts), side_b(ts=ts).no))


def gated_replay(ticks, gate, ledger, policy_b=TAKER):
    return run_replay(
        ticks, make_mapping(), ZERO, ZERO, TAKER, policy_b,
        size=Decimal(100), max_staleness=timedelta(seconds=60),
        ledger=ledger, risk_gate=gate,
    )


def decisions(ledger, phase):
    """RISK_DECISION records of one phase (phase-tagged evidence)."""
    return [r for r in ledger.records
            if r.kind == "RISK_DECISION"
            and f'"phase":"{phase}"' in r.payload_json]


class TestProductionPath:
    """P0 lesson: a gate that is only unit-tested is not a gate."""

    def test_approved_flow_records_decision_and_executes(self):
        ledger = EvidenceLedger()
        gate = RiskGate(policy())
        report = gated_replay([PairTick(side_a(), side_b())], gate, ledger)
        assert report.executions == 1
        assert report.risk_rejected == 0
        kinds = [r.kind for r in ledger.records]
        assert kinds == [
            "SCAN_SNAPSHOT", "OPPORTUNITY",
            "RISK_DECISION",  # PRE_TRADE
            "RISK_DECISION",  # EXECUTION_TIME
            "EXECUTION_SNAPSHOT", "EXECUTION", "RISK_STATE",
        ]
        # both phases approved, phase-tagged — TOCTOU evidence
        pre, exe = decisions(ledger, "PRE_TRADE"), decisions(ledger, "EXECUTION_TIME")
        assert len(pre) == len(exe) == 1
        assert '"approved":true' in pre[0].payload_json
        assert '"approved":true' in exe[0].payload_json

    def test_rejected_flow_never_reaches_executor(self):
        ledger = EvidenceLedger()
        gate = RiskGate(policy(max_leg_risk_shares=Decimal(50)))  # leg = 100
        report = gated_replay([PairTick(side_a(), side_b())], gate, ledger)
        assert report.executions == 0
        assert report.risk_rejected == 1
        assert not [r for r in ledger.records if r.kind == "EXECUTION"]
        dec = next(r for r in ledger.records if r.kind == "RISK_DECISION")
        assert "LEG_RISK_LIMIT" in dec.payload_json
        assert '"measured":"100"' in dec.payload_json  # measured vs threshold
        assert '"threshold":"50"' in dec.payload_json

    def test_gate_without_ledger_gets_replay_ledger(self):
        # provenance must not depend on the caller remembering to wire it
        ledger = EvidenceLedger()
        gate = RiskGate(policy())  # no ledger attached
        gated_replay([PairTick(side_a(), side_b())], gate, ledger)
        assert gate.ledger is ledger
        assert any(r.kind == "RISK_DECISION" for r in ledger.records)


class TestIdentity:
    """Exposure is keyed by FULL asset identity — never by market name."""

    def test_same_market_different_venue_is_separate_exposure(self):
        gate = RiskGate(policy(max_exposure_per_asset=Decimal(100)))
        ledger = EvidenceLedger()
        # degraded exec via venue-side partial fill: leg B fills 60 of
        # 100; residual 40 unwinds into thin bids (20 sold at a loss),
        # 20 shares stay open on venue_a/mkt-a-1/YES
        r1 = gated_replay([degraded_tick()], gate, ledger,
                          policy_b=TakerFillPolicy(depth_ratio=Decimal("0.6")))
        assert r1.degraded == 1
        assert gate.exposure_of(VENUE_A, MARKET_A, YES).shares == Decimal(20)
        # venue_b's same-named assets: untouched
        assert gate.exposure_of(VENUE_B, MARKET_A, YES).shares == 0
        assert gate.exposure_of(VENUE_B, MARKET_B, NO).shares == 0
        # and the SAME asset on venue_a would now be constrained: 20 + 100 > 100
        r2 = gated_replay([PairTick(side_a(), side_b())], gate, ledger)
        assert r2.risk_rejected == 1


class TestProvenance:
    def test_breaker_trip_and_reset_are_evidence(self):
        ledger = EvidenceLedger()
        gate = RiskGate(policy(max_consecutive_failures=1))
        gated_replay([degraded_tick()], gate, ledger,
                     policy_b=TakerFillPolicy(depth_ratio=Decimal("0.6")))
        assert gate.tripped
        gate.reset_breaker(operator="alex")
        kinds = [r.kind for r in ledger.records]
        assert "CIRCUIT_BREAKER" in kinds
        trip = [r for r in ledger.records
                if r.kind == "CIRCUIT_BREAKER" and "TRIPPED" in r.payload_json]
        reset = [r for r in ledger.records
                 if r.kind == "CIRCUIT_BREAKER" and "RESET" in r.payload_json]
        assert trip and reset
        assert '"operator":"alex"' in reset[0].payload_json

    def test_rejection_decisions_carry_measurements(self):
        ledger = EvidenceLedger()
        gate = RiskGate(policy(min_net_profit=Decimal("5")))
        gated_replay([PairTick(side_a(), side_b())], gate, ledger)
        dec = next(r for r in ledger.records if r.kind == "RISK_DECISION")
        assert "EDGE_BELOW_THRESHOLD" in dec.payload_json
        assert '"measured":"3.0' in dec.payload_json  # actual net profit
        assert '"threshold":"5"' in dec.payload_json


class TestReplayability:
    def test_gated_replay_persists_and_reproduces(self, tmp_path):
        ticks = [
            PairTick(side_a(), side_b()),
            PairTick(side_a(), side_b()),
        ]
        p = tmp_path / "frames.jsonl"
        save_ticks(ticks, p)

        la, lb = EvidenceLedger(), EvidenceLedger()
        ra = gated_replay(ticks, RiskGate(policy()), la)
        rb = gated_replay(load_ticks(p), RiskGate(policy()), lb)
        assert ra == rb
        assert ra.risk_rejected == rb.risk_rejected == 0
        assert ra.ledger_root_hash == rb.ledger_root_hash
        assert la.records == lb.records

    def test_consecutive_failures_trip_breaker_mid_replay(self):
        # two degraded ticks in a row -> third tick's opportunity blocked
        bad = degraded_tick()
        ticks = [bad, bad, PairTick(side_a(), side_b())]
        ledger = EvidenceLedger()
        gate = RiskGate(policy(max_consecutive_failures=2))
        report = gated_replay(ticks, gate, ledger,
                              policy_b=TakerFillPolicy(depth_ratio=Decimal("0.6")))
        assert report.executions == 2
        assert report.degraded == 2
        assert report.risk_rejected == 1  # tick 3 blocked by tripped breaker
        dec = [r for r in ledger.records if r.kind == "RISK_DECISION"][-1]
        assert "CIRCUIT_BREAKER_TRIPPED" in dec.payload_json
        assert '"phase":"PRE_TRADE"' in dec.payload_json


class TestFailClosed:
    def test_nan_policy_rejected_at_construction(self):
        with pytest.raises(ValueError):
            policy(max_total_exposure=Decimal("NaN"))

    def test_unknown_leg_market_rejected_not_assumed(self):
        # opportunity referencing a market with NO book in the map
        ledger = EvidenceLedger()
        gate = RiskGate(policy())
        from event_arbitrage_lab.scanner import ScanParams, scan_pair
        params = ScanParams(size=Decimal(100), now=NOW,
                            max_staleness=timedelta(seconds=60))
        scan = scan_pair(make_mapping(), side_a(), side_b(), ZERO, ZERO, params)
        opp = scan.opportunities[0]
        decision = gate.evaluate(opp, {}, NOW)  # empty book map
        assert not decision.approved
        reasons = {r.reason for r in decision.rejections}
        assert RiskReason.DATA_FEED_FAILURE in reasons


class TestExecutionTimeGate:
    """Verifier N6-P0-001: PRE_TRADE approval must NEVER imply
    EXECUTION_TIME approval. Books handed to the executor re-pass
    identity / freshness / spread / executable liquidity / current
    edge immediately before execution. Evidence is phase-distinguished."""

    @staticmethod
    def _opp():
        from event_arbitrage_lab.scanner import ScanParams, scan_pair
        params = ScanParams(size=Decimal(100), now=NOW,
                            max_staleness=timedelta(seconds=60))
        scan = scan_pair(make_mapping(), side_a(), side_b(), ZERO, ZERO, params)
        return scan.opportunities[0]

    def test_fresh_scan_stale_exec_blocked(self):
        # scan books fresh; exec-time leg-A book is 10s old (> 5s policy)
        stale_a = make_book(VENUE_A, MARKET_A, YES,
                            bids=[("0.45", 100)], asks=[("0.48", 100)],
                            ts=NOW - timedelta(seconds=10))
        tick = PairTick(side_a(), side_b(),
                        exec_books=(stale_a, side_b().no))
        ledger, gate = EvidenceLedger(), RiskGate(policy())
        rep = gated_replay([tick], gate, ledger)
        assert rep.executions == 0
        assert rep.risk_rejected == 1
        assert not [r for r in ledger.records
                    if r.kind in ("EXECUTION", "EXECUTION_SNAPSHOT")]
        pre, exe = decisions(ledger, "PRE_TRADE"), decisions(ledger, "EXECUTION_TIME")
        assert len(pre) == len(exe) == 1
        assert '"approved":true' in pre[0].payload_json
        assert "STALE_DATA" in exe[0].payload_json
        assert '"approved":false' in exe[0].payload_json

    def test_normal_scan_abnormal_exec_spread_blocked(self):
        # exec-time leg-A book spread 0.38 blows past max_spread 0.2
        wide_a = make_book(VENUE_A, MARKET_A, YES,
                           bids=[("0.10", 100)], asks=[("0.48", 100)])
        tick = PairTick(side_a(), side_b(),
                        exec_books=(wide_a, side_b().no))
        ledger, gate = EvidenceLedger(), RiskGate(policy())
        rep = gated_replay([tick], gate, ledger)
        assert rep.executions == 0
        assert rep.risk_rejected == 1
        exe = decisions(ledger, "EXECUTION_TIME")
        assert len(exe) == 1
        assert "ABNORMAL_MARKET" in exe[0].payload_json

    def test_scan_edge_positive_exec_edge_gone_blocked(self):
        # leg A ask moved 0.48 -> 0.51 by execution time:
        # exec net = 100 - 51 - 49 = 0 < min_net_profit
        moved_a = make_book(VENUE_A, MARKET_A, YES,
                            bids=[("0.45", 100)], asks=[("0.51", 100)])
        tick = PairTick(side_a(), side_b(),
                        exec_books=(moved_a, side_b().no))
        ledger, gate = EvidenceLedger(), RiskGate(policy())
        rep = gated_replay([tick], gate, ledger)
        assert rep.executions == 0
        assert rep.risk_rejected == 1
        exe = decisions(ledger, "EXECUTION_TIME")
        assert len(exe) == 1
        assert "EDGE_BELOW_THRESHOLD" in exe[0].payload_json
        assert not [r for r in ledger.records if r.kind == "EXECUTION"]

    def test_scan_liquidity_sufficient_exec_insufficient_blocked(self):
        # exec-time leg-A asks carry only 50 of the required 100 shares
        thin_asks_a = make_book(VENUE_A, MARKET_A, YES,
                                bids=[("0.45", 100)], asks=[("0.48", 50)])
        tick = PairTick(side_a(), side_b(),
                        exec_books=(thin_asks_a, side_b().no))
        ledger, gate = EvidenceLedger(), RiskGate(policy())
        rep = gated_replay([tick], gate, ledger)
        assert rep.executions == 0
        assert rep.risk_rejected == 1
        exe = decisions(ledger, "EXECUTION_TIME")
        assert len(exe) == 1
        assert "INSUFFICIENT_LIQUIDITY" in exe[0].payload_json

    def test_foreign_exec_identity_blocked(self):
        foreign = make_book("venue_c", "mkt-c-9", YES,
                            bids=[("0.45", 100)], asks=[("0.48", 100)])
        # (a) production path: foreign exec book never reaches the gate
        # or the executor — identity resolution skips it
        tick = PairTick(side_a(), side_b(),
                        exec_books=(foreign, side_b().no))
        ledger, gate = EvidenceLedger(), RiskGate(policy())
        rep = gated_replay([tick], gate, ledger)
        assert rep.executions == 0
        kinds = [r.kind for r in ledger.records]
        assert "EXECUTION_SKIPPED" in kinds
        assert "EXECUTION" not in kinds
        # (b) gate itself: a foreign book misfiled under a leg's identity
        # is a feed-integrity failure — rejected, never trusted
        opp = self._opp()
        key_a = (VENUE_A, MARKET_A, YES)
        key_b = (VENUE_B, MARKET_B, NO)
        decision = gate.evaluate_execution(
            opp, {key_a: foreign, key_b: side_b().no}, NOW,
            fees=(ZERO, ZERO), payout_per_unit=Decimal(1),
        )
        assert not decision.approved
        reasons = {r.reason for r in decision.rejections}
        assert RiskReason.DATA_FEED_FAILURE in reasons

    def test_valid_scan_valid_exec_passes(self):
        # both phases healthy -> executes; evidence distinguishes phases
        tick = PairTick(side_a(), side_b(),
                        exec_books=(side_a().yes, side_b().no))
        ledger, gate = EvidenceLedger(), RiskGate(policy())
        rep = gated_replay([tick], gate, ledger)
        assert rep.executions == 1
        assert rep.risk_rejected == 0
        pre, exe = decisions(ledger, "PRE_TRADE"), decisions(ledger, "EXECUTION_TIME")
        assert len(pre) == len(exe) == 1
        assert '"approved":true' in pre[0].payload_json
        assert '"approved":true' in exe[0].payload_json
        # exec-time decision pins the exact books it decided on
        assert '"exec_books"' in exe[0].payload_json
        assert '"limit_b"' in exe[0].payload_json
        # ordering: PRE_TRADE is decided before EXECUTION_TIME
        decs = [r for r in ledger.records if r.kind == "RISK_DECISION"]
        assert '"phase":"PRE_TRADE"' in decs[0].payload_json
        assert '"phase":"EXECUTION_TIME"' in decs[1].payload_json
