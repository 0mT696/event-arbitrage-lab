"""N5 ledger tests: hash-chain integrity, tamper detection, persistence,
canonical serialization determinism."""
from decimal import Decimal

from event_arbitrage_lab.ledger import (
    GENESIS_HASH,
    EvidenceLedger,
    LedgerRecord,
    canonical_json,
)


class TestCanonicalJson:
    def test_key_order_irrelevant(self):
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_decimal_as_string(self):
        assert '"0.48"' in canonical_json({"p": Decimal("0.48")})

    def test_compact_separators(self):
        assert canonical_json({"a": [1, 2]}) == '{"a":[1,2]}'


class TestLedger:
    def test_genesis_and_chain(self):
        led = EvidenceLedger()
        r0 = led.append("SNAPSHOT", {"x": 1})
        r1 = led.append("OPPORTUNITY", {"y": Decimal("0.5")})
        assert r0.prev_hash == GENESIS_HASH and r0.seq == 0
        assert r1.prev_hash == r0.hash and r1.seq == 1
        assert led.verify()
        assert led.root_hash == r1.hash

    def test_tampered_payload_breaks_chain(self):
        led = EvidenceLedger()
        led.append("SNAPSHOT", {"x": 1})
        r1 = led.append("EXECUTION", {"pnl": "3.0"})
        tampered = LedgerRecord(
            seq=r1.seq,
            kind=r1.kind,
            payload_json='{"pnl":"999.0"}',  # attacker edits history
            prev_hash=r1.prev_hash,
            hash=r1.hash,
        )
        evil = EvidenceLedger([led.records[0], tampered])
        assert not evil.verify()

    def test_reordering_breaks_chain(self):
        led = EvidenceLedger()
        led.append("A", {"i": 0})
        led.append("B", {"i": 1})
        swapped = EvidenceLedger([led.records[1], led.records[0]])
        assert not swapped.verify()

    def test_save_load_roundtrip(self, tmp_path):
        led = EvidenceLedger()
        led.append("SNAPSHOT", {"x": Decimal("1.5")})
        led.append("RESULT", {"ok": True})
        p = tmp_path / "ledger.jsonl"
        led.save(p)
        loaded = EvidenceLedger.load(p)
        assert loaded.records == led.records
        assert loaded.verify()
        assert loaded.root_hash == led.root_hash

    def test_empty_kind_rejected(self):
        led = EvidenceLedger()
        import pytest

        with pytest.raises(ValueError):
            led.append("", {"x": 1})
