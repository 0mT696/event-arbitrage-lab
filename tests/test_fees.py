"""Fee model tests — official vectors from Polymarket docs.

Source: docs.polymarket.com/trading/fees.md (fetched 2026-10-06):
    fee = C x feeRate x p x (1 - p), rounded to 5 decimal places
    (smallest fee 0.00001 USDC). Fee tables below are per 100 shares.
"""
from decimal import Decimal

from event_arbitrage_lab.fees import QuadraticTakerFee, ZeroFee


class TestOfficialVectors:
    """Values straight from the official per-100-shares fee tables."""

    def test_crypto_peak(self):
        assert QuadraticTakerFee(Decimal("0.07")).fee(
            Decimal(100), Decimal("0.50")
        ) == Decimal("1.75")

    def test_crypto_off_peak(self):
        assert QuadraticTakerFee(Decimal("0.07")).fee(
            Decimal(100), Decimal("0.30")
        ) == Decimal("1.47")

    def test_crypto_symmetry(self):
        model = QuadraticTakerFee(Decimal("0.07"))
        assert model.fee(Decimal(100), Decimal("0.30")) == model.fee(
            Decimal(100), Decimal("0.70")
        )

    def test_sports_peak(self):
        assert QuadraticTakerFee(Decimal("0.05")).fee(
            Decimal(100), Decimal("0.50")
        ) == Decimal("1.25")

    def test_finance_politics_tier(self):
        assert QuadraticTakerFee(Decimal("0.04")).fee(
            Decimal(100), Decimal("0.05")
        ) == Decimal("0.19")


class TestRoundingGranularity:
    """5-decimal granularity (not cent-ceiling). We ceil at that quantum —
    deliberate conservative deviation from the docs' round-to-nearest."""

    def test_small_trade_not_overcharged_to_cent(self):
        # 1 share @ 0.01, rate 0.04: raw = 0.000396 -> 0.0004
        # (cent-ceiling would charge 0.01: 25x overcharge)
        assert QuadraticTakerFee(Decimal("0.04")).fee(
            Decimal(1), Decimal("0.01")
        ) == Decimal("0.0004")

    def test_below_quantum_ceils_to_minimum(self):
        # raw = 0.04 * 0.0001 * 0.25 = 1e-8 -> docs round to zero;
        # we ceil to 0.00001 (conservative, bias <= 1e-5 USDC)
        assert QuadraticTakerFee(Decimal("0.04")).fee(
            Decimal("0.0001"), Decimal("0.5")
        ) == Decimal("0.00001")

    def test_exact_quantum_multiple_unchanged(self):
        assert QuadraticTakerFee(Decimal("0.07")).fee(
            Decimal(100), Decimal("0.05")
        ) == Decimal("0.3325")


class TestZeroFee:
    def test_always_zero(self):
        assert ZeroFee().fee(Decimal(1000), Decimal("0.5")) == Decimal(0)
