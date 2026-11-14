"""N2: depth-aware VWAP engine.

Gate 2: mid-price and last-trade price are never executable cost — only
walking actual book depth produces an executable quote.
Walking is prefix-deterministic: a smaller size yields a prefix of the
fills of a larger size, so re-quoting at a matched size is consistent.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from .schemas import BookLevel, Fill, Side


class ExecutableQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    side: Side
    requested: Decimal
    filled: Decimal
    total_cost: Decimal  # gross, before fees
    fills: tuple[Fill, ...]

    @property
    def fully_filled(self) -> bool:
        return self.filled == self.requested

    @property
    def vwap(self) -> Decimal:
        if self.filled == 0:
            raise ValueError("vwap undefined for zero fill")
        return self.total_cost / self.filled


def walk_book(
    levels: Sequence[BookLevel],
    size: Decimal,
    side: Side = Side.ASK,
    *,
    venue: str = "",
    market_id: str = "",
    outcome_id: str = "",
) -> ExecutableQuote:
    """Consume `size` shares against `levels` (asks asc / bids desc)."""
    if size <= 0:
        raise ValueError(f"walk size must be positive, got {size}")
    fills: list[Fill] = []
    remaining = size
    for lv in levels:
        if remaining <= 0:
            break
        take = lv.size if lv.size <= remaining else remaining
        fills.append(
            Fill(
                venue=venue or "unknown",
                market_id=market_id or "unknown",
                outcome_id=outcome_id or "unknown",
                price=lv.price,
                shares=take,
            )
        )
        remaining -= take
    filled = size - remaining
    total = sum((f.cost for f in fills), Decimal(0))
    return ExecutableQuote(
        side=side, requested=size, filled=filled, total_cost=total, fills=tuple(fills)
    )
