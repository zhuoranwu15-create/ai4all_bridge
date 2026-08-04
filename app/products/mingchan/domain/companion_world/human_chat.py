"""Companion World M5 真人一对一聊天纯领域 DTO 与状态规则。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Sequence, Tuple

HUMAN_CONVERSATION_STATUSES = frozenset({"active", "read_only"})
HUMAN_REPORT_STATUSES = frozenset({"open", "reviewed", "closed"})


@dataclass(frozen=True)
class HumanReportReason:
    """举报原因的受控条目。

    ``label`` 是客户端直接展示的中文文案——服务端给文案而不是只给码，是为了让所有端口
    的举报分类保持一致，不各自翻译。``details_required`` 为真时服务端会强制校验正文，
    契约与校验共用本表，不允许「契约说必填、实现不校验」。
    """

    reason_code: str
    label: str
    details_required: bool = False


#: 码集合或语义变更时递增；纯文案微调不动它——客户端按 version 决定要不要重拉。
HUMAN_REPORT_REASONS_VERSION = 1

#: 顺序即客户端展示顺序：具体分类在前，兜底的「其他」永远在最后。
HUMAN_REPORT_REASONS: Tuple[HumanReportReason, ...] = (
    HumanReportReason("spam", "垃圾广告"),
    HumanReportReason("harassment", "骚扰辱骂"),
    HumanReportReason("threat", "威胁恐吓"),
    HumanReportReason("hate", "仇恨言论"),
    HumanReportReason("sexual", "色情低俗"),
    HumanReportReason("privacy", "侵犯隐私"),
    # 「其他」没有分类信息，运营只能靠正文判断，因此正文必填。
    HumanReportReason("other", "其他", details_required=True),
)

_HUMAN_REPORT_REASONS_BY_CODE: Dict[str, HumanReportReason] = {
    reason.reason_code: reason for reason in HUMAN_REPORT_REASONS
}


def human_report_reason(reason_code: str) -> Optional[HumanReportReason]:
    """按码查受控举报原因；未知码返回 ``None`` 由调用方统一成 invalid_request。"""
    return _HUMAN_REPORT_REASONS_BY_CODE.get(str(reason_code or "").strip().lower())


@dataclass(frozen=True)
class HumanConversationRecord:
    """一条与 visit 一对一绑定的真人会话。"""

    id: str
    visit_id: str
    owner_platform_user_id: str
    visitor_platform_user_id: str
    status: str
    created_at: str
    last_message_at: Optional[str] = None
    owner_hidden_at: Optional[str] = None
    visitor_hidden_at: Optional[str] = None


@dataclass(frozen=True)
class HumanMessageRecord:
    """永不进入 AI message/runtime 的真人文字消息。"""

    id: str
    conversation_id: str
    sender_platform_user_id: str
    client_message_id: str
    body_text: str
    created_at: str


def human_conversation_transition_allowed(
    current_status: str, target_status: str
) -> bool:
    """真人会话只允许从 active 单向进入 read_only。"""
    return current_status == target_status or (
        current_status == "active" and target_status == "read_only"
    )


def normalize_human_message_body(
    body_text: str, *, max_chars: int = 4000, allow_empty: bool = False
) -> str:
    """校验并返回真人消息正文，不接受超长内容。

    ``allow_empty`` 供 v1.5 媒体消息使用：图片不带 caption 是常态，但长度上限仍然生效。
    """
    cleaned = str(body_text or "").strip()
    if not cleaned and not allow_empty:
        raise ValueError("human message body is required")
    if len(cleaned) > max_chars:
        raise ValueError("human message body is too long")
    return cleaned


def human_message_fingerprint(*, client_message_id: str, body_text: str) -> str:
    """生成 sender-scoped idempotency 对账指纹。"""
    payload = f"{client_message_id}\n{body_text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HumanChatRepository(Protocol):
    """M5 真人聊天持久化端口；participant 身份必须来自 session。"""

    def get_conversation_for_participant(
        self, *, conversation_id: str, platform_user_id: str
    ) -> Optional[HumanConversationRecord]: ...

    def list_messages_for_participant(
        self,
        *,
        conversation_id: str,
        platform_user_id: str,
        limit: int,
    ) -> Sequence[HumanMessageRecord]: ...


__all__ = [
    "HUMAN_CONVERSATION_STATUSES",
    "HUMAN_REPORT_REASONS",
    "HUMAN_REPORT_REASONS_VERSION",
    "HUMAN_REPORT_STATUSES",
    "HumanChatRepository",
    "HumanConversationRecord",
    "HumanMessageRecord",
    "HumanReportReason",
    "human_conversation_transition_allowed",
    "human_message_fingerprint",
    "human_report_reason",
    "normalize_human_message_body",
]
