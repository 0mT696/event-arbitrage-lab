"""N2: fee and slippage models (venue-agnostic).

N1D rule: no venue-specific structure in the core domain. The models here
are generic and parameterized; binding a model + rate to a concrete venue
happens in that venue's adapter (N1D), never in core.

All formulas are configuration, not assertions of current venue policy.
Before any live use they must be re-verified against official venue
documentation at that time (N8 Human Gate).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

CENT = Decimal("0.01")
# Polymarket official fee granularity (docs.polymarket.com/trading/fees.md,
# verified 2026-10-06): "Fees are rounded to 5 decimal places. The smallest
# fee charged is 0.00001 USDC."
FEE_QUANTUM = Decimal("0.00001")


class FeeModel(Protocol):
    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        """Dollar fee charged for buying `shares` units at `price`."""
        ...


@dataclass(frozen=True)
class ZeroFee:
    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        return Decimal(0)


@dataclass(frozen=True)
class QuadraticTakerFee:
    """fee = ceil(rate * shares * price * (1 - price)) at 0.00001 granularity.

    Quadratic taker-fee shape for event contracts (fee peaks at price 0.5,
    vanishes near 0 and 1). Formula and 5-decimal rounding granularity
    verified against Polymarket official docs (trading/fees.md, 2026-10-06);
    we CEIL at that granularity instead of round-half — deliberate
    conservative deviation, bias <= 0.00001 USDC per fill, so fees are
    never understated. Applied per fill and summed by callers.
    The venue-specific rate is bound by the adapter.
    """

    rate: Decimal = Decimal("0.07")

    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        raw = self.rate * shares * price * (1 - price)
        return raw.quantize(FEE_QUANTUM, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class FlatBpsFee:
    """Fee in basis points of notional (shares * price), ceiled to the cent."""

    bps: Decimal

    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        raw = shares * price * self.bps / Decimal(10_000)
        return raw.quantize(CENT, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class SlippageBuffer:
    """Per-unit dollar buffer against adverse movement between quote and fill.

    Gate 5: net edge must include slippage. The buffer is subtracted from
    edge as `per_unit * shares`; research runs should set it explicitly.
    """

    per_unit: Decimal = Decimal(0)

    def total(self, shares: Decimal) -> Decimal:
        if self.per_unit < 0:
            raise ValueError("slippage per_unit must be non-negative")
        return self.per_unit * shares
