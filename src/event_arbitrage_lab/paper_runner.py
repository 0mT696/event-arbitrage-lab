"""N7B: long-running paper-validation runner.

Consumes collector sweeps (N7A) and drives the FULL production path per
market per sweep — single-venue completeness scan, PRE_TRADE gate,
EXECUTION_TIME gate, paper execution — with ledger evidence at every
step:

    MARKET_FRAME (every market, every sweep — ok OR failure; books are
        pinned by content hash + tops, full payloads live in the sweep
        files; hash links evidence to raw data at N7 scale)
    FEED_RESTORED (a market's chain resumes after failed frames; carries
        down_from/down_until/duration_s/failed_frames)
    MARKET_RETIRED (a market absent for retire_after consecutive sweeps
        is permanently untracked — closed/delisted markets must not
        accrue failure frames forever; a later reappearance starts a
        fresh chain, no FEED_RESTORED)
    TICK_SCANNED (one chained scan attempt: scan_t -> exec_t, found n)
    OPPORTUNITY -> RISK_DECISION(PRE_TRADE) -> [EXECUTION_SKIPPED]
    -> RISK_DECISION(EXECUTION_TIME) -> EXECUTION_SNAPSHOT
    -> EXECUTION -> RISK_STATE
    RISK_STATE_RESTORED (service restart restored durable risk state)

Feed failures are first-class evidence (N7-BLOCKER-002): availability,
failure counts, gap durations and per-market reliability are all
rebuildable from the ledger ALONE — every tracked market in every
sweep leaves exactly one MARKET_FRAME, so the denominator is complete.
Markets retired via MARKET_RETIRED leave the denominator (a closed
market is not a feed-reliability signal).

Risk runtime state is restart-durable (N7-BLOCKER-001): the gate's
checkpoint (exposure, failure streak, breaker) is written atomically
at every batch boundary next to the ledger save, integrity-hashed, and
restored on service start. A restart never grants a clean risk state;
the restore itself is ledger evidence.

Latency model (the edge-decay measurement): a market's books at sweep t
are the SCAN-time books; the SAME market's books at sweep t+1 are the
EXECUTION-time books — "our order reaches the venue one interval after
discovery". Gate clocks come from DATA (book fetch timestamps, mirroring
run_replay), never from the runner's processing time: staleness fires on
real feed lag, not on fetch skew inside a sweep. The last frame of a
batch has no successor yet and is not traded (every traded tick models
exactly one interval of latency — no zero-latency bias in the empirics).

Determinism: same sweep sequence -> same records -> same root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .adapters.polymarket import fee_model_from_meta
from .collector import daily_path, parse_sweep
from .execution import PaperExecutionResult, TakerFillPolicy, execute_pair
from .fees import SlippageBuffer
from .ledger import EvidenceLedger, canonical_json
from .replay import ReplayReport
from .risk import RiskGate, RiskPolicy
from .scanner import BinaryMarketBooks, ScanParams, scan_market
from .schemas import OrderBook


def _book_pin(b: OrderBook) -> dict[str, Any]:
    """Content hash + top-of-book: proves WHICH data a decision used
    without duplicating full books into the ledger (N7 scale)."""
    return {
        "sha256": hashlib.sha256(
            canonical_json(b.model_dump(mode="python")).encode()
        ).hexdigest(),
        "best_bid": str(b.bids[0].price) if b.bids else None,
        "best_ask": str(b.asks[0].price) if b.asks else None,
        "levels": {"bids": len(b.bids), "asks": len(b.asks)},
        "timestamp": b.timestamp.isoformat(),
    }


@dataclass(frozen=True)
class PaperRunResult:
    report: ReplayReport
    feed_failures: int  # MARKET_FRAME ok:false records written
    feed_restores: int  # FEED_RESTORED records written
    retired: int  # MARKET_RETIRED records written
    markets: int


@dataclass
class _ChainEntry:
    books: BinaryMarketBooks
    fee: dict[str, Any]
    category: str | None
    t: datetime


class PaperProcessor:
    """Stateful sweep processor: chain state, feed-gap tracking, gate,
    counters. The service keeps ONE processor across batches; one-shot
    runs (tests, offline analysis) use run_paper_validation()."""

    def __init__(
        self,
        *,
        policy: RiskPolicy,
        size: Decimal,
        slippage: SlippageBuffer = SlippageBuffer(),
        min_profit: Decimal = Decimal(0),
        scan_staleness: timedelta | None = None,
        retire_after: int = 30,
        ledger: EvidenceLedger,
        risk_gate: RiskGate,
    ) -> None:
        if retire_after < 1:
            raise ValueError("retire_after must be >= 1")
        self.policy = policy
        self.size = size
        self.slippage = slippage
        self.min_profit = min_profit
        self.scan_stale = scan_staleness or policy.max_staleness
        self.retire_after = retire_after
        self.ledger = ledger
        self.gate = risk_gate
        if self.gate.ledger is None:
            self.gate.ledger = ledger
        self._prev: dict[str, _ChainEntry] = {}
        self._down_since: dict[str, tuple[datetime, int]] = {}
        self.n_ticks = self.n_opp = self.n_exec = self.n_clean = 0
        self.n_degraded = self.n_rej = self.n_risk_rej = 0
        self.n_feed_fail = self.n_feed_restore = self.n_retired = 0
        self.total_pnl = Decimal(0)
        self.markets: set[str] = set()

    # ---- feed evidence ----

    def _frame_ok(self, e: Mapping[str, Any]) -> bool:
        return (
            e["error"] is None and e["yes"] is not None and e["no"] is not None
        )

    def _record_failure(self, mid: str, t: datetime, error: str) -> None:
        self.n_feed_fail += 1
        self.ledger.append(
            "MARKET_FRAME",
            {"t": t.isoformat(), "market": mid, "ok": False, "error": error},
        )
        prev_down = self._down_since.get(mid)
        if prev_down is None:
            self._down_since[mid] = (t, 1)
        else:
            self._down_since[mid] = (prev_down[0], prev_down[1] + 1)
        self._prev.pop(mid, None)  # chain broken

    def _record_frame_ok(self, e: Mapping[str, Any], books: BinaryMarketBooks,
                         t: datetime) -> None:
        mid = e["market"].market_id
        self.ledger.append(
            "MARKET_FRAME",
            {
                "t": t.isoformat(), "market": mid, "ok": True,
                "fee": {k: (str(v) if isinstance(v, Decimal) else v)
                        for k, v in (e["fee"] or {}).items()},
                "yes": _book_pin(books.yes), "no": _book_pin(books.no),
            },
        )
        down = self._down_since.pop(mid, None)
        if down is not None:
            down_from, failed = down
            self.n_feed_restore += 1
            self.ledger.append(
                "FEED_RESTORED",
                {
                    "market": mid,
                    "down_from": down_from.isoformat(),
                    "down_until": t.isoformat(),
                    "duration_s": (t - down_from).total_seconds(),
                    "failed_frames": failed,
                },
            )

    # ---- tick processing (production path) ----

    def _process_tick(self, prior: _ChainEntry, cur: _ChainEntry) -> None:
        ledger, gate = self.ledger, self.gate
        books = prior.books
        market = books.market
        fee = fee_model_from_meta(prior.fee, prior.category)
        now_scan = max(books.yes.timestamp, books.no.timestamp)
        now_exec = max(cur.books.yes.timestamp, cur.books.no.timestamp)

        params = ScanParams(
            size=self.size, now=now_scan, max_staleness=self.scan_stale,
            slippage=self.slippage, min_profit=self.min_profit,
        )
        scan = scan_market(books, fee, params)
        self.n_ticks += 1
        ledger.append(
            "TICK_SCANNED",
            {"market": market.market_id,
             "scan_t": now_scan.isoformat(), "exec_t": now_exec.isoformat(),
             "latency_interval_s": (now_exec - now_scan).total_seconds(),
             "found": len(scan.opportunities)},
        )
        for r in scan.rejections:
            self.n_rej += 1
            ledger.append(
                "REJECTION",
                {"market": market.market_id, "scan_t": now_scan.isoformat(),
                 "reason": r.reason, "detail": r.detail},
            )
        for opp in scan.opportunities:
            self.n_opp += 1
            ledger.append("OPPORTUNITY",
                          {"market": market.market_id, "opportunity": opp})
            lx, ly = opp.legs

            # ---- PRE_TRADE gate against scan-time books ----
            scan_map = {
                (b.venue, b.market_id, b.outcome_id): b
                for b in (books.yes, books.no)
            }
            pre = gate.evaluate(opp, scan_map, now_scan)
            if not pre.approved:
                self.n_risk_rej += 1
                continue

            # ---- resolve exec-time books by leg identity ----
            ex = (cur.books.yes, cur.books.no)
            bx = next(
                (b for b in ex
                 if (b.venue, b.market_id, b.outcome_id)
                 == (lx.venue, lx.market_id, lx.outcome_id)),
                None,
            )
            by = next(
                (b for b in ex
                 if (b.venue, b.market_id, b.outcome_id)
                 == (ly.venue, ly.market_id, ly.outcome_id)),
                None,
            )
            if bx is None or by is None:
                ledger.append(
                    "EXECUTION_SKIPPED",
                    {"market": market.market_id,
                     "reason": "exec books do not match leg identities"},
                )
                continue

            # ---- EXECUTION_TIME gate against exec-time books ----
            exec_map = {
                (b.venue, b.market_id, b.outcome_id): b for b in (bx, by)
            }
            exe = gate.evaluate_execution(
                opp, exec_map, now_exec,
                fees=(fee, fee),
                payout_per_unit=market.payout_per_unit,
                slippage=self.slippage,
                min_profit=self.min_profit,
            )
            if not exe.approved:
                self.n_risk_rej += 1
                continue

            ledger.append(
                "EXECUTION_SNAPSHOT",
                {
                    "market": market.market_id,
                    "legs": [
                        {"venue": lx.venue, "market_id": lx.market_id,
                         "outcome_id": lx.outcome_id},
                        {"venue": ly.venue, "market_id": ly.market_id,
                         "outcome_id": ly.outcome_id},
                    ],
                    "books": [bx.model_dump(mode="python"),
                              by.model_dump(mode="python")],
                    "latency_interval_s": (now_exec - now_scan).total_seconds(),
                },
            )
            res: PaperExecutionResult = execute_pair(
                bx, by, self.size, fee, fee,
                TakerFillPolicy(), TakerFillPolicy(),
                payout_per_unit=market.payout_per_unit,
                min_profit=self.min_profit,
                slippage=self.slippage,
            )
            self.n_exec += 1
            self.total_pnl += res.net_pnl
            if (
                res.paired_shares > 0
                and res.residual_shares == 0
                and res.unwind_realized_pnl >= 0
            ):
                self.n_clean += 1
            else:
                self.n_degraded += 1
            ledger.append("EXECUTION", {"market": market.market_id,
                                        "result": res})
            gate.record_execution(opp, res)

    # ---- batch entry ----

    def process_sweeps(self, sweeps: Sequence[dict[str, Any]]) -> PaperRunResult:
        for sweep in sweeps:
            t = sweep["t"]
            seen: set[str] = set()
            for e in sweep["markets"]:
                mid = e["market"].market_id
                seen.add(mid)
                self.markets.add(mid)
                if not self._frame_ok(e):
                    self._record_failure(
                        mid, t, e["error"] or "missing book payloads"
                    )
                    continue
                books = BinaryMarketBooks(
                    market=e["market"], yes=e["yes"], no=e["no"]
                )
                self._record_frame_ok(e, books, t)
                prior = self._prev.get(mid)
                cur = _ChainEntry(books=books, fee=e["fee"],
                                  category=e.get("category"), t=t)
                if prior is not None:
                    self._process_tick(prior, cur)
                self._prev[mid] = cur
            # tracked markets absent from this sweep = feed failure;
            # absent beyond retire_after = permanently gone (closed/
            # delisted) — retire instead of accruing frames forever
            for mid in (set(self._prev) | set(self._down_since)) - seen:
                down = self._down_since.get(mid)
                if down is not None and down[1] >= self.retire_after:
                    down_from, failed = down
                    self.n_retired += 1
                    self.ledger.append(
                        "MARKET_RETIRED",
                        {
                            "t": t.isoformat(),
                            "market": mid,
                            "down_from": down_from.isoformat(),
                            "consecutive_failed_sweeps": failed,
                            "reason": f"unavailable for {failed} "
                                      f"consecutive sweeps, now absent "
                                      f"(retire_after={self.retire_after})",
                        },
                    )
                    self._down_since.pop(mid, None)
                    self._prev.pop(mid, None)
                    continue
                self._record_failure(mid, t, "absent from sweep")
        return PaperRunResult(
            report=ReplayReport(
                ticks=self.n_ticks,
                opportunities=self.n_opp,
                executions=self.n_exec,
                clean_hedged=self.n_clean,
                degraded=self.n_degraded,
                total_net_pnl=self.total_pnl.quantize(Decimal("0.000001")),
                rejections=self.n_rej,
                ledger_root_hash=self.ledger.root_hash,
                risk_rejected=self.n_risk_rej,
            ),
            feed_failures=self.n_feed_fail,
            feed_restores=self.n_feed_restore,
            retired=self.n_retired,
            markets=len(self.markets),
        )


def run_paper_validation(
    sweeps: Sequence[dict[str, Any]],
    *,
    policy: RiskPolicy,
    size: Decimal,
    slippage: SlippageBuffer = SlippageBuffer(),
    min_profit: Decimal = Decimal(0),
    scan_staleness: timedelta | None = None,
    retire_after: int = 30,
    ledger: EvidenceLedger | None = None,
    risk_gate: RiskGate | None = None,
) -> PaperRunResult:
    """One-shot paper validation over a sweep sequence (tests/offline).

    The risk gate is MANDATORY on this path by construction: pass one or
    a default is built from `policy` — there is no ungated paper run."""
    ledger = ledger if ledger is not None else EvidenceLedger()
    gate = risk_gate if risk_gate is not None else RiskGate(policy)
    proc = PaperProcessor(
        policy=policy, size=size, slippage=slippage, min_profit=min_profit,
        scan_staleness=scan_staleness, retire_after=retire_after,
        ledger=ledger, risk_gate=gate,
    )
    return proc.process_sweeps(sweeps)


# ---- durable risk checkpoint (N7-BLOCKER-001) ----

CHECKPOINT_FILE = "risk-checkpoint.json"


CHECKPOINT_ENVELOPE_VERSION = 2
_CHECKPOINT_BODY_KEYS = ("v", "state", "ledger_root", "sweep_file",
                         "sweep_offset")


def write_checkpoint(
    path: Path,
    gate: RiskGate,
    ledger_root: str,
    *,
    sweep_file: str | None = None,
    sweep_offset: int | None = None,
) -> None:
    """Atomic, integrity-hashed checkpoint write. The sweep file offset
    lets a restart resume EXACTLY where the ledger ends — no duplicated
    evidence, no skipped frames.

    Envelope v2 (N7D-OBS-001 gate): sha256 covers the FULL canonical
    envelope body — version, state, ledger_root, sweep_file and
    sweep_offset — so tampering with ANY field (e.g. rewinding the
    sweep offset to duplicate/skip evidence, or swapping ledger_root to
    mask a torn write) is detected, not just state edits."""
    state = gate.checkpoint()
    body = {
        "v": CHECKPOINT_ENVELOPE_VERSION,
        "state": state,
        "ledger_root": ledger_root,
        "sweep_file": sweep_file,
        "sweep_offset": sweep_offset,
    }
    payload = {
        **body,
        "sha256": hashlib.sha256(canonical_json(body).encode()).hexdigest(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True))
    os.replace(tmp, path)  # atomic on POSIX


def read_checkpoint(path: Path) -> dict[str, Any] | None:
    """Integrity-verified checkpoint read. Fail closed on tamper:
    a corrupt checkpoint raises, it never yields a clean state."""
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"risk checkpoint malformed: {path} (not an object)")
    if payload.get("v") != CHECKPOINT_ENVELOPE_VERSION:
        raise ValueError(
            f"unsupported checkpoint file version: {payload.get('v')!r}")
    keys = set(payload)
    required = set(_CHECKPOINT_BODY_KEYS) | {"sha256"}
    if keys != required:
        raise ValueError(
            f"risk checkpoint envelope malformed: {path} "
            f"(missing={sorted(required - keys)}, "
            f"extra={sorted(keys - required)})"
        )
    body = {k: payload[k] for k in _CHECKPOINT_BODY_KEYS}
    expected = payload["sha256"]
    actual = hashlib.sha256(canonical_json(body).encode()).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise ValueError(
            f"risk checkpoint integrity failure: {path} "
            f"(expected {expected}, got {actual}) — refusing clean state"
        )
    return payload


def restore_gate_from_checkpoint(
    path: Path, gate: RiskGate, ledger: EvidenceLedger | None
) -> dict[str, Any] | None:
    """Restore gate state from a durable checkpoint.

    Fail closed: integrity mismatch or malformed state raises — the
    service must NOT start with a silently clean risk state. Returns the
    restored state dict (for evidence), or None if no checkpoint exists
    (legitimate first start)."""
    payload = read_checkpoint(path)
    if payload is None:
        return None
    state = payload["state"]
    gate.restore_checkpoint(state)
    if ledger is not None:
        ledger.append(
            "RISK_STATE_RESTORED",
            {
                "source": str(path),
                "checkpoint_sha256": payload["sha256"],
                "ledger_root_at_checkpoint": payload.get("ledger_root"),
                "state": state,
            },
        )
    return state


# ---- long-running service: tail today's sweep file, process batches ----


@dataclass
class _TailState:
    path: Path | None = None
    offset: int = 0


def _read_new_frames(state: _TailState, path: Path) -> list[dict[str, Any]]:
    if state.path != path:
        state.path = path
        state.offset = 0
    if not path.exists():
        return []
    with path.open() as fh:
        fh.seek(state.offset)
        chunk = fh.read()
    # hold back a torn tail line (collector mid-write); retried next poll
    complete = chunk.endswith("\n")
    lines = [l for l in chunk.splitlines() if l.strip()]
    if not complete and lines:
        tail = lines.pop()
        state.offset += len(chunk.encode()) - len(tail.encode()) - 1
    else:
        state.offset += len(chunk.encode())
    frames = []
    for line in lines:
        try:
            frames.append(parse_sweep(line))
        except ValueError:
            continue  # corrupt line: skip (collector writes are line-atomic)
    return frames


def cumulative_counts(ledger: EvidenceLedger) -> dict[str, int]:
    """Restart-safe cumulative counters, rebuilt from the ledger alone
    (N7-BLOCKER-002: no in-memory 'since start' counters)."""
    counts: dict[str, int] = {}
    for r in ledger.records:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    rejected = 0
    for r in ledger.records:
        if r.kind == "RISK_DECISION" and '"approved":false' in r.payload_json:
            rejected += 1
    return {
        "market_frames": counts.get("MARKET_FRAME", 0),
        "market_frames_failed": sum(
            1 for r in ledger.records
            if r.kind == "MARKET_FRAME" and '"ok":false' in r.payload_json
        ),
        "ticks_scanned": counts.get("TICK_SCANNED", 0),
        "opportunities": counts.get("OPPORTUNITY", 0),
        "executions": counts.get("EXECUTION", 0),
        "risk_rejected": rejected,
        "feed_restores": counts.get("FEED_RESTORED", 0),
        "risk_state_restores": counts.get("RISK_STATE_RESTORED", 0),
        "markets_retired": counts.get("MARKET_RETIRED", 0),
    }


def run_service(
    *,
    data_dir: str | Path,
    out_dir: str | Path,
    policy: RiskPolicy,
    size: Decimal = Decimal(100),
    poll: float = 5.0,
    retire_after: int = 30,
    max_iterations: int | None = None,  # tests/smoke only
) -> None:
    """Incremental runner: poll the current day's sweep file, process
    new sweeps, persist ledger + risk checkpoint at every batch
    boundary, restore risk state on start (never a clean restart)."""
    data = Path(data_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gate = RiskGate(policy)
    processor: PaperProcessor | None = None
    state = _TailState()
    day: str | None = None
    ledger: EvidenceLedger | None = None
    restored = False
    iterations = 0

    while True:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != day:
            day = today
            ledger_path = out / f"ledger-{day}.jsonl"
            ledger = (
                EvidenceLedger.load(ledger_path)
                if ledger_path.exists()
                else EvidenceLedger()
            )
            gate.ledger = ledger
            sweep_path = daily_path(data, day)
            if not restored:
                # durable risk state + exact resume position:
                # the checkpoint carries (sweep_file, sweep_offset) and
                # the ledger root at that offset. Divergence between
                # checkpoint and ledger = torn write -> fail closed.
                # Neither EOF-seek (loses frames written while down) nor
                # offset-0 (duplicates evidence) is acceptable.
                payload = read_checkpoint(out / CHECKPOINT_FILE)
                if payload is not None:
                    if (
                        payload.get("sweep_file") == str(sweep_path)
                        and ledger.records
                    ):
                        if payload.get("ledger_root") != ledger.root_hash:
                            raise RuntimeError(
                                "checkpoint/ledger divergence: checkpoint "
                                f"root {payload.get('ledger_root')} != ledger "
                                f"root {ledger.root_hash} — refusing to "
                                "resume (possible torn write)"
                            )
                        state.path = sweep_path
                        state.offset = int(payload.get("sweep_offset") or 0)
                    gate.restore_checkpoint(payload["state"])
                    ledger.append(
                        "RISK_STATE_RESTORED",
                        {
                            "source": str(out / CHECKPOINT_FILE),
                            "checkpoint_sha256": payload["sha256"],
                            "ledger_root_at_checkpoint":
                                payload.get("ledger_root"),
                            "state": payload["state"],
                        },
                    )
                restored = True
            processor = PaperProcessor(
                policy=policy, size=size, retire_after=retire_after,
                ledger=ledger, risk_gate=gate,
            )
        frames = _read_new_frames(state, daily_path(data, day))
        if frames and ledger is not None and processor is not None:
            processor.process_sweeps(frames)
            ledger.save(out / f"ledger-{day}.jsonl")
            write_checkpoint(
                out / CHECKPOINT_FILE, gate, ledger.root_hash,
                sweep_file=str(daily_path(data, day)),
                sweep_offset=state.offset,
            )
            cumulative = cumulative_counts(ledger)
            (out / f"report-{day}.json").write_text(
                json.dumps(
                    {
                        "day": day,
                        "cumulative": cumulative,  # ledger-derived
                        "ledger_root_hash": ledger.root_hash,
                        "breaker_tripped": gate.tripped,
                        "consecutive_failures": gate.consecutive_failures,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        time.sleep(poll)


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="N7B paper-validation runner")
    ap.add_argument("--data", required=True, help="collector output dir")
    ap.add_argument("--out", required=True, help="runner output dir (ledgers)")
    ap.add_argument("--size", type=Decimal, default=Decimal(100))
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--max-staleness-s", type=float, default=45.0)
    ap.add_argument("--max-consecutive-failures", type=int, default=5)
    ap.add_argument("--retire-after", type=int, default=30,
                    help="retire a market absent for this many sweeps")
    ap.add_argument("--max-iterations", type=int, default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)
    policy = RiskPolicy(
        max_staleness=timedelta(seconds=args.max_staleness_s),
        min_net_profit=Decimal("0.01"),
        min_net_edge_per_share=Decimal("0.0001"),
        min_capacity=Decimal(1),
        max_exposure_per_asset=Decimal("10000"),
        max_total_exposure=Decimal("100000"),
        max_leg_risk_shares=Decimal("10000"),
        max_spread=Decimal("0.2"),
        max_consecutive_failures=args.max_consecutive_failures,
    )
    run_service(
        data_dir=args.data, out_dir=args.out, policy=policy,
        size=args.size, poll=args.poll, retire_after=args.retire_after,
        max_iterations=args.max_iterations,
    )


if __name__ == "__main__":
    main()
