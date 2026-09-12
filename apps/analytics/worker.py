"""analytics-worker - the Kafka consumer, as its own process.

Split out of app.py so the queue can be scaled independently of HTTP traffic.
That split is what makes two things possible that were not before: scaling on
consumer lag (the queue's own backpressure signal, which the kafka-exporter
has been publishing all along with nothing ever moving it), and separating
what asynchronous processing costs from what serving the API costs - usually
the larger of the two, and previously invisible because both ran in one pod.

Runs as a plain process, not under gunicorn: there is no HTTP to serve. It
exposes its metrics on its own port so it is still scrapeable.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time

from confluent_kafka import Consumer
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import start_http_server

# app.py holds the shared setup - tracing, logging, Redis, the Postgres pool
# and the order_events write path. Importing it runs that setup here too.
import app as ctx

METRICS_PORT = int(os.environ.get("WORKER_METRICS_PORT", "9090"))
POLL_TIMEOUT_S = float(os.environ.get("KAFKA_POLL_TIMEOUT_S", "1.0"))

_stopping = threading.Event()


def _handle_sigterm(signum, frame):
    """Stop at a message boundary rather than mid-batch.

    Kubernetes sends SIGTERM before it removes a pod. Committing what has
    already been processed and then exiting is what keeps a scale-in event
    from re-delivering a batch to whichever replica survives.
    """
    _stopping.set()


def consume_loop():
    if not ctx.KAFKA_BOOTSTRAP_SERVERS:
        ctx.logger.error(json.dumps({"level": "error", "_msg": "KAFKA_BOOTSTRAP_SERVERS unset"}))
        return

    consumer = Consumer(
        {
            "bootstrap.servers": ctx.KAFKA_BOOTSTRAP_SERVERS,
            "group.id": ctx.KAFKA_CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
            # Commit only after the batch has actually been processed -
            # otherwise a restart silently drops whatever was in flight, and
            # consumer lag (the metric the autoscaling experiments read)
            # stops reflecting real outstanding work.
            "enable.auto.commit": False,
            "max.poll.interval.ms": int(os.environ.get("KAFKA_MAX_POLL_INTERVAL_MS", "300000")),
        }
    )
    consumer.subscribe([ctx.KAFKA_TOPIC])

    try:
        while not _stopping.is_set():
            messages = consumer.consume(num_messages=ctx.KAFKA_BATCH_SIZE, timeout=POLL_TIMEOUT_S)
            if not messages:
                continue

            processed = 0
            for msg in messages:
                if msg.error():
                    ctx.logger.error(
                        json.dumps({"level": "error", "_msg": "kafka poll error", "error": str(msg.error())})
                    )
                    continue
                if _process(msg):
                    processed += 1

            if processed:
                consumer.commit(asynchronous=False)
                ctx.logger.debug(
                    json.dumps(
                        {
                            "_msg": f"batch committed n={processed}/{len(messages)}",
                            "level": "debug",
                            "batch_processed": processed,
                            "batch_received": len(messages),
                        }
                    )
                )
    finally:
        try:
            consumer.commit(asynchronous=False)
        except Exception:
            pass
        consumer.close()
        if ctx.db is not None:
            ctx.db.close()


def _process(msg) -> bool:
    headers = {k: v.decode("utf-8") for k, v in (msg.headers() or [])}
    trace_ctx = propagate.extract(headers)
    with ctx.tracer.start_as_current_span(
        "analytics.consume_order", context=trace_ctx, kind=SpanKind.CONSUMER
    ) as span:
        span.set_attribute("messaging.system", "kafka")
        span.set_attribute("messaging.destination", msg.topic())
        span.set_attribute("messaging.destination_kind", "topic")
        span.set_attribute("messaging.operation", "receive")
        span.set_attribute("messaging.kafka.partition", msg.partition())
        span.set_attribute("messaging.kafka.consumer_group", ctx.KAFKA_CONSUMER_GROUP)

        payload = {}
        try:
            payload = json.loads(msg.value())
            span.set_attribute("order.id", payload.get("order_id", ""))
        except (json.JSONDecodeError, TypeError):
            pass
        order_id = payload.get("order_id") if isinstance(payload, dict) else None
        trace_id_hex = format(span.get_span_context().trace_id, "032x")
        started = time.perf_counter()

        # Receipt, aggregation and the cache write are steps, not events worth
        # a line each at INFO. Five INFO lines per message meant a 30-minute
        # run produced ~384k of them, all reading as bare labels because the
        # log viewer renders only _msg - and log volume is itself a cost line.
        ctx.log_json(
            span,
            level="debug",
            _msg="kafka consume",
            topic=msg.topic(),
            partition=msg.partition(),
            offset=msg.offset(),
            order_id=order_id,
        )

        try:
            with ctx.tracer.start_as_current_span("analytics.aggregate_batch") as agg_span:
                ctx.BATCH_PROFILE.run()
                ctx.log_json(agg_span, level="debug", _msg="aggregate batch computed", order_id=order_id)

            if isinstance(payload, dict) and order_id:
                ctx.persist_event(payload, trace_id_hex)

            if ctx.redis_client:
                start = time.perf_counter()
                with ctx.tracer.start_as_current_span("analytics.cache_update") as cache_span:
                    cache_span.set_attribute("db.system", "redis")
                    cache_span.set_attribute("db.operation", "INCR")
                    cache_span.set_attribute("db.statement", f"INCR {ctx.REDIS_KEY_PROCESSED}")
                    new_total = ctx.redis_client.incr(ctx.REDIS_KEY_PROCESSED)
                    ctx.log_json(
                        cache_span,
                        level="debug",
                        _msg="redis INCR",
                        key=ctx.REDIS_KEY_PROCESSED,
                        new_value=new_total,
                    )
                ctx.CACHE_LATENCY.observe(time.perf_counter() - start)

            dur_ms = round((time.perf_counter() - started) * 1000, 1)
            amount = payload.get("amount_cents") if isinstance(payload, dict) else None
            region = payload.get("region") if isinstance(payload, dict) else None
            ctx.log_json(
                span,
                msg=(
                    f"order consumed order={order_id} amount={amount} region={region} "
                    f"p{msg.partition()}@{msg.offset()} dur={dur_ms}ms"
                ),
                order_id=order_id,
                partition=msg.partition(),
                offset=msg.offset(),
                amount_cents=amount,
                region=region,
                duration_ms=dur_ms,
            )
            ctx.ORDERS_CONSUMED.inc()
            return True
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            ctx.log_json(
                span,
                level="error",
                _msg=f"order consume failed order={order_id} p{msg.partition()}@{msg.offset()}: {e}",
                order_id=order_id,
                partition=msg.partition(),
                offset=msg.offset(),
                error_type=type(e).__name__,
                error=str(e),
            )
            return False


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    start_http_server(METRICS_PORT)
    consume_loop()
