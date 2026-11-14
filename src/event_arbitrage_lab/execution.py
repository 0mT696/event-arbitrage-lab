r"""N4: Paper Execution Engine.

Answers the question N2/N3 cannot: is a mathematical arbitrage actually
fillable? Simulates two-leg execution with latency-driven book movement,
partial fills, rejections and leg risk — then unwinds residual exposure
and accounts PnL honestly.

Determinism: all "randomness" (fill ratios, rejections, book movement) is
injected via FillPolicy implementations and book snapshots. No I/O, no
random module, no real orders — replay-friendly for N5.

State machine (illegal transitions raise IllegalTransition):

    QUOTED -> LEG_A_WORKING -> LEG_B_WORKING -> HEDGED ------> CLOSED
                                       \---> RESIDUAL_OPEN -> UNWINDING -> CLOSED
                                       \---> RESIDUAL_OPEN -> CLOSED (residual held)
    QUOTED / LEG_A_WORKING -> ABORTED
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from typing import Protocol, Sequence

from .fees import FeeModel, SlippageBuffer, ZeroFee
from .schemas import BookLevel, Fill, OrderBook, Side
from .vwap import walk_book

PRECISION = Decimal("0.000001")


class PairState(str, Enum):
    QUOTED = "QUOTED"
    LEG_A_WORKING = "LEG_A_WORKING"
    LEG_B_WORKING = "LEG_B_WORKING"
    HEDGED = "HEDGED"
    RESIDUAL_OPEN = "RESIDUAL_OPEN"
    UNWINDING = "UNWINDING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


_TRANSITIONS: dict[PairState, set[PairState]] = {
    PairState.QUOTED: {PairState.LEG_A_WORKING, PairState.ABORTED},
    PairState.LEG_A_WORKING: {PairState.LEG_B_WORKING, PairState.ABORTED},
    PairState.LEG_B_WORKING: {PairState.HEDGED, PairState.RESIDUAL_OPEN},
    PairState.HEDGED: {PairState.CLOSED},
    PairState.RESIDUAL_OPEN: {PairState.UNWINDING, PairState.CLOSED},
    PairState.UNWINDING: {PairState.CLOSED},
    PairState.CLOSED: set(),
    PairState.ABORTED: set(),
}


class IllegalTransition(Exception):
    """Raised when the execution state machine is advanced illegally."""


class PairStateMachine:
    def __init__(self) -> None:
        self.state = PairState.QUOTED

    def advance(self, to: PairState) -> None:
        if to not in _TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {to.value} not allowed")
        self.state = to


class LegStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True)
class LegOutcome:
    side: Side  # BUY for entry legs, SELL(BID side) for unwind
    status: LegStatus
    fills: tuple[Fill, ...]

    @property
    def shares(self) -> Decimal:
        return sum((f.shares for f in self.fills), Decimal(0))

    @property
    def notional(self) -> Decimal:
        """Cost for BUY legs, proceeds for SELL legs."""
        return sum((f.cost for f in self.fills), Decimal(0))


class FillPolicy(Protocol):
    """How one leg's order interacts with one book snapshot."""

    def execute(
        self, book: OrderBook, size: Decimal, limit_price: Decimal | None
    ) -> LegOutcome:
        ...


@dataclass(frozen=True)
class TakerFillPolicy:
    """Immediate taker against the ask side.

    depth_ratio < 1 models partial availability / partial fills;
    reject=True models a venue-side rejection; limit_price caps acceptable
    levels (levels above the limit are not taken).
    """

    depth_ratio: Decimal = Decimal(1)
    reject: bool = False

    def execute(
        self, book: OrderBook, size: Decimal, limit_price: Decimal | None
    ) -> LegOutcome:
        if self.reject or size <= 0:
            return LegOutcome(Side.ASK, LegStatus.REJECTED, ())
        levels = book.asks
        if limit_price is not None:
            levels = tuple(lv for lv in levels if lv.price <= limit_price)
        if self.depth_ratio < 1:
            # ROUND_FLOOR: modeled available depth must never exceed
            # ratio * actual depth (fail-closed fill simulation)
            scaled = []
            for lv in levels:
                scaled_size = (lv.size * self.depth_ratio).quantize(
                    PRECISION, rounding=ROUND_FLOOR
                )
                if scaled_size > 0:  # floored-to-zero depth is unavailable
                    scaled.append(BookLevel(price=lv.price, size=scaled_size))
            levels = tuple(scaled)
        quote = walk_book(
            levels, size, Side.ASK,
            venue=book.venue, market_id=book.market_id, outcome_id=book.outcome_id,
        )
        if quote.filled <= 0:
            return LegOutcome(Side.ASK, LegStatus.REJECTED, ())
        status = LegStatus.FILLED if quote.fully_filled else LegStatus.PARTIAL
        return LegOutcome(Side.ASK, status, quote.fills)


