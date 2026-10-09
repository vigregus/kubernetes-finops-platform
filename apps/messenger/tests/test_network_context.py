"""Сетевая размерность метрик: закрытые наборы, IP не участвует (RES-009)."""
from __future__ import annotations

from messenger.telemetry import network


def test_без_заголовков_шлюза_всё_unknown():
    assert network.from_headers({}) == network.NetworkContext("unknown", "unknown")


def test_известная_страна_и_класс_проходят_в_нормальной_форме():
    ctx = network.from_headers({"x-client-country": " ru ", "x-client-network-class": "Mobile"})
    assert (ctx.country, ctx.net_class) == ("RU", "mobile")


def test_неизвестная_страна_сворачивается_в_other_чтобы_не_плодить_серии():
    assert network.from_headers({"x-client-country": "ZZ"}).country == "other"


def test_негодные_значения_не_становятся_метками():
    ctx = network.from_headers(
        {"x-client-country": "10.0.0.1", "x-client-network-class": "satellite"}
    )
    assert (ctx.country, ctx.net_class) == ("unknown", "unknown")


def test_состав_стран_задаётся_настройкой(monkeypatch):
    monkeypatch.setenv("NETWORK_COUNTRIES", "FI")
    assert network.from_headers({"x-client-country": "FI"}).country == "FI"
    assert network.from_headers({"x-client-country": "RU"}).country == "other"
