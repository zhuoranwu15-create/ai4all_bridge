"""异步居民许愿应用服务：受理、恢复、收回、生成和定时投递。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Tuple

from app.config import settings
from app.platform.moderation.text_sanitizer import TextSanitizerUnavailable
from app.products.zhaoxi.application.companion_world_wish import (
    MAX_WISH_TEXT_CHARS,
    WishGenerationFailed,
    WishTextRejected,
    generate_wish_persona_from_sanitized,
    review_generated_wish_candidate,
    sanitize_wish_text,
)
from app.products.zhaoxi.domain.companion_world.persona_catalog import (
    render_persona,
    resolve_avatar_ref,
)
from app.products.zhaoxi.domain.companion_world.resident_wishes import (
    ResidentWishProjection,
    ResidentWishRecord,
    project_resident_wish,
)
from app.products.zhaoxi.infrastructure.persistence.companion_world_mailbox import (
    expire_due_character_letters,
)
from app.products.zhaoxi.infrastructure.persistence.resident_wishes import (
    claim_resident_wish_job,
    complete_resident_wish_generation,
    create_resident_wish,
    deliver_resident_wish,
    fail_resident_wish_job,
    get_current_resident_wish,
    get_resident_wish_by_request,
    withdraw_resident_wish,
)
from app.time_utils import BEIJING_TZ


class ResidentWishError(Exception):
    """异步许愿稳定业务错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _naive_beijing(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def _db_time(value: datetime) -> str:
    return _naive_beijing(value).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _record(row: Mapping[str, Any]) -> ResidentWishRecord:
    return ResidentWishRecord(
        id=str(row["id"]),
        owner_platform_user_id=str(row["owner_platform_user_id"]),
        universe_id=str(row["universe_id"]),
        client_request_id=str(row["client_request_id"]),
        request_fingerprint=str(row["request_fingerprint"]),
        status=str(row["status"]),
        submitted_at=str(row["submitted_at"]),
        deliver_not_before=str(row["deliver_not_before"]),
        deliver_by=str(row["deliver_by"]),
        letter_id=row.get("letter_id"),
        closed_at=row.get("closed_at"),
        terminal_reason=row.get("terminal_reason"),
    )


def _daily_max(config: Any) -> int:
    try:
        return int(getattr(config, "companion_world_wish_daily_max", 0) or 0)
    except (TypeError, ValueError):
        return 0


class CompanionWorldResidentWishService:
    """编排异步许愿；持久化原语负责 owner 锁、CAS 与 exactly-once。"""

    def __init__(self, *, config: Any = None) -> None:
        self.config = config or settings

    def submit(
        self,
        platform_user_id: str,
        *,
        wish_text: str,
        client_request_id: str,
        now: datetime,
    ) -> Tuple[ResidentWishProjection, bool]:
        """同步完成输入复核，再在一个事务中写 wish 与 durable job。"""
        cleaned = str(wish_text or "").strip()
        if not cleaned or len(cleaned) > MAX_WISH_TEXT_CHARS:
            raise ResidentWishError("invalid_request")
        fingerprint = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        replay = get_resident_wish_by_request(
            owner_platform_user_id=platform_user_id,
            client_request_id=client_request_id,
        )
        if replay is not None:
            if str(replay["request_fingerprint"]) != fingerprint:
                raise ResidentWishError("idempotency_conflict")
            return project_resident_wish(_record(replay)), True

        try:
            sanitized = sanitize_wish_text(cleaned)
        except WishTextRejected as err:
            raise ResidentWishError("wish_text_rejected") from err
        except TextSanitizerUnavailable as err:
            raise ResidentWishError("wish_review_unavailable") from err
        except WishGenerationFailed as err:
            raise ResidentWishError("wish_review_unavailable") from err

        current = _naive_beijing(now).replace(microsecond=0)
        try:
            row, created = create_resident_wish(
                owner_platform_user_id=platform_user_id,
                client_request_id=client_request_id,
                request_fingerprint=fingerprint,
                wish_text=sanitized.text,
                input_safety=sanitized.as_safety_record(),
                submitted_at=_db_time(current),
                deliver_not_before=_db_time(current + timedelta(hours=24)),
                deliver_by=_db_time(current + timedelta(hours=72)),
                daily_window_start=_db_time(current - timedelta(days=1)),
                daily_max=_daily_max(self.config),
            )
        except ValueError as err:
            code = str(err)
            if code in {
                "world_not_ready",
                "resident_capacity_exceeded",
                "wish_already_pending",
                "wish_rate_limited",
                "idempotency_conflict",
            }:
                raise ResidentWishError(code) from err
            raise ResidentWishError("wish_service_unavailable") from err
        except Exception as err:  # noqa: BLE001 - 不能返回已受理但没有 durable job
            raise ResidentWishError("wish_service_unavailable") from err
        return project_resident_wish(_record(row)), not created

    def current(
        self, platform_user_id: str, *, now: datetime
    ) -> Optional[ResidentWishProjection]:
        """先做 request-time 信件过期，再恢复最近一笔愿望。"""
        expire_due_character_letters(
            owner_platform_user_id=platform_user_id,
            now=_db_time(now),
        )
        row = get_current_resident_wish(owner_platform_user_id=platform_user_id)
        return project_resident_wish(_record(row)) if row else None

    def withdraw(
        self, platform_user_id: str, *, wish_id: str, now: datetime
    ) -> Tuple[ResidentWishProjection, bool]:
        """投递前原子收回；与投递竞争只会有一个 CAS 成功。"""
        row, replayed, error = withdraw_resident_wish(
            wish_id=wish_id,
            owner_platform_user_id=platform_user_id,
            now=_db_time(now),
        )
        if error:
            raise ResidentWishError(error)
        if row is None:
            raise ResidentWishError("wish_not_found")
        return project_resident_wish(_record(row)), replayed

    def maintain_batch(
        self, *, now: datetime, batch_size: int
    ) -> Dict[str, Any]:
        """有界处理到期任务；每笔都由独立 lease/CAS 保护。"""
        current = _naive_beijing(now).replace(microsecond=0)
        metrics = {
            "claimed": 0,
            "generated": 0,
            "delivered": 0,
            "retried": 0,
            "unfulfilled": 0,
            "cancelled": 0,
        }
        results = []
        for _ in range(max(1, min(int(batch_size), 500))):
            token = secrets.token_urlsafe(24)
            claimed = claim_resident_wish_job(
                now=_db_time(current),
                lease_expires_at=_db_time(
                    current
                    + timedelta(
                        seconds=max(
                            60,
                            int(
                                getattr(
                                    self.config,
                                    "companion_world_wish_job_lease_seconds",
                                    600,
                                )
                            ),
                        )
                    )
                ),
                claim_token=token,
            )
            if claimed is None:
                break
            metrics["claimed"] += 1
            result = self._process_claim(claimed, claim_token=token, now=current)
            status = str(result["status"])
            if status in metrics:
                metrics[status] += 1
            results.append(result)
        return {"metrics": metrics, "results": results}

    def _process_claim(
        self, claimed: Mapping[str, Any], *, claim_token: str, now: datetime
    ) -> Dict[str, str]:
        wish_id = str(claimed["wish_id"])
        if (
            claimed.get("owner_status") != "active"
            or claimed.get("universe_status") != "active"
            or claimed.get("onboarding_state") != "confirmed"
        ):
            status = fail_resident_wish_job(
                wish_id=wish_id,
                claim_token=claim_token,
                error_code="account_unavailable",
                now=_db_time(now),
                next_attempt_at=str(claimed["deliver_by"]),
            )
            return {"wish_id": wish_id, "status": status}
        generation = claimed.get("generation")
        if generation:
            try:
                result = deliver_resident_wish(
                    wish_id=wish_id,
                    claim_token=claim_token,
                    generation=generation,
                    now=_db_time(now),
                    letter_expires_at=_db_time(
                        now
                        + timedelta(
                            days=max(
                                1,
                                int(
                                    getattr(
                                        self.config,
                                        "companion_world_mailbox_letter_ttl_days",
                                        30,
                                    )
                                ),
                            )
                        )
                    ),
                )
                return {"wish_id": wish_id, "status": str(result["status"])}
            except Exception:  # noqa: BLE001 - 投递失败也必须回到持久重试/72h 终态
                retry_at = now + timedelta(
                    seconds=max(
                        60,
                        int(
                            getattr(
                                self.config,
                                "companion_world_wish_retry_seconds",
                                3600,
                            )
                        ),
                    )
                )
                status = fail_resident_wish_job(
                    wish_id=wish_id,
                    claim_token=claim_token,
                    error_code="wish_delivery_failed",
                    now=_db_time(now),
                    next_attempt_at=_db_time(retry_at),
                )
                return {
                    "wish_id": wish_id,
                    "status": "retried" if status == "queued" else status,
                }

        try:
            persona, generation_safety = generate_wish_persona_from_sanitized(
                str(claimed.get("wish_text") or "")
            )
            rendered = render_persona(persona)
            candidate = {
                "name": persona.name,
                "avatar_ref": resolve_avatar_ref(persona.avatar_key),
                "relationship_type": persona.relationship_type,
                "personality_traits": list(persona.personality_traits),
                "normalized_summary": rendered.normalized_summary,
                "tags": list(rendered.tags),
                "persona_seed": {
                    "SOUL.md": rendered.soul_markdown,
                    "IDENTITY.md": rendered.identity_markdown,
                },
                "letter_body": (
                    f"你好，我是{persona.name}。听说这里有人在等一段新的相遇。"
                    "如果你愿意，可以先打开这封信认识我；如果现在不是时候，也没关系。"
                ),
            }
            review = review_generated_wish_candidate(candidate)
            stored = complete_resident_wish_generation(
                wish_id=wish_id,
                claim_token=claim_token,
                generation=candidate,
                safety={**generation_safety, "candidate_review": review},
                next_attempt_at=str(claimed["deliver_not_before"]),
                now=_db_time(now),
            )
            return {
                "wish_id": wish_id,
                "status": "generated" if stored else "cancelled",
            }
        except Exception as err:  # noqa: BLE001 - 所有生成/二审异常都在窗口内持久重试
            retry_at = now + timedelta(
                seconds=max(
                    60,
                    int(
                        getattr(
                            self.config,
                            "companion_world_wish_retry_seconds",
                            3600,
                        )
                    ),
                )
            )
            status = fail_resident_wish_job(
                wish_id=wish_id,
                claim_token=claim_token,
                error_code=(
                    str(err)[:64]
                    if isinstance(err, WishGenerationFailed)
                    else "wish_generation_failed"
                ),
                now=_db_time(now),
                next_attempt_at=_db_time(retry_at),
            )
            return {
                "wish_id": wish_id,
                "status": "retried" if status == "queued" else status,
            }


__all__ = ["CompanionWorldResidentWishService", "ResidentWishError"]
