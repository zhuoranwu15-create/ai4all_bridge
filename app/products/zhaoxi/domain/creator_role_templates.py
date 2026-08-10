"""用户自建角色模板的领域常量、校验、状态与只读数据对象。"""
from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

ZHAOXI_APP_ID = "zhaoxi"
CREATOR_ROLE_TEMPLATE_CODE_PREFIX = "urt_"
CREATOR_ROLE_TEMPLATE_MAX_SLOTS = 3
CREATOR_ROLE_TEMPLATE_CODE_RANDOM_BYTES = 24
CREATOR_ROLE_TEMPLATE_CODE_MAX_CHARS = 64
CREATOR_ROLE_TEMPLATE_CODE_GENERATION_RETRIES = 20

AI_NAME_MIN_CHARS = 1
AI_NAME_MAX_CHARS = 24
PERSONALITY_MIN_CHARS = 1
PERSONALITY_MAX_CHARS = 800
MISSION_MIN_CHARS = 1
MISSION_MAX_CHARS = 500
OPENING_LINE_MIN_CHARS = 1
OPENING_LINE_MAX_CHARS = 300
PUBLIC_SUMMARY_MIN_CHARS = 1
PUBLIC_SUMMARY_MAX_CHARS = 60
REVIEW_REASON_MAX_CHARS = 1000
REVIEW_CATEGORIES_JSON_MAX_CHARS = 2000
CREATOR_ROLE_TEMPLATE_REVIEW_CATEGORIES = frozenset(
    {
        "prompt_injection",
        "real_person_impersonation",
        "minor_persona",
        "illegal_or_dangerous",
        "self_harm",
        "hate_or_abuse",
        "sexual_content",
        "professional_deception",
        "unsafe_companion_role",
        "other_unsafe_content",
    }
)
EVENT_METADATA_JSON_MAX_CHARS = 2000
DISABLED_REASON_MAX_CHARS = 500
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_CODE_PATTERN = re.compile(r"^urt_[A-Za-z0-9_-]{22,60}$")


class CreatorRoleTemplateStatus(str, Enum):
    """角色模板持有记录的持久化状态。"""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    ACTIVE = "active"
    REJECTED = "rejected"
    DISABLED_CREATOR = "disabled_creator"
    DISABLED_ADMIN = "disabled_admin"
    DELETED = "deleted"


class CreatorRoleTemplateReviewStatus(str, Enum):
    """一个不可变模板版本的审核状态。"""

    PENDING = "pending"
    REVIEWING = "reviewing"
    PASSED = "passed"
    REJECTED = "rejected"


class CreatorRoleTemplateReviewRunStatus(str, Enum):
    """一次 LLM 审核调用的执行结果。"""

    RUNNING = "running"
    PASSED = "passed"
    REJECTED = "rejected"
    ERROR = "error"


class CreatorRoleTemplateSummaryEditStatus(str, Enum):
    """一个版本的一次性公开简介编辑状态。"""

    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class CreatorRoleTemplateEventType(str, Enum):
    """模板生命周期审计事件类型。"""

    CREATED = "created"
    REVIEW_STARTED = "review_started"
    REVIEW_PASSED = "review_passed"
    REVIEW_REJECTED = "review_rejected"
    REVIEW_ERROR = "review_error"
    VERSION_ACTIVATED = "version_activated"
    DISABLED_BY_CREATOR = "disabled_by_creator"
    ENABLED = "enabled"
    DISABLED_BY_ADMIN = "disabled_by_admin"
    DELETED = "deleted"
    ATTRIBUTION_APPLIED = "attribution_applied"


class CreatorRoleTemplateActorType(str, Enum):
    """触发模板审计事件的主体类型。"""

    CREATOR = "creator"
    ADMIN = "admin"
    SYSTEM = "system"


class CreatorRoleTemplateError(ValueError):
    """角色模板领域错误；API 层可把 ``code`` 映射为稳定错误响应。"""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class CreatorRoleTemplateContent:
    """一次需要整体审核和版本化的四字段角色模板内容。"""

    ai_name: str
    personality_text: str
    mission_text: str
    opening_line: Optional[str]


