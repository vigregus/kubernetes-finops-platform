"""Атомарный приём сообщения и двух событий outbox."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import asyncpg

from messenger.domain.errors import Reason
from messenger.domain.ids import (
    AttachmentId,
    ClientMessageId,
    ConversationId,
    MessageId,
    UserId,
    new_event_id,
    new_message_id,
)
from messenger.domain.message import (
    Message,
    MessageKind,
    MessagePayload,
    validate_message_payload,
)
from messenger.repositories import conversations, messages, outbox
from messenger.telemetry import metrics, tracing

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SendMessageResult:
    message: Message | None = None
    created: bool = False
    rejection: Reason | None = None

    @property
    def ok(self) -> bool:
        return self.message is not None and self.rejection is None


async def send_message(
    conn: asyncpg.Connection,
    *,
    sender_id: UserId,
    conversation_id: ConversationId,
    client_message_id: ClientMessageId,
    kind: MessageKind,
    payload: MessagePayload,
    attachment_ids: tuple[AttachmentId, ...] = (),
    reply_to_message_id: MessageId | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> SendMessageResult:
    """Принимает сообщение идемпотентно по клиентскому идентификатору.

    Kafka здесь не вызывается: сообщение и обе записи outbox либо фиксируются
    одним коммитом, либо целиком откатываются. Сетевой вызов выполняет relay
    уже после освобождения транзакции и блокировки строки беседы.

    `trace_id` и `span_id` — контекст создателя записи, а не текущий:
    отправитель поставит на них ссылку. Ему нужен участок, а не только
    трасса, иначе ссылку строить не на что.
    """
    if kind is MessageKind.SYSTEM:
        return SendMessageResult(rejection=Reason.UNSUPPORTED_MEDIA_TYPE)

    # Негодный запрос не должен ждать занятую строку беседы.
    validate_message_payload(kind, payload)

    origin = {
        **({"trace_id": trace_id} if trace_id else {}),
        **({"span_id": span_id} if span_id else {}),
    }

    # Время держится вокруг всей транзакции, а не только вокруг вставки:
    # `messenger_message_commit_duration_seconds` — это T_commit из
    # сквозного пути (docs/messenger/06-observability.md, часть 1), а
    # блокировка строки беседы в `membership.check` — часть этого же
    # времени, а не сторонняя задержка. Отклонённые по членству (403-like)
    # в счёт не идут вовсе: SLI «Запись сообщения» их явно исключает, и
    # запись метрики здесь только шумела бы кардинальностью результата,
    # который не про приём сообщения, а про его законность.
    started = time.perf_counter()
    with tracing.span("postgres.transaction"):
        try:
            async with conn.transaction():
                # Проверка членства вкладывается в транзакцию, а не идёт
                # рядом с ней, и это не оформление: она *и есть* блокировка
                # строки беседы (`SELECT ... FOR UPDATE`). Вне транзакции
                # между проверкой и вставкой помещается гонка, и в беседу,
                # из которой только что вышли, уедет сообщение.
                with tracing.span("membership.check"):
                    last_seq = await messages.lock_active_conversation(
                        conn,
                        conversation_id=conversation_id,
                        sender_id=sender_id,
                    )
                if last_seq is None:
                    return SendMessageResult(rejection=Reason.NOT_A_MEMBER)

                existing = await messages.fetch_by_client_id(
                    conn,
                    conversation_id=conversation_id,
                    sender_id=sender_id,
                    client_message_id=client_message_id,
                )
                if existing is not None:
                    return SendMessageResult(message=existing, created=False)

                sequence = await messages.allocate_sequence(
                    conn, conversation_id=conversation_id
                )
                with tracing.span("message.insert"):
                    message = await messages.insert_message(
                        conn,
                        message_id=new_message_id(),
                        conversation_id=conversation_id,
                        conversation_seq=sequence,
                        sender_id=sender_id,
                        client_message_id=client_message_id,
                        kind=kind,
                        payload=payload,
                        reply_to_message_id=reply_to_message_id,
                    )

                members = await conversations.list_active_members(
                    conn, conversation_id=conversation_id
                )
                recipients = [
                    str(member.user_id)
                    for member in members
                    if member.user_id != sender_id
                ]
                common = {
                    "event_version": 1,
                    "occurred_at": message.created_at.isoformat(),
                    "message_id": str(message.message_id),
                    "conversation_id": str(message.conversation_id),
                    "type": message.kind.value,
                }
                fact_event_id = new_event_id()
                content_event_id = new_event_id()
                # Один спан на обе записи: в контракте узел один, и две
                # записи — одна логическая операция. Число уходит атрибутом,
                # чтобы разница была видна, если она однажды появится.
                with tracing.span("outbox.insert") as insert_span:
                    insert_span.set_attribute("messenger.outbox.records", 2)
                    await outbox.insert_event(
                        conn,
                        aggregate_id=message.message_id,
                        event_id=fact_event_id,
                        event_type="message.created",
                        partition_key=str(conversation_id),
                        payload={
                            **common,
                            "event_id": str(fact_event_id),
                            "event_type": "message.created",
                            "conversation_seq": message.conversation_seq,
                            "sender_id": str(sender_id),
                            "recipient_ids": recipients,
                            "has_attachments": bool(attachment_ids),
                            "content_ref": str(message.message_id),
                            **origin,
                        },
                    )
                    await outbox.insert_event(
                        conn,
                        aggregate_id=message.message_id,
                        event_id=content_event_id,
                        event_type="message.content",
                        partition_key=str(conversation_id),
                        payload={
                            **common,
                            "event_id": str(content_event_id),
                            "event_type": "message.content",
                            "payload": {
                                key: value
                                for key, value in {
                                    "text": message.payload.text,
                                    "duration_ms": message.payload.duration_ms,
                                    "attachment_count": len(attachment_ids),
                                }.items()
                                if value is not None
                            },
                            **origin,
                        },
                    )
                # Метрика пишется до выхода из `async with conn.transaction()`,
                # то есть до фактического COMMIT на проводе: буферизованные
                # asyncpg-запросы этого блока летят одним пакетом на выходе
                # из контекста, и ждать этого момента отдельно значило бы
                # либо повторно оборачивать то же самое, либо дать функции
                # незаметно вернуть результат до того, как её единственная
                # метрика записана.
                metrics.message_commit(time.perf_counter() - started, result="success")
                return SendMessageResult(message=message, created=True)
        except Exception as exc:
            # Исключение здесь — это отказ базы, а не отклонённый домен:
            # доменные исходы (NOT_A_MEMBER, ValueError выше по стеку)
            # возвращаются, а не бросаются. `messenger_message_commit_duration_seconds`
            # с `result=failed` — это и есть числитель отказа SLI «Запись
            # сообщения», а не диагностика причины: причину несёт исключение,
            # которое летит дальше нетронутым.
            metrics.message_commit(time.perf_counter() - started, result="failed")
            # `message_id` в записи нет: он рождается внутри транзакции
            # (`new_message_id()` вызывается только перед `message.insert`),
            # и отказ мог случиться раньше - в блокировке строки беседы
            # или в самой вставке. `client_message_id` есть всегда: это
            # вход функции, и по нему разбор "что случилось с попыткой
            # отправить" возможен даже когда `message_id` никогда
            # не появился.
            log.error(
                "транзакция приёма сообщения не завершилась",
                extra={"event": "message_commit_failed", "result": "failed",
                       "error_code": type(exc).__name__,
                       "conversation_id": str(conversation_id),
                       "sender_id": str(sender_id),
                       "client_message_id": str(client_message_id)},
            )
            raise
