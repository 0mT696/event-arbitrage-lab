"""N5 adversarial tests: attack the evidence ledger like an auditor with
write access to the JSONL file. verify() proves internal consistency;
verify_root() adds the external anchor that catches truncation and
full-rehash history rewriting."""
import json
from datetime import datetime, timezone
from decimal import Decimal

from event_arbitrage_lab.ledger import (
    GENESIS_HASH,
    EvidenceLedger,
    LedgerRecord,
    canonical_json,
)


def make_ledger(n: int = 4) -> EvidenceLedger:
    led = EvidenceLedger()
    for i in range(n):
        led.append(
            "FILL",
            {
                "tick": i,
                "price": Decimal("0.48") + Decimal(i) / 100,
                "ts": datetime(2026, 10, 6, 12, i, 0, tzinfo=timezone.utc),
            },
        )
    return led


def rehash_all(records: list[LedgerRecord]) -> list[LedgerRecord]:
    """Attacker capability: rewrite every hash after editing history."""
    out = []
    prev = GENESIS_HASH
    for i, r in enumerate(records):
        h = LedgerRecord.compute_hash(i, r.kind, r.payload_json, prev)
        out.append(
            LedgerRecord(seq=i, kind=r.kind, payload_json=r.payload_json,
                         prev_hash=prev, hash=h)
        )
        prev = h
    return out


class TestInternalConsistency:
    def test_middle_record_deletion_breaks_chain(self):
        led = make_ledger()
        recs = list(led.records)
        del recs[2]
        assert not EvidenceLedger(recs).verify()

    def test_payload_bit_flip_breaks_chain(self):
        led = make_ledger()
        recs = list(led.records)
        r = recs[1]
        recs[1] = LedgerRecord(seq=r.seq, kind=r.kind,
                               payload_json=r.payload_json.replace("0.49", "0.01"),
                               prev_hash=r.prev_hash, hash=r.hash)
        assert not EvidenceLedger(recs).verify()

    def test_genesis_anchor_enforced(self):
        led = make_ledger()
        recs = list(led.records)
        r = recs[0]
        recs[0] = LedgerRecord(seq=r.seq, kind=r.kind, payload_json=r.payload_json,
                               prev_hash="f" * 64, hash=r.hash)
        assert not EvidenceLedger(recs).verify()


class TestExternalAnchor:
    """Attacks verify() cannot see alone — caught only via the root hash
    pinned at evidence-production time (e.g. inside ReplayReport)."""

    def test_tail_truncation_caught_by_root_anchor(self):
        led = make_ledger()
        root_at_export = led.root_hash
        truncated = EvidenceLedger(led.records[:2])  # hide the last fills
        assert truncated.verify()  # internally consistent — scope limit
        assert not truncated.verify_root(root_at_export)  # anchor catches it

    def test_full_rehash_caught_by_root_anchor(self):
        led = make_ledger()
        root_at_export = led.root_hash
        recs = list(led.records)
        r = recs[1]
        recs[1] = LedgerRecord(seq=r.seq, kind=r.kind,
                               payload_json=r.payload_json.replace("0.49", "0.01"),
                               prev_hash=r.prev_hash, hash=r.hash)
        forged = EvidenceLedger(rehash_all(recs))
        assert forged.verify()  # self-consistent forgery — scope limit
        assert not forged.verify_root(root_at_export)  # anchor catches it

    def test_forged_append_caught_by_root_anchor(self):
        led = make_ledger()
        root_at_export = led.root_hash
        led.append("FILL", {"tick": 99, "price": Decimal("0.01")})
        assert led.verify()
        assert not led.verify_root(root_at_export)


class TestPersistence:
    def test_tampered_jsonl_detected_after_load(self, tmp_path):
        led = make_ledger()
        p = tmp_path / "evidence.jsonl"
        led.save(p)
        root_at_export = led.root_hash
        lines = p.read_text().splitlines()
        obj = json.loads(lines[1])
        obj["payload_json"] = obj["payload_json"].replace("0.49", "0.01")
        lines[1] = json.dumps(obj)
        p.write_text("\n".join(lines) + "\n")
        loaded = EvidenceLedger.load(p)
        assert not loaded.verify()
        assert not loaded.verify_root(root_at_export)

    def test_save_load_roundtrip_preserves_root(self, tmp_path):
        led = make_ledger()
        p = tmp_path / "evidence.jsonl"
        led.save(p)
        loaded = EvidenceLedger.load(p)
        assert loaded.verify_root(led.root_hash)
        assert loaded.records == led.records


class TestCanonicalDeterminism:
    def test_same_appends_identical_chain_across_instances(self):
        a, b = make_ledger(), make_ledger()
        assert a.records == b.records
        assert a.root_hash == b.root_hash

    def test_hash_independent_of_dict_key_order(self):
        h1 = canonical_json({"b": 1, "a": Decimal("0.5")})
        h2 = canonical_json({"a": Decimal("0.5"), "b": 1})
        assert h1 == h2
