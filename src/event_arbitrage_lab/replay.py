"""N5: replay runner.

Feeds recorded book snapshots through the N3 scanner and N4 paper
executor, recording everything into the evidence ledger. Deterministic:
same snapshot file -> same report -> same ledger root hash.

Latency modeling: a PairTick may carry `exec_books` — the book pair at
execution time (a later snapshot). If absent, execution happens against
the scan-time books (zero-latency replay).

Evidence provenance: the ledger records SCAN_SNAPSHOT (all four
scan-time books) on every tick and EXECUTION_SNAPSHOT (the exact two
books fed to the executor, after identity resolution) before every
execution — every fill/reject/unwind is explainable from the ledger
alone, without rerunning anything.

Risk gating (N6) runs TWICE per opportunity when a gate is attached:
PRE_TRADE against the scan-time books, then EXECUTION_TIME against
the exact resolved exec books (identity + freshness + spread +
executor-mirroring liquidity/edge simulation). Both decisions are
ledger evidence with a distinguishing "phase" field; a rejection in
either phase blocks execution (fail closed, TOCTOU-safe).

ReplayFrame persistence schema (JSONL, one frame per line):

    {
      "v": 1,                          # schema version (fail-closed on mismatch)
      "books_a": {"market": <Market>, "yes": <OrderBook>, "no": <OrderBook>},
      "books_b": {"market": <Market>, "yes": <OrderBook>, "no": <OrderBook>},
      "exec_books": [<OrderBook>, <OrderBook>] | null
    }

All nested models use pydantic JSON mode (Decimal -> str, datetime ->
ISO8601), so save_ticks/load_ticks round-trips are byte-exact and
replaying loaded frames reproduces identical reports and root hashes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    from .risk import RiskGate

from .execution import FillPolicy, PaperExecutionResult, execute_pair
from .fees import FeeModel, SlippageBuffer
from .ledger import EvidenceLedger
from .rules import VenuePairMapping
from .scanner import BinaryMarketBooks, ScanParams, scan_pair
from .schemas import Market, OrderBook, StrategyKind


@dataclass(frozen=True)
class PairTick:
    """One replay step: both venues' binary books at one point in time."""

    books_a: BinaryMarketBooks
    books_b: BinaryMarketBooks
    exec_books: tuple[OrderBook, OrderBook] | None = None  # latency override

    def all_books(self) -> tuple[OrderBook, ...]:
        return (self.books_a.yes, self.books_a.no, self.books_b.yes, self.books_b.no)

    def lookup(self, venue: str, market_id: str, outcome_id: str) -> OrderBook | None:
        for b in self.all_books():
            if (b.venue, b.market_id, b.outcome_id) == (venue, market_id, outcome_id):
                return b
        return None


@dataclass(frozen=True)
class ReplayReport:
    ticks: int
    opportunities: int
    executions: int
    clean_hedged: int  # paired > 0 and no residual exposure left
    degraded: int  # aborted / residual held / unwind loss
    total_net_pnl: Decimal
    rejections: int
    ledger_root_hash: str
    risk_rejected: int = 0  # opportunities blocked by the N6 risk gate


def save_snapshots(books: Iterable[OrderBook], path: str | Path) -> int:
    """Persist canonical book snapshots as JSONL (venue-agnostic)."""
    lines = [json.dumps(b.model_dump(mode="json")) for b in books]
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""))
    return len(lines)


def load_snapshots(path: str | Path) -> list[OrderBook]:
    return [
        OrderBook.model_validate(json.loads(line))
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]


TICK_SCHEMA_VERSION = 1


def _side_to_json(side: BinaryMarketBooks) -> dict:
    return {
        "market": side.market.model_dump(mode="json"),
        "yes": side.yes.model_dump(mode="json"),
        "no": side.no.model_dump(mode="json"),
    }


def _side_from_json(d: dict) -> BinaryMarketBooks:
    return BinaryMarketBooks(
        market=Market.model_validate(d["market"]),
        yes=OrderBook.model_validate(d["yes"]),
        no=OrderBook.model_validate(d["no"]),
    )


def save_ticks(ticks: Iterable[PairTick], path: str | Path) -> int:
    """Persist full ReplayFrames (scan books + latency override) as JSONL.

    This is the complete replay input — unlike save_snapshots, nothing
    that influences execution is left out."""
    lines = []
    for t in ticks:
        frame = {
            "v": TICK_SCHEMA_VERSION,
            "books_a": _side_to_json(t.books_a),
            "books_b": _side_to_json(t.books_b),
            "exec_books": (
                [b.model_dump(mode="json") for b in t.exec_books]
                if t.exec_books is not None
                else None
            ),
        }
        lines.append(json.dumps(frame, sort_keys=True))
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""))
    return len(lines)


def load_ticks(path: str | Path) -> list[PairTick]:
    """Load ReplayFrames; unknown schema version fails closed."""
    ticks: list[PairTick] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        if frame.get("v") != TICK_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported replay frame version: {frame.get('v')!r} "
                f"(expected {TICK_SCHEMA_VERSION})"
            )
        exec_books = frame.get("exec_books")
        ticks.append(
            PairTick(
                books_a=_side_from_json(frame["books_a"]),
                books_b=_side_from_json(frame["books_b"]),
                exec_books=(
                    tuple(OrderBook.model_validate(b) for b in exec_books)
                    if exec_books is not None
                    else None
                ),
            )
        )
    return ticks


