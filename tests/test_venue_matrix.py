"""N1B: venue capability matrix must conform to the N1D schema.

Fail-Closed: unknowns stay null (the schema's None), never guessed.
"""
import json
from pathlib import Path

import pytest

from event_arbitrage_lab.adapter import VenueCapability

MATRIX_PATH = Path(__file__).parent.parent / "research" / "venue_capabilities.json"


@pytest.fixture(scope="module")
def rows():
    with MATRIX_PATH.open() as f:
        data = json.load(f)
    assert isinstance(data, list) and data, "matrix must be a non-empty list"
    return data


class TestVenueCapabilityMatrix:
    def test_every_row_validates_against_schema(self, rows):
        for row in rows:
            cap = VenueCapability(**row)
            assert cap.venue.strip()

    def test_expected_candidates_present(self, rows):
        venues = {r["venue"] for r in rows}
        assert {"polymarket", "kalshi", "limitless", "manifold", "sx_bet"} <= venues

    def test_unknowns_are_explicit_null_not_missing(self, rows):
        for row in rows:
            for field in ("rest_api", "websocket", "orderbook_data", "historical_data"):
                assert field in row, f"{row['venue']}: {field} missing (unknown must be null)"

    def test_free_text_fields_carry_sources_or_unknown(self, rows):
        for row in rows:
            for field in ("fee_structure", "settlement_rules", "rate_limits"):
                assert row[field].strip(), f"{row['venue']}: {field} empty"
