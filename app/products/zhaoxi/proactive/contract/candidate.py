"""统一主动消息候选协议 `ProactiveCandidate`。

阶段2 不改存储：候选仍以"normalize 后的 reactivation 候选 dict"形态写进 account_state
metadata。本类型是该 dict 的**typed 视图**，提供 `from_legacy` / `to_legacy` 双向
round-trip —— 选择层用结构化对象工作，落库时回到既有 dict，存储字节不变。

字段覆盖 `normalize_reactivation_candidate` 保留的全部 14 个键（id/type/text/topic/reason/
generated_at/scheduled_slot/scheduled_at/content_invitation_id/confidence/
source_message_cutoff_id/dedupe/policy/metadata），故 `to_legacy(from_legacy(d))` 经
normalize 后与直接 normalize(d) 等价。`features`/`scope` 是协议为未来（排序信号、全局召回）
预留的维度，**不写入 legacy 存储**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# legacy 候选 dict 的可选标量键（值非空才写回），与 normalize 保留集合一致。
_LEGACY_OPTIONAL_TEXT_KEYS = (
    "topic",
    "reason",
    "generated_at",
    "scheduled_slot",
    "scheduled_at",
    "content_invitation_id",
)
_LEGACY_OPTIONAL_DICT_KEYS = ("dedupe", "policy", "metadata")


@dataclass(frozen=True)
class ProactiveCandidate:
    """一条自主外联候选的统一表示。

    `kind` 对应 legacy 候选的 `type`（topic_followup / content_invitation）；
    `candidate_id` 对应 legacy 的 `id`。
    """

    account_id: Optional[str]
    kind: str
    text: str
    candidate_id: Optional[str] = None
    scope: str = "account"  # account | global（全局召回阶段3 才真正接入）
    topic: Optional[str] = None
    reason: Optional[str] = None
    generated_at: Optional[str] = None
    scheduled_slot: Optional[str] = None
    scheduled_at: Optional[str] = None
    content_invitation_id: Optional[str] = None
    confidence: Optional[float] = None
    source_message_cutoff_id: Optional[int] = None
    dedupe: Optional[Dict[str, Any]] = None
    policy: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    # 排序信号位（兴趣/回复率/疲劳等）。阶段2 恒空，不参与存储与决策。
    features: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy(
        cls,
        legacy: Dict[str, Any],
        *,
        account_id: Optional[str] = None,
        scope: str = "account",
    ) -> "ProactiveCandidate":
        """从既有 reactivation 候选 dict 构造（容忍缺键）。"""
        confidence = legacy.get("confidence")
        cutoff = legacy.get("source_message_cutoff_id")
        return cls(
            account_id=account_id,
            kind=str(legacy.get("type") or ""),
            text=str(legacy.get("text") or ""),
            candidate_id=legacy.get("id"),
            scope=scope,
            topic=legacy.get("topic"),
            reason=legacy.get("reason"),
            generated_at=legacy.get("generated_at"),
            scheduled_slot=legacy.get("scheduled_slot"),
            scheduled_at=legacy.get("scheduled_at"),
            content_invitation_id=legacy.get("content_invitation_id"),
            confidence=float(confidence) if confidence is not None else None,
            source_message_cutoff_id=int(cutoff) if cutoff is not None else None,
            dedupe=legacy.get("dedupe") if isinstance(legacy.get("dedupe"), dict) else None,
            policy=legacy.get("policy") if isinstance(legacy.get("policy"), dict) else None,
            metadata=legacy.get("metadata") if isinstance(legacy.get("metadata"), dict) else None,
        )

    def to_legacy(self) -> Dict[str, Any]:
        """回到既有候选 dict（仅含 legacy 键；`features`/`scope` 不落库）。

        产物交给 `normalize_reactivation_candidate` 做最终校验/裁剪（upsert 内会调），
        故此处只需保证键齐全、不引入 legacy 之外的键。
        """
        legacy: Dict[str, Any] = {
            "id": self.candidate_id,
            "type": self.kind,
            "text": self.text,
        }
        values = {
            "topic": self.topic,
            "reason": self.reason,
            "generated_at": self.generated_at,
            "scheduled_slot": self.scheduled_slot,
            "scheduled_at": self.scheduled_at,
            "content_invitation_id": self.content_invitation_id,
        }
        for key in _LEGACY_OPTIONAL_TEXT_KEYS:
            value = values[key]
            if value is not None and str(value).strip():
                legacy[key] = value
        if self.confidence is not None:
            legacy["confidence"] = self.confidence
        if self.source_message_cutoff_id is not None:
            legacy["source_message_cutoff_id"] = self.source_message_cutoff_id
        for key, value in (
            ("dedupe", self.dedupe),
            ("policy", self.policy),
            ("metadata", self.metadata),
        ):
            if isinstance(value, dict):
                legacy[key] = value
        return legacy
