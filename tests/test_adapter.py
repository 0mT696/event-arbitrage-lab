"""N1D adapter-contract tests: canonical boundary, tri-state capabilities."""
from decimal import Decimal

from event_arbitrage_lab.adapter import MarketDataAdapter, VenueCapability
from event_arbitrage_lab.fees import QuadraticTakerFee
from event_arbitrage_lab.schemas import OrderBook

from conftest import MARKET_A, NOW, VENUE_A, YES, make_book, make_market


class FakeAdapter:
    """Minimal conforming adapter: returns canonical types only."""

    def capabilities(self) -> VenueCapability:
        return VenueCapability(venue=VENUE_A, rest_api=True, orderbook_data=True)

    def list_markets(self):
        return [make_market()]

    def fetch_orderbook(self, market_id: str, outcome_id: str) -> OrderBook:
        return make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 100)])

    def fee_model(self) -> QuadraticTakerFee:
        return QuadraticTakerFee(rate=Decimal("0.07"))


class TestVenueCapability:
    def test_unknown_defaults_to_none_not_yes(self):
        cap = VenueCapability(venue="candidate-x")
        assert cap.rest_api is None
        assert cap.websocket is None
        assert cap.orderbook_data is None
        assert cap.historical_data is None

    def test_partial_research_fill(self):
        cap = VenueCapability(venue="candidate-x", rest_api=True, fee_structure="2% on winnings (src)")
        assert cap.rest_api is True
        assert cap.websocket is None  # still a research gap


class TestAdapterContract:
    def test_fake_adapter_satisfies_protocol(self):
        adapter: MarketDataAdapter = FakeAdapter()
        assert isinstance(adapter.capabilities(), VenueCapability)
        market = adapter.list_markets()[0]
        assert market.venue == VENUE_A
        book = adapter.fetch_orderbook(MARKET_A, YES)
        assert isinstance(book, OrderBook)
        assert book.timestamp.tzinfo is not None
        assert adapter.fee_model().fee(Decimal(100), Decimal("0.5")) == Decimal("1.75")

    def test_adapter_output_feeds_core_unchanged(self):
        """Canonical adapter output is directly consumable by N2 math."""
        from event_arbitrage_lab.fees import ZeroFee
        from event_arbitrage_lab.schemas import StrategyKind
        from event_arbitrage_lab.strategies import two_leg_arbitrage

        adapter = FakeAdapter()
        from conftest import NO
        yes = adapter.fetch_orderbook(MARKET_A, YES)
        no = make_book(VENUE_A, MARKET_A, NO, asks=[("0.49", 100)])
        opp = two_leg_arbitrage(
            yes, no, ZeroFee(), ZeroFee(), Decimal(100),
            strategy=StrategyKind.SINGLE_VENUE_COMPLETENESS,
        )
        assert opp is not None
        assert opp.net_profit == Decimal("3.000000")
        assert NOW is not None
