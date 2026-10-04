"""G4: вложения на живой базе и живом хранилище, без HTTP-слоя.

Проверяется конвейер, а не коды ответов (формы и коды — юнит-тесты
эндпоинтов): настоящие Postgres и MinIO, настоящие предподписанные ссылки,
настоящий `PUT` браузерным способом (`httpx` по ссылке, без ключей).

`ATT-005`: тип и размер отвергаются **до** выдачи ссылки — ни строки, ни
подписи. `ATT-006`: лимит инициаций. `ATT-003`: изображение проходит
инициацию, загрузку, обработку и уходит сообщением, а скачанные по ссылке
байты равны загруженным. `ATT-007`: исполняемый файл под видом картинки
отклонён **при обработке**, объект удалён. `ATT-002`: тестовый файл EICAR
отклонён, сообщение с ним не создаётся. `ATT-008`: недоступный сканер
оставляет вложение в обработке, и прикрепить его нельзя. `ATT-004`:
голосовое — длительность проверена при инициации, битрейт выведен из
фактического размера, длительность сообщения берётся из вложения, а не от
клиента; заявил малое, загрузил большое — отклонено. `ATT-001`:
неприкреплённое старше срока исчезает вместе с объектом, прикреплённое
не трогается.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import timedelta

import httpx

from messenger.adapters.object_store import ObjectStore, ObjectStoreSettings
from messenger.adapters.ratelimit import LimitDecision
from messenger.adapters.scanner import StubScanner, UnavailableScanner
from messenger.domain.conversation import ConversationType
from messenger.domain.errors import Reason
from messenger.domain.ids import AttachmentId, ClientMessageId, ConversationId
from messenger.domain.message import MessageKind, MessagePayload
from messenger.repositories import attachments as attachments_repo
from messenger.repositories import conversations, users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import attachments as service
from messenger.services import messages as message_service

failures: list[str] = []

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EXE = b"MZ\x90\x00" + b"\x00" * 64
# Голос: 40 000 байт за 10 с — 32 кбит/с, правдоподобный Opus.
WEBM_VOICE = b"\x1a\x45\xdf\xa3" + b"\x00" * (40_000 - 4)
# Тестовая строка антивирусов, собранная из частей — по той же причине, что
# и в `adapters/scanner.py`: исходник сам не должен срабатывать.
EICAR = (
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR" + "-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
).encode()


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def pool_settings() -> PoolSettings:
    return PoolSettings(
        host=os.getenv("DATABASE_HOST", "messenger-db-pool"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "messenger"),
        user=os.getenv("DATABASE_USER", "messenger"),
        password=os.getenv("DATABASE_PASSWORD", ""),
        min_size=1,
        max_size=3,
    )


class CountingLimiter:
    """Счётчик вместо Redis: проверяется решение сервиса, а не сам Redis."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    async def take(self, key, *, limit, window_seconds, on_failure):
        self.used += 1
        if self.used <= self.limit:
            return LimitDecision(allowed=True)
        return LimitDecision(allowed=False, retry_after_seconds=7)


async def upload(http: httpx.AsyncClient, init, content: bytes) -> int:
    """Загрузка так, как её сделал бы браузер: по ссылке, с заголовками из ответа."""
    response = await http.put(init.upload_url, content=content, headers=init.upload_headers)
    return response.status_code


async def new_attachment(
    conn, store, http, user, *, content_type, content, name="f", duration_ms=None
):
    init = await service.init_upload(
        conn,
        store=store,
        limiter=CountingLimiter(10_000),
        user_id=user.user_id,
        content_type=content_type,
        size_bytes=len(content),
        file_name=name,
        duration_ms=duration_ms,
    )
    assert init.ok, init.rejection
    assert await upload(http, init, content) == 200
    return init.attachment.attachment_id


