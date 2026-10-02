"""Проверка вредоносности: интерфейс и подставной сканер.

В этом срезе настоящего антивируса нет, и это названо вслух
(`IMPLEMENTATION-PLAN.md`, бэклог G4): на одном узле, уже упирающемся в CPU,
ClamAV с его гигабайтом на сигнатуры — отдельное решение, а не побочный
эффект загрузки картинок. Интерфейс (`domain.attachment.Scanner`) при этом
настоящий: подключение ClamAV — новая реализация, не правка конвейера.

Подставной сканер не «всегда чисто»: он узнаёт тестовый файл EICAR, общий для
всех антивирусов, — так `ATT-002` проверяется живым путём от загрузки до
`rejected`, а не только юнитом. Строка собрана из частей, чтобы файл с
исходниками сам не срабатывал на антивирусе разработчика.
"""
from __future__ import annotations

import os

from messenger.domain.attachment import Scanner, ScanVerdict

_EICAR = (
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR" + "-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
).encode()


class StubScanner:
    async def scan(self, content: bytes) -> ScanVerdict:
        return ScanVerdict.INFECTED if _EICAR in content else ScanVerdict.CLEAN


class UnavailableScanner:
    """Сканер, который не отвечает, — для проверки `ATT-008` без поломки кластера."""

    async def scan(self, content: bytes) -> ScanVerdict:
        return ScanVerdict.UNAVAILABLE


def scanner_from_env() -> Scanner:
    mode = os.getenv("ATTACHMENT_SCANNER", "stub")
    if mode == "unavailable":
        return UnavailableScanner()
    return StubScanner()
