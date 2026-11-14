"""N1 rule-validator tests (gate 1: only verified MECE outcome sets count)."""
from decimal import Decimal

import pytest

from event_arbitrage_lab.rules import (
    RuleViolation,
    validate_market_rules,
    validate_pair_rules,
)

from conftest import VENUE_B, MARKET_B, make_mapping, make_market


class TestMarketRules:
    def test_mece_market_passes(self):
        validate_market_rules(make_market())

    def test_not_mutually_exclusive_rejected(self):
        with pytest.raises(RuleViolation, match="mutually exclusive"):
            validate_market_rules(make_market(mutually_exclusive=False))

    def test_not_exhaustive_rejected(self):
        with pytest.raises(RuleViolation, match="exhaustive"):
            validate_market_rules(make_market(exhaustive=False))


class TestPairRules:
    def test_verified_pair_passes(self):
        validate_pair_rules(
            make_mapping(),
            make_market(),
            make_market(venue=VENUE_B, market_id=MARKET_B),
        )

    @pytest.mark.parametrize("verified,equiv", [(False, True), (True, False), (False, False)])
    def test_unusable_mapping_rejected(self, verified, equiv):
        with pytest.raises(RuleViolation, match="not usable"):
            validate_pair_rules(
                make_mapping(verified=verified, equivalent=equiv),
                make_market(),
                make_market(venue=VENUE_B, market_id=MARKET_B),
            )

    def test_event_mismatch_rejected(self):
        with pytest.raises(RuleViolation, match="event_id"):
            validate_pair_rules(
                make_mapping(),
                make_market(event_id="other-event"),
                make_market(venue=VENUE_B, market_id=MARKET_B),
            )

    def test_payout_mismatch_rejected(self):
        with pytest.raises(RuleViolation, match="payout_per_unit"):
            validate_pair_rules(
                make_mapping(),
                make_market(payout=Decimal(1)),
                make_market(venue=VENUE_B, market_id=MARKET_B, payout=Decimal("0.5")),
            )
