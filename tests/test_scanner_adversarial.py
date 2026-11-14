"""Adversarial E2E regression tests — Verifier-mandated (P0 Rule Gate).

Every case constructs an input that MUST yield zero opportunities.
The gate under test: scan_pair must run full pair eligibility
(validate_pair_rules) before any Opportunity can be produced.

These tests exist because unit-tested gates mean nothing if the
production path bypasses them. They attack the production path.
"""
from datetime import timedelta
from decimal import Decimal

from event_arbitrage_lab.fees import ZeroFee
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

PARAMS = ScanParams(size=Decimal(100), now=NOW, max_staleness=timedelta(seconds=5))
ZERO = ZeroFee()


def aggressive_books(venue, market_id, market):
    """Books priced at a screaming 'arb' (0.40/0.40) — exactly the bait a
    broken scanner would eat. Bids at 0.35 keep the book uncrossed."""
    return BinaryMarketBooks(
        market=market,
        yes=make_book(venue, market_id, YES, bids=[("0.35", 100)], asks=[("0.40", 100)]),
        no=make_book(venue, market_id, NO, bids=[("0.35", 100)], asks=[("0.40", 100)]),
    )


def scan(market_a, market_b, mapping=None):
    mapping = mapping or make_mapping()
    return scan_pair(
        mapping,
        aggressive_books(VENUE_A, MARKET_A, market_a),
        aggressive_books(VENUE_B, MARKET_B, market_b),
        ZERO,
        ZERO,
        PARAMS,
    )


class TestAdversarialRuleGate:
    def test_positive_control_valid_pair_scans(self):
        """Gate must not over-block: fully eligible pair still scans."""
        res = scan(make_market(), make_market(venue=VENUE_B, market_id=MARKET_B))
        assert len(res.opportunities) == 4

    def test_verifier_attack_not_exhaustive(self):
        """The exact P0 input: ME but NOT exhaustive -> 0 opportunities."""
        res = scan(
            make_market(exhaustive=False),
            make_market(venue=VENUE_B, market_id=MARKET_B),
        )
        assert res.opportunities == ()
        assert any(r.reason == "RULE_GATE" for r in res.rejections)

    def test_not_mutually_exclusive(self):
        res = scan(
            make_market(mutually_exclusive=False),
            make_market(venue=VENUE_B, market_id=MARKET_B),
        )
        assert res.opportunities == ()
        assert any(r.reason == "RULE_GATE" for r in res.rejections)

    def test_event_mismatch(self):
        res = scan(
            make_market(),
            make_market(venue=VENUE_B, market_id=MARKET_B, event_id="other-event"),
        )
        assert res.opportunities == ()
        assert any(r.reason == "RULE_GATE" for r in res.rejections)

    def test_payout_mismatch_between_venues(self):
        res = scan(
            make_market(payout=Decimal(1)),
            make_market(venue=VENUE_B, market_id=MARKET_B, payout=Decimal("0.5")),
        )
        assert res.opportunities == ()
        assert any(r.reason == "RULE_GATE" for r in res.rejections)

    def test_payout_mismatch_mapping_vs_market(self):
        res = scan(
            make_market(payout=Decimal(1)),
            make_market(venue=VENUE_B, market_id=MARKET_B, payout=Decimal(1)),
            mapping=make_mapping(payout=Decimal("0.5")),
        )
        assert res.opportunities == ()
        assert any(r.reason == "RULE_GATE" for r in res.rejections)

    def test_unverified_mapping(self):
        res = scan(
            make_market(),
            make_market(venue=VENUE_B, market_id=MARKET_B),
            mapping=make_mapping(verified=False),
        )
        assert res.opportunities == ()
        assert any(r.reason == "MAPPING_NOT_USABLE" for r in res.rejections)

    def test_negrisk_unverified_completeness(self):
        """N1C rule: negRisk market (exhaustive=False until human-confirmed)
        must never yield a completeness opportunity."""
        neg_risk_style = make_market(exhaustive=False)  # adapter's negRisk output
        res = scan(neg_risk_style, make_market(venue=VENUE_B, market_id=MARKET_B))
        assert res.opportunities == ()
        strategies = [
            r.detail for r in res.rejections
        ]
        assert res.rejections  # gate explains itself
        assert all("exhaustive" in s for s in strategies)
