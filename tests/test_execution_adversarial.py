"""N4 adversarial tests: attack the paper executor where latency, fees,
partial fills and unwind interact. Every test asserts either honest
accounting or fail-closed behavior — never silent loss."""
from decimal import Decimal

from event_arbitrage_lab.execution import (
    LegStatus,
    PairState,
    TakerFillPolicy,
    execute_pair,
)
from event_arbitrage_lab.fees import QuadraticTakerFee, SlippageBuffer, ZeroFee

from conftest import MARKET_A, MARKET_B, NO, VENUE_A, VENUE_B, YES, make_book

TAKER = TakerFillPolicy()
ZERO = ZeroFee()
FEE7 = QuadraticTakerFee(Decimal("0.07"))


def book_a(bids=(("0.45", 100),), asks=(("0.48", 100),)):
    return make_book(VENUE_A, MARKET_A, YES, bids=list(bids), asks=list(asks))


def book_b(asks=(("0.49", 100),)):
    return make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 100)], asks=list(asks))


class TestFeeAwareLimit:
    """The limit must protect NET profitability (gate 5 at exec time).
    Gross-of-fees limits let latency drift push us into net losers."""

    def test_gross_breakeven_net_loser_rejected(self):
        # book_b ask 0.505: gross edge = 1 - 0.48 - 0.505 = +0.005/share,
        # but taker fee ~0.0175/share makes the pair a net loser.
        # Old (gross) limit 0.52 would have taken it: net -0.2498/100sh.
        res = execute_pair(
            book_a(), book_b(asks=(("0.505", 100),)), Decimal(100),
            ZERO, FEE7, TAKER, TAKER,
        )
        assert res.leg_b.shares == 0
        assert res.paired_shares == 0
        # refused the chase; only the unwind loss is realized
        assert res.unwind_realized_pnl == Decimal("-3.000000")
        assert res.net_pnl == Decimal("-3.000000")

    def test_both_legs_fee_turns_pair_negative(self):
        # Both venues charge 0.07 quadratic: 0.48+0.49 gross edge +0.03/sh,
        # fees ~0.035/sh -> net negative. Engine must refuse leg B.
        res = execute_pair(
            book_a(), book_b(), Decimal(100), FEE7, FEE7, TAKER, TAKER
        )
        assert res.leg_b.shares == 0
        assert res.paired_shares == 0
        assert res.final_state is PairState.CLOSED  # via unwind

    def test_net_profitable_pair_still_fills(self):
        # control: honest edge survives the tighter limit
        res = execute_pair(book_a(), book_b(), Decimal(100), ZERO, FEE7, TAKER, TAKER)
        assert res.leg_b.status is LegStatus.FILLED
        assert res.net_pnl == Decimal("1.2507")


class TestLegRisk:
    def test_leg_b_total_rejection_unwinds_everything(self):
        res = execute_pair(
            book_a(), book_b(), Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(reject=True),
        )
        assert res.leg_b.status is LegStatus.REJECTED
        assert res.residual_shares == 0  # fully unwound
        assert res.unwind is not None and res.unwind.shares == Decimal(100)
        assert res.unwind_realized_pnl == Decimal("-3.000000")
        kinds = [e.split(":")[0] for e in res.events]
        assert kinds == ["leg_a", "leg_b", "residual_open", "unwind", "closed"]

    def test_unwind_zero_depth_reports_basis_honestly(self):
        # leg B fills only 60 -> residual 40; bids side EMPTY -> nothing
        # unwindable. net_pnl must NOT include the locked residual capital.
        res = execute_pair(
            book_a(bids=()), book_b(), Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
        )
        assert res.unwind is not None and res.unwind.status is LegStatus.REJECTED
        assert res.residual_shares == Decimal(40)
        assert res.residual_cost_basis == Decimal("19.200000")  # 40 x 0.48
        assert res.net_pnl == Decimal("1.800000")  # paired part only
        # the exposure is visible, not hidden inside a fake "profit"
        assert res.residual_cost_basis > res.net_pnl

    def test_unwind_partial_fills_split_basis_correctly(self):
        # bids only take 20 of the 40 residual; basis charged only on sold
        res = execute_pair(
            book_a(bids=(("0.45", 20),)), book_b(), Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
        )
        assert res.unwind.shares == Decimal(20)
        assert res.unwind_realized_pnl == Decimal("-0.600000")  # 20 x (0.45-0.48)
        assert res.residual_shares == Decimal(20)
        assert res.residual_cost_basis == Decimal("9.600000")  # 20 x 0.48


