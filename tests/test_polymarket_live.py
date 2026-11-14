"""Optional live smoke test against Polymarket's public read endpoints.

Disabled by default; run explicitly with:
    EAL_LIVE_SMOKE=1 python3 -m pytest tests/test_polymarket_live.py -q

Evidence-gathering only; never part of the offline gate.
"""
import os

import pytest

from event_arbitrage_lab.adapters.polymarket import PolymarketAdapter

pytestmark = pytest.mark.skipif(
    not os.environ.get("EAL_LIVE_SMOKE"), reason="live smoke test disabled"
)


def test_live_fetch_first_active_market_book():
    adapter = PolymarketAdapter()
    markets = adapter.list_markets(limit=5)
    assert markets, "no active markets returned"
    market = markets[0]
    book = adapter.fetch_orderbook(market.market_id, market.outcomes[0].outcome_id)
    assert book.venue == "polymarket"
    assert book.timestamp.tzinfo is not None
    for lv in (*book.bids, *book.asks):
        assert 0 < lv.price < 1 and lv.size > 0
    print(
        f"\nlive ok: {market.market_id} '{market.outcomes[0].label}' "
        f"bids={len(book.bids)} asks={len(book.asks)}"
    )
