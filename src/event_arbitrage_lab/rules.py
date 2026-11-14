"""N1: market-rule validation. Fail-Closed.

Gate 1 of the roadmap: no prediction-based trade is called arbitrage.
Only markets whose outcome set is explicitly mutually exclusive AND
exhaustive are eligible; cross-venue pairs additionally require a
verified semantic mapping with equivalent resolution sources.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator

from .schemas import Market, StrictDecimalModel


class RuleViolation(Exception):
    """Raised when a market or pair fails arbitrage-eligibility rules."""


def validate_market_rules(market: Market) -> None:
    """Assert a market's outcome set is arbitrage-eligible. Fail-Closed."""
    if not market.mutually_exclusive:
        raise RuleViolation(
            f"{market.venue}/{market.market_id}: outcomes not confirmed mutually exclusive"
        )
    if not market.exhaustive:
        raise RuleViolation(
            f"{market.venue}/{market.market_id}: outcomes not confirmed exhaustive"
        )


class VenuePairMapping(StrictDecimalModel):
    """Cross-venue claim: two markets resolve the same event equivalently.

    `usable()` is the Fail-Closed gate: unverified or non-equivalent pairs
    must never produce opportunities.
    """

    event_id: str = Field(min_length=1)
    venue_a: str = Field(min_length=1)
    market_a: str = Field(min_length=1)
    venue_b: str = Field(min_length=1)
    market_b: str = Field(min_length=1)
    verified: bool
    resolution_source_equivalent: bool
    payout_per_unit: Decimal = Decimal(1)

    @field_validator("payout_per_unit")
    @classmethod
    def _payout_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("payout_per_unit must be positive")
        return v

    def usable(self) -> bool:
        return self.verified and self.resolution_source_equivalent


def validate_pair_rules(mapping: VenuePairMapping, market_a: Market, market_b: Market) -> None:
    """Assert a cross-venue pair is arbitrage-eligible. Fail-Closed."""
    if not mapping.usable():
        raise RuleViolation(
            f"pair {mapping.market_a}/{mapping.market_b} not usable: "
            f"verified={mapping.verified}, "
            f"resolution_source_equivalent={mapping.resolution_source_equivalent}"
        )
    if (market_a.venue, market_a.market_id) != (mapping.venue_a, mapping.market_a):
        raise RuleViolation("market_a does not match mapping")
    if (market_b.venue, market_b.market_id) != (mapping.venue_b, mapping.market_b):
        raise RuleViolation("market_b does not match mapping")
    if market_a.event_id != mapping.event_id or market_b.event_id != mapping.event_id:
        raise RuleViolation("market event_id does not match mapping event_id")
    validate_market_rules(market_a)
    validate_market_rules(market_b)
    if market_a.payout_per_unit != market_b.payout_per_unit:
        raise RuleViolation("payout_per_unit mismatch between venues")
    if market_a.payout_per_unit != mapping.payout_per_unit:
        raise RuleViolation("payout_per_unit mismatch between mapping and markets")
