"""N6 risk gate functional tests: policy validation, each check's
trigger, exposure/failure state machine, circuit breaker lifecycle."""
from datetime import timedelta
from decimal import Decimal

import pytest

from event_arbitrage_lab.execution import TakerFillPolicy, execute_pair
from event_arbitrage_lab.fees import ZeroFee
from event_arbitrage_lab.ledger import EvidenceLedger
from event_arbitrage_lab.risk import (
    RiskGate,
    RiskPolicy,
    RiskReason,
)
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

ZERO = ZeroFee()
TAKER = TakerFillPolicy()


def policy(**over) -> RiskPolicy:
    base = dict(
        max_staleness=timedelta(seconds=5),
        min_net_profit=Decimal("0.01"),
        min_net_edge_per_share=Decimal("0.0001"),
        min_capacity=Decimal(1),
        max_exposure_per_asset=Decimal(500),
        max_total_exposure=Decimal(10000),
        max_leg_risk_shares=Decimal(500),
        max_spread=Decimal("0.2"),
        max_consecutive_failures=2,
    )
    base.update(over)
    return RiskPolicy(**base)


def side_a(yes_ask="0.48", ts=NOW):
    return BinaryMarketBooks(
        market=make_market(),
        yes=make_book(VENUE_A, MARKET_A, YES,
                      bids=[("0.45", 100)], asks=[(yes_ask, 100)], ts=ts),
        no=make_book(VENUE_A, MARKET_A, NO,
                     bids=[("0.46", 100)], asks=[("0.52", 100)], ts=ts),
    )


def side_b(no_ask="0.49", ts=NOW):
    return BinaryMarketBooks(
        market=make_market(venue=VENUE_B, market_id=MARKET_B),
        yes=make_book(VENUE_B, MARKET_B, YES,
                      bids=[("0.48", 100)], asks=[("0.51", 100)], ts=ts),
        no=make_book(VENUE_B, MARKET_B, NO,
                     bids=[("0.46", 100)], asks=[(no_ask, 100)], ts=ts),
    )


def scan_one(a=None, b=None, size=Decimal(100)):
    a = a or side_a()
    b = b or side_b()
    params = ScanParams(size=size, now=NOW, max_staleness=timedelta(seconds=60))
    res = scan_pair(make_mapping(), a, b, ZERO, ZERO, params)
    assert len(res.opportunities) == 1
    return res.opportunities[0]


def books_map(a=None, b=None):
    a = a or side_a()
    b = b or side_b()
    return {(x.venue, x.market_id, x.outcome_id): x
            for x in (a.yes, a.no, b.yes, b.no)}


class TestPolicyValidation:
    def test_valid_policy_constructs(self):
        policy()

    @pytest.mark.parametrize("bad", [
        dict(max_staleness=timedelta(0)),
        dict(min_net_profit=Decimal("-1")),
        dict(max_exposure_per_asset=Decimal(0)),
        dict(max_total_exposure=Decimal("-5")),
        dict(max_leg_risk_shares=Decimal(0)),
        dict(max_spread=Decimal("1.5")),
        dict(max_spread=Decimal(0)),
        dict(max_consecutive_failures=0),
        dict(min_net_profit=Decimal("NaN")),
        dict(max_total_exposure=Decimal("Infinity")),
    ])
    def test_invalid_policies_rejected(self, bad):
        with pytest.raises(ValueError):
            policy(**bad)


class TestChecks:
    def test_clean_opportunity_approved(self):
        gate = RiskGate(policy())
        d = gate.evaluate(scan_one(), books_map(), NOW)
        assert d.approved and d.rejections == ()

    def test_stale_book_rejected(self):
        from datetime import timedelta as td
        old = NOW - td(seconds=30)
        a = side_a(ts=old)
        gate = RiskGate(policy())  # gate limit 5s, scanner allowed 60s
        d = gate.evaluate(scan_one(a=a), books_map(a=a), NOW)
        assert not d.approved
        assert RiskReason.STALE_DATA in {r.reason for r in d.rejections}

    def test_future_timestamp_rejected(self):
        future = NOW + timedelta(seconds=30)
        gate = RiskGate(policy())
        # scanner would reject this itself; gate is defense in depth
        d = gate.evaluate(scan_one(), books_map(a=side_a(ts=future)), NOW)
        assert not d.approved
        assert RiskReason.STALE_DATA in {r.reason for r in d.rejections}

    def test_missing_book_is_feed_failure(self):
        gate = RiskGate(policy())
        m = books_map()
        del m[(VENUE_B, MARKET_B, NO)]
        d = gate.evaluate(scan_one(), m, NOW)
        assert not d.approved
        assert RiskReason.DATA_FEED_FAILURE in {r.reason for r in d.rejections}

    def test_edge_threshold(self):
        gate = RiskGate(policy(min_net_profit=Decimal("5")))  # opp nets 3.0
        d = gate.evaluate(scan_one(), books_map(), NOW)
        assert not d.approved
        assert RiskReason.EDGE_BELOW_THRESHOLD in {r.reason for r in d.rejections}

    def test_liquidity_threshold(self):
        gate = RiskGate(policy(min_capacity=Decimal(500)))  # opp has 100
        d = gate.evaluate(scan_one(), books_map(), NOW)
        assert not d.approved
        assert RiskReason.INSUFFICIENT_LIQUIDITY in {r.reason for r in d.rejections}

    def test_leg_risk_threshold(self):
        gate = RiskGate(policy(max_leg_risk_shares=Decimal(50)))  # leg = 100
        d = gate.evaluate(scan_one(), books_map(), NOW)
        assert not d.approved
        assert RiskReason.LEG_RISK_LIMIT in {r.reason for r in d.rejections}

    def test_abnormal_market_one_sided(self):
        bad_yes = make_book(VENUE_A, MARKET_A, YES, bids=[("0.45", 100)], asks=[])
        a = BinaryMarketBooks(market=make_market(), yes=bad_yes, no=side_a().no)
        gate = RiskGate(policy())
        d = gate.evaluate(scan_one(), books_map(a=a), NOW)
        assert not d.approved
        assert RiskReason.ABNORMAL_MARKET in {r.reason for r in d.rejections}

    def test_abnormal_market_wide_spread(self):
        wide_yes = make_book(VENUE_A, MARKET_A, YES,
                             bids=[("0.20", 100)], asks=[("0.48", 100)])
        a = BinaryMarketBooks(market=make_market(), yes=wide_yes, no=side_a().no)
        gate = RiskGate(policy(max_spread=Decimal("0.1")))
        d = gate.evaluate(scan_one(), books_map(a=a), NOW)
        assert not d.approved
        assert RiskReason.ABNORMAL_MARKET in {r.reason for r in d.rejections}

    def test_all_violations_collected_not_just_first(self):
        gate = RiskGate(policy(min_net_profit=Decimal("5"),
                               min_capacity=Decimal(500),
                               max_leg_risk_shares=Decimal(50)))
        d = gate.evaluate(scan_one(), books_map(), NOW)
        reasons = {r.reason for r in d.rejections}
        assert {RiskReason.EDGE_BELOW_THRESHOLD,
                RiskReason.INSUFFICIENT_LIQUIDITY,
                RiskReason.LEG_RISK_LIMIT} <= reasons


