"""AI Feed eligibility、slot claim、生成与发布编排。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app import db
from app.time_utils import BEIJING_TZ
from app.products.mingchan.jobs.world_content.generator import FeedGenerationRequest, FeedTextGenerator
from app.products.mingchan.jobs.world_content.windows import FeedWindows


def _db_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def generate_ai_feed_batch(
    *,
    now: datetime,
    windows: FeedWindows,
    generator: FeedTextGenerator,
    batch_size: int,
    claim_lease_seconds: int,
    retry_max_attempts: int,
    retry_base_seconds: int,
    after_universe_id: Optional[str] = None,
) -> Dict[str, Any]:
    """执行一批 universe 级 AI Feed；LLM 调用始终在 DB 事务外。"""
    current = (
        now.astimezone(BEIJING_TZ).replace(tzinfo=None)
        if now.tzinfo is not None
        else now
    )
    current_db = _db_time(current)
    closed = db.close_expired_ai_feed_slots(now=current_db, limit=batch_size)
    slot = windows.resolve(current)
    if slot is None:
        return {
            "status": "outside_window",
            "closed_expired": closed,
            "scanned": 0,
            "published": 0,
            "next_after_universe_id": None,
            "results": [],
        }

    worlds = db.list_ai_feed_eligible_worlds(
        inbound_since=_db_time(current - timedelta(days=7)),
        after_universe_id=after_universe_id,
        limit=batch_size,
    )
    next_after_universe_id = (
        str(worlds[-1]["universe_id"]) if len(worlds) >= max(1, batch_size) else None
    )
    results: List[Dict[str, Any]] = []
    published_count = 0
    stale_before = _db_time(current - timedelta(seconds=max(1, claim_lease_seconds)))
    for world in worlds:
        author = db.select_ai_feed_author(universe_id=world["universe_id"])
        if author is None:
            results.append({"universe_id": world["universe_id"], "status": "no_author"})
            continue
        claim_token = f"feedclaim_{uuid.uuid4().hex}"
        post, acquired = db.claim_ai_feed_slot(
            universe_id=world["universe_id"],
            author_resident_id=author["resident_id"],
            ai_local_date=slot.local_date,
            ai_slot=slot.name,
            slot_window_end_at=_db_time(slot.window_end_at),
            claim_token=claim_token,
            claimed_at=current_db,
            stale_before=stale_before,
            max_attempts=retry_max_attempts,
        )
        if post is None or not acquired:
            results.append(
                {"universe_id": world["universe_id"], "status": "already_claimed"}
            )
            continue

        bound_author = db.get_ai_feed_author(
            universe_id=world["universe_id"],
            resident_id=post["author_resident_id"],
            require_active=True,
        )
        if bound_author is None:
            db.skip_ai_feed_post(
                post_id=post["id"],
                claim_token=claim_token,
                reason="author_inactive",
            )
            results.append(
                {"universe_id": world["universe_id"], "status": "author_inactive"}
            )
            continue

        try:
            text = str(
                generator.generate(
                    FeedGenerationRequest(
                        universe_id=world["universe_id"],
                        resident_id=bound_author["resident_id"],
                        resident_name=str(bound_author["name"]),
                        slot=slot.name,
                        local_date=slot.local_date,
                    )
                )
                or ""
            ).strip()
        except Exception as err:  # noqa: BLE001 — 生成失败按 slot retry 状态机处理
            next_attempt = current + timedelta(
                seconds=max(1, retry_base_seconds)
                * (2 ** max(int(post.get("attempt_count") or 1) - 1, 0))
            )
            deferred = db.defer_ai_feed_post(
                post_id=post["id"],
                claim_token=claim_token,
                next_attempt_at=_db_time(next_attempt),
                max_attempts=retry_max_attempts,
            )
            results.append(
                {
                    "universe_id": world["universe_id"],
                    "status": "generation_failed",
                    "error": str(err)[:200],
                    "retry_status": (
                        str(deferred.get("status")) if deferred is not None else "unknown"
                    ),
                }
            )
            continue

        if not text or len(text) > 2000:
            db.skip_ai_feed_post(
                post_id=post["id"],
                claim_token=claim_token,
                reason="invalid_generated_content",
            )
            results.append(
                {"universe_id": world["universe_id"], "status": "invalid_content"}
            )
            continue

        try:
            db.publish_ai_feed_post_with_outbox(
                post_id=post["id"],
                claim_token=claim_token,
                text=text,
                published_at=current_db,
                outbox_idempotency_key=f"world-post-published:v1:{post['id']}",
                payload={
                    "v": 1,
                    "post_id": post["id"],
                    "universe_id": world["universe_id"],
                    "source_type": "ai_feed",
                    "published_at": current_db,
                },
            )
        except ValueError as err:
            reason = str(err)
            if reason in {"author is not active", "slot window closed"}:
                db.skip_ai_feed_post(
                    post_id=post["id"],
                    claim_token=claim_token,
                    reason=(
                        "author_inactive"
                        if reason == "author is not active"
                        else "window_closed"
                    ),
                )
                results.append(
                    {"universe_id": world["universe_id"], "status": reason}
                )
                continue
            raise
        published_count += 1
        results.append(
            {
                "universe_id": world["universe_id"],
                "post_id": post["id"],
                "status": "published",
            }
        )

    return {
        "status": "ok",
        "slot": slot.name,
        "local_date": slot.local_date,
        "closed_expired": closed,
        "scanned": len(worlds),
        "published": published_count,
        "next_after_universe_id": next_after_universe_id,
        "results": results,
    }
