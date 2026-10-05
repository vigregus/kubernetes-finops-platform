"""G4: свой coturn на живом кластере (`CALL-004`, `CALL-011b`).

Проверяется то, ради чего coturn вообще нужен, и то, чем он опасен — настоящим
клиентом TURN по UDP, без библиотек (формат сообщений RFC 8656/5389 собран здесь):

 * учётные данные, выданные `CoturnProvider` (HMAC от общего секрета), принимает
   сам сервер — выделение релея получается, адрес в диапазоне портов из манифеста;
 * чужой пароль и **просроченный** срок отвергнуты (`401`);
 * **данные идут через релей**: два клиента, байты A → релей → B и обратно, по
   Send/Data Indication и по каналу (ChannelBind/ChannelData), сверка побайтно;
 * релей **не пускает во внутреннюю сеть**: разрешение на адрес пода или
   локальной сети отвергнуто (`403`), на публичный — выдано.

Последнее — главная проверка безопасности: TURN пересылает пакеты на адрес,
который назвал клиент, и без запрета частных диапазонов любой вошедший открывал
бы себе путь к подам кластера.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import socket
import struct
import sys
import time

from messenger.adapters.turn import CoturnProvider
from typing_check import check, failures

MAGIC = 0x2112A442
REALM = "turn.finops.local"
RELAY_PORTS = range(30500, 30520)

ALLOCATE, ALLOCATE_OK, ALLOCATE_ERR = 0x0003, 0x0103, 0x0113
PERMISSION, PERMISSION_OK, PERMISSION_ERR = 0x0008, 0x0108, 0x0118
CHANNEL_BIND, CHANNEL_BIND_OK = 0x0009, 0x0109
SEND_INDICATION, DATA_INDICATION = 0x0016, 0x0017


def attribute(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value + b"\0" * (-len(value) % 4)


def message(kind: int, attributes: list[bytes], key: bytes | None = None) -> bytes:
    transaction = secrets.token_bytes(12)
    body = b"".join(attributes)
    if key is not None:
        # Длина в заголовке включает сам MESSAGE-INTEGRITY (24 байта): подпись
        # считается по сообщению, каким оно будет, а не каким было.
        header = struct.pack("!HHI12s", kind, len(body) + 24, MAGIC, transaction)
        body += attribute(0x0008, hmac.new(key, header + body, hashlib.sha1).digest())
    return struct.pack("!HHI12s", kind, len(body), MAGIC, transaction) + body


def parse(data: bytes) -> tuple[int, dict[int, bytes]]:
    kind, length, _, _ = struct.unpack("!HHI12s", data[:20])
    attributes: dict[int, bytes] = {}
    offset = 20
    while offset < 20 + length:
        attribute_type, size = struct.unpack("!HH", data[offset:offset + 4])
        attributes[attribute_type] = data[offset + 4:offset + 4 + size]
        offset += 4 + size + (-size % 4)
    return kind, attributes


def error_code(attributes: dict[int, bytes]) -> int:
    raw = attributes.get(0x0009, b"\0\0\0\0")
    return (raw[2] & 0x07) * 100 + raw[3]


def xor_address(address: str, port: int) -> bytes:
    packed = socket.inet_aton(address)
    xored = struct.pack("!I", struct.unpack("!I", packed)[0] ^ MAGIC)
    return b"\0\x01" + struct.pack("!H", port ^ (MAGIC >> 16)) + xored


def decode_xor_address(raw: bytes) -> tuple[str, int]:
    port = struct.unpack("!H", raw[2:4])[0] ^ (MAGIC >> 16)
    address = socket.inet_ntoa(struct.pack("!I", struct.unpack("!I", raw[4:8])[0] ^ MAGIC))
    return address, port


class TurnClient:
    def __init__(self, host: str, port: int = 3478) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.target = (socket.gethostbyname(host), port)
        self.realm = REALM
        self.nonce = b""

    def ask(self, payload: bytes) -> tuple[int, dict[int, bytes]]:
        self.sock.sendto(payload, self.target)
        data, _ = self.sock.recvfrom(2048)
        return parse(data)

    def authorize(self, username: str, password: str) -> bytes:
        key = hashlib.md5(f"{username}:{self.realm}:{password}".encode()).digest()  # noqa: S324 - схема RFC 5389
        self._identity = [
            attribute(0x0006, username.encode()),
            attribute(0x0014, self.realm.encode()),
            attribute(0x0015, self.nonce),
        ]
        return key

    def challenge(self) -> None:
        """Первый запрос без учётных данных: сервер отвечает `401` с realm и nonce."""
        kind, attributes = self.ask(message(ALLOCATE, [attribute(0x0019, b"\x11\0\0\0")]))
        if kind == ALLOCATE_ERR and 0x0015 in attributes:
            self.nonce = attributes[0x0015]
            self.realm = attributes.get(0x0014, REALM.encode()).decode()

    def allocate(self, username: str, password: str) -> tuple[int, dict[int, bytes]]:
        key = self.authorize(username, password)
        request = [attribute(0x0019, b"\x11\0\0\0"), *self._identity]
        return self.ask(message(ALLOCATE, request, key))

    def permit(self, username: str, password: str, address: str) -> tuple[int, dict[int, bytes]]:
        key = self.authorize(username, password)
        request = [attribute(0x0012, xor_address(address, 0)), *self._identity]
        return self.ask(message(PERMISSION, request, key))


    def send(self, peer: tuple[str, int], data: bytes) -> None:
        """Send Indication: «отправь это пиру через мой релей» (без ответа)."""
        payload = message(SEND_INDICATION, [attribute(0x0012, xor_address(*peer)),
                                            attribute(0x0013, data)])
        self.sock.sendto(payload, self.target)

    def bind_channel(self, username: str, password: str, number: int,
                     peer: tuple[str, int]) -> tuple[int, dict[int, bytes]]:
        key = self.authorize(username, password)
        request = [attribute(0x000C, struct.pack("!HH", number, 0)),
                   attribute(0x0012, xor_address(*peer)), *self._identity]
        return self.ask(message(CHANNEL_BIND, request, key))

    def send_channel(self, number: int, data: bytes) -> None:
        """ChannelData: короткая форма отправки по привязанному каналу."""
        self.sock.sendto(struct.pack("!HH", number, len(data)) + data, self.target)

    def receive(self) -> tuple[tuple[str, int], bytes] | None:
        """Ждёт Data Indication: «пир прислал тебе это». `None` — не дождались."""
        try:
            while True:
                data, _ = self.sock.recvfrom(2048)
                if len(data) >= 20 and parse(data)[0] == DATA_INDICATION:
                    _, attributes = parse(data)
                    return decode_xor_address(attributes[0x0012]), attributes[0x0013]
        except TimeoutError:
            return None


def credentials(secret: str, *, ttl: int = 600, subject: str | None = None) -> tuple[str, str]:
    provider = CoturnProvider(secret=secret, urls=("turn:x",))
    server = asyncio.run(provider.ice_servers(ttl_seconds=ttl, subject=subject))[0]
    return server.username or "", server.credential or ""


def allocated(host: str, secret: str, subject: str):
    """Клиент TURN с выделением: (клиент, имя, пароль, релейный адрес) или `None`."""
    username, password = credentials(secret, subject=subject)
    client = TurnClient(host)
    client.challenge()
    kind, attributes = client.allocate(username, password)
    if kind != ALLOCATE_OK or 0x0016 not in attributes:
        return None
    return client, username, password, decode_xor_address(attributes[0x0016])


def relay_traffic(host: str, secret: str) -> None:
    """**Данные идут** через релей между двумя клиентами — не только выделение.

    Выделение и разрешение доказывают, что сервер принял данные, но не что он
    пересылает хоть байт: сломанный релей прошёл бы такие проверки. Здесь
    клиент A через свой релей отправляет байты клиенту B (и обратно), и они
    сверяются побайтно.
    """
    a = allocated(host, secret, f"relay-a-{secrets.token_hex(4)}")
    b = allocated(host, secret, f"relay-b-{secrets.token_hex(4)}")
    check("два клиента получили свои релейные адреса", a is not None and b is not None)
    if a is None or b is None:
        return
    a_client, a_user, a_pass, a_relay = a
    b_client, b_user, b_pass, b_relay = b
    check("у клиентов разные релейные порты", a_relay[1] != b_relay[1], f"{a_relay} {b_relay}")

    # Разрешение — на адрес **релея пира**: пакет от него придёт с адреса релея.
    kind_a, attr_a = a_client.permit(a_user, a_pass, b_relay[0])
    kind_b, attr_b = b_client.permit(b_user, b_pass, a_relay[0])
    ok = kind_a == PERMISSION_OK and kind_b == PERMISSION_OK
    check("разрешения между релеями выданы (стенд: TURN_ALLOW_SELF_PEER)", ok,
          f"A {kind_a:#06x}/{error_code(attr_a)}, B {kind_b:#06x}/{error_code(attr_b)}: "
          "если 403 — релей назван частным адресом, и без внешнего адреса пира не разрешить")
    if not ok:
        return

    for label, sender, target_relay, receiver, sender_relay, payload in (
        ("A → релей → B", a_client, b_relay, b_client, a_relay, secrets.token_bytes(48)),
        ("B → релей → A", b_client, a_relay, a_client, b_relay, secrets.token_bytes(48)),
    ):
        sender.send(target_relay, payload)
        got = receiver.receive()
        check(f"данные {label} дошли побайтно (Send → Data Indication)",
              got is not None and got[1] == payload, f"получено: {got}")
        check(f"{label}: пир в Data Indication — релей отправителя",
              got is not None and got[0] == sender_relay, f"получено: {got}")

    # Канал (ChannelBind + ChannelData) — то, чем пользуется браузер вместо Send.
    kind, attributes = a_client.bind_channel(a_user, a_pass, 0x4000, b_relay)
    check("канал привязан (ChannelBind)", kind == CHANNEL_BIND_OK,
          f"тип {kind:#06x}, код {error_code(attributes)}")
    if kind == CHANNEL_BIND_OK:
        payload = secrets.token_bytes(1000)
        a_client.send_channel(0x4000, payload)
        got = b_client.receive()
        check("данные по каналу (ChannelData, 1000 байт) дошли побайтно",
              got is not None and got[1] == payload, f"получено: {None if got is None else len(got[1])}")


def run() -> None:
    secret = os.environ.get("TURN_COTURN_SECRET", "")
    host = os.environ.get("TURN_ADDRESS", "messenger-turn.messenger.svc.cluster.local")
    check("секрет TURN доступен проверке", bool(secret))
    if not secret:
        return

    # --- учётные данные от CoturnProvider принимает сам сервер -----------------------
    username, password = credentials(secret)
    client = TurnClient(host)
    client.challenge()
    check("сервер отвечает на запрос без учётных данных вызовом (401 с nonce)", bool(client.nonce))
    kind, attributes = client.allocate(username, password)
    check("CALL-004: выделение релея по данным CoturnProvider получено",
          kind == ALLOCATE_OK, f"тип {kind:#06x}, код {error_code(attributes)}")
    if kind == ALLOCATE_OK and 0x0016 in attributes:
        address, port = decode_xor_address(attributes[0x0016])
        check("релейный порт — из диапазона манифеста", port in RELAY_PORTS, f"{address}:{port}")

        # --- релей не пускает во внутреннюю сеть ---------------------------------------
        for target, label in (
            ("10.0.0.1", "адрес пода кластера (10/8)"),
            ("192.168.1.10", "локальная сеть (192.168/16)"),
            ("172.16.5.5", "частный диапазон 172.16/12"),
            ("169.254.169.254", "метаданные облака (link-local)"),
            ("127.0.0.1", "loopback"),
        ):
            kind, attributes = client.permit(username, password, target)
            check(f"SEC: разрешение на {label} отвергнуто",
                  kind == PERMISSION_ERR and error_code(attributes) == 403,
                  f"тип {kind:#06x}, код {error_code(attributes)}")
        kind, attributes = client.permit(username, password, "8.8.8.8")
        check("разрешение на публичный адрес выдано", kind == PERMISSION_OK,
              f"тип {kind:#06x}, код {error_code(attributes)}")

    # --- данные идут через релей: A ↔ B -------------------------------------------------
    relay_traffic(host, secret)

    # --- чужой пароль и просроченный срок -------------------------------------------------
    other = TurnClient(host)
    other.challenge()
    kind, attributes = other.allocate(username, "не-тот-пароль")
    check("чужой пароль отвергнут (401)", kind == ALLOCATE_ERR and error_code(attributes) == 401,
          f"тип {kind:#06x}, код {error_code(attributes)}")

    expired_user = f"{int(time.time()) - 60}:просрочено"
    expired_pass = __import__("base64").b64encode(
        hmac.new(secret.encode(), expired_user.encode(), hashlib.sha1).digest()).decode()
    late = TurnClient(host)
    late.challenge()
    kind, attributes = late.allocate(expired_user, expired_pass)
    check("просроченные данные с верной подписью отвергнуты (401)",
          kind == ALLOCATE_ERR and error_code(attributes) == 401,
          f"тип {kind:#06x}, код {error_code(attributes)}")

    # --- секрет другого сервиса не подходит ---------------------------------------------------
    forged_user, forged_pass = credentials("чужой-секрет")
    forged = TurnClient(host)
    forged.challenge()
    kind, attributes = forged.allocate(forged_user, forged_pass)
    check("подпись чужим секретом отвергнута (401)",
          kind == ALLOCATE_ERR and error_code(attributes) == 401,
          f"тип {kind:#06x}, код {error_code(attributes)}")


def main() -> int:
    run()
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nTURN: данные от API принимаются, чужие и просроченные — нет, во внутреннюю сеть не пускает")
    return 0


if __name__ == "__main__":
    sys.exit(main())
