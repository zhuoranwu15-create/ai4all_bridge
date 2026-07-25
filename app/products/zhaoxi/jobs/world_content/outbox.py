"""Companion World 事务 outbox worker。"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Protocol

from app import db
from app.time_utils import BEIJING_TZ

logger = logging.getLogger("ai4all.world_content.outbox")


class WorldEventPublisher(Protocol):
    """领域事件发布端口；实现必须按 idempotency_key 去重。"""

    def publish(
        self, *, event_type: str, payload: Dict[str, Any], idempotency_key: str
    ) -> None: ...


class LoggingWorldEventPublisher:
    """首版本地 consumer：记录结构化事件；后续可替换为真实 event bus adapter。"""

    def publish(
        self, *, event_type: str, payload: Dict[str, Any], idempotency_key: str
    ) -> None:
        logger.info(
            "world event delivered type=%s key=%s post_id=%s",
            event_type,
            idempotency_key,
            payload.get("post_id"),
        )


def _db_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def dispatch_world_outbox_batch(
    *,
    now: datetime,
    publisher: WorldEventPublisher,
    batch_size: int,
    claim_lease_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
) -> Dict[str, Any]:
    """claim 并分发一批 outbox；handler 在 claim 事务外运行。"""
    current = (
        now.astimezone(BEIJING_TZ).replace(tzinfo=None)
        if now.tzinfo is not None
        else now
    )
    claim_token = f"woutclaim_{uuid.uuid4().hex}"
    rows = db.claim_companion_world_outbox(
        batch_size=batch_size,
        now=_db_time(current),
        claim_token=claim_token,
        stale_before=_db_time(
            current - timedelta(seconds=max(1, claim_lease_seconds))
        ),
    )
    delivered = 0
    failed = 0
    dead = 0
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("outbox payload must be an object")
            publisher.publish(
                event_type=str(row["event_type"]),
                payload=payload,
                idempotency_key=str(row["idempotency_key"]),
            )
            updated = db.complete_companion_world_outbox(
                outbox_id=row["id"],
                claim_token=claim_token,
                delivered_at=_db_time(current),
            )
            if updated and updated["status"] == "delivered":
                delivered += 1
        except Exception as err:  # noqa: BLE001 — 单事件失败不阻断同批其余事件
            failed += 1
            attempts = max(1, int(row.get("attempts") or 1))
            next_attempt = current + timedelta(
                seconds=max(1, retry_base_seconds) * (2 ** max(attempts - 1, 0))
            )
            updated = db.fail_companion_world_outbox(
                outbox_id=row["id"],
                claim_token=claim_token,
                error=str(err),
                next_attempt_at=_db_time(next_attempt),
                max_attempts=max_attempts,
            )
            if updated and updated["status"] == "dead":
                dead += 1
                logger.error(
                    "world outbox dead id=%s key=%s error=%s",
                    row["id"],
                    row["idempotency_key"],
                    err,
                )
    queue = db.get_companion_world_outbox_metrics(now=_db_time(current))
    return {
        "claimed": len(rows),
        "delivered": delivered,
        "failed": failed,
        "dead": dead,
        "queue": queue,
    }
