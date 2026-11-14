"""N1 schema tests: fail-closed construction rules."""
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from event_arbitrage_lab.schemas import BookLevel, Market, OrderBook, Outcome

from conftest import NOW, VENUE_A, MARKET_A, YES, make_book, make_market


class TestBookLevel:
    def test_valid(self):
        lv = BookLevel(price=Decimal("0.48"), size=Decimal(100))
        assert lv.price == Decimal("0.48")

    @pytest.mark.parametrize("bad", ["0", "1", "1.01", "-0.5"])
    def test_price_out_of_bounds_rejected(self, bad):
        with pytest.raises(ValidationError):
            BookLevel(price=Decimal(bad), size=Decimal(100))

    @pytest.mark.parametrize("bad", ["0", "-10"])
    def test_non_positive_size_rejected(self, bad):
        with pytest.raises(ValidationError):
            BookLevel(price=Decimal("0.5"), size=Decimal(bad))

    def test_float_coerced_without_binary_artifacts(self):
        assert BookLevel(price=0.1, size=100).price == Decimal("0.1")


class TestOrderBook:
    def test_unsorted_bids_rejected(self):
        with pytest.raises(ValidationError, match="descending"):
            make_book(VENUE_A, MARKET_A, YES, bids=[("0.4", 10), ("0.5", 10)])

    def test_unsorted_asks_rejected(self):
        with pytest.raises(ValidationError, match="ascending"):
            make_book(VENUE_A, MARKET_A, YES, asks=[("0.6", 10), ("0.5", 10)])

    def test_crossed_book_rejected(self):
        with pytest.raises(ValidationError, match="crossed"):
            make_book(VENUE_A, MARKET_A, YES, bids=[("0.55", 10)], asks=[("0.50", 10)])

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            OrderBook(
                venue=VENUE_A,
                market_id=MARKET_A,
                outcome_id=YES,
                timestamp=datetime(2026, 10, 6, 12, 0),  # naive
            )

    def test_empty_sides_allowed(self):
        b = make_book(VENUE_A, MARKET_A, YES)
        assert b.best_ask is None and b.ask_depth() == 0


class TestMarket:
    def test_fewer_than_two_outcomes_rejected(self):
        with pytest.raises(ValidationError):
            Market(
                venue=VENUE_A,
                market_id=MARKET_A,
                event_id="e",
                outcomes=(Outcome(outcome_id="only", label="ONLY"),),
                mutually_exclusive=True,
                exhaustive=True,
                resolution_source="oracle",
            )

    def test_duplicate_outcome_ids_rejected(self):
        with pytest.raises(ValidationError, match="distinct"):
            Market(
                venue=VENUE_A,
                market_id=MARKET_A,
                event_id="e",
                outcomes=(
                    Outcome(outcome_id="x", label="A"),
                    Outcome(outcome_id="x", label="B"),
                ),
                mutually_exclusive=True,
                exhaustive=True,
                resolution_source="oracle",
            )

    def test_valid_binary_market(self):
        m = make_market()
        assert m.payout_per_unit == Decimal(1)
        assert NOW is not None  # fixture sanity
