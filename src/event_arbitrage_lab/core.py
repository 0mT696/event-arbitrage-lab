from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


def executable_vwap(levels: list[Level], quantity: Decimal) -> Decimal | None:
    """Return the average executable price for a buy of quantity."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    remaining = quantity
    notional = Decimal("0")
    for level in levels:
        if level.price < 0 or level.size <= 0:
            raise ValueError("invalid order-book level")
        take = min(remaining, level.size)
        notional += take * level.price
        remaining -= take
        if remaining == 0:
            return notional / quantity
    return None


def net_edge(
    settlement_value: Decimal,
    cost_a: Decimal,
    cost_b: Decimal,
    fees: Decimal = Decimal("0"),
    slippage_buffer: Decimal = Decimal("0"),
) -> Decimal:
    return settlement_value - cost_a - cost_b - fees - slippage_buffer
