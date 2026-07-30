"""M3 文字 Feed 纯领域服务：校验、幂等 fingerprint 与发布/下架编排。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from app.products.zhaoxi.domain.companion_world.contracts import (
    CompanionWorldError,
    FeedRepository,
    UniversePostRecord,
)

_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_POST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_FEED_TEXT_CODEPOINTS = 2000
# 一条动态最多 4 张图（v1.5 定稿，与 m0055 的 position 0..3 对齐）。
MAX_FEED_POST_IMAGES = 4


@dataclass(frozen=True)
class _RetireRule:
    """一种下架语义对应的固定原因与作者/类型约束。"""

    reason_code: str
    author_type: str
    forbid_post_types: Tuple[str, ...] = ()


# 主人可用的两种下架语义。原因码是持久化层区分「用户删自己内容」与「主人隐藏 AI 内容」的
# 唯一依据（终态 status 统一为 deleted），也是审计口径，因此只能在这里单点定义。
_RETIRE_MODES: dict[str, _RetireRule] = {
    "delete": _RetireRule(reason_code="owner_deleted", author_type="human"),
    "hide": _RetireRule(
        reason_code="owner_hidden",
        author_type="resident",
        forbid_post_types=("farewell",),
    ),
}


def user_post_fingerprint(text: str, media_ids: Sequence[str] = ()) -> str:
    """按冻结 canonical JSON 形状生成用户动态 fingerprint。

    纯文字动态**保持 v1 形状不变**（不塞空的 ``media_ids``）：v1.5 之前发出的动态存的是 v1
    指纹，跨版本重放同一个 ``client_request_id`` 必须仍判为重放而不是 ``idempotency_conflict``。
    带图动态是 v1.5 才有的形状，用 v2，``media_ids`` 有序参与——换图或改顺序都算换内容。
    """
    payload = (
        json.dumps(
            {"v": 2, "text": text, "media_ids": list(media_ids)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if media_ids
        else json.dumps(
            {"v": 1, "text": text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
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
        media_ids: Sequence[str] = (),
    ) -> Tuple[UniversePostRecord, bool]:
        """校验并直接发布用户动态（v1.5 起可带图）；返回 ``(post, created)``。

        ``media_ids`` 非空时正文可以为空（只发图是常态），长度上限仍然生效；上限 4 张
        （``media_count_exceeded``）。资产的归属与门控由 API 层在解析 ``media_ref`` 时完成，
        本层只管数量与"文本或图至少有一个"。
        """
        clean_request_id = str(client_request_id or "").strip()
        clean_text = str(text or "").strip()
        clean_media_ids = [str(mid or "").strip() for mid in media_ids]
        if not _CLIENT_REQUEST_ID_RE.fullmatch(clean_request_id):
            raise CompanionWorldError("invalid_request")
        if any(not mid for mid in clean_media_ids):
            raise CompanionWorldError("media_ref_invalid")
        if len(set(clean_media_ids)) != len(clean_media_ids):
            raise CompanionWorldError("media_ref_invalid")
        if len(clean_media_ids) > MAX_FEED_POST_IMAGES:
            raise CompanionWorldError("media_count_exceeded")
        if len(clean_text) > MAX_FEED_TEXT_CODEPOINTS:
            raise CompanionWorldError("invalid_request")
        if not clean_text and not clean_media_ids:
            raise CompanionWorldError("media_content_required")
        return self._repository.publish_user_post(
            platform_user_id=platform_user_id,
            client_request_id=clean_request_id,
            text=clean_text,
            request_fingerprint=user_post_fingerprint(clean_text, clean_media_ids),
            published_at=published_at,
            media_ids=tuple(clean_media_ids),
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

    def retire_post(
        self,
        platform_user_id: str,
        *,
        post_id: str,
        mode: str,
        retired_at: str,
    ) -> Tuple[UniversePostRecord, bool]:
        """主人下架一条动态；返回 ``(post, changed)``，``changed=False`` 即重放。

        ``mode`` 是本层唯一的语义输入，``reason_code`` 与作者/类型约束都由它推导——调用方
        （含 HTTP 层）不能自带下架原因或作者/世界 ID，避免用户内容与系统生成记录被混为一类。

        - ``delete``：只允许主人删自己的文字动态（``author_type='human'``）。
        - ``hide``：只允许主人隐藏 AI 居民动态（``author_type='resident'``）；离别动态
          （``post_type='farewell'``）M2 不允许隐藏——它同时是世界 readiness 的信号，隐藏
          会让整个 Feed 变成 ``world_not_ready``。
        """
        clean_post_id = str(post_id or "").strip()
        if not clean_post_id or not _POST_ID_RE.fullmatch(clean_post_id):
            # 格式非法与不存在同码，不给出「ID 形状对不对」的区分信号。
            raise CompanionWorldError("post_not_found")
        rule = _RETIRE_MODES.get(str(mode or "").strip())
        if rule is None:
            raise CompanionWorldError("invalid_request")
        return self._repository.retire_post(
            platform_user_id=platform_user_id,
            post_id=clean_post_id,
            reason_code=rule.reason_code,
            expected_author_type=rule.author_type,
            forbid_post_types=rule.forbid_post_types,
            retired_at=retired_at,
        )
