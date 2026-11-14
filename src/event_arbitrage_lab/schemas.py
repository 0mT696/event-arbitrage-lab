"""N1: normalized market schemas (pydantic v2, Decimal precision).

Fail-Closed by construction: invalid books/markets raise at validation time.
Prices are dollars per $1 payout share, hence 0 < price < 1.
Floats are normalized through str() at the boundary to kill binary artifacts.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ZERO = Decimal(0)
ONE = Decimal(1)


def _coerce_decimal(v: Any) -> Any:
    """Normalize floats via str() to kill binary artifacts; pass everything
    else through untouched (bool/str/datetime/tuple fields are legitimate)."""
    if isinstance(v, float):
        return Decimal(str(v))
    return v


class Side(str, Enum):
    BID = "BID"
    ASK = "ASK"


class StrictDecimalModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def _floats_via_str(cls, v: Any) -> Any:
        return _coerce_decimal(v)


class BookLevel(StrictDecimalModel):
    price: Decimal
    size: Decimal

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, v: Decimal) -> Decimal:
        if not (ZERO < v < ONE):
            raise ValueError(f"price must be in (0, 1), got {v}")
        return v

    @field_validator("size")
    @classmethod
    def _size_positive(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError(f"size must be positive, got {v}")
        return v


class Outcome(StrictDecimalModel):
    outcome_id: str = Field(min_length=1)
    label: str = Field(min_length=1)  # e.g. "YES" / "NO" / candidate name


class Market(StrictDecimalModel):
    """One venue's instantiation of an event.

    `mutually_exclusive` and `exhaustive` are semantic claims about the
    outcome set; the rule validator (rules.py) refuses to treat a market
    as arbitrage-eligible unless both are explicitly True.
    """

    venue: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    outcomes: tuple[Outcome, ...]
    mutually_exclusive: bool
    exhaustive: bool
    resolution_source: str = Field(min_length=1)
    payout_per_unit: Decimal = ONE

    @field_validator("outcomes")
    @classmethod
    def _distinct_outcomes(cls, v: tuple[Outcome, ...]) -> tuple[Outcome, ...]:
        if len(v) < 2:
            raise ValueError("a market needs at least 2 outcomes")
        ids = [o.outcome_id for o in v]
        if len(set(ids)) != len(ids):
            raise ValueError("outcome ids must be distinct")
        return v

    @field_validator("payout_per_unit")
    @classmethod
    def _payout_positive(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError("payout_per_unit must be positive")
        return v


class OrderBook(StrictDecimalModel):
    venue: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    outcome_id: str = Field(min_length=1)
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware (staleness checks depend on it)")
        return v

    @model_validator(mode="after")
    def _sorted_and_uncrossed(self) -> OrderBook:
        for a, b in zip(self.bids, self.bids[1:]):
            if a.price <= b.price:
                raise ValueError("bids must be strictly descending by price")
        for a, b in zip(self.asks, self.asks[1:]):
            if a.price >= b.price:
                raise ValueError("asks must be strictly ascending by price")
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise ValueError(
                f"crossed book rejected: best bid {self.bids[0].price} "
                f">= best ask {self.asks[0].price}"
            )
        return self

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    def ask_depth(self) -> Decimal:
        return sum((lv.size for lv in self.asks), ZERO)


class Fill(StrictDecimalModel):
    venue: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    outcome_id: str = Field(min_length=1)
    price: Decimal
    shares: Decimal

    @property
    def cost(self) -> Decimal:
        return self.price * self.shares


class StrategyKind(str, Enum):
    CROSS_VENUE_BINARY = "CROSS_VENUE_BINARY"
    SINGLE_VENUE_COMPLETENESS = "SINGLE_VENUE_COMPLETENESS"


class Leg(StrictDecimalModel):
    venue: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    outcome_id: str = Field(min_length=1)
    shares: Decimal
    vwap: Decimal
    gross_cost: Decimal
    fee: Decimal

    @property
    def total_cost(self) -> Decimal:
        return self.gross_cost + self.fee


class Opportunity(StrictDecimalModel):
    """A validated arbitrage candidate with full cost breakdown.

    net_profit = guaranteed_payout - gross_cost - total_fees - slippage_buffer
    (README: net_edge = settlement - cost_a - cost_b - fees - slippage_buffer).
    `assumptions` travels with the opportunity as part of its evidence.

    Formal definitions (N3 closure):
    - capacity_units == shares: the maximum paired units executable at the
      walked book depth. This IS the capacity; not an approximation.
    - confidence: deliberately ABSENT. No confidence model exists until N7
      produces statistical evidence (fill rates, edge decay). Any earlier
      confidence number would be fabricated precision.
    """

    strategy: StrategyKind
    legs: tuple[Leg, ...]
    shares: Decimal  # matched units, each guaranteed payout_per_unit
    gross_cost: Decimal
    total_fees: Decimal
    slippage_buffer: Decimal  # total dollar buffer subtracted from edge
    total_cost: Decimal  # gross_cost + total_fees + slippage_buffer
    guaranteed_payout: Decimal
    net_profit: Decimal
    edge_per_unit: Decimal
    roi: Decimal  # net_profit / total_cost
    assumptions: tuple[str, ...]

    @property
    def capacity_units(self) -> Decimal:
        """Formal N3 capacity: max paired units at walked depth."""
        return self.shares
