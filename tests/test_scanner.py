"""N3 scanner tests: end-to-end detection + fail-closed gates
(mapping, market mismatch, staleness, minimum depth)."""
from datetime import timedelta
from decimal import Decimal

from event_arbitrage_lab.fees import QuadraticTakerFee, ZeroFee
from event_arbitrage_lab.scanner import BinaryMarketBooks, ScanParams, scan_pair

from conftest import (
    VENUE_B,
    MARKET_B,
    NO,
    NOW,
    VENUE_A,
    MARKET_A,
    YES,
    make_book,
    make_mapping,
    make_market,
)

PARAMS = ScanParams(size=Decimal(100), now=NOW, max_staleness=timedelta(seconds=5))


def side_a():
    return BinaryMarketBooks(
        market=make_market(),
        yes=make_book(VENUE_A, MARKET_A, YES, bids=[("0.45", 100)], asks=[("0.48", 100)]),
        no=make_book(VENUE_A, MARKET_A, NO, bids=[("0.49", 100)], asks=[("0.52", 100)]),
    )


def side_b():
    return BinaryMarketBooks(
        market=make_market(venue=VENUE_B, market_id=MARKET_B),
        yes=make_book(VENUE_B, MARKET_B, YES, bids=[("0.48", 100)], asks=[("0.51", 100)]),
        no=make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 100)], asks=[("0.49", 100)]),
    )


class TestScanPair:
    def test_cross_venue_arb_found(self):
        # A.YES 0.48 + B.NO 0.49 = 0.97 -> 0.03/unit gross edge
        res = scan_pair(make_mapping(), side_a(), side_b(), ZeroFee(), ZeroFee(), PARAMS)
        assert res.rejections == ()
        assert len(res.opportunities) == 1
        assert res.opportunities[0].net_profit == Decimal("3.000000")

    def test_unverified_mapping_fail_closed(self):
        res = scan_pair(
            make_mapping(verified=False), side_a(), side_b(), ZeroFee(), ZeroFee(), PARAMS
        )
        assert res.opportunities == ()
        assert res.rejections[0].reason == "MAPPING_NOT_USABLE"

    def test_nonequivalent_resolution_fail_closed(self):
        res = scan_pair(
            make_mapping(equivalent=False), side_a(), side_b(), ZeroFee(), ZeroFee(), PARAMS
        )
        assert res.opportunities == ()
        assert res.rejections[0].reason == "MAPPING_NOT_USABLE"

    def test_market_mismatch_fail_closed(self):
        wrong = BinaryMarketBooks(
            market=make_market(venue=VENUE_B, market_id=MARKET_A),  # not mapping.market_b
            yes=make_book(VENUE_B, MARKET_A, YES, bids=[("0.48", 10)], asks=[("0.51", 10)]),
            no=make_book(VENUE_B, MARKET_A, NO, bids=[("0.46", 10)], asks=[("0.49", 10)]),
        )
        res = scan_pair(make_mapping(), side_a(), wrong, ZeroFee(), ZeroFee(), PARAMS)
        assert res.opportunities == ()
        assert res.rejections[0].reason == "MARKET_MISMATCH"

    def test_stale_book_rejected(self):
        a = side_a()
        b = BinaryMarketBooks(
            market=make_market(venue=VENUE_B, market_id=MARKET_B),
            yes=make_book(
                VENUE_B, MARKET_B, YES,
                bids=[("0.48", 100)], asks=[("0.51", 100)],
                ts=NOW - timedelta(seconds=30),  # older than max_staleness
            ),
            no=make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 100)], asks=[("0.49", 100)]),
        )
        res = scan_pair(make_mapping(), a, b, ZeroFee(), ZeroFee(), PARAMS)
        assert res.opportunities == ()
        assert any(r.reason == "STALE_BOOK" for r in res.rejections)

    def test_future_timestamp_rejected(self):
        a = BinaryMarketBooks(
            market=make_market(),
            yes=make_book(
                VENUE_A, MARKET_A, YES,
                bids=[("0.45", 100)], asks=[("0.48", 100)],
                ts=NOW + timedelta(seconds=30),  # clock skew / bad feed
            ),
            no=make_book(VENUE_A, MARKET_A, NO, bids=[("0.49", 100)], asks=[("0.52", 100)]),
        )
        res = scan_pair(make_mapping(), a, side_b(), ZeroFee(), ZeroFee(), PARAMS)
        assert res.opportunities == ()
        assert any(r.reason == "STALE_BOOK" for r in res.rejections)

    def test_min_depth_filter(self):
        a = side_a()
        b = BinaryMarketBooks(
            market=make_market(venue=VENUE_B, market_id=MARKET_B),
            yes=make_book(VENUE_B, MARKET_B, YES, bids=[("0.48", 100)], asks=[("0.51", 100)]),
            no=make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 60)], asks=[("0.49", 60)]),
        )
        params = ScanParams(
            size=Decimal(100), now=NOW,
            max_staleness=timedelta(seconds=5), min_size=Decimal(100),
        )
        res = scan_pair(make_mapping(), a, b, ZeroFee(), ZeroFee(), params)
        assert res.opportunities == ()
        assert any(r.reason == "INSUFFICIENT_DEPTH" for r in res.rejections)

    def test_fees_applied_end_to_end(self):
        res = scan_pair(make_mapping(), side_a(), side_b(), ZeroFee(), QuadraticTakerFee(), PARAMS)
        assert len(res.opportunities) == 1
        opp = res.opportunities[0]
        assert opp.total_fees == Decimal("1.7493")  # 0.07*100*0.49*0.51, 5dp ceil
        assert opp.net_profit == Decimal("1.2507")

    def test_opportunities_sorted_by_profit(self):
        a = side_a()
        b = BinaryMarketBooks(
            market=make_market(venue=VENUE_B, market_id=MARKET_B),
            yes=make_book(VENUE_B, MARKET_B, YES, bids=[("0.44", 100)], asks=[("0.47", 100)]),
            no=make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 100)], asks=[("0.49", 100)]),
        )
        res = scan_pair(make_mapping(), a, b, ZeroFee(), ZeroFee(), PARAMS)
        assert len(res.opportunities) >= 2
        profits = [o.net_profit for o in res.opportunities]
        assert profits == sorted(profits, reverse=True)
