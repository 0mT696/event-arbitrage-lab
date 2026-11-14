"""N2 strategy tests: constructed-arb exactness, gate 3 (unfillable leg kills),
gate 5 (fees + slippage inside net edge)."""
from decimal import Decimal

import pytest

from event_arbitrage_lab.fees import FlatBpsFee, QuadraticTakerFee, SlippageBuffer, ZeroFee
from event_arbitrage_lab.schemas import StrategyKind
from event_arbitrage_lab.strategies import completeness, two_leg_arbitrage

from conftest import VENUE_B, MARKET_A, MARKET_B, NO, VENUE_A, YES, make_book

XB = StrategyKind.CROSS_VENUE_BINARY


class TestTwoLegArbitrage:
    def test_arb_detected_exact_numbers(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.49", 100)])
        opp = two_leg_arbitrage(yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100), strategy=XB)
        assert opp is not None
        assert opp.gross_cost == Decimal("97.00")
        assert opp.slippage_buffer == 0
        assert opp.guaranteed_payout == Decimal("100.000000")
        assert opp.net_profit == Decimal("3.000000")
        assert opp.edge_per_unit == Decimal("0.030000")
        assert len(opp.assumptions) > 0  # gate 5: assumptions ship with the edge

    def test_no_arb_returns_none(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.52", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.51", 100)])
        assert two_leg_arbitrage(yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100), strategy=XB) is None

    def test_fees_can_kill_edge(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.495", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.49", 100)])
        # gross edge 1.5; Quadratic taker fee on NO leg alone is 1.75
        assert (
            two_leg_arbitrage(yes_a, no_b, ZeroFee(), QuadraticTakerFee(), Decimal(100), strategy=XB)
            is None
        )

    def test_venue_b_fee_exact_in_opportunity(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.40", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.50", 100)])
        opp = two_leg_arbitrage(yes_a, no_b, ZeroFee(), QuadraticTakerFee(), Decimal(100), strategy=XB)
        assert opp is not None
        assert opp.total_fees == Decimal("1.75")  # ceil(0.07*100*0.5*0.5)
        assert opp.net_profit == Decimal("100") - Decimal("90.00") - Decimal("1.75")

    def test_slippage_buffer_inside_net_edge(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.49", 100)])
        slip = SlippageBuffer(per_unit=Decimal("0.02"))
        opp = two_leg_arbitrage(
            yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100), strategy=XB, slippage=slip
        )
        assert opp is not None
        assert opp.slippage_buffer == Decimal("2.00")
        assert opp.net_profit == Decimal("1.000000")

    def test_slippage_can_kill_edge(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.49", 100)])
        slip = SlippageBuffer(per_unit=Decimal("0.04"))  # 4.00 total > 3.00 gross edge
        assert (
            two_leg_arbitrage(
                yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100), strategy=XB, slippage=slip
            )
            is None
        )

    def test_gate3_unfillable_leg_matches_down(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.40", 60)])  # depth only 60
        opp = two_leg_arbitrage(yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100), strategy=XB)
        assert opp is not None
        assert opp.shares == Decimal(60)  # matched down, no unhedged residual
        assert all(l.shares == Decimal(60) for l in opp.legs)
        assert opp.gross_cost == Decimal("28.80") + Decimal("24.00")

    def test_gate3_empty_leg_kills_opportunity(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES)
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.49", 100)])
        assert two_leg_arbitrage(yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100), strategy=XB) is None

    def test_same_outcome_same_market_rejected(self):
        yes = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 10)])
        with pytest.raises(ValueError, match="complementary"):
            two_leg_arbitrage(yes, yes, ZeroFee(), ZeroFee(), Decimal(10), strategy=XB)

    def test_min_profit_threshold(self):
        yes_a = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", 100)])
        no_b = make_book(VENUE_B, MARKET_B, NO, asks=[("0.49", 100)])
        assert (
            two_leg_arbitrage(
                yes_a, no_b, ZeroFee(), ZeroFee(), Decimal(100),
                strategy=XB, min_profit=Decimal(5),
            )
            is None
        )


class TestCompleteness:
    def test_binary_completeness_detected(self):
        yes = make_book(VENUE_A, MARKET_A, YES, asks=[("0.46", 50)])
        no = make_book(VENUE_A, MARKET_A, NO, asks=[("0.50", 50)])
        opp = completeness((yes, no), (ZeroFee(), ZeroFee()), Decimal(50))
        assert opp is not None
        assert opp.strategy is StrategyKind.SINGLE_VENUE_COMPLETENESS
        assert opp.net_profit == Decimal("2.000000")

    def test_three_outcome_completeness(self):
        o1 = make_book(VENUE_A, MARKET_A, YES, asks=[("0.30", 10)])
        o2 = make_book(VENUE_A, MARKET_A, NO, asks=[("0.30", 10)])
        o3 = make_book(VENUE_A, MARKET_A, "maybe", asks=[("0.35", 10)])
        opp = completeness((o1, o2, o3), (ZeroFee(), ZeroFee(), ZeroFee()), Decimal(10))
        assert opp is not None
        assert opp.net_profit == Decimal("0.500000")

    def test_complete_market_no_arb(self):
        yes = make_book(VENUE_A, MARKET_A, YES, asks=[("0.52", 50)])
        no = make_book(VENUE_A, MARKET_A, NO, asks=[("0.50", 50)])
        assert completeness((yes, no), (ZeroFee(), ZeroFee()), Decimal(50)) is None

    def test_fees_inside_completeness_edge(self):
        yes = make_book(VENUE_A, MARKET_A, YES, asks=[("0.46", 50)])
        no = make_book(VENUE_A, MARKET_A, NO, asks=[("0.50", 50)])
        fee = FlatBpsFee(100)  # 1% of notional: 0.23 + 0.25 = 0.48 -> still < 2.00 edge
        opp = completeness((yes, no), (fee, fee), Decimal(50))
        assert opp is not None
        assert opp.total_fees == Decimal("0.48")
        assert opp.net_profit == Decimal("2.000000") - Decimal("0.48")

    def test_mismatched_venues_rejected(self):
        yes = make_book(VENUE_A, MARKET_A, YES, asks=[("0.46", 50)])
        no = make_book(VENUE_B, MARKET_A, NO, asks=[("0.50", 50)])
        with pytest.raises(ValueError, match="share venue and market"):
            completeness((yes, no), (ZeroFee(), ZeroFee()), Decimal(50))

    def test_fee_models_length_mismatch_rejected(self):
        yes = make_book(VENUE_A, MARKET_A, YES, asks=[("0.46", 50)])
        no = make_book(VENUE_A, MARKET_A, NO, asks=[("0.50", 50)])
        with pytest.raises(ValueError):
            completeness((yes, no), (ZeroFee(),), Decimal(50))
