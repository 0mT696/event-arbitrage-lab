"""N5 adversarial tests: attack replay wiring — exec-book identity,
degraded classification, cross-instance determinism, anchored evidence."""
from datetime import timedelta
from decimal import Decimal

import pytest

from event_arbitrage_lab.execution import TakerFillPolicy
from event_arbitrage_lab.fees import ZeroFee
from event_arbitrage_lab.ledger import EvidenceLedger
from event_arbitrage_lab.replay import (
    PairTick,
    load_ticks,
    run_replay,
    save_ticks,
)
from event_arbitrage_lab.scanner import BinaryMarketBooks

from conftest import (
    MARKET_A,
    MARKET_B,
    NO,
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


def replay(ticks, ledger=None):
    return run_replay(
        ticks, make_mapping(), ZERO, ZERO, TAKER, TAKER,
        size=Decimal(100), max_staleness=timedelta(seconds=60), ledger=ledger,
    )


class TestExecBookIdentity:
    """Latency override must match books by leg identity. Positional trust
    lets a swapped or foreign book be executed as if it were the leg."""

    def test_swapped_exec_books_still_match_correct_legs(self):
        yes_a_exec = side_a().yes
        no_b_moved = make_book(VENUE_B, MARKET_B, NO,
                               bids=[("0.50", 100)], asks=[("0.53", 100)])
        # deliberately swapped order in the tuple
        tick = PairTick(side_a(), side_b(), exec_books=(no_b_moved, yes_a_exec))
        report = replay([tick])
        assert report.executions == 1
        # leg B saw the MOVED book (0.53 > limit 0.52): refused, unwound
        assert report.total_net_pnl == Decimal("-3.000000")

    def test_foreign_exec_book_rejected_fail_closed(self):
        # exec override carries venue_b YES book, but the leg needs NO:
        # identity mismatch -> skip, never execute against the wrong book
        foreign = make_book(VENUE_B, MARKET_B, YES,
                            bids=[("0.44", 100)], asks=[("0.45", 100)])
        tick = PairTick(side_a(), side_b(), exec_books=(side_a().yes, foreign))
        ledger = EvidenceLedger()
        report = replay([tick], ledger=ledger)
        assert report.executions == 0
        skips = [r for r in ledger.records if r.kind == "EXECUTION_SKIPPED"]
        assert skips and "do not match leg identities" in skips[0].payload_json
        assert not [r for r in ledger.records if r.kind == "EXECUTION"]


class TestDegradedClassification:
    def test_unwind_loss_is_degraded_even_when_fully_unwound(self):
        # leg B partial at exec time (60/100); residual unwound at a loss.
        # residual==0 + paired>0 used to count as "clean" — wrong.
        yes_a_exec = side_a().yes
        no_b_partial = make_book(VENUE_B, MARKET_B, NO,
                                 bids=[("0.46", 100)], asks=[("0.49", 60)])
        tick = PairTick(side_a(), side_b(), exec_books=(yes_a_exec, no_b_partial))
        report = replay([tick])
        assert report.executions == 1
        assert report.clean_hedged == 0
        assert report.degraded == 1
        # 60 hedged x 0.03 minus 40 unwound x 0.03 = +0.60
        assert report.total_net_pnl == Decimal("0.600000")


class TestReplayFramePersistence:
    """Verifier N5-BLOCKER-001: the COMPLETE replay input (scan books +
    latency override) must persist and reproduce identical evidence."""

    def latency_ticks(self):
        moved_no_b = make_book(VENUE_B, MARKET_B, NO,
                               bids=[("0.50", 100)], asks=[("0.53", 100)])
        return [
            PairTick(side_a(), side_b(),
                     exec_books=(side_a().yes, moved_no_b)),
            PairTick(side_a(), side_b()),  # zero-latency tick
        ]

    def test_roundtrip_preserves_ticks_exactly(self, tmp_path):
        ticks = self.latency_ticks()
        p = tmp_path / "frames.jsonl"
        assert save_ticks(ticks, p) == 2
        loaded = load_ticks(p)
        assert loaded == ticks  # exec_books included, byte-exact

    def test_persisted_latency_replay_identical_evidence(self, tmp_path):
        ticks = self.latency_ticks()
        p = tmp_path / "frames.jsonl"
        save_ticks(ticks, p)

        la, lb = EvidenceLedger(), EvidenceLedger()
        ra = replay(ticks, ledger=la)
        rb = replay(load_ticks(p), ledger=lb)
        assert ra == rb  # identical report incl. per-execution PnL
        assert ra.ledger_root_hash == rb.ledger_root_hash
        assert la.records == lb.records

    def test_unknown_frame_version_fails_closed(self, tmp_path):
        p = tmp_path / "frames.jsonl"
        save_ticks(self.latency_ticks(), p)
        lines = p.read_text().splitlines()
        lines[0] = lines[0].replace('"v": 1', '"v": 999')
        p.write_text("\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="version"):
            load_ticks(p)


class TestExecutionProvenance:
    """Evidence must explain WHICH books produced each fill/reject/unwind."""

    def test_execution_snapshot_records_moved_book(self):
        moved_no_b = make_book(VENUE_B, MARKET_B, NO,
                               bids=[("0.50", 100)], asks=[("0.53", 100)])
        tick = PairTick(side_a(), side_b(),
                        exec_books=(side_a().yes, moved_no_b))
        ledger = EvidenceLedger()
        replay([tick], ledger=ledger)
        kinds = [r.kind for r in ledger.records]
        assert kinds == [
            "SCAN_SNAPSHOT",
            "OPPORTUNITY",
            "EXECUTION_SNAPSHOT",
            "EXECUTION",
        ]
        snap = next(r for r in ledger.records if r.kind == "EXECUTION_SNAPSHOT")
        # the moved 0.53 ask is in the evidence — that is WHY leg B refused
        assert '"0.53"' in snap.payload_json
        assert '"latency_override":true' in snap.payload_json
        # scan-time books recorded separately
        scan = next(r for r in ledger.records if r.kind == "SCAN_SNAPSHOT")
        assert '"0.49"' in scan.payload_json  # original pre-latency price

    def test_zero_latency_marks_override_false(self):
        ledger = EvidenceLedger()
        replay([PairTick(side_a(), side_b())], ledger=ledger)
        snap = next(r for r in ledger.records if r.kind == "EXECUTION_SNAPSHOT")
        assert '"latency_override":false' in snap.payload_json


class TestCrossInstanceDeterminism:
    def test_full_record_streams_identical(self):
        ticks = [PairTick(side_a(), side_b()), PairTick(side_a(), side_b())]
        la, lb = EvidenceLedger(), EvidenceLedger()
        ra, rb = replay(ticks, ledger=la), replay(ticks, ledger=lb)
        assert la.records == lb.records
        assert ra == rb
        assert ra.ledger_root_hash == la.root_hash

    def test_replay_jsonl_roundtrip_anchored(self, tmp_path):
        ledger = EvidenceLedger()
        report = replay([PairTick(side_a(), side_b())], ledger=ledger)
        p = tmp_path / "evidence.jsonl"
        ledger.save(p)
        loaded = EvidenceLedger.load(p)
        assert loaded.verify_root(report.ledger_root_hash)
