"""N5 replay tests: snapshot serde roundtrip, end-to-end replay with
evidence trail, determinism (identical root hash), latency override."""
from datetime import timedelta
from decimal import Decimal

from event_arbitrage_lab.execution import TakerFillPolicy
from event_arbitrage_lab.fees import ZeroFee
from event_arbitrage_lab.ledger import EvidenceLedger
from event_arbitrage_lab.replay import (
    PairTick,
    load_snapshots,
    run_replay,
    save_snapshots,
)
from event_arbitrage_lab.scanner import BinaryMarketBooks

from conftest import (
    MARKET_A,
    MARKET_B,
    NO,
    NOW,
    VENUE_A,
    VENUE_B,
    YES,
    make_book,
    make_mapping,
    make_market,
)

TAKER = TakerFillPolicy()
ZERO = ZeroFee()


def side_a(yes_ask="0.48", no_ask="0.52"):
    return BinaryMarketBooks(
        market=make_market(),
        yes=make_book(VENUE_A, MARKET_A, YES, bids=[("0.45", 100)], asks=[(yes_ask, 100)]),
        no=make_book(VENUE_A, MARKET_A, NO, bids=[("0.46", 100)], asks=[(no_ask, 100)]),
    )


def side_b(no_ask="0.49"):
    return BinaryMarketBooks(
        market=make_market(venue=VENUE_B, market_id=MARKET_B),
        yes=make_book(VENUE_B, MARKET_B, YES, bids=[("0.48", 100)], asks=[("0.51", 100)]),
        no=make_book(VENUE_B, MARKET_B, NO, bids=[("0.46", 100)], asks=[(no_ask, 100)]),
    )


def replay(ticks):
    return run_replay(
        ticks, make_mapping(), ZERO, ZERO, TAKER, TAKER,
        size=Decimal(100), max_staleness=timedelta(seconds=60),
    )


class TestSnapshotSerde:
    def test_roundtrip(self, tmp_path):
        books = [side_a().yes, side_a().no, side_b().yes, side_b().no]
        p = tmp_path / "snap.jsonl"
        assert save_snapshots(books, p) == 4
        loaded = load_snapshots(p)
        assert loaded == books


class TestReplay:
    def test_arb_tick_produces_full_evidence_trail(self):
        ledger = EvidenceLedger()
        report = replay_with_ledger(ledger, [PairTick(side_a(), side_b())])
        assert report.ticks == 1
        assert report.opportunities == 1
        assert report.executions == 1
        assert report.clean_hedged == 1
        assert report.degraded == 0
        assert report.total_net_pnl == Decimal("3.000000")
        kinds = [r.kind for r in ledger.records]
        assert kinds == [
            "SCAN_SNAPSHOT",
            "OPPORTUNITY",
            "EXECUTION_SNAPSHOT",
            "EXECUTION",
        ]
        assert ledger.verify()
        assert report.ledger_root_hash == ledger.root_hash

    def test_no_arb_tick_records_snapshot_only(self):
        report = replay([PairTick(side_a(yes_ask="0.52"), side_b(no_ask="0.53"))])
        assert report.opportunities == 0 and report.executions == 0

    def test_determinism_same_root_hash(self):
        ticks = [PairTick(side_a(), side_b()), PairTick(side_a(), side_b())]
        assert replay(ticks).ledger_root_hash == replay(ticks).ledger_root_hash

    def test_latency_override_unfillable_arb(self):
        """Book moved by execution time: replay shows the loss, honestly."""
        moved_b_no = make_book(VENUE_B, MARKET_B, NO, bids=[("0.50", 100)], asks=[("0.53", 100)])
        tick = PairTick(
            side_a(), side_b(),
            exec_books=(side_a().yes, moved_b_no),
        )
        ledger = EvidenceLedger()
        report = replay_with_ledger(ledger, [tick])
        assert report.executions == 1
        assert report.clean_hedged == 0
        assert report.degraded == 1
        assert report.total_net_pnl == Decimal("-3.000000")
        assert ledger.verify()


def replay_with_ledger(ledger, ticks):
    return run_replay(
        ticks, make_mapping(), ZERO, ZERO, TAKER, TAKER,
        size=Decimal(100), max_staleness=timedelta(seconds=60), ledger=ledger,
    )