@dataclass(frozen=True)
class CreatorRoleTemplate:
    """角色模板 owner 记录，不含任何被邀请账号的数据。"""

    id: str
    app_id: str
    creator_platform_user_id: str
    slot_no: int
    campaign_code: str
    status: str
    used_count: int
    activated_at: Optional[str]
    expires_at: Optional[str]
    disabled_by_admin_user_id: Optional[str]
    disabled_reason: Optional[str]
    status_before_admin_disable: Optional[str]
    deleted_at: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CreatorRoleTemplate":
        """从 repository row 构造领域 DTO。"""
        return cls(
            id=row["id"],
            app_id=row["app_id"],
            creator_platform_user_id=row["creator_platform_user_id"],
            slot_no=int(row["slot_no"]),
            campaign_code=row["campaign_code"],
            status=row["status"],
            used_count=int(row["used_count"] or 0),
            activated_at=row["activated_at"],
            expires_at=row["expires_at"],
            disabled_by_admin_user_id=row["disabled_by_admin_user_id"],
            disabled_reason=row["disabled_reason"],
            status_before_admin_disable=row["status_before_admin_disable"],
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class CreatorRoleTemplateVersion:
    """角色模板的不可变内容版本和当前审核/发布投影。"""

    id: str
    creator_role_template_id: str
    version_no: int
    ai_name: str
    personality_text: str
    mission_text: str
    opening_line: Optional[str]
    generated_summary: Optional[str]
    public_summary: Optional[str]
    summary_edit_status: str
    review_status: str
    is_published: bool
    review_categories_json: str
    review_reason: Optional[str]
    reviewed_at: Optional[str]
    published_at: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CreatorRoleTemplateVersion":
        """从 repository row 构造版本 DTO。"""
        return cls(
            id=row["id"],
            creator_role_template_id=row["creator_role_template_id"],
            version_no=int(row["version_no"]),
            ai_name=row["ai_name"],
            personality_text=row["personality_text"],
            mission_text=row["mission_text"],
            opening_line=row["opening_line"],
            generated_summary=row["generated_summary"],
            public_summary=row["public_summary"],
            summary_edit_status=str(row["summary_edit_status"] or "unavailable"),
            review_status=row["review_status"],
            is_published=bool(row["is_published"]),
            review_categories_json=row["review_categories_json"],
            review_reason=row["review_reason"],
            reviewed_at=row["reviewed_at"],
            published_at=row["published_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class CreatedCreatorRoleTemplate:
    """创建事务返回的模板与首个完整版本。"""

    template: CreatorRoleTemplate
    version: CreatorRoleTemplateVersion


@dataclass(frozen=True)
class CreatorRoleTemplateReview:
    """一次四字段整体审核及默认简介生成结果；不包含原始模型输出。"""

    decision: str
    field_results: Dict[str, str]
    categories: Tuple[str, ...]
    reason: str
    generated_summary: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    latency_ms: int = 0
    error_code: Optional[str] = None
    failure_code: Optional[str] = None

    @property
    def available(self) -> bool:
        """审核是否得到可落库的 pass/reject 结论。"""
        return self.decision in {"pass", "reject"} and self.error_code is None


@dataclass(frozen=True)
class CreatorRoleTemplateReviewClaim:
    """已在数据库 claim 的审核 run 及其不可变版本内容。"""

    run_id: str
    attempt_no: int
    template: CreatorRoleTemplate
    version: CreatorRoleTemplateVersion


@dataclass(frozen=True)
class CreatorRoleTemplateMutation:
    """一次生命周期事务完成后的模板与目标版本投影。"""

    template: CreatorRoleTemplate
    version: CreatorRoleTemplateVersion


@dataclass(frozen=True)
class CreatorRoleTemplateReviewOutcome:
    """一次持久化审核编排的结论、run 标识和最终模板投影。"""

    run_id: str
    review: CreatorRoleTemplateReview
    mutation: CreatorRoleTemplateMutation


@dataclass(frozen=True)
class CreatorRoleTemplateSummaryReview:
    """创建者一次简介修改的安全审核结果。"""

    decision: str
    categories: Tuple[str, ...]
    reason: str
    model: Optional[str] = None
    provider: Optional[str] = None
    latency_ms: int = 0
    error_code: Optional[str] = None
    failure_code: Optional[str] = None

    @property
    def available(self) -> bool:
        """是否得到会消耗编辑机会的明确 pass/reject 结论。"""
        return self.decision in {"pass", "reject"} and self.error_code is None


@dataclass(frozen=True)
class CreatorRoleTemplateSummaryReviewClaim:
    """已 CAS claim 的简介修改审核及其上下文。"""

    run_id: str
    attempt_no: int
    template: CreatorRoleTemplate
    version: CreatorRoleTemplateVersion
    submitted_summary: str


@dataclass(frozen=True)
class CreatorRoleTemplateSummaryReviewOutcome:
    """一次简介修改审核完成后的版本投影。"""

    run_id: str
    review: CreatorRoleTemplateSummaryReview
    mutation: CreatorRoleTemplateMutation


def _normalize_field(
    value: str,
    *,
    field_name: str,
    min_chars: int,
    max_chars: int,
    allow_newlines: bool,
) -> str:
    cleaned = str(value or "").strip()
    if not (min_chars <= len(cleaned) <= max_chars):
        raise CreatorRoleTemplateError(f"{field_name}_length_invalid")
    if not allow_newlines and ("\n" in cleaned or "\r" in cleaned):
        raise CreatorRoleTemplateError(f"{field_name}_must_be_single_line")
    for char in cleaned:
        if char == "\n" and allow_newlines:
            continue
        if unicodedata.category(char) in {"Cc", "Cf"}:
            raise CreatorRoleTemplateError(f"{field_name}_contains_control_characters")
    return cleaned


def normalize_creator_role_template_content(
    *, ai_name: str, personality_text: str, mission_text: str,
    opening_line: Optional[str]
) -> CreatorRoleTemplateContent:
    """机械校验并规范化四字段；``None`` 仅用于兼容历史已发布版本。"""
    return CreatorRoleTemplateContent(
        ai_name=_normalize_field(
            ai_name,
            field_name="ai_name",
            min_chars=AI_NAME_MIN_CHARS,
            max_chars=AI_NAME_MAX_CHARS,
            allow_newlines=False,
        ),
        personality_text=_normalize_field(
            personality_text,
            field_name="personality_text",
            min_chars=PERSONALITY_MIN_CHARS,
            max_chars=PERSONALITY_MAX_CHARS,
            allow_newlines=True,
        ),
        mission_text=_normalize_field(
            mission_text,
            field_name="mission_text",
            min_chars=MISSION_MIN_CHARS,
            max_chars=MISSION_MAX_CHARS,
            allow_newlines=True,
        ),
        opening_line=(
            None
            if opening_line is None
            else _normalize_field(
                opening_line,
                field_name="opening_line",
                min_chars=OPENING_LINE_MIN_CHARS,
                max_chars=OPENING_LINE_MAX_CHARS,
                allow_newlines=True,
            )
        ),
    )


def normalize_creator_role_template_summary(*, summary: str, ai_name: str) -> str:
    """校验公开简介的单行、长度与必须包含角色名字约束。"""
    cleaned = _normalize_field(
        summary,
        field_name="public_summary",
        min_chars=PUBLIC_SUMMARY_MIN_CHARS,
        max_chars=PUBLIC_SUMMARY_MAX_CHARS,
        allow_newlines=False,
    )
    normalized_name = _normalize_field(
        ai_name,
        field_name="ai_name",
        min_chars=AI_NAME_MIN_CHARS,
        max_chars=AI_NAME_MAX_CHARS,
        allow_newlines=False,
    )
    if normalized_name not in cleaned:
        raise CreatorRoleTemplateError("public_summary_must_include_ai_name")
    return cleaned


def new_creator_role_template_campaign_code() -> str:
    """生成带保留前缀、至少 128 bit 熵的 URL-safe 模板活动码。"""
    return CREATOR_ROLE_TEMPLATE_CODE_PREFIX + secrets.token_urlsafe(
        CREATOR_ROLE_TEMPLATE_CODE_RANDOM_BYTES
    )


def is_creator_role_template_campaign_code(code: str) -> bool:
    """判断 code 是否符合系统生成的用户角色模板命名空间与格式。"""
    value = str(code or "")
    return (
        len(value) <= CREATOR_ROLE_TEMPLATE_CODE_MAX_CHARS
        and _CODE_PATTERN.fullmatch(value) is not None
    )


__all__ = [
    "AI_NAME_MAX_CHARS",
    "CREATOR_ROLE_TEMPLATE_REVIEW_CATEGORIES",
    "CREATOR_ROLE_TEMPLATE_CODE_GENERATION_RETRIES",
    "CREATOR_ROLE_TEMPLATE_CODE_PREFIX",
    "CREATOR_ROLE_TEMPLATE_MAX_SLOTS",
    "DATETIME_FORMAT",
    "DISABLED_REASON_MAX_CHARS",
    "EVENT_METADATA_JSON_MAX_CHARS",
    "MISSION_MAX_CHARS",
    "OPENING_LINE_MAX_CHARS",
    "PERSONALITY_MAX_CHARS",
    "PUBLIC_SUMMARY_MAX_CHARS",
    "REVIEW_CATEGORIES_JSON_MAX_CHARS",
    "REVIEW_REASON_MAX_CHARS",
    "ZHAOXI_APP_ID",
    "CreatedCreatorRoleTemplate",
    "CreatorRoleTemplate",
    "CreatorRoleTemplateActorType",
    "CreatorRoleTemplateContent",
    "CreatorRoleTemplateError",
    "CreatorRoleTemplateEventType",
    "CreatorRoleTemplateReviewRunStatus",
    "CreatorRoleTemplateReviewStatus",
    "CreatorRoleTemplateSummaryEditStatus",
    "CreatorRoleTemplateReview",
    "CreatorRoleTemplateReviewClaim",
    "CreatorRoleTemplateReviewOutcome",
    "CreatorRoleTemplateStatus",
    "CreatorRoleTemplateVersion",
    "CreatorRoleTemplateMutation",
    "CreatorRoleTemplateSummaryReview",
    "CreatorRoleTemplateSummaryReviewClaim",
    "CreatorRoleTemplateSummaryReviewOutcome",
    "is_creator_role_template_campaign_code",
    "new_creator_role_template_campaign_code",
    "normalize_creator_role_template_content",
    "normalize_creator_role_template_summary",
]