@dataclass(frozen=True)
class PaperExecutionResult:
    final_state: PairState
    paired_shares: Decimal
    residual_shares: Decimal  # unhedged shares still held after unwind attempt
    leg_a: LegOutcome
    leg_b: LegOutcome
    unwind: LegOutcome | None
    paired_expected_pnl: Decimal  # settlement expectation on the hedged part
    unwind_realized_pnl: Decimal  # realized PnL from unwinding residual
    total_fees: Decimal
    net_pnl: Decimal  # realized+expected PnL; EXCLUDES residual_cost_basis
    events: tuple[str, ...]  # evidence timeline (N5 consumes this)
    # Locked capital still at risk in the unhedged remainder (VWAP basis
    # + pro-rata leg-A fees). Reported separately so net_pnl can never be
    # misread as "no loss" while residual exposure is still open.
    residual_cost_basis: Decimal = Decimal(0)


def fills_fees(fills: Sequence[Fill], fee_model: FeeModel) -> Decimal:
    """Total fees over a fill sequence (public: the N6 execution-time
    gate uses the exact same accounting as the executor)."""
    return sum((fee_model.fee(f.shares, f.price) for f in fills), Decimal(0))


def leg_b_limit(
    *,
    payout_per_unit: Decimal,
    vwap_a: Decimal,
    shares_a: Decimal,
    fees_a_total: Decimal,
    fee_b: FeeModel,
    slippage: SlippageBuffer,
    min_profit: Decimal,
) -> Decimal:
    """Fee-aware limit price for leg B given leg A's actual VWAP.

    Gate 5 at execution time: the limit must leave room for FEES, not
    just gross edge — otherwise latency drift can push us into fills
    that are gross-profitable but net losers. Shared between the paper
    executor and the N6 execution-time risk gate so both enforce the
    SAME ceiling (a gate that mirrors a different executor is theater).
    """
    limit_gross = (
        payout_per_unit - vwap_a - slippage.per_unit - min_profit / shares_a
    )
    fees_a_per_share = fees_a_total / shares_a
    # Worst-case per-share leg-B fee over (0, limit_gross]: for the
    # quadratic shape the max on that interval sits at min(limit, 0.5);
    # for notional-flat shapes at the limit itself. max() of both probes
    # covers either shape — model-agnostic and fail-closed.
    probe_hi = min(max(limit_gross, PRECISION), Decimal("0.999999"))
    probe_mid = Decimal("0.5")
    fee_b_worst = max(
        fee_b.fee(Decimal(1), probe_hi), fee_b.fee(Decimal(1), probe_mid)
    )
    return limit_gross - fees_a_per_share - fee_b_worst


def _sell_into_bids(
    book: OrderBook, shares: Decimal, fee_model: FeeModel
) -> tuple[LegOutcome, Decimal]:
    """Unwind: sell `shares` into the bid side. Returns outcome + fees."""
    quote = walk_book(
        book.bids, shares, Side.BID,
        venue=book.venue, market_id=book.market_id, outcome_id=book.outcome_id,
    )
    if quote.filled <= 0:
        return LegOutcome(Side.BID, LegStatus.REJECTED, ()), Decimal(0)
    status = LegStatus.FILLED if quote.fully_filled else LegStatus.PARTIAL
    return LegOutcome(Side.BID, status, quote.fills), fills_fees(quote.fills, fee_model)


