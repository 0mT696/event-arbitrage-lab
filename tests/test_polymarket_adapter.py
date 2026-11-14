"""Polymarket adapter tests — recorded fixtures only, zero network."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from event_arbitrage_lab.adapters.polymarket import (
    CATEGORY_TAKER_RATES,
    MAX_TAKER_RATE,
    PolymarketAdapter,
)

FIXED_NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

GAMMA_BINARY = {
    "id": 520001,
    "question": "Will X win the 2026 election?",
    "outcomes": json.dumps(["Yes", "No"]),
    "clobTokenIds": json.dumps(["token-yes-1", "token-no-1"]),
    "conditionId": "0xcond1",
    "category": "Politics",
    "negRisk": False,
}

GAMMA_NEGRISK = {
    "id": 520002,
    "question": "Who wins the 2026 election?",
    "outcomes": json.dumps(["Alice", "Bob", "Carol"]),
    "clobTokenIds": json.dumps(["tok-a", "tok-b", "tok-c"]),
    "conditionId": "0xcond2",
    "category": None,
    "negRisk": True,
}

CLOB_BOOK_UNSORTED = {
    "market": "0xcond1",
    "asset_id": "token-yes-1",
    "bids": [
        {"price": "0.44", "size": "50"},
        {"price": "0.46", "size": "200"},  # unsorted on purpose
        {"price": "0.46", "size": "0"},    # zero-size: dropped
    ],
    "asks": [
        {"price": "0.52", "size": "150"},
        {"price": "0.50", "size": "100"},  # unsorted on purpose
    ],
}


def make_adapter():
    def transport(url: str):
        if "/markets" in url:
            return [GAMMA_BINARY, GAMMA_NEGRISK]
        if "token_id=token-yes-1" in url:
            return CLOB_BOOK_UNSORTED
        raise AssertionError(f"unexpected url: {url}")

    return PolymarketAdapter(transport=transport, clock=lambda: FIXED_NOW)


class TestListMarkets:
    def test_binary_market_mece(self):
        adapter = make_adapter()
        markets = adapter.list_markets()
        binary = next(m for m in markets if m.market_id == "520001")
        assert binary.venue == "polymarket"
        assert binary.event_id == "0xcond1"
        assert len(binary.outcomes) == 2
        assert binary.mutually_exclusive and binary.exhaustive

    def test_negrisk_not_exhaustive_until_verified(self):
        adapter = make_adapter()
        markets = adapter.list_markets()
        neg = next(m for m in markets if m.market_id == "520002")
        assert len(neg.outcomes) == 3
        assert neg.mutually_exclusive
        assert not neg.exhaustive  # fail-closed: scanner must reject

    def test_non_yesno_binary_labels_are_exhaustive(self):
        # N7 widening 2026-10-06: a 2-outcome CTF market is MECE by
        # protocol regardless of labels ("Up/Down", team names)
        raw = dict(GAMMA_BINARY)
        raw["id"] = 520003
        raw["outcomes"] = json.dumps(["Up", "Down"])
        raw["clobTokenIds"] = json.dumps(["tok-u", "tok-d"])
        adapter = PolymarketAdapter(
            transport=lambda url: [raw] if "/markets" in url else None,
            clock=lambda: FIXED_NOW,
        )
        m = adapter.list_markets()[0]
        assert m.mutually_exclusive and m.exhaustive

    def test_negrisk_binary_still_not_exhaustive(self):
        # standing rule unchanged: negRisk (even 2-outcome) stays closed
        raw = dict(GAMMA_BINARY)
        raw["id"] = 520004
        raw["negRisk"] = True
        adapter = PolymarketAdapter(
            transport=lambda url: [raw] if "/markets" in url else None,
            clock=lambda: FIXED_NOW,
        )
        m = adapter.list_markets()[0]
        assert not m.exhaustive


class TestFetchOrderbook:
    def test_normalized_sorted_book(self):
        adapter = make_adapter()
        adapter.list_markets()
        book = adapter.fetch_orderbook("520001", "0")
        # sorted desc, zero-size level dropped
        assert [(lv.price, lv.size) for lv in book.bids] == [
            (Decimal("0.46"), Decimal(200)),
            (Decimal("0.44"), Decimal(50)),
        ]
        assert [lv.price for lv in book.asks] == [Decimal("0.50"), Decimal("0.52")]
        assert book.timestamp == FIXED_NOW

    def test_unknown_market_rejected(self):
        adapter = make_adapter()
        with pytest.raises(ValueError, match="list_markets"):
            adapter.fetch_orderbook("999999", "0")

    def test_unknown_outcome_rejected(self):
        adapter = make_adapter()
        adapter.list_markets()
        with pytest.raises(ValueError, match="not in market"):
            adapter.fetch_orderbook("520001", "7")


class TestFeeBinding:
    def test_category_rate_bound(self):
        adapter = make_adapter()
        adapter.list_markets()
        model = adapter.fee_model_for("520001")  # Politics -> 0.04
        assert model.rate == CATEGORY_TAKER_RATES["politics"]

    def test_unknown_category_gets_max_rate(self):
        adapter = make_adapter()
        adapter.list_markets()
        model = adapter.fee_model_for("520002")  # category None
        assert model.rate == MAX_TAKER_RATE

    def test_default_fee_model_is_conservative(self):
        adapter = make_adapter()
        assert adapter.fee_model().rate == MAX_TAKER_RATE


class TestPerMarketFeeSchedule:
    """Gamma feeSchedule (per-market, official) takes precedence over the
    category table; anything unparseable fails closed to MAX."""

    @staticmethod
    def adapter_with(market_extra: dict):
        raw = dict(GAMMA_BINARY)
        raw.update(market_extra)

        def transport(url: str):
            if "/markets" in url:
                return [raw]
            raise AssertionError(f"unexpected url: {url}")

        adapter = PolymarketAdapter(transport=transport, clock=lambda: FIXED_NOW)
        adapter.list_markets()
        return adapter

    def test_fee_schedule_overrides_category(self):
        # legacy sports-v2 market: category "Sports" maps to 0.05 but the
        # market's own schedule says 0.03 — per-market wins
        adapter = self.adapter_with(
            {
                "category": "Sports",
                "feesEnabled": True,
                "feeSchedule": {
                    "exponent": 1,
                    "rate": 0.03,
                    "takerOnly": True,
                    "rebateRate": 0.15,
                },
            }
        )
        assert adapter.fee_model_for("520001").rate == Decimal("0.03")

    def test_fees_disabled_is_zero_fee(self):
        adapter = self.adapter_with({"feesEnabled": False, "feeSchedule": None})
        model = adapter.fee_model_for("520001")
        assert model.fee(Decimal(100), Decimal("0.5")) == Decimal(0)

    def test_enabled_without_schedule_gets_max(self):
        adapter = self.adapter_with({"feesEnabled": True, "feeSchedule": None})
        assert adapter.fee_model_for("520001").rate == MAX_TAKER_RATE

    def test_unmodeled_exponent_gets_max(self):
        adapter = self.adapter_with(
            {
                "feesEnabled": True,
                "feeSchedule": {"exponent": 2, "rate": 0.04, "takerOnly": True},
            }
        )
        assert adapter.fee_model_for("520001").rate == MAX_TAKER_RATE

    def test_missing_fee_fields_fall_back_to_category(self):
        adapter = make_adapter()
        adapter.list_markets()
        assert adapter.fee_model_for("520001").rate == Decimal("0.04")


class TestCapabilities:
    def test_self_report(self):
        cap = make_adapter().capabilities()
        assert cap.venue == "polymarket"
        assert cap.rest_api and cap.websocket and cap.orderbook_data