class TestExposureState:
    def _degraded_execution(self, gate):
        # leg B fills 60/100, no bids to unwind -> residual 40 held
        opp = scan_one()
        a, b = side_a(), side_b()
        book_x = a.yes
        book_y = b.no
        no_bids_a = make_book(VENUE_A, MARKET_A, YES,
                              bids=[], asks=[("0.48", 100)])
        res = execute_pair(
            book_x, book_y, Decimal(100), ZERO, ZERO,
            TAKER, TakerFillPolicy(depth_ratio=Decimal("0.6")),
            unwind_book=no_bids_a,
        )
        gate.record_execution(opp, res)
        return res

    def test_residual_accumulates_by_identity(self):
        gate = RiskGate(policy())
        self._degraded_execution(gate)
        exp = gate.exposure_of(VENUE_A, MARKET_A, YES)
        assert exp.shares == Decimal(40)
        assert exp.basis == Decimal("19.200000")
        # other identities untouched
        assert gate.exposure_of(VENUE_B, MARKET_B, NO).shares == 0
        assert gate.exposure_of(VENUE_A, MARKET_A, NO).shares == 0

    def test_exposure_blocks_next_oversized_trade(self):
        gate = RiskGate(policy(max_exposure_per_asset=Decimal(100)))
        self._degraded_execution(gate)  # 40 residual now open
        # next trade would add 100 worst-case -> 140 > 100
        d = gate.evaluate(scan_one(), books_map(), NOW)
        assert not d.approved
        assert RiskReason.EXPOSURE_LIMIT_ASSET in {r.reason for r in d.rejections}

    def test_settlement_clears_exposure(self):
        gate = RiskGate(policy(max_exposure_per_asset=Decimal(100)))
        self._degraded_execution(gate)
        gate.record_settlement(VENUE_A, MARKET_A)
        assert gate.exposure_of(VENUE_A, MARKET_A, YES).shares == 0
        assert gate.evaluate(scan_one(), books_map(), NOW).approved

    def test_clean_execution_resets_failure_streak(self):
        gate = RiskGate(policy())
        opp = scan_one()
        good = execute_pair(side_a().yes, side_b().no, Decimal(100),
                            ZERO, ZERO, TAKER, TAKER)
        gate.record_execution(opp, good)
        assert gate.consecutive_failures == 0
        assert not gate.tripped


class TestCircuitBreaker:
    def test_trips_after_policy_failures_and_blocks_all(self):
        gate = RiskGate(policy(max_consecutive_failures=2))
        self_exec = TestExposureState()
        self_exec._degraded_execution(gate)
        assert not gate.tripped
        self_exec._degraded_execution(gate)
        assert gate.tripped
        d = gate.evaluate(scan_one(), books_map(), NOW)
        assert not d.approved
        assert d.rejections[0].reason is RiskReason.CIRCUIT_BREAKER_TRIPPED
        assert len(d.rejections) == 1  # tripped: nothing else evaluated

    def test_manual_reset_with_operator(self):
        gate = RiskGate(policy(max_consecutive_failures=1))
        TestExposureState()._degraded_execution(gate)
        assert gate.tripped
        gate.reset_breaker(operator="alex")
        assert not gate.tripped
        assert gate.evaluate(scan_one(), books_map(), NOW).approved

    def test_reset_requires_operator(self):
        gate = RiskGate(policy())
        with pytest.raises(ValueError):
            gate.reset_breaker(operator="")

    def test_reset_does_not_clear_exposure(self):
        gate = RiskGate(policy(max_consecutive_failures=1))
        TestExposureState()._degraded_execution(gate)
        gate.reset_breaker(operator="alex")
        assert gate.exposure_of(VENUE_A, MARKET_A, YES).shares == Decimal(40)
