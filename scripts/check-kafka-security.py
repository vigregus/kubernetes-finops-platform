#!/usr/bin/env python3
"""Статический гейт разделения факта сообщения и его содержимого (SEC-010)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FACT = ROOT / "packages/contracts/kafka/message.created.v1.json"
CONTENT = ROOT / "packages/contracts/kafka/message.content.v1.json"
KAFKA = ROOT / "gitops/02-infra/kafka-cluster/manifests/kafka.yaml"
USERS = ROOT / "gitops/04-messenger/messenger-kafka-topics/manifests/users.yaml"


def fail(message: str) -> None:
    print(f"  ✗ {message}")
    raise SystemExit(1)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    fact = load_json(FACT)
    content = load_json(CONTENT)
    fact_fields = set(fact.get("properties", {}))
    if {"payload", "text"} & fact_fields:
        fail("message.created.v1 раскрывает содержимое")
    if "payload" not in set(content.get("required", [])):
        fail("message.content.v1 не требует payload")

    kafka_docs = list(yaml.safe_load_all(KAFKA.read_text(encoding="utf-8")))
    kafka = next(doc for doc in kafka_docs if doc.get("kind") == "Kafka")
    spec = kafka["spec"]["kafka"]
    listener = next(item for item in spec["listeners"] if item["name"] == "plain")
    if listener.get("authentication", {}).get("type") != "scram-sha-512":
        fail("внутренний Kafka listener не требует SCRAM")
    if spec.get("authorization", {}).get("type") != "simple":
        fail("Kafka ACL не включены")

    users = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(USERS.read_text(encoding="utf-8"))
    }
    unread = users.get("messenger-unread")
    if unread is None:
        fail("нет отдельной учётной записи consumer-unread")
    acls = unread["spec"]["authorization"]["acls"]
    readable_topics = {
        acl["resource"].get("name")
        for acl in acls
        if acl["resource"]["type"] == "topic" and "Read" in acl["operations"]
    }
    if "messenger.events.v1" not in readable_topics:
        fail("consumer-unread не читает поток фактов")
    if "messenger.content.v1" in readable_topics:
        fail("consumer-unread получил доступ к содержимому")

    outbox = users.get("messenger-outbox")
    if outbox is None:
        fail("нет отдельной учётной записи outbox")
    writable_topics = {
        acl["resource"].get("name")
        for acl in outbox["spec"]["authorization"]["acls"]
        if acl["resource"]["type"] == "topic" and "Write" in acl["operations"]
    }
    if writable_topics != {"messenger.events.v1", "messenger.content.v1"}:
        fail("outbox должен писать ровно в fact и content топики")

    print("  SEC-010: схема и ACL не дают consumer-unread читать содержимое")
    return 0


if __name__ == "__main__":
    sys.exit(main())
