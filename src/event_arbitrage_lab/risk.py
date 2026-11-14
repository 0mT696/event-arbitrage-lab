"""N6: Risk Engine & Circuit Breaker — the gate between Opportunity and
Paper Execution.

    Market Data -> Scanner -> Opportunity -> [RISK GATE] -> Paper Execution

Three invariants (same as the rest of the system):

1. FAIL CLOSED. Every policy field is mandatory and validated at
   construction. Missing books, malformed values, evaluation errors,
   a tripped breaker — all produce rejection, never silent pass.
2. ASSET IDENTITY. Exposure is keyed by the full asset identity
   (venue, market_id, outcome_id). Nothing is ever aggregated across
   venues or markets by name alone.
3. EVIDENCE PROVENANCE. Every decision — approve OR reject — is written
   to the ledger with measured values vs thresholds. Breaker trips and
   manual resets are evidence too. Given the ledger alone, an auditor
   can reconstruct WHY every execution was allowed or blocked.

Determinism: the gate takes `now` from the caller (tick data in replay),
never a wall clock. Same inputs -> same decisions -> same evidence.

Two phases (TOCTOU defense — a PRE_TRADE approval NEVER implies an
EXECUTION_TIME approval):

- PRE_TRADE: evaluates the scan-time opportunity and scan-time books
  (staleness, edge, liquidity, exposure, leg risk, breaker).
- EXECUTION_TIME: re-evaluates the EXECUTION-time books immediately
  before the executor sees them — identity, freshness, spread, and a
  full executor-mirroring simulation (same leg-B limit helper, same
  fee accounting, same net-PnL formula). Books that moved against the
  opportunity between scan and execution are blocked here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from .execution import PRECISION, PaperExecutionResult, fills_fees, leg_b_limit
from .fees import FeeModel, SlippageBuffer
from .ledger import EvidenceLedger
from .schemas import Opportunity, OrderBook, Side
from .vwap import walk_book

AssetKey = tuple[str, str, str]  # (venue, market_id, outcome_id)


class RiskReason(str, Enum):
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"
    STALE_DATA = "STALE_DATA"
    EDGE_BELOW_THRESHOLD = "EDGE_BELOW_THRESHOLD"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    EXPOSURE_LIMIT_ASSET = "EXPOSURE_LIMIT_ASSET"
    EXPOSURE_LIMIT_TOTAL = "EXPOSURE_LIMIT_TOTAL"
    LEG_RISK_LIMIT = "LEG_RISK_LIMIT"
    ABNORMAL_MARKET = "ABNORMAL_MARKET"
    DATA_FEED_FAILURE = "DATA_FEED_FAILURE"
    EVALUATION_ERROR = "EVALUATION_ERROR"


class RiskPhase(str, Enum):
    """Which gate produced a RISK_DECISION — evidence must distinguish
    scan-time approval from execution-time approval (TOCTOU)."""

    PRE_TRADE = "PRE_TRADE"
    EXECUTION_TIME = "EXECUTION_TIME"


@dataclass(frozen=True)
class RiskRejection:
    reason: RiskReason
    detail: str
    measured: str  # str for canonical, hash-stable serialization
    threshold: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    rejections: tuple[RiskRejection, ...]
    breaker_tripped: bool


@dataclass(frozen=True)
class RiskPolicy:
    """All fields mandatory — a risk limit you forgot to set is a limit
    that does not exist. Construction validates everything (fail closed)."""

    max_staleness: timedelta
    min_net_profit: Decimal  # absolute, per opportunity
    min_net_edge_per_share: Decimal
    min_capacity: Decimal  # minimum matched units
    max_exposure_per_asset: Decimal  # unhedged shares per asset identity
    max_total_exposure: Decimal  # total locked cost basis (capital)
    max_leg_risk_shares: Decimal  # worst-case unhedged shares per trade
    max_spread: Decimal  # best_ask - best_bid above this = abnormal book
    max_consecutive_failures: int  # degraded executions before trip

    def __post_init__(self) -> None:
        dec_fields = (
            self.min_net_profit,
            self.min_net_edge_per_share,
            self.min_capacity,
            self.max_exposure_per_asset,
            self.max_total_exposure,
            self.max_leg_risk_shares,
            self.max_spread,
        )
        if any(not d.is_finite() for d in dec_fields):
            raise ValueError("policy decimals must be finite (no NaN/Inf)")
        if self.max_staleness <= timedelta(0):
            raise ValueError("max_staleness must be positive")
        if self.min_net_profit < 0 or self.min_net_edge_per_share < 0:
            raise ValueError("edge thresholds must be non-negative")
        if self.min_capacity < 0:
            raise ValueError("min_capacity must be non-negative")
        if self.max_exposure_per_asset <= 0 or self.max_total_exposure <= 0:
            raise ValueError("exposure limits must be positive")
        if self.max_leg_risk_shares <= 0:
            raise ValueError("max_leg_risk_shares must be positive")
        if not (Decimal(0) < self.max_spread < Decimal(1)):
            raise ValueError("max_spread must be in (0, 1)")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")


@dataclass
class _Exposure:
    shares: Decimal = Decimal(0)  # open unhedged residual
    basis: Decimal = Decimal(0)  # locked cost basis (capital at risk)


class RiskGate:
    """Stateful gate: evaluates opportunities, tracks exposure and failure
    streaks, trips the circuit breaker. State changes and decisions are
    ledger-recorded when a ledger is attached."""

    def __init__(
        self, policy: RiskPolicy, ledger: EvidenceLedger | None = None
    ) -> None:
        self.policy = policy  # validated at construction
        self.ledger = ledger
        self._exposure: dict[AssetKey, _Exposure] = {}
        self._consecutive_failures = 0
        self._tripped = False
        self._trip_reason = ""

    # ---- evaluation ----

    def evaluate(
        self,
        opp: Opportunity,
        books: Mapping[AssetKey, OrderBook],
        now: datetime,
    ) -> RiskDecision:
        """PRE_TRADE evaluation of one opportunity against scan-time
        books. ALL applicable rejections are collected (full audit),
        not just the first."""
        try:
            rejections = self._evaluate_inner(opp, books, now)
        except Exception as exc:  # fail closed on ANY evaluation error
            rejections = [
                RiskRejection(
                    RiskReason.EVALUATION_ERROR,
                    f"{type(exc).__name__}: {exc}",
                    "",
                    "",
                )
            ]
        return self._decide(opp, RiskPhase.PRE_TRADE, rejections)

    def evaluate_execution(
        self,
        opp: Opportunity,
        books: Mapping[AssetKey, OrderBook],
        now: datetime,
        *,
        fees: tuple[FeeModel, FeeModel],
        payout_per_unit: Decimal,
        slippage: SlippageBuffer = SlippageBuffer(),
        min_profit: Decimal = Decimal(0),
    ) -> RiskDecision:
        """EXECUTION_TIME evaluation against the books the executor is
        ABOUT to see. A PRE_TRADE approval never implies this one:
        anything handed to the executor must re-pass identity,
        freshness, spread and an executor-mirroring simulation of
        liquidity and net edge (same leg-B limit helper, same fee
        accounting, same net formula as execute_pair)."""
        extra: dict[str, object] = {}
        try:
            rejections, extra = self._evaluate_execution_inner(
                opp, books, now,
                fees=fees, payout_per_unit=payout_per_unit,
                slippage=slippage, min_profit=min_profit,
            )
        except Exception as exc:  # fail closed on ANY evaluation error
            rejections = [
                RiskRejection(
                    RiskReason.EVALUATION_ERROR,
                    f"{type(exc).__name__}: {exc}",
                    "",
                    "",
                )
            ]
        return self._decide(
            opp, RiskPhase.EXECUTION_TIME, rejections, extra=extra
        )

    def _decide(
        self,
        opp: Opportunity,
        phase: RiskPhase,
        rejections: Sequence[RiskRejection],
        extra: Mapping[str, object] | None = None,
    ) -> RiskDecision:
        decision = RiskDecision(
            approved=not rejections,
            rejections=tuple(rejections),
            breaker_tripped=self._tripped,
        )
        payload: dict[str, object] = {
            "phase": phase.value,
            "approved": decision.approved,
            "strategy": opp.strategy.value,
            "legs": [
                {"venue": l.venue, "market_id": l.market_id,
                 "outcome_id": l.outcome_id, "shares": l.shares}
                for l in opp.legs
            ],
            "net_profit": opp.net_profit,
            "rejections": [
                {"reason": r.reason.value, "detail": r.detail,
                 "measured": r.measured, "threshold": r.threshold}
                for r in decision.rejections
            ],
            "breaker_tripped": self._tripped,
        }
        if extra:
            payload.update(extra)
        self._record("RISK_DECISION", payload)
        return decision

    def _breaker_rejection(self) -> list[RiskRejection]:
        return [
            RiskRejection(
                RiskReason.CIRCUIT_BREAKER_TRIPPED,
                f"breaker tripped: {self._trip_reason}; manual reset required",
                "tripped",
                "not tripped",
            )
        ]

    def _book_health(
        self,
        leg_key: AssetKey,
        books: Mapping[AssetKey, OrderBook],
        now: datetime,
        *,
        source: str,
    ) -> tuple[OrderBook | None, list[RiskRejection]]:
        """Identity + freshness + abnormality checks for ONE leg book.

        Shared by both phases — the checks are phase-independent; only
        the books differ (scan-time vs execution-time)."""
        p = self.policy
        rej: list[RiskRejection] = []
        tag = f"{leg_key[0]}/{leg_key[1]}/{leg_key[2]}"
        book = books.get(leg_key)
        if book is None:
            rej.append(RiskRejection(
                RiskReason.DATA_FEED_FAILURE,
                f"no {source} book for leg {tag}", "missing", "present"))
            return None, rej
        if (book.venue, book.market_id, book.outcome_id) != leg_key:
            # a book filed under a foreign identity is a feed integrity
            # failure — never trust it, never execute against it
            rej.append(RiskRejection(
                RiskReason.DATA_FEED_FAILURE,
                f"{source} book identity "
                f"{book.venue}/{book.market_id}/{book.outcome_id} "
                f"!= leg {tag}",
                f"{book.venue}/{book.market_id}/{book.outcome_id}", tag))
            return None, rej
        age = now - book.timestamp
        if age < timedelta(0) or age > p.max_staleness:
            rej.append(RiskRejection(
                RiskReason.STALE_DATA,
                f"{tag} {source} book age {age} "
                f"(future timestamps count as stale)",
                str(age), f"0..{p.max_staleness}"))
        if not book.asks or not book.bids:
            rej.append(RiskRejection(
                RiskReason.ABNORMAL_MARKET,
                f"{tag} {source} one-sided book: cannot enter AND exit",
                f"bids={len(book.bids)}/asks={len(book.asks)}",
                "both sides non-empty"))
        else:
            spread = book.asks[0].price - book.bids[0].price
            if spread > p.max_spread:
                rej.append(RiskRejection(
                    RiskReason.ABNORMAL_MARKET,
                    f"{tag} {source} spread {spread} exceeds limit",
                    str(spread), str(p.max_spread)))
        return book, rej

    def _evaluate_inner(
        self,
        opp: Opportunity,
        books: Mapping[AssetKey, OrderBook],
        now: datetime,
    ) -> list[RiskRejection]:
        p = self.policy
        if self._tripped:
            return self._breaker_rejection()

        rej: list[RiskRejection] = []

        # --- data/feed + staleness + abnormal market (per leg book) ---
        for leg in opp.legs:
            key = (leg.venue, leg.market_id, leg.outcome_id)
            _, leg_rej = self._book_health(key, books, now, source="scan")
            rej.extend(leg_rej)

        # --- edge threshold ---
        if opp.net_profit < p.min_net_profit:
            rej.append(RiskRejection(
                RiskReason.EDGE_BELOW_THRESHOLD, "net profit below minimum",
                str(opp.net_profit), str(p.min_net_profit)))
        per_share = opp.net_profit / opp.shares
        if per_share < p.min_net_edge_per_share:
            rej.append(RiskRejection(
                RiskReason.EDGE_BELOW_THRESHOLD, "net edge per share below minimum",
                str(per_share), str(p.min_net_edge_per_share)))

        # --- liquidity ---
        if opp.capacity_units < p.min_capacity:
            rej.append(RiskRejection(
                RiskReason.INSUFFICIENT_LIQUIDITY, "capacity below minimum",
                str(opp.capacity_units), str(p.min_capacity)))

        # --- leg risk: worst case = entire first leg left unhedged ---
        leg_a = opp.legs[0]
        if leg_a.shares > p.max_leg_risk_shares:
            rej.append(RiskRejection(
                RiskReason.LEG_RISK_LIMIT,
                "worst-case unhedged shares exceed limit",
                str(leg_a.shares), str(p.max_leg_risk_shares)))

        # --- exposure (identity-keyed, projected worst case) ---
        key_a = (leg_a.venue, leg_a.market_id, leg_a.outcome_id)
        current = self._exposure.get(key_a, _Exposure())
        projected_shares = current.shares + leg_a.shares
        if projected_shares > p.max_exposure_per_asset:
            rej.append(RiskRejection(
                RiskReason.EXPOSURE_LIMIT_ASSET,
                f"projected unhedged shares on {key_a[0]}/{key_a[1]}/{key_a[2]}",
                str(projected_shares), str(p.max_exposure_per_asset)))
        projected_total = (
            sum((e.basis for e in self._exposure.values()), Decimal(0))
            + leg_a.total_cost
        )
        if projected_total > p.max_total_exposure:
            rej.append(RiskRejection(
                RiskReason.EXPOSURE_LIMIT_TOTAL, "projected locked capital",
                str(projected_total), str(p.max_total_exposure)))

        return rej

    def _evaluate_execution_inner(
        self,
        opp: Opportunity,
        books: Mapping[AssetKey, OrderBook],
        now: datetime,
        *,
        fees: tuple[FeeModel, FeeModel],
        payout_per_unit: Decimal,
        slippage: SlippageBuffer,
        min_profit: Decimal,
    ) -> tuple[list[RiskRejection], dict[str, object]]:
        """Executor-mirroring checks on the EXECUTION-time books.

        Mirrors execute_pair exactly: walk leg A asks -> fee-aware leg-B
        limit (shared helper) -> walk leg B asks within the limit ->
        net PnL with identical accounting. If the pair is no longer
        fillable or no longer net-profitable on THESE books, the trade
        is blocked no matter what the scan-time books looked like.

        Returns (rejections, evidence-extra): the extra pins the exact
        exec-book tops, limit and simulated net into the RISK_DECISION
        record — the decision is explainable from the ledger alone."""
        p = self.policy
        if self._tripped:
            return self._breaker_rejection(), {}

        rej: list[RiskRejection] = []
        leg_a, leg_b = opp.legs[0], opp.legs[1]
        key_a = (leg_a.venue, leg_a.market_id, leg_a.outcome_id)
        key_b = (leg_b.venue, leg_b.market_id, leg_b.outcome_id)

        book_a, rej_a = self._book_health(key_a, books, now, source="exec")
        book_b, rej_b = self._book_health(key_b, books, now, source="exec")
        rej.extend(rej_a)
        rej.extend(rej_b)
        if book_a is None or book_b is None:
            return rej, {}  # cannot simulate without both books

        fee_a, fee_b = fees

        # --- leg A executable liquidity (executor walks the asks) ---
        quote_a = walk_book(
            book_a.asks, leg_a.shares, Side.ASK,
            venue=book_a.venue, market_id=book_a.market_id,
            outcome_id=book_a.outcome_id,
        )
        if quote_a.filled < leg_a.shares:
            rej.append(RiskRejection(
                RiskReason.INSUFFICIENT_LIQUIDITY,
                f"exec leg A asks fill {quote_a.filled} of {leg_a.shares}",
                str(quote_a.filled), str(leg_a.shares)))
            return rej, {}  # leg-B limit undefined without a full leg A
        fees_a_total = fills_fees(quote_a.fills, fee_a)

        # --- leg B within the SAME fee-aware limit the executor uses ---
        limit_b = leg_b_limit(
            payout_per_unit=payout_per_unit,
            vwap_a=quote_a.vwap,
            shares_a=quote_a.filled,
            fees_a_total=fees_a_total,
            fee_b=fee_b,
            slippage=slippage,
            min_profit=min_profit,
        )
        levels_b = tuple(lv for lv in book_b.asks if lv.price <= limit_b)
        quote_b = walk_book(
            levels_b, quote_a.filled, Side.ASK,
            venue=book_b.venue, market_id=book_b.market_id,
            outcome_id=book_b.outcome_id,
        )
        if quote_b.filled < quote_a.filled:
            rej.append(RiskRejection(
                RiskReason.INSUFFICIENT_LIQUIDITY,
                f"exec leg B fills {quote_b.filled} of {quote_a.filled} "
                f"within limit {limit_b}",
                str(quote_b.filled), str(quote_a.filled)))
        fees_b_total = fills_fees(quote_b.fills, fee_b)

        # --- net edge on the exec-time books (executor accounting) ---
        paired = min(quote_a.filled, quote_b.filled)
        ratio = paired / quote_a.filled
        cost_a_paired = (quote_a.total_cost * ratio).quantize(PRECISION)
        fees_a_paired = (fees_a_total * ratio).quantize(PRECISION)
        net = (
            paired * payout_per_unit
            - cost_a_paired
            - quote_b.total_cost
            - fees_a_paired
            - fees_b_total
            - slippage.total(paired)
        ).quantize(PRECISION)
        if net < p.min_net_profit:
            rej.append(RiskRejection(
                RiskReason.EDGE_BELOW_THRESHOLD,
                f"exec-time net {net} below minimum "
                f"(scan-time was {opp.net_profit})",
                str(net), str(p.min_net_profit)))
        per_share = net / paired if paired > 0 else Decimal(0)
        if per_share < p.min_net_edge_per_share:
            rej.append(RiskRejection(
                RiskReason.EDGE_BELOW_THRESHOLD,
                "exec-time net edge per share below minimum",
                str(per_share), str(p.min_net_edge_per_share)))

        # provenance: the exact exec-book state this decision used
        extra: dict[str, object] = {
            "exec_books": [
                {"venue": k[0], "market_id": k[1], "outcome_id": k[2],
                 "best_bid": (str(b.bids[0].price) if b.bids else None),
                 "best_ask": (str(b.asks[0].price) if b.asks else None),
                 "timestamp": b.timestamp.isoformat()}
                for k, b in ((key_a, book_a), (key_b, book_b))
            ],
            "limit_b": str(limit_b),
            "exec_net": str(net),
        }
        return rej, extra

    # ---- state updates ----

    def record_execution(
        self, opp: Opportunity, result: PaperExecutionResult
    ) -> None:
        """Update exposure (identity-keyed) and failure streak; trip the
        breaker when the streak hits the policy limit."""
        leg_a = opp.legs[0]
        key = (leg_a.venue, leg_a.market_id, leg_a.outcome_id)
        if result.residual_shares > 0:
            exp = self._exposure.setdefault(key, _Exposure())
            exp.shares += result.residual_shares
            exp.basis += result.residual_cost_basis

        clean = (
            result.paired_shares > 0
            and result.residual_shares == 0
            and result.unwind_realized_pnl >= 0
        )
        if clean:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.policy.max_consecutive_failures:
                self._trip(
                    f"{self._consecutive_failures} consecutive degraded executions"
                )

        self._record(
            "RISK_STATE",
            {
                "exposure": {
                    f"{k[0]}/{k[1]}/{k[2]}": {"shares": e.shares, "basis": e.basis}
                    for k, e in sorted(self._exposure.items())
                },
                "consecutive_failures": self._consecutive_failures,
                "breaker_tripped": self._tripped,
            },
        )

    def record_settlement(
        self, venue: str, market_id: str, outcome_id: str | None = None
    ) -> None:
        """Market settled -> residual exposure ends. outcome_id=None clears
        all outcomes of the market."""
        keys = [
            k for k in self._exposure
            if k[0] == venue and k[1] == market_id
            and (outcome_id is None or k[2] == outcome_id)
        ]
        for k in keys:
            del self._exposure[k]
        self._record(
            "RISK_STATE",
            {"settled": f"{venue}/{market_id}/{outcome_id or '*'}",
             "cleared": len(keys)},
        )

    # ---- circuit breaker ----

    def _trip(self, reason: str) -> None:
        if self._tripped:
            return
        self._tripped = True
        self._trip_reason = reason
        self._record("CIRCUIT_BREAKER", {"event": "TRIPPED", "reason": reason})

    def reset_breaker(self, operator: str) -> None:
        """Manual reset — the ONLY way back from tripped (fail closed
        means no auto-reset). The reset itself is evidence; operator is
        mandatory so resets are attributable."""
        if not operator or not operator.strip():
            raise ValueError("operator identity required for breaker reset")
        was = self._tripped
        self._tripped = False
        self._trip_reason = ""
        self._consecutive_failures = 0
        # exposure is NOT cleared: real residual positions do not vanish
        self._record(
            "CIRCUIT_BREAKER",
            {"event": "RESET", "operator": operator, "was_tripped": was},
        )

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def exposure_of(self, venue: str, market_id: str, outcome_id: str) -> _Exposure:
        return self._exposure.get((venue, market_id, outcome_id), _Exposure())

    # ---- durable runtime state (N7-BLOCKER-001) ----
    # A process/systemd restart must NEVER grant a clean risk state.
    # checkpoint() captures the full runtime state; restore_checkpoint()
    # applies it back with integrity + shape validation (fail closed).
    # The runner persists the checkpoint atomically at every batch
    # boundary, together with the ledger save.

    CHECKPOINT_VERSION = 1

    def checkpoint(self) -> dict[str, object]:
        """Full runtime state, canonical form (Decimal -> str)."""
        return {
            "v": self.CHECKPOINT_VERSION,
            "exposure": {
                f"{k[0]}/{k[1]}/{k[2]}": {
                    "shares": str(e.shares),
                    "basis": str(e.basis),
                }
                for k, e in sorted(self._exposure.items())
            },
            "consecutive_failures": self._consecutive_failures,
            "breaker_tripped": self._tripped,
            "trip_reason": self._trip_reason,
        }

    def restore_checkpoint(self, cp: Mapping[str, object]) -> None:
        """Restore runtime state from a durable checkpoint.

        Fail closed: unknown version, malformed exposure, negative
        streak — all raise ValueError; a corrupt checkpoint means NO
        state is restored and the caller must escalate, never guess."""
        if cp.get("v") != self.CHECKPOINT_VERSION:
            raise ValueError(
                f"unsupported risk checkpoint version: {cp.get('v')!r}"
            )
        try:
            exposure_raw = cp["exposure"]
            streak = cp["consecutive_failures"]
            tripped = cp["breaker_tripped"]
            trip_reason = cp["trip_reason"]
        except KeyError as exc:
            raise ValueError(f"risk checkpoint missing field: {exc}") from exc
        if not isinstance(streak, int) or streak < 0:
            raise ValueError("risk checkpoint: bad consecutive_failures")
        if not isinstance(tripped, bool) or not isinstance(trip_reason, str):
            raise ValueError("risk checkpoint: bad breaker fields")
        exposure: dict[AssetKey, _Exposure] = {}
        if not isinstance(exposure_raw, Mapping):
            raise ValueError("risk checkpoint: exposure must be a mapping")
        for key, vals in exposure_raw.items():
            parts = str(key).split("/")
            if len(parts) != 3 or not all(parts):
                raise ValueError(f"risk checkpoint: bad asset key {key!r}")
            if not isinstance(vals, Mapping):
                raise ValueError(f"risk checkpoint: bad exposure for {key!r}")
            shares = Decimal(str(vals["shares"]))
            basis = Decimal(str(vals["basis"]))
            if not (shares.is_finite() and basis.is_finite()):
                raise ValueError("risk checkpoint: non-finite exposure")
            if shares < 0 or basis < 0:
                raise ValueError("risk checkpoint: negative exposure")
            if shares > 0 or basis > 0:
                exposure[(parts[0], parts[1], parts[2])] = _Exposure(
                    shares=shares, basis=basis
                )
        self._exposure = exposure
        self._consecutive_failures = streak
        self._tripped = tripped
        self._trip_reason = trip_reason

    # ---- evidence ----

    def _record(self, kind: str, payload: object) -> None:
        if self.ledger is not None:
            self.ledger.append(kind, payload)
