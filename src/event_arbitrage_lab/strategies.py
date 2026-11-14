"""N2: arbitrage strategies. Pure functions over schemas.

net_profit = guaranteed_payout - gross_cost - total_fees - slippage_buffer
(README gate 5: fees, slippage, settlement and capital-lock assumptions
are part of edge; assumptions are recorded on the Opportunity itself.)

Gate 3: an opportunity is invalid if one leg cannot fill at the required
size — legs are matched at min(fill depths) and the deeper leg is
re-quoted at the matched size, so no unhedged residual exists.

Fee models are mandatory parameters: silently defaulting to zero fees is
forbidden. Slippage defaults to an explicit zero buffer.
"""
from __future__ import annotations

from decimal import Decimal

from .fees import FeeModel, SlippageBuffer
from .schemas import (
    Leg,
    Opportunity,
    OrderBook,
    Side,
    StrategyKind,
)
from .vwap import walk_book

PRECISION = Decimal("0.000001")

CROSS_VENUE_ASSUMPTIONS: tuple[str, ...] = (
    "mapping verified: both markets resolve the same event with equivalent settlement semantics",
    "mutually exclusive and exhaustive outcomes on both venues",
    "both legs executable at walked book levels (research assumption; N4 verifies in paper execution)",
    "payout currency and withdrawal path equivalent across venues",
    "capital locked until settlement; time value not priced (research basis)",
)

SINGLE_VENUE_ASSUMPTIONS: tuple[str, ...] = (
    "outcomes mutually exclusive and exhaustive on this market",
    "all legs executable at walked book levels (research assumption; N4 verifies in paper execution)",
    "single-venue settlement at payout_per_unit",
)


def _leg_cost(book: OrderBook, shares: Decimal, fee_model: FeeModel) -> Leg | None:
    """Walk the ask side for exactly `shares`; None if depth insufficient."""
    quote = walk_book(
        book.asks,
        shares,
        Side.ASK,
        venue=book.venue,
        market_id=book.market_id,
        outcome_id=book.outcome_id,
    )
    if not quote.fully_filled:
        return None
    fee = sum((fee_model.fee(f.shares, f.price) for f in quote.fills), Decimal(0))
    return Leg(
        venue=book.venue,
        market_id=book.market_id,
        outcome_id=book.outcome_id,
        shares=quote.filled,
        vwap=quote.vwap,
        gross_cost=quote.total_cost,
        fee=fee,
    )


def _build_opportunity(
    strategy: StrategyKind,
    legs: tuple[Leg, ...],
    payout_per_unit: Decimal,
    slippage: SlippageBuffer,
    min_profit: Decimal,
    assumptions: tuple[str, ...],
) -> Opportunity | None:
    shares = legs[0].shares
    gross = sum((l.gross_cost for l in legs), Decimal(0))
    fees = sum((l.fee for l in legs), Decimal(0))
    slip = slippage.total(shares)
    total = gross + fees + slip
    payout = (shares * payout_per_unit).quantize(PRECISION)
    net = (payout - total).quantize(PRECISION)
    if net <= min_profit:
        return None
    return Opportunity(
        strategy=strategy,
        legs=legs,
        shares=shares,
        gross_cost=gross,
        total_fees=fees,
        slippage_buffer=slip,
        total_cost=total,
        guaranteed_payout=payout,
        net_profit=net,
        edge_per_unit=(net / shares).quantize(PRECISION),
        roi=net / total,
        assumptions=assumptions,
    )


def two_leg_arbitrage(
    book_x: OrderBook,
    book_y: OrderBook,
    fee_x: FeeModel,
    fee_y: FeeModel,
    size: Decimal,
    *,
    strategy: StrategyKind,
    payout_per_unit: Decimal = Decimal(1),
    slippage: SlippageBuffer = SlippageBuffer(),
    min_profit: Decimal = Decimal(0),
) -> Opportunity | None:
    """Buy complementary outcomes: X on one book, Y on another.

    Cross-venue: X = YES on venue A, Y = NO on venue B (or vice versa).
    Single-venue: X = YES, Y = NO on the same market (binary completeness).
    One guaranteed payout unit per matched pair.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if book_x.outcome_id == book_y.outcome_id and book_x.market_id == book_y.market_id:
        raise ValueError("legs must be complementary (distinct) outcomes")

    first_x = walk_book(book_x.asks, size, Side.ASK)
    first_y = walk_book(book_y.asks, size, Side.ASK)
    matched = min(first_x.filled, first_y.filled)
    if matched <= 0:
        return None  # gate 3: a leg that cannot fill kills the opportunity

    leg_x = _leg_cost(book_x, matched, fee_x)
    leg_y = _leg_cost(book_y, matched, fee_y)
    if leg_x is None or leg_y is None:
        return None  # defensive: matched <= depth, cannot happen

    assumptions = (
        CROSS_VENUE_ASSUMPTIONS
        if strategy is StrategyKind.CROSS_VENUE_BINARY
        else SINGLE_VENUE_ASSUMPTIONS
    )
    return _build_opportunity(
        strategy, (leg_x, leg_y), payout_per_unit, slippage, min_profit, assumptions
    )


def completeness(
    books: tuple[OrderBook, ...],
    fee_models: tuple[FeeModel, ...],
    size: Decimal,
    *,
    payout_per_unit: Decimal = Decimal(1),
    slippage: SlippageBuffer = SlippageBuffer(),
    min_profit: Decimal = Decimal(0),
) -> Opportunity | None:
    """Buy every outcome of one mutually-exclusive-exhaustive market.

    Binary case: YES ask + NO ask < payout - fees - slippage.
    N-outcome: same per leg. All books must be distinct outcomes of the
    same market on one venue.
    """
    if len(books) < 2:
        raise ValueError("completeness requires at least 2 outcome books")
    if len(books) != len(fee_models):
        raise ValueError("books and fee_models must have equal length")
    first = books[0]
    for b in books[1:]:
        if b.venue != first.venue or b.market_id != first.market_id:
            raise ValueError("completeness books must share venue and market")
    ids = [b.outcome_id for b in books]
    if len(set(ids)) != len(ids):
        raise ValueError("completeness books must be distinct outcomes")
    if size <= 0:
        raise ValueError("size must be positive")

    fills = [walk_book(b.asks, size, Side.ASK).filled for b in books]
    matched = min(fills)
    if matched <= 0:
        return None

    legs: list[Leg] = []
    for b, fm in zip(books, fee_models):
        leg = _leg_cost(b, matched, fm)
        if leg is None:
            return None
        legs.append(leg)

    return _build_opportunity(
        StrategyKind.SINGLE_VENUE_COMPLETENESS,
        tuple(legs),
        payout_per_unit,
        slippage,
        min_profit,
        SINGLE_VENUE_ASSUMPTIONS,
    )