async def run() -> None:
    marker = uuid.uuid4().hex[:8]
    store_settings = ObjectStoreSettings.from_env()
    if store_settings is None:
        print("  S3_* не заданы - проверка вложений пропущена")
        return
    store = ObjectStore(store_settings)
    pool = await create_pool(pool_settings(), application_name="attachments-check")
    http = httpx.AsyncClient(timeout=30.0, verify=False)  # noqa: S501 - локальный CA
    created_keys: list[str] = []

    async with pool.acquire() as conn:
        alice = (
            await users.ensure_user(
                conn,
                external_id=f"attachments-check-{marker}-a",
                display_name="Аня",
                email=f"att-{marker}-a@example.org",
                email_verified=True,
            )
        ).user
        bob = (
            await users.ensure_user(
                conn,
                external_id=f"attachments-check-{marker}-b",
                display_name="Боря",
                email=f"att-{marker}-b@example.org",
                email_verified=True,
            )
        ).user
        conversation_id = ConversationId(uuid.uuid4())
        await conversations.insert_conversation(
            conn,
            conversation_id=conversation_id,
            type=ConversationType.DIRECT,
            direct_key=f"attachments-check:{marker}",
        )
        for member in (alice, bob):
            await conversations.add_member(
                conn, conversation_id=conversation_id, user_id=member.user_id
            )

    try:
        async with pool.acquire() as conn:
            print("ATT-005: отказ до выдачи ссылки")
            bad_type = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(100), user_id=alice.user_id,
                content_type="application/x-msdownload", size_bytes=10, file_name="a.exe",
            )
            check("исполняемый тип -> 415, ссылки нет",
                  bad_type.rejection is Reason.UNSUPPORTED_MEDIA_TYPE and bad_type.upload_url is None)
            too_big = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(100), user_id=alice.user_id,
                content_type="image/png", size_bytes=11 * 1024 * 1024, file_name="big.png",
            )
            check("картинка выше 10 МиБ -> 413, ссылки нет",
                  too_big.rejection is Reason.PAYLOAD_TOO_LARGE and too_big.upload_url is None)
            rows = await conn.fetchval(
                "SELECT count(*) FROM attachments WHERE uploader_id = $1", alice.user_id
            )
            check("отказы не оставили ни одной строки", rows == 0, f"строк: {rows}")

            print("ATT-006: лимит инициаций")
            limiter = CountingLimiter(2)
            outcomes = [
                await service.init_upload(
                    conn, store=store, limiter=limiter, user_id=bob.user_id,
                    content_type="text/plain", size_bytes=3, file_name="n.txt",
                )
                for _ in range(3)
            ]
            check("третья инициация за окно -> 429 с Retry-After",
                  outcomes[2].rejection is Reason.RATE_LIMITED
                  and outcomes[2].retry_after_seconds == 7)
            check("первые две прошли", outcomes[0].ok and outcomes[1].ok)
            created_keys += [o.attachment.object_key for o in outcomes if o.ok]

            print("ATT-003: изображение от загрузки до сообщения")
            image_id = await new_attachment(conn, store, http, alice,
                                            content_type="image/png", content=PNG, name="кот.png")
            before = await service.status(conn, user_id=alice.user_id, attachment_id=image_id)
            check("до complete вложение pending", before.attachment.state.value == "pending")
            created_keys.append(before.attachment.object_key)
            stranger = await service.complete(
                conn, store=store, user_id=bob.user_id, attachment_id=image_id
            )
            check("чужой complete неотличим от несуществующего",
                  stranger.rejection is Reason.ATTACHMENT_NOT_FOUND)
            done = await service.complete(
                conn, store=store, user_id=alice.user_id, attachment_id=image_id
            )
            check("complete -> processing", done.attachment.state.value == "processing")
            again = await service.complete(
                conn, store=store, user_id=alice.user_id, attachment_id=image_id
            )
            check("повторный complete безопасен", again.ok and again.attachment.state.value == "processing")

            early = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.IMAGE,
                payload=MessagePayload(), attachment_ids=(image_id,),
            )
            check("пока processing, прикрепить нельзя (409)",
                  early.rejection is Reason.ATTACHMENT_NOT_READY)

            outcome = await service.process_batch(
                conn, store=store, scanner=StubScanner(), owner="attachments-check"
            )
            check("обработка довела до ready", outcome.ready >= 1, str(outcome))

            foreign = await message_service.send_message(
                conn, sender_id=bob.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.IMAGE,
                payload=MessagePayload(), attachment_ids=(image_id,),
            )
            check("чужое вложение прикрепить нельзя",
                  foreign.rejection is Reason.ATTACHMENT_NOT_READY)
            wrong_kind = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.FILE,
                payload=MessagePayload(), attachment_ids=(image_id,),
            )
            check("картинку нельзя выдать за файл",
                  wrong_kind.rejection is Reason.ATTACHMENT_NOT_READY)

            client_message_id = ClientMessageId(uuid.uuid4())
            sent = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=client_message_id, kind=MessageKind.IMAGE,
                payload=MessagePayload(text="смотри"), attachment_ids=(image_id,),
            )
            check("ready прикрепляется к сообщению", sent.ok and sent.created)
            retry = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=client_message_id, kind=MessageKind.IMAGE,
                payload=MessagePayload(text="смотри"), attachment_ids=(image_id,),
            )
            check("повтор той же отправки идемпотентен", retry.ok and not retry.created)
            second = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.IMAGE,
                payload=MessagePayload(), attachment_ids=(image_id,),
            )
            check("одно вложение к двум сообщениям нельзя",
                  second.rejection is Reason.ATTACHMENT_NOT_READY)

            views = await service.views_for_messages(
                conn, store=store, message_ids=[sent.message.message_id]
            )
            view = views[sent.message.message_id][0]
            check("в выдаче есть ссылка на скачивание", view.download_url.startswith("http"))
            downloaded = await http.get(view.download_url)
            check("скачанное равно загруженному",
                  downloaded.status_code == 200 and downloaded.content == PNG,
                  f"{downloaded.status_code}, {len(downloaded.content)} байт")
            check("имя файла доехало до ответа",
                  "filename*=UTF-8''" in downloaded.headers.get("content-disposition", ""),
                  downloaded.headers.get("content-disposition", ""))

            print("ATT-007: тип не совпал с фактическим")
            fake_id = await new_attachment(conn, store, http, alice,
                                           content_type="image/png", content=EXE, name="cat.png")
            fake = await service.complete(
                conn, store=store, user_id=alice.user_id, attachment_id=fake_id
            )
            check("на входе принят (проверка при обработке)",
                  fake.attachment.state.value == "processing")
            await service.process_batch(
                conn, store=store, scanner=StubScanner(), owner="attachments-check"
            )
            fake_after = await service.status(conn, user_id=alice.user_id, attachment_id=fake_id)
            check("исполняемый под видом картинки -> rejected/type_mismatch",
                  fake_after.attachment.state.value == "rejected"
                  and fake_after.attachment.rejection_reason.value == "type_mismatch")
            check("объект удалён из хранилища",
                  await store.head(fake_after.attachment.object_key) is None)

            print("ATT-002: заражённый файл")
            virus_id = await new_attachment(conn, store, http, alice,
                                            content_type="text/plain", content=EICAR, name="v.txt")
            await service.complete(conn, store=store, user_id=alice.user_id, attachment_id=virus_id)
            await service.process_batch(
                conn, store=store, scanner=StubScanner(), owner="attachments-check"
            )
            virus = await service.status(conn, user_id=alice.user_id, attachment_id=virus_id)
            check("EICAR -> rejected/malware",
                  virus.attachment.state.value == "rejected"
                  and virus.attachment.rejection_reason.value == "malware")
            blocked = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.FILE,
                payload=MessagePayload(), attachment_ids=(virus_id,),
            )
            check("сообщение с заражённым не создаётся",
                  blocked.rejection is Reason.ATTACHMENT_NOT_READY)

            print("ATT-008: сканер недоступен - отказ в закрытую сторону")
            slow_id = await new_attachment(conn, store, http, alice,
                                           content_type="text/plain", content=b"hello", name="h.txt")
            await service.complete(conn, store=store, user_id=alice.user_id, attachment_id=slow_id)
            await service.process_batch(
                conn, store=store, scanner=UnavailableScanner(), owner="attachments-check"
            )
            slow = await service.status(conn, user_id=alice.user_id, attachment_id=slow_id)
            check("остаётся processing, не ready и не rejected",
                  slow.attachment.state.value == "processing")
            held = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.FILE,
                payload=MessagePayload(), attachment_ids=(slow_id,),
            )
            check("и недоступно для сообщения", held.rejection is Reason.ATTACHMENT_NOT_READY)
            # Отсрочка вернула строку в очередь не сразу: снимаем её руками,
            # чтобы не ждать 30 секунд — предмет проверки не паузa.
            await conn.execute(
                "UPDATE attachments SET lease_until = now() - interval '1 second' "
                "WHERE attachment_id = $1", slow_id,
            )
            await service.process_batch(
                conn, store=store, scanner=StubScanner(), owner="attachments-check"
            )
            recovered = await service.status(conn, user_id=alice.user_id, attachment_id=slow_id)
            check("сканер вернулся - вложение стало ready",
                  recovered.attachment.state.value == "ready")

            print("размер: заявил малое, загрузил большое")
            liar = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(100), user_id=alice.user_id,
                content_type="text/plain", size_bytes=10, file_name="l.txt",
            )
            await upload(http, liar, b"x" * (6 * 1024 * 1024))
            liar_done = await service.complete(
                conn, store=store, user_id=alice.user_id,
                attachment_id=liar.attachment.attachment_id,
            )
            check("-> rejected/too_large, объект удалён",
                  liar_done.attachment.state.value == "rejected"
                  and liar_done.attachment.rejection_reason.value == "too_large"
                  and await store.head(liar.attachment.object_key) is None)

            print("ATT-004: голосовое сообщение")
            voice_init = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(10_000), user_id=alice.user_id,
                content_type="audio/webm;codecs=opus", size_bytes=len(WEBM_VOICE),
                file_name="voice.webm", duration_ms=10_000,
            )
            check("голосовое с длительностью получает ссылку", voice_init.ok)
            no_duration = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(10_000), user_id=alice.user_id,
                content_type="audio/webm", size_bytes=len(WEBM_VOICE), file_name="v.webm",
            )
            check("без длительности -> отказ до ссылки",
                  no_duration.rejection is Reason.INVALID_VOICE and no_duration.upload_url is None)
            absurd = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(10_000), user_id=alice.user_id,
                content_type="audio/webm", size_bytes=5 * 1024 * 1024, file_name="v.webm",
                duration_ms=1_000,
            )
            check("5 МиБ за секунду -> отказ до ссылки", absurd.rejection is Reason.INVALID_VOICE)

            check("PUT голосового принят хранилищем",
                  await upload(http, voice_init, WEBM_VOICE) == 200)
            voice_id = voice_init.attachment.attachment_id
            voice_done = await service.complete(
                conn, store=store, user_id=alice.user_id, attachment_id=voice_id
            )
            check("complete -> processing, битрейт выведен: 32 кбит/с",
                  voice_done.attachment.state.value == "processing"
                  and voice_done.attachment.bitrate_kbps == 32
                  and voice_done.attachment.duration_ms == 10_000,
                  f"{voice_done.attachment.bitrate_kbps}")
            await service.process_batch(
                conn, store=store, scanner=StubScanner(), owner="attachments-check"
            )
            voice_sent = await message_service.send_message(
                conn, sender_id=alice.user_id, conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()), kind=MessageKind.VOICE,
                # Клиент прислал другую цифру: в сообщении окажется проверенная.
                payload=MessagePayload(duration_ms=99_000), attachment_ids=(voice_id,),
            )
            check("голосовое уходит сообщением", voice_sent.ok and voice_sent.created)
            check("длительность сообщения — из вложения, а не от клиента",
                  voice_sent.message.payload.duration_ms == 10_000,
                  str(voice_sent.message.payload.duration_ms))
            voice_views = await service.views_for_messages(
                conn, store=store, message_ids=[voice_sent.message.message_id]
            )
            voice_view = voice_views[voice_sent.message.message_id][0]
            voice_bytes = await http.get(voice_view.download_url)
            check("скачанное голосовое равно загруженному",
                  voice_bytes.status_code == 200 and voice_bytes.content == WEBM_VOICE)
            check("в выдаче есть длительность", voice_view.attachment.duration_ms == 10_000)

            liar_voice = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(10_000), user_id=alice.user_id,
                content_type="audio/webm", size_bytes=len(WEBM_VOICE), file_name="lie.webm",
                duration_ms=10_000,
            )
            await upload(http, liar_voice, b"\x1a\x45\xdf\xa3" + b"\x00" * (5 * 1024 * 1024))
            liar_voice_done = await service.complete(
                conn, store=store, user_id=alice.user_id,
                attachment_id=liar_voice.attachment.attachment_id,
            )
            check("заявил 10 с, загрузил 5 МиБ -> rejected/invalid_audio, объект удалён",
                  liar_voice_done.attachment.state.value == "rejected"
                  and liar_voice_done.attachment.rejection_reason.value == "invalid_audio"
                  and await store.head(liar_voice.attachment.object_key) is None)

            exe_voice_id = await new_attachment(
                conn, store, http, alice, content_type="audio/webm",
                content=EXE + b"\x00" * 40_000, name="x.webm", duration_ms=10_000,
            )
            await service.complete(
                conn, store=store, user_id=alice.user_id, attachment_id=exe_voice_id
            )
            await service.process_batch(
                conn, store=store, scanner=StubScanner(), owner="attachments-check"
            )
            exe_voice = await service.status(conn, user_id=alice.user_id, attachment_id=exe_voice_id)
            check("исполняемый под видом голосового -> rejected/type_mismatch",
                  exe_voice.attachment.state.value == "rejected"
                  and exe_voice.attachment.rejection_reason.value == "type_mismatch")

            missing = await service.init_upload(
                conn, store=store, limiter=CountingLimiter(100), user_id=alice.user_id,
                content_type="text/plain", size_bytes=3, file_name="m.txt",
            )
            no_object = await service.complete(
                conn, store=store, user_id=alice.user_id,
                attachment_id=missing.attachment.attachment_id,
            )
            check("complete без загрузки -> 409",
                  no_object.rejection is Reason.ATTACHMENT_UPLOAD_MISSING)

            print("ATT-001: уборка неприкреплённых")
            orphan_id = await new_attachment(conn, store, http, alice,
                                             content_type="text/plain", content=b"orphan")
            orphan = await service.status(conn, user_id=alice.user_id, attachment_id=orphan_id)
            cleaned = await service.cleanup_orphans(
                conn, store=store, older_than=timedelta(seconds=0)
            )
            gone = await service.status(conn, user_id=alice.user_id, attachment_id=orphan_id)
            check("неприкреплённое стёрто: и запись, и объект",
                  cleaned.erased >= 1 and gone.attachment.state.value == "erased"
                  and await store.head(orphan.attachment.object_key) is None)
            still = await attachments_repo.fetch(conn, attachment_id=AttachmentId(image_id))
            check("прикреплённое не тронуто",
                  still.state.value == "attached"
                  and await store.head(still.object_key) is not None)
    finally:
        async with pool.acquire() as conn:
            keys = await conn.fetch(
                "SELECT object_key FROM attachments WHERE uploader_id IN "
                "(SELECT user_id FROM users WHERE external_id LIKE $1)",
                f"attachments-check-{marker}-%",
            )
            for row in keys:
                await store.delete(row["object_key"])
            for key in created_keys:
                await store.delete(key)
            await conn.execute(
                "DELETE FROM attachments WHERE uploader_id IN "
                "(SELECT user_id FROM users WHERE external_id LIKE $1)",
                f"attachments-check-{marker}-%",
            )
            await conn.execute(
                "DELETE FROM outbox WHERE partition_key = $1", str(conversation_id)
            )
            await conn.execute(
                "DELETE FROM messages WHERE conversation_id = $1", conversation_id
            )
            await conn.execute(
                "DELETE FROM conversation_members WHERE conversation_id = $1", conversation_id
            )
            await conn.execute(
                "DELETE FROM conversations WHERE conversation_id = $1", conversation_id
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1",
                f"attachments-check-{marker}-%",
            )
        await pool.close()
        await http.aclose()
        await store.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\n  упало проверок: {len(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
