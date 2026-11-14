"""N3: opportunity scanner.

Net-edge filter over a verified venue pair:
- fees, slippage  (via strategies; gate 5)
- staleness       (books older than max_staleness are rejected; risk-gate seed)
- minimum depth   (matched size below min_size is rejected; gate 3)

Fail-Closed: unverified / non-equivalent mappings and market mismatches
produce Rejections, never opportunities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .fees import FeeModel, SlippageBuffer
from .rules import (
    RuleViolation,
    VenuePairMapping,
    validate_market_rules,
    validate_pair_rules,
)
from .schemas import Market, Opportunity, OrderBook, StrategyKind
from .strategies import completeness, two_leg_arbitrage


@dataclass(frozen=True)
class BinaryMarketBooks:
    """One venue's binary market: YES and NO order books."""

    market: Market
    yes: OrderBook
    no: OrderBook

    def __post_init__(self) -> None:
        for b in (self.yes, self.no):
            if b.venue != self.market.venue or b.market_id != self.market.market_id:
                raise ValueError("book venue/market must match BinaryMarketBooks.market")
        if self.yes.outcome_id == self.no.outcome_id:
            raise ValueError("YES and NO books must be distinct outcomes")


@dataclass(frozen=True)
class ScanParams:
    size: Decimal
    now: datetime
    max_staleness: timedelta
    min_size: Decimal = Decimal(0)  # minimum matched depth to accept
    min_profit: Decimal = Decimal(0)
    slippage: SlippageBuffer = SlippageBuffer()


@dataclass(frozen=True)
class Rejection:
    reason: str
    detail: str


@dataclass(frozen=True)
class ScanResult:
    opportunities: tuple[Opportunity, ...]  # sorted by net_profit desc
    rejections: tuple[Rejection, ...]


def _staleness_rejections(
    books: list[OrderBook], params: ScanParams
) -> list[Rejection]:
    out = []
    for b in books:
        age = params.now - b.timestamp
        if age > params.max_staleness or age < timedelta(0):
            out.append(
                Rejection(
                    "STALE_BOOK",
                    f"{b.venue}/{b.market_id}/{b.outcome_id}: age {age} "
                    f"outside [0, {params.max_staleness}]",
                )
            )
    return out


def scan_pair(
    mapping: VenuePairMapping,
    books_a: BinaryMarketBooks,
    books_b: BinaryMarketBooks,
    fee_a: FeeModel,
    fee_b: FeeModel,
    params: ScanParams,
) -> ScanResult:
    """Scan one mapped pair: both cross-venue directions plus both
    single-venue completeness checks."""
    # --- Market Rule Gate (P0 fix): NOTHING below may produce an
    # Opportunity unless full pair eligibility passes. Specific rejections
    # first for observability, comprehensive rule validation last. ---
    if not mapping.usable():
        return ScanResult(
            (),
            (
                Rejection(
                    "MAPPING_NOT_USABLE",
                    f"verified={mapping.verified}, "
                    f"resolution_source_equivalent={mapping.resolution_source_equivalent}",
                ),
            ),
        )
    if (books_a.market.venue, books_a.market.market_id) != (
        mapping.venue_a,
        mapping.market_a,
    ) or (books_b.market.venue, books_b.market.market_id) != (
        mapping.venue_b,
        mapping.market_b,
    ):
        return ScanResult(
            (),
            (
                Rejection(
                    "MARKET_MISMATCH",
                    f"mapping expects {mapping.venue_a}/{mapping.market_a} and "
                    f"{mapping.venue_b}/{mapping.market_b}",
                ),
            ),
        )
    try:
        # full pair eligibility: event match, MECE on BOTH markets,
        # payout compatibility across mapping and both markets
        validate_pair_rules(mapping, books_a.market, books_b.market)
    except RuleViolation as exc:
        return ScanResult((), (Rejection("RULE_GATE", str(exc)),))

    stale = _staleness_rejections(
        [books_a.yes, books_a.no, books_b.yes, books_b.no], params
    )
    if stale:
        return ScanResult((), tuple(stale))

    candidates = [
        two_leg_arbitrage(
            books_a.yes,
            books_b.no,
            fee_a,
            fee_b,
            params.size,
            strategy=StrategyKind.CROSS_VENUE_BINARY,
            payout_per_unit=mapping.payout_per_unit,
            slippage=params.slippage,
            min_profit=params.min_profit,
        ),
        two_leg_arbitrage(
            books_b.yes,
            books_a.no,
            fee_b,
            fee_a,
            params.size,
            strategy=StrategyKind.CROSS_VENUE_BINARY,
            payout_per_unit=mapping.payout_per_unit,
            slippage=params.slippage,
            min_profit=params.min_profit,
        ),
        completeness(
            (books_a.yes, books_a.no),
            (fee_a, fee_a),
            params.size,
            payout_per_unit=mapping.payout_per_unit,
            slippage=params.slippage,
            min_profit=params.min_profit,
        ),
        completeness(
            (books_b.yes, books_b.no),
            (fee_b, fee_b),
            params.size,
            payout_per_unit=mapping.payout_per_unit,
            slippage=params.slippage,
            min_profit=params.min_profit,
        ),
    ]

    opps: list[Opportunity] = []
    rejections: list[Rejection] = list(stale)
    for c in candidates:
        if c is None:
            continue
        if c.shares < params.min_size:
            rejections.append(
                Rejection(
                    "INSUFFICIENT_DEPTH",
                    f"{c.strategy.value}: matched {c.shares} < min_size {params.min_size}",
                )
            )
            continue
        opps.append(c)

    opps.sort(key=lambda o: o.net_profit, reverse=True)
    return ScanResult(tuple(opps), tuple(rejections))


def scan_market(
    books: BinaryMarketBooks,
    fee: FeeModel,
    params: ScanParams,
) -> ScanResult:
    """Single-venue completeness scan (N7): buy every outcome of ONE
    binary market. No mapping involved — the identity of market,
    resolution source and payout is intrinsic, so no VenuePairMapping
    is needed. Rule gate first (fail-closed), then staleness, then the
    completeness strategy with both legs on this market's fee model."""
    try:
        validate_market_rules(books.market)
    except RuleViolation as exc:
        return ScanResult((), (Rejection("RULE_GATE", str(exc)),))
    stale = _staleness_rejections([books.yes, books.no], params)
    if stale:
        return ScanResult((), tuple(stale))
    opp = completeness(
        (books.yes, books.no),
        (fee, fee),
        params.size,
        payout_per_unit=books.market.payout_per_unit,
        slippage=params.slippage,
        min_profit=params.min_profit,
    )
    if opp is None:
        return ScanResult((), ())
    if opp.shares < params.min_size:
        return ScanResult(
            (),
            (
                Rejection(
                    "INSUFFICIENT_DEPTH",
                    f"{opp.strategy.value}: matched {opp.shares} "
                    f"< min_size {params.min_size}",
                ),
            ),
        )
    return ScanResult((opp,), ())
