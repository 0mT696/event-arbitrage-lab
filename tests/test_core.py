from decimal import Decimal

from event_arbitrage_lab.core import Level, executable_vwap, net_edge


def test_vwap_uses_depth():
    levels = [Level(Decimal("0.48"), Decimal("2")), Level(Decimal("0.50"), Decimal("3"))]
    assert executable_vwap(levels, Decimal("3")) == Decimal("0.4866666666666666666666666667")


def test_net_edge_subtracts_costs():
    assert net_edge(Decimal("1"), Decimal("0.48"), Decimal("0.49"), Decimal("0.01"), Decimal("0.005")) == Decimal("0.015")


def test_vwap_rejects_insufficient_depth():
    levels = [Level(Decimal("0.48"), Decimal("1"))]
    assert executable_vwap(levels, Decimal("2")) is None
