"""N1D: adapter contract.

Core domain consumes ONLY canonical schemas. Venue-specific clients
implement MarketDataAdapter and must never leak platform structures
(field names, enums, fee shapes) upward into the core domain.

VenueCapability is the row schema for the N1B Venue Capability Matrix:
research fills it per candidate venue. `None` means UNKNOWN — and
unknown is not yes (Fail-Closed).
"""
from __future__ import annotations

from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .fees import FeeModel
from .schemas import Market, OrderBook


class VenueCapability(BaseModel):
    """One row of the N1B Venue Capability Matrix.

    All capability flags are tri-state: True / False / None(unknown).
    N1C venue selection must treat None as a research gap, not as a yes.
    """

    model_config = ConfigDict(frozen=True)

    venue: str = Field(min_length=1)
    rest_api: bool | None = None
    websocket: bool | None = None
    orderbook_data: bool | None = None
    historical_data: bool | None = None
    fee_structure: str = ""  # summary + source URL, verified as of a date
    market_outcome_semantics: str = ""  # outcome typing, neg-risk, multi-outcome rules
    settlement_rules: str = ""  # resolution source, timing, disputes, currency
    rate_limits: str = ""
    geo_account_restrictions: str = ""
    notes: str = ""


class MarketDataAdapter(Protocol):
    """Uniform contract every venue adapter must satisfy.

    Implementations translate venue-native payloads into canonical
    Market / OrderBook at the boundary. Core code never sees raw payloads.
    """

    def capabilities(self) -> VenueCapability:
        """Self-reported capabilities (N1B research verifies them)."""
        ...

    def list_markets(self) -> Sequence[Market]:
        """Canonical markets with rule metadata filled (MECE flags may be
        unconfirmed -> core rule validator will reject them, Fail-Closed)."""
        ...

    def fetch_orderbook(self, market_id: str, outcome_id: str) -> OrderBook:
        """Canonical, validated OrderBook (sorting/cross checks apply)."""
        ...

    def fee_model(self) -> FeeModel:
        """The venue's fee model with rate bound (e.g. QuadraticTakerFee(rate))."""
        ...
