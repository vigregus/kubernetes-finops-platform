"""Проверка токена против настоящего Keycloak.

Модульные тесты подписывают токен ключом, который сами же и создали, —
это проверяет нашу логику, но не то, что она сходится с реалмом. Здесь
токен выдаёт сам Keycloak, ключи читаются из реалма, а издатель и
аудитория сверяются с тем, что в токене действительно написано.

Учётные данные нигде не печатаются и не передаются аргументами команды:
аргументы видны в списке процессов контейнера.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import jwt

from messenger.adapters import oidc
from messenger.domain.identity import TokenRejection

failures: list[str] = []

KEYCLOAK = os.getenv(
    "KEYCLOAK_URL", "http://messenger-idp-service.keycloak.svc.cluster.local:8080"
)
REALM = os.getenv("KEYCLOAK_REALM", "messenger")
CLIENT = os.getenv("KEYCLOAK_CLIENT", "messenger-api")


def ok(what: str) -> None:
    print(f"  \033[32m✓\033[0m {what}")


def bad(what: str, detail: str = "") -> None:
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def check(what: str, condition: bool, detail: str = "") -> None:
    ok(what) if condition else bad(what, detail)


async def admin_token(http: httpx.AsyncClient) -> str:
    """Токен администратора из master. Нужен только чтобы прочитать секрет."""
    response = await http.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.environ["KEYCLOAK_ADMIN"],
            "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def client_secret(http: httpx.AsyncClient, token: str) -> str:
    """Секрет ресурсного сервера. Выдал его Keycloak, в git он не попадает."""
    headers = {"Authorization": f"Bearer {token}"}
    found = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
        params={"clientId": CLIENT},
        headers=headers,
    )
    found.raise_for_status()
    entries = found.json()
    if not entries:
        raise RuntimeError(f"клиента {CLIENT} нет в реалме {REALM}")
    internal_id = entries[0]["id"]

    secret = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients/{internal_id}/client-secret",
        headers=headers,
    )
    secret.raise_for_status()
    return secret.json()["value"]


async def service_token(http: httpx.AsyncClient, secret: str) -> str:
    """Токен служебной учётной записи реалма. Настоящий, подписанный."""
    response = await http.post(
        f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": CLIENT, "client_secret": secret},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def settings(**overrides) -> oidc.OidcSettings:
    base = {
        "issuer": os.environ["OIDC_ISSUER"],
        "jwks_url": os.environ["OIDC_JWKS_URL"],
        "audience": os.getenv("OIDC_AUDIENCE", "messenger-api"),
    }
    base.update(overrides)
    return oidc.OidcSettings(**base)


async def run() -> None:
    async with httpx.AsyncClient(timeout=10.0) as http:
        token = await service_token(http, await client_secret(http, await admin_token(http)))

    payload = jwt.decode(token, options={"verify_signature": False})
    check(
        "издатель в токене совпадает с настроенным",
        payload["iss"] == os.environ["OIDC_ISSUER"],
        f"в токене {payload['iss']!r}, настроен {os.environ['OIDC_ISSUER']!r}",
    )
    audience = payload.get("aud")
    audience = audience if isinstance(audience, list) else [audience]
    check(
        "ресурсный сервер назван в аудитории",
        os.getenv("OIDC_AUDIENCE", "messenger-api") in audience,
        f"в токене aud={audience}",
    )

    # Ключи берутся из реалма по внутреннему адресу - тем же путём,
    # каким за ними пойдёт API.
    conf = settings()
    keys = oidc.JwksCache(settings=conf)
    result = await oidc.verify_access_token(token, keys=keys, settings=conf)
    check("настоящий токен принят", result.ok, str(result.rejection))
    check("ключи реалма прочитаны", keys.has_keys)

    # Отказы. Каждый - отдельная причина, и различать их обязательно:
    # всплеск одного означает ротацию ключей, другого - сломанное
    # обновление токена у клиентов.
    чужая = settings(audience="другое-приложение")
    result = await oidc.verify_access_token(token, keys=oidc.JwksCache(settings=чужая), settings=чужая)
    check(
        "токен не принят чужим ресурсным сервером",
        result.rejection is TokenRejection.WRONG_AUDIENCE,
        str(result.rejection),
    )

    чужой = settings(issuer="https://idp.чужой.local/realms/messenger")
    result = await oidc.verify_access_token(token, keys=oidc.JwksCache(settings=чужой), settings=чужой)
    check(
        "токен от другого издателя не принят",
        result.rejection is TokenRejection.WRONG_ISSUER,
        str(result.rejection),
    )

    # Подпись портится заменой одного знака в третьем сегменте.
    head, body, signature = token.split(".")
    подделка = f"{head}.{body}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    result = await oidc.verify_access_token(
        подделка, keys=oidc.JwksCache(settings=conf), settings=conf
    )
    check(
        "испорченная подпись не принята",
        result.rejection is TokenRejection.BAD_SIGNATURE,
        str(result.rejection),
    )


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nпроверка токена подтверждена против живого Keycloak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
