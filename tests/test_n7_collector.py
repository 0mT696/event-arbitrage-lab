"""N7A collector tests — recorded fixtures, zero network."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from event_arbitrage_lab.adapters.polymarket import PolymarketAdapter
from event_arbitrage_lab.collector import (
    collect_sweep,
    discover_targets,
    parse_sweep,
    read_sweeps,
    serialize_sweep,
    sweep_errors,
)

FIXED_NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def gamma_market(mid, outcomes, vol, neg_risk=False):
    return {
        "id": mid,
        "question": f"q{mid}",
        "outcomes": json.dumps(outcomes),
        "clobTokenIds": json.dumps([f"tok-{mid}-0", f"tok-{mid}-1"]
                                   [: len(outcomes)]),
        "conditionId": f"0xcond{mid}",
        "category": "crypto",
        "negRisk": neg_risk,
        "volume24hr": vol,
        "liquidity": "10000",
        "feesEnabled": True,
        "feeSchedule": json.dumps({"rate": "0.07", "exponent": 1,
                                   "takerOnly": True}),
    }


def book_payload(mid, idx):
    return {
        "market": f"0xcond{mid}",
        "asset_id": f"tok-{mid}-{idx}",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.52", "size": "100"}],
    }


def make_adapter(pages, fail_books_for=()):
    def transport(url):
        if "/markets" in url:
            # crude offset parse
            off = int(url.split("offset=")[1].split("&")[0])
            page_size = int(url.split("limit=")[1].split("&")[0])
            all_ms = [m for page in pages for m in page]
            return all_ms[off: off + page_size]
        for mid in fail_books_for:
            if f"tok-{mid}-" in url:
                raise ConnectionError("simulated feed failure")
        for page in pages:
            for m in page:
                mid = m["id"]
                for idx in (0, 1):
                    if f"token_id=tok-{mid}-{idx}" in url:
                        return book_payload(mid, idx)
        raise AssertionError(f"unexpected url: {url}")

    return PolymarketAdapter(transport=transport, clock=lambda: FIXED_NOW)


class TestDiscovery:
    def test_eligible_binary_sorted_by_volume_negrisk_excluded(self):
        pages = [[
            gamma_market(1, ["Up", "Down"], 200000),          # eligible
            gamma_market(2, ["Yes", "No"], 90000, neg_risk=True),  # negRisk out
            gamma_market(3, ["Yes", "No"], 80000),            # eligible
            gamma_market(4, ["Yes", "No"], 10),               # below min volume
        ]]
        adapter = make_adapter(pages)
        targets = discover_targets(adapter, max_targets=10,
                                   min_volume_24hr=Decimal("50000"), page=4)
        ids = [m.market_id for m in targets]
        assert ids == ["1", "3"]
        # non-Yes/No binary labels (Up/Down) ARE eligible binaries
        assert targets[0].exhaustive

    def test_max_targets_caps(self):
        pages = [[gamma_market(i, ["Yes", "No"], 100000 + i) for i in range(5)]]
        adapter = make_adapter(pages)
        targets = discover_targets(adapter, max_targets=2,
                                   min_volume_24hr=Decimal(0), page=5)
        assert len(targets) == 2


class TestSweep:
    def test_sweep_roundtrip_byte_stable(self, tmp_path):
        pages = [[gamma_market(1, ["Up", "Down"], 200000)]]
        adapter = make_adapter(pages)
        targets = discover_targets(adapter, min_volume_24hr=Decimal(0), page=4)
        sweep = collect_sweep(adapter, targets, now=FIXED_NOW)
        line = serialize_sweep(sweep)
        parsed = parse_sweep(line)
        assert serialize_sweep(parsed) == line  # canonical round-trip
        e = parsed["markets"][0]
        assert e["yes"].asks[0].price == Decimal("0.52")
        assert e["no"].bids[0].price == Decimal("0.45")
        assert e["error"] is None
        assert e["fee"]["rate"] == Decimal("0.07")

    def test_feed_failure_recorded_as_data(self):
        pages = [[gamma_market(1, ["Yes", "No"], 100000),
                  gamma_market(2, ["Yes", "No"], 90000)]]
        adapter = make_adapter(pages, fail_books_for=(2,))
        targets = discover_targets(adapter, min_volume_24hr=Decimal(0), page=4)
        sweep = collect_sweep(adapter, targets, now=FIXED_NOW)
        assert sweep_errors(sweep) == 1
        bad = sweep["markets"][1]
        assert bad["error"].startswith("ConnectionError")
        assert bad["yes"] is None and bad["no"] is None
        good = sweep["markets"][0]
        assert good["error"] is None and good["yes"] is not None

    def test_unknown_version_fails_closed(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text('{"v": 999, "t": "2026-10-06T12:00:00+00:00", "markets": []}\n')
        with pytest.raises(ValueError, match="unsupported sweep version"):
            read_sweeps(p)


class TestClosedMarketDrop:
    def test_404_market_dropped_after_failure_recorded(self, tmp_path):
        import urllib.error

        from event_arbitrage_lab.collector import run_forever

        pages = [[gamma_market(1, ["Yes", "No"], 200000),
                  gamma_market(2, ["Yes", "No"], 100000)]]

        def transport(url):
            if "/markets" in url:
                return pages[0]
            if "tok-2-" in url:
                raise urllib.error.HTTPError(
                    url, 404, "Not Found", {}, None)  # market closed
            for idx in (0, 1):
                if f"token_id=tok-1-{idx}" in url:
                    return book_payload(1, idx)
            raise AssertionError(f"unexpected url: {url}")

        adapter = PolymarketAdapter(transport=transport,
                                    clock=lambda: FIXED_NOW)
        run_forever(out_dir=tmp_path, interval=0.01, max_sweeps=3,
                    adapter=adapter)
        sweeps = read_sweeps(next(tmp_path.glob("sweeps-*.jsonl")))
        assert len(sweeps) == 3
        # sweep 1: both markets attempted; the 404 failure is evidence
        assert len(sweeps[0]["markets"]) == 2
        assert "404" in sweeps[0]["markets"][1]["error"]
        # sweeps 2-3: closed market no longer polled (no rate-limit burn)
        assert [len(s["markets"]) for s in sweeps[1:]] == [1, 1]
