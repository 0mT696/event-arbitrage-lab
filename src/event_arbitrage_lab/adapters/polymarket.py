"""Polymarket read-only adapter (N1C first venue).

Public endpoints, no credentials, no signing, no order paths — discovery
and order-book reads only, per the N1C decision record.

    Gamma:  GET {gamma_base}/markets           market discovery + metadata
    CLOB:   GET {clob_base}/book?token_id=...  order book per outcome token

Fees — VERIFIED against official docs 2026-10-06
(docs.polymarket.com/trading/fees.md + polymarket.com/fees):

    fee = C x feeRate x p x (1 - p)   # taker only; makers never pay

Official category rates: crypto 0.07, sports 0.05, finance/politics/
mentions/tech 0.04, economics/culture/weather/general 0.05,
geopolitics/world 0 (explicitly fee-free). Fees round to 5 decimals
(min 0.00001 USDC). Gamma exposes a PER-MARKET feeSchedule
({rate, exponent, takerOnly, rebateRate}) which takes precedence over
the category table; exponent != 1 or missing-but-enabled -> MAX rate
(fail-closed: never understate fees). Maker/taker rebate programs are
ignored (cannot be relied upon -> gross fee is the conservative basis).

Transport and clock are injected: tests run on recorded fixtures with zero
network; determinism preserved for N5 replay.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from ..adapter import VenueCapability
from ..fees import FeeModel, QuadraticTakerFee, ZeroFee
from ..schemas import BookLevel, Market, OrderBook, Outcome

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Official taker rates (docs.polymarket.com/trading/fees.md, 2026-10-06).
# Fallback only — per-market feeSchedule from Gamma takes precedence.
CATEGORY_TAKER_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "general": Decimal("0.05"),
    "other": Decimal("0.05"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "geopolitics": Decimal(0),
    "world": Decimal(0),
}
MAX_TAKER_RATE = max(CATEGORY_TAKER_RATES.values())  # 0.07; unknown -> conservative

JsonTransport = Callable[[str], Any]
Clock = Callable[[], datetime]


def _urllib_transport(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "event-arbitrage-lab/0.1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _loads_maybe(value: Any) -> Any:
    """Gamma returns some arrays as JSON-encoded strings."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _decimal_maybe(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class PolymarketAdapter:
    """Implements the N1D MarketDataAdapter protocol (read-only subset)."""

    venue = "polymarket"

    def __init__(
        self,
        transport: JsonTransport | None = None,
        clock: Clock | None = None,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
    ) -> None:
        self._transport = transport or _urllib_transport
        self._clock = clock or _default_clock
        self._gamma = gamma_base.rstrip("/")
        self._clob = clob_base.rstrip("/")
        # market_id -> {"outcomes": {outcome_id: token_id}, "category": str|None}
        self._meta: dict[str, dict[str, Any]] = {}

    # ---- N1D contract ----

    def capabilities(self) -> VenueCapability:
        return VenueCapability(
            venue=self.venue,
            rest_api=True,
            websocket=True,
            orderbook_data=True,
            historical_data=True,
            fee_structure="category-based quadratic taker fee; geopolitics/world zero",
            settlement_rules="UMA optimistic oracle; USDC on Polygon",
            rate_limits="per-IP; /book 1500/10s (N1B round 1)",
            geo_account_restrictions="reads anonymous; Polymarket US separate entity",
            notes="self-report; N1B matrix row is the verified version",
        )

    def list_markets(
        self, limit: int = 50, offset: int = 0, order: str | None = None
    ) -> list[Market]:
        url = (
            f"{self._gamma}/markets?active=true&closed=false"
            f"&limit={limit}&offset={offset}"
        )
        if order is not None:
            url += f"&order={order}&ascending=false"
        payload = self._transport(url)
        markets: list[Market] = []
        for raw in payload:
            market = self._to_market(raw)
            if market is not None:
                markets.append(market)
        return markets

    def fetch_orderbook(self, market_id: str, outcome_id: str) -> OrderBook:
        meta = self._meta.get(str(market_id))
        if meta is None:
            raise ValueError(
                f"market {market_id} unknown — call list_markets() first (fail-closed)"
            )
        token_id = meta["outcomes"].get(outcome_id)
        if token_id is None:
            raise ValueError(f"outcome {outcome_id} not in market {market_id}")
        return self._fetch_book_by_token(market_id, outcome_id, token_id)

    def fee_model(self) -> FeeModel:
        """Default: conservative max rate (unknown category)."""
        return QuadraticTakerFee(rate=MAX_TAKER_RATE)

    def fee_model_for(self, market_id: str) -> FeeModel:
        """Per-market fee model via the shared meta-based builder."""
        meta = self._meta.get(str(market_id)) or {}
        return fee_model_from_meta(meta.get("fee") or {}, meta.get("category"))

    # ---- mapping ----

    def _to_market(self, raw: dict[str, Any]) -> Market | None:
        try:
            market_id = str(raw["id"])
            outcome_labels = _loads_maybe(raw["outcomes"])
            token_ids = _loads_maybe(raw.get("clobTokenIds") or raw.get("clob_token_ids"))
        except (KeyError, TypeError, json.JSONDecodeError):
            return None  # malformed record: skip, never guess
        if len(outcome_labels) != len(token_ids):
            return None

        outcomes = tuple(
            Outcome(outcome_id=str(i), label=str(label))
            for i, label in enumerate(outcome_labels)
        )
        neg_risk = bool(raw.get("negRisk") or raw.get("neg_risk") or False)
        # Binary CTF market: exactly 2 outcome tokens whose payout vector
        # sums to payout_per_unit BY PROTOCOL — MECE by construction,
        # regardless of label ("Yes/No", "Up/Down", team names). The
        # exhaustive exception mechanism is negRisk (multi-outcome
        # conversion), which stays fail-closed until human-verified.
        # Widened 2026-10-06 for N7: the highest-volume binaries
        # (crypto Up/Down, sports) are not labeled Yes/No.
        is_binary = len(outcome_labels) == 2
        category = raw.get("category")

        # Per-market fee config (Gamma: feesEnabled + feeSchedule).
        fees_enabled_raw = raw.get("feesEnabled")
        schedule = _loads_maybe(raw.get("feeSchedule")) or {}
        if not isinstance(schedule, dict):
            schedule = {}
        fee_rate = schedule.get("rate")
        fee_exponent = schedule.get("exponent")

        self._meta[market_id] = {
            "outcomes": {o.outcome_id: str(t) for o, t in zip(outcomes, token_ids)},
            "category": category,
            "volume_24hr": _decimal_maybe(raw.get("volume24hr")),
            "liquidity": _decimal_maybe(raw.get("liquidity")),
            "fee": {
                "enabled": (
                    bool(fees_enabled_raw) if fees_enabled_raw is not None else None
                ),
                "rate": (
                    Decimal(str(fee_rate)) if fee_rate is not None else None
                ),
                "exponent": (
                    int(fee_exponent) if fee_exponent is not None else None
                ),
                "taker_only": schedule.get("takerOnly"),
            },
        }
        return Market(
            venue=self.venue,
            market_id=market_id,
            event_id=str(raw.get("conditionId") or raw.get("condition_id") or market_id),
            outcomes=outcomes,
            # binary (2-outcome): MECE by construction; negRisk:
            # mutually exclusive yes, exhaustive NOT claimed until
            # human-verified (standing rule, unchanged)
            mutually_exclusive=True,
            exhaustive=is_binary and not neg_risk,
            resolution_source="UMA optimistic oracle (per N1B)",
            payout_per_unit=Decimal(1),
        )

    def meta(self, market_id: str) -> dict[str, Any]:
        """Raw per-market extras (fee config, volume, liquidity).

        Fail-closed: unknown market -> empty dict."""
        return dict(self._meta.get(str(market_id)) or {})

    def _fetch_book_by_token(
        self, market_id: str, outcome_id: str, token_id: str
    ) -> OrderBook:
        payload = self._transport(f"{self._clob}/book?token_id={token_id}")

        def levels(key: str) -> tuple[BookLevel, ...]:
            rows = payload.get(key) or []
            out = [
                BookLevel(price=Decimal(str(r["price"])), size=Decimal(str(r["size"])))
                for r in rows
                if Decimal(str(r["size"])) > 0
            ]
            out.sort(key=lambda lv: lv.price, reverse=(key == "bids"))
            return tuple(out)

        return OrderBook(
            venue=self.venue,
            market_id=str(market_id),
            outcome_id=outcome_id,
            bids=levels("bids"),
            asks=levels("asks"),
            timestamp=self._clock(),
        )


def fee_model_from_meta(fee: dict[str, Any], category: Any = None) -> FeeModel:
    """Per-market fee model from Gamma fee meta, most specific first.

    1. Gamma per-market feeSchedule: feesEnabled=False -> ZeroFee;
       enabled + rate + exponent==1 -> QuadraticTakerFee(rate);
       enabled but schedule missing or exponent!=1 -> MAX (fail-closed).
    2. Category table (official docs, verified 2026-10-06).
    3. Unknown -> MAX_TAKER_RATE (fail-closed).

    Shared by the live adapter and the N7 offline paper runner so both
    price fees from the SAME rules (no interface drift)."""
    enabled = fee.get("enabled")
    if enabled is False:
        return ZeroFee()
    if enabled is True:
        rate = fee.get("rate")
        if rate is not None and fee.get("exponent") == 1:
            return QuadraticTakerFee(rate=Decimal(str(rate)))
        return QuadraticTakerFee(rate=MAX_TAKER_RATE)
    rate = CATEGORY_TAKER_RATES.get(str(category).lower(), MAX_TAKER_RATE)
    return QuadraticTakerFee(rate=rate)