def execute_pair(
    book_a: OrderBook,
    book_b: OrderBook,
    size: Decimal,
    fee_a: FeeModel,
    fee_b: FeeModel,
    policy_a: FillPolicy,
    policy_b: FillPolicy,
    *,
    payout_per_unit: Decimal = Decimal(1),
    min_profit: Decimal = Decimal(0),
    slippage: SlippageBuffer = SlippageBuffer(),
    unwind_book: OrderBook | None = None,
    unwind_fee: FeeModel | None = None,
    unwind_residual: bool = True,
) -> PaperExecutionResult:
    """Simulate paired two-leg execution.

    book_a: leg A book at decision time. book_b: leg B book AFTER latency
    (pass the same snapshot for zero-latency, a moved one to model drift).
    Leg B is protected by a limit price derived from leg A's actual VWAP,
    net of worst-case per-share fees, slippage and min_profit: we never
    chase a pair that stopped being NET-profitable (gate 5 applied at
    execution time).

    Identity invariant: `unwind_book`, if given, MUST be the same asset as
    the residual (book_a's venue/market_id/outcome_id). A mismatch blocks
    the unwind fail-closed — no fills, no realized PnL; the residual stays
    open and is reported with its cost basis.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    sm = PairStateMachine()
    events: list[str] = []

    # ---- leg A ----
    sm.advance(PairState.LEG_A_WORKING)
    out_a = policy_a.execute(book_a, size, None)
    events.append(f"leg_a:{out_a.status.value}:{out_a.shares}")
    if out_a.shares <= 0:
        sm.advance(PairState.ABORTED)
        events.append("aborted:leg_a_no_fill")
        return PaperExecutionResult(
            PairState.ABORTED, Decimal(0), Decimal(0), out_a,
            LegOutcome(Side.ASK, LegStatus.NOT_ATTEMPTED, ()), None,
            Decimal(0), Decimal(0), Decimal(0), Decimal(0), tuple(events),
        )
    cost_a = out_a.notional
    vwap_a = cost_a / out_a.shares
    fees_a_total = fills_fees(out_a.fills, fee_a)

    # ---- leg B, limit-protected ----
    sm.advance(PairState.LEG_B_WORKING)
    limit_b = leg_b_limit(
        payout_per_unit=payout_per_unit,
        vwap_a=vwap_a,
        shares_a=out_a.shares,
        fees_a_total=fees_a_total,
        fee_b=fee_b,
        slippage=slippage,
        min_profit=min_profit,
    )
    out_b = policy_b.execute(book_b, out_a.shares, limit_b)
    events.append(f"leg_b:{out_b.status.value}:{out_b.shares}:limit={limit_b}")
    fees_b = fills_fees(out_b.fills, fee_b)

    paired = min(out_a.shares, out_b.shares)
    residual = out_a.shares - paired
    ratio_paired = paired / out_a.shares
    cost_a_paired = (cost_a * ratio_paired).quantize(PRECISION)
    fees_a_paired = (fees_a_total * ratio_paired).quantize(PRECISION)

    paired_pnl = (
        paired * payout_per_unit
        - cost_a_paired
        - out_b.notional
        - fees_a_paired
        - fees_b
        - slippage.total(paired)
    ).quantize(PRECISION)

    # ---- residual / leg risk ----
    unwind_outcome: LegOutcome | None = None
    unwind_pnl = Decimal(0)
    unwind_fees = Decimal(0)
    residual_held = residual
    if residual <= 0:
        sm.advance(PairState.HEDGED)
        events.append(f"hedged:paired={paired}")
    else:
        sm.advance(PairState.RESIDUAL_OPEN)
        events.append(f"residual_open:{residual}")
        if unwind_residual:
            book_u = unwind_book if unwind_book is not None else book_a
            fee_u = unwind_fee if unwind_fee is not None else fee_a
            # IDENTITY INVARIANT: the unwind book must be the SAME asset as
            # the residual (leg A's venue/market/outcome). Selling residual
            # into a foreign book fabricates PnL — fail closed instead:
            # no unwind, no realized PnL, residual stays open and visible.
            if (book_u.venue, book_u.market_id, book_u.outcome_id) != (
                book_a.venue,
                book_a.market_id,
                book_a.outcome_id,
            ):
                events.append(
                    "unwind_blocked:identity_mismatch:"
                    f"{book_u.venue}/{book_u.market_id}/{book_u.outcome_id}"
                )
                sm.advance(PairState.CLOSED)
                residual_basis = (
                    vwap_a * residual
                    + fees_a_total * (residual / out_a.shares)
                ).quantize(PRECISION)
                net = paired_pnl  # unwind contributes nothing
                events.append(f"closed:net={net}:residual_basis={residual_basis}")
                return PaperExecutionResult(
                    final_state=sm.state,
                    paired_shares=paired,
                    residual_shares=residual,
                    leg_a=out_a,
                    leg_b=out_b,
                    unwind=None,
                    paired_expected_pnl=paired_pnl,
                    unwind_realized_pnl=Decimal(0),
                    total_fees=fees_a_total + fees_b,
                    net_pnl=net,
                    events=tuple(events),
                    residual_cost_basis=residual_basis,
                )
            sm.advance(PairState.UNWINDING)
            unwind_outcome, unwind_fees = _sell_into_bids(book_u, residual, fee_u)
            sold = unwind_outcome.shares
            proceeds = unwind_outcome.notional
            if sold > 0:
                # cost basis charged only on the portion actually sold;
                # unsold remainder stays open as residual_held exposure
                basis_sold = (vwap_a * sold).quantize(PRECISION)
                fees_a_sold = (fees_a_total * (sold / out_a.shares)).quantize(PRECISION)
                unwind_pnl = (proceeds - basis_sold - fees_a_sold - unwind_fees).quantize(
                    PRECISION
                )
            residual_held = residual - sold
            events.append(f"unwind:sold={sold}:pnl={unwind_pnl}")

    sm.advance(PairState.CLOSED)
    # honest exposure accounting: capital locked in unsold residual
    # (VWAP basis + pro-rata leg-A fees) is reported separately; net_pnl
    # only reflects settled/realized parts
    residual_basis = Decimal(0)
    if residual_held > 0:
        residual_basis = (
            vwap_a * residual_held
            + fees_a_total * (residual_held / out_a.shares)
        ).quantize(PRECISION)
    total_fees = fees_a_total + fees_b + unwind_fees
    net = (paired_pnl + unwind_pnl).quantize(PRECISION)
    events.append(f"closed:net={net}:residual_basis={residual_basis}")
    return PaperExecutionResult(
        final_state=sm.state,
        paired_shares=paired,
        residual_shares=residual_held,
        leg_a=out_a,
        leg_b=out_b,
        unwind=unwind_outcome,
        paired_expected_pnl=paired_pnl,
        unwind_realized_pnl=unwind_pnl,
        total_fees=total_fees,
        net_pnl=net,
        events=tuple(events),
        residual_cost_basis=residual_basis,
    )
