"""Shared builders for tests."""
from datetime import datetime, timezone
from decimal import Decimal

from event_arbitrage_lab.rules import VenuePairMapping
from event_arbitrage_lab.schemas import BookLevel, Market, OrderBook, Outcome

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

VENUE_A = "venue_a"
VENUE_B = "venue_b"
EVENT = "evt-election-x"
MARKET_A = "mkt-a-1"
MARKET_B = "mkt-b-1"
YES = "yes"
NO = "no"


def make_market(
    venue=VENUE_A,
    market_id=MARKET_A,
    event_id=EVENT,
    mutually_exclusive=True,
    exhaustive=True,
    payout=Decimal(1),
    resolution="venue resolution oracle",
) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        event_id=event_id,
        outcomes=(Outcome(outcome_id=YES, label="YES"), Outcome(outcome_id=NO, label="NO")),
        mutually_exclusive=mutually_exclusive,
        exhaustive=exhaustive,
        resolution_source=resolution,
        payout_per_unit=payout,
    )


def make_book(venue, market_id, outcome_id, bids=(), asks=(), ts=NOW) -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id=market_id,
        outcome_id=outcome_id,
        bids=tuple(BookLevel(price=p, size=s) for p, s in bids),
        asks=tuple(BookLevel(price=p, size=s) for p, s in asks),
        timestamp=ts,
    )


def make_mapping(verified=True, equivalent=True, payout=Decimal(1)) -> VenuePairMapping:
    return VenuePairMapping(
        event_id=EVENT,
        venue_a=VENUE_A,
        market_a=MARKET_A,
        venue_b=VENUE_B,
        market_b=MARKET_B,
        verified=verified,
        resolution_source_equivalent=equivalent,
        payout_per_unit=payout,
    )