class TestDepthModeling:
    def test_depth_ratio_never_rounds_up(self):
        # 7 x 1e-7 = 7e-7 -> FLOOR to 0 (level unavailable).
        # HALF_EVEN would round up to 1e-6, inventing depth that a real
        # venue does not have.
        tiny = make_book(VENUE_A, MARKET_A, YES, asks=[("0.48", Decimal(7))])
        res = execute_pair(
            tiny, book_b(), Decimal("0.000001"), ZERO, ZERO,
            TakerFillPolicy(depth_ratio=Decimal("0.0000001")), TAKER,
        )
        assert res.final_state is PairState.ABORTED
        assert res.leg_a.shares == 0

    def test_floored_to_zero_levels_dropped_not_crash(self):
        # sub-quantum scaled depth must be skipped, never passed to the
        # >0-validated BookLevel
        tiny = make_book(
            VENUE_A, MARKET_A, YES, asks=[("0.48", Decimal("0.0000015"))]
        )
        res = execute_pair(
            tiny, book_b(), Decimal(1), ZERO, ZERO,
            TakerFillPolicy(depth_ratio=Decimal("0.5")), TAKER,
        )
        assert res.leg_a.shares == 0
        assert res.final_state is PairState.ABORTED


class TestLimitEdgeCases:
    def test_nonpositive_limit_blocks_leg_b(self):
        # vwap_a 0.98 -> limit_gross 0.02; every realistic ask is above it
        res = execute_pair(
            book_a(asks=(("0.98", 100),)), book_b(), Decimal(100),
            ZERO, ZERO, TAKER, TAKER,
        )
        assert res.leg_b.shares == 0
        assert res.paired_shares == 0

    def test_slippage_tightens_limit(self):
        # without slippage the limit is 0.52; ask 0.51 would fill.
        # slippage 0.02 -> limit 0.50 -> 0.51 refused.
        res = execute_pair(
            book_a(), book_b(asks=(("0.51", 100),)), Decimal(100), ZERO, ZERO,
            TAKER, TAKER, slippage=SlippageBuffer(per_unit=Decimal("0.02")),
        )
        assert res.leg_b.shares == 0
        assert res.paired_shares == 0


class TestUnwindIdentity:
    """Verifier N4-BLOCKER-001: unwind_book must be the SAME asset as the
    residual. A foreign book fabricates PnL — fail closed, no unwind,
    no realized PnL, residual stays open with its cost basis visible."""

    def _residual_setup(self, unwind_book):
        # leg B fills 60/100 -> residual 40 YES shares on venue_a/mkt-a-1
        return execute_pair(
            book_a(), book_b(), Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
            unwind_book=unwind_book,
        )

    def test_wrong_venue_blocked(self):
        foreign = make_book(VENUE_B, MARKET_A, YES, bids=[("0.99", 100)], asks=[])
        res = self._residual_setup(foreign)
        assert res.unwind is None
        assert res.unwind_realized_pnl == 0
        assert res.residual_shares == Decimal(40)
        assert res.residual_cost_basis == Decimal("19.200000")
        assert res.net_pnl == res.paired_expected_pnl  # no fabricated PnL
        assert any("unwind_blocked:identity_mismatch" in e for e in res.events)

    def test_wrong_market_blocked(self):
        foreign = make_book(VENUE_A, MARKET_B, YES, bids=[("0.99", 100)], asks=[])
        res = self._residual_setup(foreign)
        assert res.unwind is None
        assert res.unwind_realized_pnl == 0
        assert res.residual_shares == Decimal(40)

    def test_wrong_outcome_blocked(self):
        foreign = make_book(VENUE_A, MARKET_A, NO, bids=[("0.99", 100)], asks=[])
        res = self._residual_setup(foreign)
        assert res.unwind is None
        assert res.unwind_realized_pnl == 0
        assert res.residual_shares == Decimal(40)

    def test_valid_identity_unwinds_normally(self):
        # same asset, deeper bids than book_a: unwind proceeds at 0.47
        legit = make_book(VENUE_A, MARKET_A, YES, bids=[("0.47", 100)], asks=[])
        res = self._residual_setup(legit)
        assert res.unwind is not None and res.unwind.shares == Decimal(40)
        assert res.unwind_realized_pnl == Decimal("-0.400000")  # 40 x (0.47-0.48)
        assert res.residual_shares == 0

    def test_verifier_attack_reproduced_and_blocked(self):
        # The verifier's exact attack: venue_b/mkt-b-1/NO bids at 0.99 used
        # to "unwind" venue_a/mkt-a-1/YES residual -> fake +20.4 unwind PnL.
        attack_book = make_book(VENUE_B, MARKET_B, NO, bids=[("0.99", 100)], asks=[])
        res = self._residual_setup(attack_book)
        assert res.unwind is None
        assert res.unwind_realized_pnl == 0  # NOT +20.4
        assert res.net_pnl == Decimal("1.800000")  # NOT +22.2
        assert res.residual_shares == Decimal(40)


class TestDeterminism:
    def test_residual_scenario_deterministic(self):
        def run():
            return execute_pair(
                book_a(bids=(("0.45", 20),)), book_b(), Decimal(100),
                ZERO, FEE7, TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
            )

        r1, r2 = run(), run()
        assert r1 == r2  # full dataclass equality incl. events & basis
