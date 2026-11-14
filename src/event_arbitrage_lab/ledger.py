"""N5: evidence ledger — append-only, hash-chained, JSONL-persisted.

Every snapshot / opportunity / execution / rejection is recorded as a
LedgerRecord whose hash commits to both its payload and the previous
record's hash. Tampering with any historical record breaks the chain;
verify() detects it. Serialization is canonical (sorted keys, Decimal as
str, enums as values) so replaying the same input reproduces identical
hashes — determinism is the audit anchor.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

GENESIS_HASH = "0" * 64


def canon(obj: Any) -> Any:
    """Canonicalize for hashing/persistence: Decimal->str, Enum->value,
    datetime->isoformat, pydantic/dataclass -> plain dicts."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return canon(obj.model_dump(mode="python"))
    if is_dataclass(obj) and not isinstance(obj, type):
        return canon(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canon(v) for v in obj]
    return obj


def canonical_json(payload: Any) -> str:
    return json.dumps(canon(payload), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class LedgerRecord:
    seq: int
    kind: str
    payload_json: str  # canonical JSON of the payload
    prev_hash: str
    hash: str

    @staticmethod
    def compute_hash(seq: int, kind: str, payload_json: str, prev_hash: str) -> str:
        h = hashlib.sha256()
        h.update(prev_hash.encode())
        h.update(b"|")
        h.update(str(seq).encode())
        h.update(b"|")
        h.update(kind.encode())
        h.update(b"|")
        h.update(payload_json.encode())
        return h.hexdigest()


class EvidenceLedger:
    """Append-only hash-chained evidence log."""

    def __init__(self, records: Iterable[LedgerRecord] = ()) -> None:
        self._records = list(records)

    def append(self, kind: str, payload: Any) -> LedgerRecord:
        if not kind or not kind.strip():
            raise ValueError("record kind must be non-empty")
        seq = len(self._records)
        prev = self._records[-1].hash if self._records else GENESIS_HASH
        payload_json = canonical_json(payload)
        rec = LedgerRecord(
            seq=seq,
            kind=kind,
            payload_json=payload_json,
            prev_hash=prev,
            hash=LedgerRecord.compute_hash(seq, kind, payload_json, prev),
        )
        self._records.append(rec)
        return rec

    @property
    def records(self) -> tuple[LedgerRecord, ...]:
        return tuple(self._records)

    @property
    def root_hash(self) -> str:
        return self._records[-1].hash if self._records else GENESIS_HASH

    def verify(self) -> bool:
        """Re-walk the chain; any tampering, reordering or gap returns False.

        Scope note: verify() proves INTERNAL consistency only. Two attacks
        it cannot see on its own — tail truncation (a shortened chain is
        still self-consistent) and full re-hash (rewriting every hash after
        editing a payload). Both are caught by anchoring the root hash
        externally (e.g. the ReplayReport emitted at run time) and using
        verify_root() instead.
        """
        prev = GENESIS_HASH
        for i, rec in enumerate(self._records):
            if rec.seq != i or rec.prev_hash != prev:
                return False
            if rec.hash != LedgerRecord.compute_hash(
                rec.seq, rec.kind, rec.payload_json, rec.prev_hash
            ):
                return False
            prev = rec.hash
        return True

    def verify_root(self, expected_root: str) -> bool:
        """verify() PLUS the external anchor: chain must be internally
        consistent AND end at the root hash captured when the evidence was
        produced. Detects truncation and full-rehash history rewriting."""
        return self.verify() and self.root_hash == expected_root

    def save(self, path: str | Path) -> None:
        lines = [
            json.dumps(canon(asdict(r)), sort_keys=True) for r in self._records
        ]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""))

    @classmethod
    def load(cls, path: str | Path) -> "EvidenceLedger":
        records = [
            LedgerRecord(**json.loads(line))
            for line in Path(path).read_text().splitlines()
            if line.strip()
        ]
        return cls(records)
