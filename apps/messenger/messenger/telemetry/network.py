"""Грубая сетевая размерность для метрик звонков (`RES-009`).

Из сырых данных клиента (путь, транспорт) нельзя сказать «в мобильных сетях РФ», а
IP в метку ставить нельзя (высокая кардинальность, персональные данные). Поэтому
сервер вычисляет и пишет **только низкокардинальный агрегат**: страну и класс сети.
Сам адрес не сохраняется и не попадает ни в метрики, ни в журнал этого модуля.

Откуда берётся значение. Базы GeoIP и классов автономных систем в проекте нет, и
подделывать её нельзя: страну и класс сети **сообщает входной шлюз** заголовками
`X-Client-Country` и `X-Client-Network-Class`, когда у него есть такие данные
(Cloudflare, провайдер GeoIP на ingress). Нет заголовка — `unknown`. Значение
всегда проходит закрытый набор, поэтому подделка клиентом меняет только метку своей
же серии, но не создаёт новых.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

UNKNOWN = "unknown"
OTHER = "other"
CLASSES = ("mobile", "fixed", UNKNOWN)

# Страны, которые различаем в метках; остальные — `other`. Закрытый список держит число
# серий малым, а состав задаётся настройкой (`NETWORK_COUNTRIES`).
_DEFAULT_COUNTRIES = (
    "RU,BY,KZ,UA,UZ,AM,GE,AZ,KG,TJ,MD,TR,DE,NL,FI,US,GB,FR,ES,PL,LT,LV,EE,CZ,RS,IL,AE,CN,IN,JP"
)
_CODE = re.compile(r"^[A-Za-z]{2}$")


def _countries() -> frozenset[str]:
    raw = os.getenv("NETWORK_COUNTRIES", _DEFAULT_COUNTRIES)
    return frozenset(item.strip().upper() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class NetworkContext:
    country: str = UNKNOWN
    net_class: str = UNKNOWN


def from_headers(headers: Mapping[str, str]) -> NetworkContext:
    raw_country = (headers.get("x-client-country") or "").strip()
    country = UNKNOWN
    if _CODE.match(raw_country):
        code = raw_country.upper()
        country = code if code in _countries() else OTHER
    raw_class = (headers.get("x-client-network-class") or "").strip().lower()
    net_class = raw_class if raw_class in CLASSES else UNKNOWN
    return NetworkContext(country=country, net_class=net_class)
