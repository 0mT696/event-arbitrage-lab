"""N7A: long-running market data collector (read-only).

Design rules:
- READ-ONLY: discovery + order-book reads through the venue adapter.
  No order path, no credentials, no signing — same boundary as N1D.
- FAILURES ARE DATA: a per-market fetch error is recorded in the sweep
  frame (N7 wants feed-failure statistics), never silently dropped.
- VERSIONED FRAMES: sweep schema is versioned; readers fail closed on
  unknown versions (same discipline as ReplayFrame).
- CLOCK FROM THE CALLER in tests; real clock injected in production.

Sweep frame (one JSONL line per sweep):

    {
      "v": 1,
      "t": "<sweep start, ISO8601 UTC>",
      "markets": [
        {"market": <Market>, "fee": <adapter fee meta>, "category": <str|null>,
         "yes": <OrderBook|null>, "no": <OrderBook|null>,
         "error": <str|null>}
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from .adapters.polymarket import PolymarketAdapter
from .schemas import Market, OrderBook

COLLECT_SCHEMA_VERSION = 1


def discover_targets(
    adapter: PolymarketAdapter,
    *,
    max_targets: int = 30,
    min_volume_24hr: Decimal = Decimal("50000"),
    page: int = 50,
) -> list[Market]:
    """Select the observation universe: highest-volume arbitrage-eligible
    binary markets.

    Eligibility mirrors the engine's own rule gate: mutually exclusive
    AND exhaustive (binary non-negRisk per the adapter's fail-closed
    mapping). negRisk-linked markets stay out until human confirmation
    (standing rule). Ordered by Gamma volume24hr desc."""
    targets: list[Market] = []
    offset = 0
    # walk volume-ordered pages until we have enough eligible markets
    # or the well runs dry (short page = end of list)
    while len(targets) < max_targets:
        batch = adapter.list_markets(limit=page, offset=offset, order="volume24hr")
        if not batch:
            break
        for m in batch:
            if not (m.mutually_exclusive and m.exhaustive):
                continue
            meta = adapter.meta(m.market_id)
            vol = meta.get("volume_24hr") or Decimal(0)
            if vol < min_volume_24hr:
                continue
            targets.append(m)
            if len(targets) >= max_targets:
                break
        if len(batch) < page:
            break
        offset += page
    return targets


def collect_sweep(
    adapter: PolymarketAdapter,
    targets: Sequence[Market],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Fetch YES+NO books for every target. Per-market errors are
    recorded in-frame (feed failure is empirical data), never raised."""
    entries: list[dict[str, Any]] = []
    for m in targets:
        meta = adapter.meta(m.market_id)
        entry: dict[str, Any] = {
            "market": m,
            "fee": meta.get("fee") or {},
            "category": meta.get("category"),
            "yes": None,
            "no": None,
            "error": None,
        }
        try:
            # outcome ids are positional ("0"/"1") per adapter mapping
            entry["yes"] = adapter.fetch_orderbook(m.market_id, m.outcomes[0].outcome_id)
            entry["no"] = adapter.fetch_orderbook(m.market_id, m.outcomes[1].outcome_id)
        except Exception as exc:  # transport/parse failure = data
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    return {"v": COLLECT_SCHEMA_VERSION, "t": now.isoformat(), "markets": entries}


def serialize_sweep(sweep: dict[str, Any]) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, (Market, OrderBook)):
            return o.model_dump(mode="json")
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(f"not serializable: {type(o).__name__}")

    return json.dumps(sweep, sort_keys=True, default=default)


def parse_sweep(line: str) -> dict[str, Any]:
    """Parse one sweep frame; unknown schema version fails closed."""
    frame = json.loads(line)
    if frame.get("v") != COLLECT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported sweep version: {frame.get('v')!r} "
            f"(expected {COLLECT_SCHEMA_VERSION})"
        )
    frame["t"] = datetime.fromisoformat(frame["t"])
    for e in frame["markets"]:
        e["market"] = Market.model_validate(e["market"])
        for side in ("yes", "no"):
            if e[side] is not None:
                e[side] = OrderBook.model_validate(e[side])
        fee = e.get("fee") or {}
        if fee.get("rate") is not None:
            fee["rate"] = Decimal(str(fee["rate"]))
        e["fee"] = fee
    return frame


def read_sweeps(path: str | Path) -> list[dict[str, Any]]:
    return [
        parse_sweep(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]


def sweep_errors(sweep: dict[str, Any]) -> int:
    return sum(1 for e in sweep["markets"] if e["error"] is not None)


# ---- long-running CLI ----


def daily_path(out_dir: Path, day: str) -> Path:
    return out_dir / f"sweeps-{day}.jsonl"


def run_forever(
    *,
    out_dir: str | Path,
    interval: float = 10.0,
    max_targets: int = 30,
    min_volume_24hr: Decimal = Decimal("50000"),
    rediscovery: timedelta = timedelta(hours=6),
    max_sweeps: int | None = None,  # tests/smoke only
    adapter: PolymarketAdapter | None = None,  # tests inject a fake
) -> None:
    """Collector main loop. Daily-rotated JSONL; discovery refreshed on
    a timer; global transport failures back off instead of spinning."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    adapter = adapter or PolymarketAdapter()
    targets: list[Market] = []
    discovered_at = datetime.min.replace(tzinfo=timezone.utc)
    failures = 0
    sweeps = 0

    while True:
        now = datetime.now(timezone.utc)
        if not targets or now - discovered_at > rediscovery:
            targets = discover_targets(
                adapter, max_targets=max_targets, min_volume_24hr=min_volume_24hr
            )
            discovered_at = now
            print(
                f"[{now.isoformat()}] discovery: {len(targets)} targets",
                flush=True,
            )
        started = time.monotonic()
        try:
            sweep = collect_sweep(adapter, targets, now=now)
            failures = 0
        except Exception as exc:  # discovery-level/transport catastrophe
            failures += 1
            backoff = min(60.0, 2.0 ** failures)
            print(f"[{now.isoformat()}] sweep failed ({exc}); backoff {backoff}s", flush=True)
            time.sleep(backoff)
            continue
        # markets whose book endpoint 404s are closed/resolved — the
        # failure frame is already recorded as data; stop polling them
        # until the next rediscovery instead of burning rate limit
        gone = {
            e["market"].market_id
            for e in sweep["markets"]
            if e["error"] is not None and "404" in e["error"]
        }
        if gone:
            targets = [m for m in targets if m.market_id not in gone]
            print(f"[{now.isoformat()}] dropped closed markets: {sorted(gone)}",
                  flush=True)
        day = now.strftime("%Y-%m-%d")
        with daily_path(out, day).open("a") as fh:
            fh.write(serialize_sweep(sweep) + "\n")
        sweeps += 1
        if sweeps % 360 == 0:  # ~hourly heartbeat at 10s interval
            print(
                f"[{now.isoformat()}] heartbeat: {sweeps} sweeps, "
                f"{sweep_errors(sweep)} market errors in last sweep",
                flush=True,
            )
        if max_sweeps is not None and sweeps >= max_sweeps:
            return
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="N7A read-only market collector")
    ap.add_argument("--out", required=True, help="output directory for sweeps")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--targets", type=int, default=30)
    ap.add_argument("--min-volume", type=Decimal, default=Decimal("50000"))
    ap.add_argument("--max-sweeps", type=int, default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)
    run_forever(
        out_dir=args.out,
        interval=args.interval,
        max_targets=args.targets,
        min_volume_24hr=args.min_volume,
        max_sweeps=args.max_sweeps,
    )


if __name__ == "__main__":
    main()
