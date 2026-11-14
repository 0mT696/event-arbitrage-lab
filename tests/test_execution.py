"""N4 paper execution tests: fillability, leg risk, unwind accounting,
state machine legality, determinism, scanner-to-executor end-to-end."""
from datetime import timedelta
from decimal import Decimal

import pytest

from event_arbitrage_lab.execution import (
    IllegalTransition,
    LegStatus,
    PairState,
    PairStateMachine,
    TakerFillPolicy,
    execute_pair,
)
from event_arbitrage_lab.fees import QuadraticTakerFee, SlippageBuffer, ZeroFee
from event_arbitrage_lab.scanner import BinaryMarketBooks, ScanParams, scan_pair

from conftest import (
    MARKET_A,
    MARKET_B,
    NO,
    NOW,
    VENUE_A,
    VENUE_B,
    YES,
    make_book,
    make_mapping,
    make_market,
)


def book_a(bids=(("0.45", 100),), asks=(("0.48", 100),)):
    return make_book(VENUE_A, MARKET_A, YES, bids=list(bids), asks=list(asks))


def book_b(asks=(("0.49", 100),)):
    return make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 100)], asks=list(asks))


TAKER = TakerFillPolicy()
ZERO = ZeroFee()


class TestHappyPath:
    def test_full_pair_hedged(self):
        res = execute_pair(book_a(), book_b(), Decimal(100), ZERO, ZERO, TAKER, TAKER)
        assert res.final_state is PairState.CLOSED
        assert res.paired_shares == Decimal(100)
        assert res.residual_shares == 0
        assert res.leg_a.status is LegStatus.FILLED
        assert res.leg_b.status is LegStatus.FILLED
        assert res.paired_expected_pnl == Decimal("3.000000")
        assert res.net_pnl == Decimal("3.000000")
        assert res.unwind is None
        assert any(e.startswith("hedged") for e in res.events)


class TestFailures:
    def test_leg_a_rejected_aborts(self):
        res = execute_pair(
            book_a(), book_b(), Decimal(100), ZERO, ZERO,
            TakerFillPolicy(reject=True), TAKER,
        )
        assert res.final_state is PairState.ABORTED
        assert res.leg_b.status is LegStatus.NOT_ATTEMPTED
        assert res.net_pnl == 0

    def test_book_moved_beyond_limit_kills_trade(self):
        """The core N4 question: math arb (0.48+0.53=1.01 -> none) — but even if
        the scanner quoted 0.49, latency moves the book to 0.53; the limit
        price (0.52) refuses the chase. Residual unwound at a loss."""
        res = execute_pair(
            book_a(), book_b(asks=(("0.53", 100),)), Decimal(100), ZERO, ZERO, TAKER, TAKER
        )
        assert res.leg_b.shares == 0
        assert res.paired_shares == 0
        # unwind 100 @0.45 vs basis 0.48 -> -3.00 realized
        assert res.unwind_realized_pnl == Decimal("-3.000000")
        assert res.net_pnl == Decimal("-3.000000")
        assert res.residual_shares == 0


class TestPartialFills:
    def test_leg_b_partial_unwinds_residual(self):
        res = execute_pair(
            book_a(), book_b(), Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
        )
        assert res.leg_b.status is LegStatus.PARTIAL
        assert res.paired_shares == Decimal(60)
        # paired: 60*(1-0.48-0.49) = 1.80 ; unwind 40: 40*(0.45-0.48) = -1.20
        assert res.paired_expected_pnl == Decimal("1.800000")
        assert res.unwind_realized_pnl == Decimal("-1.200000")
        assert res.net_pnl == Decimal("0.600000")

    def test_limit_price_selects_levels(self):
        res = execute_pair(
            book_a(), book_b(asks=(("0.50", 60), ("0.55", 40))), Decimal(100),
            ZERO, ZERO, TAKER, TAKER,
        )
        # limit_b = 1 - 0.48 = 0.52 -> only the 0.50 level is taken
        assert res.leg_b.shares == Decimal(60)
        assert res.paired_shares == Decimal(60)
        # paired: 60 - 28.8 - 30 = 1.2 ; unwind 40: -1.2 -> net 0
        assert res.net_pnl == Decimal("0.000000")

    def test_unwind_depth_insufficient_leaves_residual(self):
        res = execute_pair(
            book_a(bids=(("0.45", 20),)), book_b(), Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
        )
        assert res.unwind is not None and res.unwind.shares == Decimal(20)
        assert res.residual_shares == Decimal(20)  # leg risk still on the books
        # unwind pnl only on the 20 sold: 20*(0.45-0.48) = -0.6
        assert res.unwind_realized_pnl == Decimal("-0.600000")


class TestCosts:
    def test_fees_reduce_net(self):
        res = execute_pair(
            book_a(), book_b(), Decimal(100), ZERO, QuadraticTakerFee(Decimal("0.07")),
            TAKER, TAKER,
        )
        assert res.total_fees == Decimal("1.7493")  # 0.07*100*0.49*0.51, 5dp ceil
        assert res.net_pnl == Decimal("1.2507")

    def test_slippage_buffer_subtracted(self):
        res = execute_pair(
            book_a(), book_b(), Decimal(100), ZERO, ZERO, TAKER, TAKER,
            slippage=SlippageBuffer(per_unit=Decimal("0.01")),
        )
        assert res.net_pnl == Decimal("2.000000")


class TestStateMachine:
    def test_illegal_transitions_raise(self):
        sm = PairStateMachine()
        with pytest.raises(IllegalTransition):
            sm.advance(PairState.LEG_B_WORKING)
        with pytest.raises(IllegalTransition):
            sm.advance(PairState.CLOSED)
        sm.advance(PairState.LEG_A_WORKING)
        sm.advance(PairState.ABORTED)
        with pytest.raises(IllegalTransition):
            sm.advance(PairState.QUOTED)  # terminal

    def test_hedged_then_closed_only(self):
        sm = PairStateMachine()
        sm.advance(PairState.LEG_A_WORKING)
        sm.advance(PairState.LEG_B_WORKING)
        sm.advance(PairState.HEDGED)
        with pytest.raises(IllegalTransition):
            sm.advance(PairState.UNWINDING)
        sm.advance(PairState.CLOSED)


class TestDeterminism:
    def test_same_inputs_same_result(self):
        r1 = execute_pair(book_a(), book_b(), Decimal(100), ZERO, ZERO, TAKER, TAKER)
        r2 = execute_pair(book_a(), book_b(), Decimal(100), ZERO, ZERO, TAKER, TAKER)
        assert r1 == r2


class TestEndToEnd:
    def test_scanner_opportunity_matches_execution(self):
        a = BinaryMarketBooks(
            market=make_market(),
            yes=book_a(),
            no=make_book(VENUE_A, MARKET_A, NO, bids=[("0.49", 100)], asks=[("0.52", 100)]),
        )
        b = BinaryMarketBooks(
            market=make_market(venue=VENUE_B, market_id=MARKET_B),
            yes=make_book(VENUE_B, MARKET_B, YES, bids=[("0.48", 100)], asks=[("0.51", 100)]),
            no=book_b(),
        )
        params = ScanParams(size=Decimal(100), now=NOW, max_staleness=timedelta(seconds=5))
        scan = scan_pair(make_mapping(), a, b, ZERO, ZERO, params)
        assert len(scan.opportunities) == 1
        opp = scan.opportunities[0]

        res = execute_pair(book_a(), book_b(), Decimal(100), ZERO, ZERO, TAKER, TAKER)
        # scanner edge == executed pnl when books don't move
        assert res.net_pnl == opp.net_profit == Decimal("3.000000")
