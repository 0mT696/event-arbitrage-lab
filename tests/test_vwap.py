"""N2 VWAP engine tests (gate 2: only book depth is executable cost)."""
from decimal import Decimal

import pytest

from event_arbitrage_lab.schemas import BookLevel
from event_arbitrage_lab.vwap import walk_book


def levels(*pairs):
    return tuple(BookLevel(price=p, size=s) for p, s in pairs)


class TestWalkBook:
    def test_single_level_full_fill(self):
        q = walk_book(levels(("0.48", 100)), Decimal(50))
        assert q.fully_filled
        assert q.total_cost == Decimal("24.00")
        assert q.vwap == Decimal("0.48")

    def test_multi_level_vwap(self):
        q = walk_book(levels(("0.40", 60), ("0.45", 40)), Decimal(100))
        assert q.total_cost == Decimal("42.00")
        assert q.vwap == Decimal("0.42")

    def test_partial_fill(self):
        q = walk_book(levels(("0.40", 30)), Decimal(100))
        assert not q.fully_filled
        assert q.filled == Decimal(30)

    def test_prefix_determinism(self):
        lv = levels(("0.40", 60), ("0.45", 40))
        small = walk_book(lv, Decimal(50))
        big = walk_book(lv, Decimal(100))
        assert small.fills[0].price == big.fills[0].price
        assert small.total_cost == Decimal("20.00")

    @pytest.mark.parametrize("bad", ["0", "-5"])
    def test_non_positive_size_rejected(self, bad):
        with pytest.raises(ValueError):
            walk_book(levels(("0.5", 10)), Decimal(bad))

    def test_zero_fill_vwap_undefined(self):
        q = walk_book((), Decimal(10))
        assert q.filled == 0
        with pytest.raises(ValueError):
            _ = q.vwap