def run_replay(
    ticks: Sequence[PairTick] | Iterable[PairTick],
    mapping: VenuePairMapping,
    fee_a: FeeModel,
    fee_b: FeeModel,
    policy_a: FillPolicy,
    policy_b: FillPolicy,
    *,
    size: Decimal,
    max_staleness: timedelta,
    slippage: SlippageBuffer = SlippageBuffer(),
    min_profit: Decimal = Decimal(0),
    ledger: EvidenceLedger | None = None,
    risk_gate: "RiskGate | None" = None,
) -> ReplayReport:
    """Replay ticks through scanner + [risk gate] + paper executor.

    When risk_gate is provided it sits ON the production path: every
    opportunity is gated before execution and every decision lands in the
    ledger (the gate receives the replay ledger if it has none)."""
    if size <= 0:
        raise ValueError("size must be positive")
    ledger = ledger if ledger is not None else EvidenceLedger()
    if risk_gate is not None and risk_gate.ledger is None:
        risk_gate.ledger = ledger  # provenance: decisions must be evidence

    n_opp = n_exec = n_clean = n_degraded = n_rej = n_risk_rej = 0
    total_pnl = Decimal(0)
    n_ticks = 0

    for i, tick in enumerate(ticks):
        n_ticks += 1
        books = tick.all_books()
        now = max(b.timestamp for b in books)  # clock from data, deterministic
        ledger.append(
            "SCAN_SNAPSHOT",
            {"tick": i, "books": [b.model_dump(mode="python") for b in books]},
        )

        params = ScanParams(
            size=size,
            now=now,
            max_staleness=max_staleness,
            slippage=slippage,
            min_profit=min_profit,
        )
        scan = scan_pair(mapping, tick.books_a, tick.books_b, fee_a, fee_b, params)

        for r in scan.rejections:
            n_rej += 1
            ledger.append("REJECTION", {"tick": i, "reason": r.reason, "detail": r.detail})

        for opp in scan.opportunities:
            n_opp += 1
            ledger.append("OPPORTUNITY", {"tick": i, "opportunity": opp})

            if len(opp.legs) != 2:
                ledger.append(
                    "EXECUTION_SKIPPED",
                    {"tick": i, "reason": "framework v1 executes 2-leg opportunities only"},
                )
                continue
            lx, ly = opp.legs
            book_x = tick.lookup(lx.venue, lx.market_id, lx.outcome_id)
            book_y = tick.lookup(ly.venue, ly.market_id, ly.outcome_id)
            if book_x is None or book_y is None:
                ledger.append(
                    "EXECUTION_SKIPPED", {"tick": i, "reason": "leg books not in tick"}
                )
                continue

            # ---- N6 risk gate (production path) ----
            if risk_gate is not None:
                books_map = {(
                    b.venue, b.market_id, b.outcome_id,
                ): b for b in books}
                decision = risk_gate.evaluate(opp, books_map, now)
                if not decision.approved:
                    n_risk_rej += 1
                    continue
            if tick.exec_books is not None:
                # latency override: match by leg IDENTITY, never by
                # position — a swapped/foreign book must not be executed
                ex = tick.exec_books
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
                        {"tick": i, "reason": "exec books do not match leg identities"},
                    )
                    continue
                book_x, book_y = bx, by

            fee_x = fee_a if lx.venue == mapping.venue_a else fee_b
            fee_y = fee_a if ly.venue == mapping.venue_a else fee_b

            # ---- N6 execution-time gate (TOCTOU defense) ----
            # PRE_TRADE approval was against SCAN-time books; the books
            # the executor is about to see must re-pass identity,
            # freshness, spread, executable liquidity and CURRENT net
            # edge. A scan-time approval never implies execution-time
            # approval.
            if risk_gate is not None:
                exec_map = {
                    (b.venue, b.market_id, b.outcome_id): b
                    for b in (book_x, book_y)
                }
                exec_decision = risk_gate.evaluate_execution(
                    opp, exec_map, now,
                    fees=(fee_x, fee_y),
                    payout_per_unit=mapping.payout_per_unit,
                    slippage=slippage,
                    min_profit=min_profit,
                )
                if not exec_decision.approved:
                    n_risk_rej += 1
                    continue

            # provenance: the exact books the executor will see, pinned
            # into the ledger BEFORE execution — every fill/reject/unwind
            # is explainable from evidence alone
            ledger.append(
                "EXECUTION_SNAPSHOT",
                {
                    "tick": i,
                    "legs": [
                        {"venue": lx.venue, "market_id": lx.market_id,
                         "outcome_id": lx.outcome_id},
                        {"venue": ly.venue, "market_id": ly.market_id,
                         "outcome_id": ly.outcome_id},
                    ],
                    "books": [
                        book_x.model_dump(mode="python"),
                        book_y.model_dump(mode="python"),
                    ],
                    "latency_override": tick.exec_books is not None,
                },
            )

            res = execute_pair(
                book_x, book_y, size, fee_x, fee_y, policy_a, policy_b,
                payout_per_unit=mapping.payout_per_unit,
                min_profit=min_profit,
                slippage=slippage,
            )
            n_exec += 1
            total_pnl += res.net_pnl
            # clean = fully hedged AND no residual exposure AND no unwind
            # loss; anything else (abort, residual held, unwind loss) is
            # degraded — per ReplayReport field docs
            if (
                res.paired_shares > 0
                and res.residual_shares == 0
                and res.unwind_realized_pnl >= 0
            ):
                n_clean += 1
            else:
                n_degraded += 1
            ledger.append("EXECUTION", {"tick": i, "result": res})
            if risk_gate is not None:
                risk_gate.record_execution(opp, res)

    return ReplayReport(
        ticks=n_ticks,
        opportunities=n_opp,
        executions=n_exec,
        clean_hedged=n_clean,
        degraded=n_degraded,
        total_net_pnl=total_pnl.quantize(Decimal("0.000001")),
        rejections=n_rej,
        ledger_root_hash=ledger.root_hash,
        risk_rejected=n_risk_rej,
    )
