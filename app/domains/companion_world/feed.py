"""M3 文字 Feed 纯领域服务：校验、幂等 fingerprint 与发布/下架编排。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional, Tuple

from app.domains.companion_world.contracts import (
    CompanionWorldError,
    FeedRepository,
    UniversePostRecord,
)

_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
MAX_FEED_TEXT_CODEPOINTS = 2000


def user_post_fingerprint(text: str) -> str:
    """按冻结 canonical JSON 形状生成用户文字动态 fingerprint。"""
    payload = json.dumps(
        {"v": 1, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CompanionWorldFeedService:
    """只编排 Feed 领域规则；SQL、事务、时间与 HTTP 均由 adapter 提供。"""

    def __init__(self, repository: FeedRepository) -> None:
        self._repository = repository

    def publish_user_post(
        self,
        platform_user_id: str,
        *,
        client_request_id: str,
        text: str,
        published_at: str,
    ) -> Tuple[UniversePostRecord, bool]:
        """校验并直接发布用户文字动态；返回 ``(post, created)``。"""
        clean_request_id = str(client_request_id or "").strip()
        clean_text = str(text or "").strip()
        if not _CLIENT_REQUEST_ID_RE.fullmatch(clean_request_id):
            raise CompanionWorldError("invalid_request")
        if not clean_text or len(clean_text) > MAX_FEED_TEXT_CODEPOINTS:
            raise CompanionWorldError("invalid_request")
        return self._repository.publish_user_post(
            platform_user_id=platform_user_id,
            client_request_id=clean_request_id,
            text=clean_text,
            request_fingerprint=user_post_fingerprint(clean_text),
            published_at=published_at,
        )

    def list_published_posts(
        self,
        platform_user_id: str,
        *,
        cursor_published_at: Optional[str],
        cursor_post_id: Optional[str],
        limit: int,
    ) -> Tuple[UniversePostRecord, ...]:
        """按稳定 tuple cursor 列出 owner home world 的 published posts。"""
        if limit < 1 or limit > 51:
            raise CompanionWorldError("invalid_request")
        return tuple(
            self._repository.list_published_posts(
                platform_user_id=platform_user_id,
                cursor_published_at=cursor_published_at,
                cursor_post_id=cursor_post_id,
                limit=limit,
            )
        )

    def delete_post(
        self,
        platform_user_id: str,
        *,
        post_id: str,
        reason_code: str,
        deleted_at: str,
    ) -> UniversePostRecord:
        """保留未来内容治理下架接缝；M3 不暴露审核/管理路由。"""
        clean_reason = str(reason_code or "").strip()
        if not str(post_id or "").strip() or not clean_reason:
            raise CompanionWorldError("invalid_request")
        return self._repository.delete_post(
            platform_user_id=platform_user_id,
            post_id=post_id,
            reason_code=clean_reason,
            deleted_at=deleted_at,
        )
