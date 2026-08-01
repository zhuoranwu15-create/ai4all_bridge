"""用户角色模板的版本发布、有效期和启停生命周期服务。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

from app.db._core import connect
from app.products.zhaoxi.domain.creator_role_templates import (
    DATETIME_FORMAT,
    CreatorRoleTemplate,
    CreatorRoleTemplateError,
    CreatorRoleTemplateMutation,
    CreatorRoleTemplateVersion,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    create_creator_role_template_candidate_version,
    disable_creator_role_template_by_admin,
    enable_creator_role_template_by_admin,
    get_creator_role_template,
    publish_creator_role_template_version,
    set_creator_role_template_creator_enabled,
)
from app.time_utils import beijing_naive_now, parse_db_timestamp

CREATOR_ROLE_TEMPLATE_VALIDITY_DAYS = 360


def require_creator_role_template_eligibility(
    *, creator_platform_user_id: str, app_id: str
) -> Dict[str, str]:
    """无副作用校验创建资格并返回 active 默认账号与可用个人邀请码。"""
    owner_id = str(creator_platform_user_id or "").strip()
    cleaned_app_id = str(app_id or "").strip()
    if not owner_id or cleaned_app_id != "zhaoxi":
        raise CreatorRoleTemplateError("creator_role_template_not_eligible")
    with connect() as conn:
        membership = conn.execute(
            """
            SELECT 1 FROM product_memberships
            WHERE platform_user_id = ? AND app_id = ? AND status = 'active'
            """,
            (owner_id, cleaned_app_id),
        ).fetchone()
        account = conn.execute(
            """
            SELECT a.id, a.onboarding_state
            FROM account_owner_bindings AS b
            JOIN accounts AS a ON a.id = b.account_id
            WHERE b.platform_user_id = ? AND b.app_id = ? AND b.status = 'active'
              AND a.app_id = ? AND a.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
            """,
            (owner_id, cleaned_app_id, cleaned_app_id),
        ).fetchone()
        referral = conn.execute(
            """
            SELECT code FROM referral_codes
            WHERE platform_user_id = ? AND app_id = ? AND code_type = 'personal'
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > datetime('now', '+8 hours'))
              AND (max_uses IS NULL OR used_count < max_uses)
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (owner_id, cleaned_app_id),
        ).fetchone()
    if (
        membership is None
        or account is None
        or account["onboarding_state"] not in {"complete", "timed_out"}
        or referral is None
    ):
        raise CreatorRoleTemplateError("creator_role_template_not_eligible")
    return {"account_id": str(account["id"]), "invite_code": str(referral["code"])}


def get_creator_personal_invite_code(
    *, creator_platform_user_id: str, app_id: str
) -> Optional[str]:
    """只读取得 creator 当前可用 personal code；不存在时不自动创建。"""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT code FROM referral_codes
            WHERE platform_user_id = ? AND app_id = ? AND code_type = 'personal'
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > datetime('now', '+8 hours'))
              AND (max_uses IS NULL OR used_count < max_uses)
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (creator_platform_user_id, app_id),
        ).fetchone()
    return str(row["code"]) if row is not None else None


def _now(value: Optional[datetime]) -> datetime:
    current = value or beijing_naive_now()
    return current.replace(tzinfo=None, microsecond=0)


def effective_creator_role_template_status(
    template: CreatorRoleTemplate, *, now: Optional[datetime] = None
) -> str:
    """投影请求时有效状态；过期不回写数据库，也不依赖 scheduler。"""
    if template.deleted_at is not None or template.status == "deleted":
        return "deleted"
    if template.status == "disabled_admin":
        return "disabled_admin"
    expires_at = parse_db_timestamp(template.expires_at)
    if expires_at is not None and _now(now) >= expires_at:
        return "expired"
    return template.status


def create_creator_role_template_version(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    ai_name: Optional[str] = None,
    personality_text: Optional[str] = None,
    mission_text: Optional[str] = None,
) -> CreatorRoleTemplateVersion:
    """合并部分字段并创建需要整体重审的完整候选版本。"""
    return create_creator_role_template_candidate_version(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        ai_name=ai_name,
        personality_text=personality_text,
        mission_text=mission_text,
    )


def publish_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    now: Optional[datetime] = None,
) -> CreatorRoleTemplateMutation:
    """主动发布 passed 版本；首次固定 360 天，后续发布绝不续期。"""
    current = _now(now)
    return publish_creator_role_template_version(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        version_id=version_id,
        activated_at=current.strftime(DATETIME_FORMAT),
        first_expires_at=(
            current + timedelta(days=CREATOR_ROLE_TEMPLATE_VALIDITY_DAYS)
        ).strftime(DATETIME_FORMAT),
    )


def disable_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    now: Optional[datetime] = None,
) -> CreatorRoleTemplate:
    """创建者停用当前 active 链接，不改变首次激活和到期时间。"""
    return set_creator_role_template_creator_enabled(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        enabled=False,
        changed_at=_now(now).strftime(DATETIME_FORMAT),
    )


def enable_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    now: Optional[datetime] = None,
) -> CreatorRoleTemplate:
    """创建者仅能在原有效期内恢复自己停用的链接；不能解除 Admin 停用。"""
    current = _now(now)
    template = get_creator_role_template(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
    )
    if template is None:
        raise CreatorRoleTemplateError("creator_role_template_not_found")
    if effective_creator_role_template_status(template, now=current) == "expired":
        raise CreatorRoleTemplateError("creator_role_template_expired")
    return set_creator_role_template_creator_enabled(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        enabled=True,
        changed_at=current.strftime(DATETIME_FORMAT),
    )


def admin_disable_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    admin_user_id: str,
    reason: str,
    now: Optional[datetime] = None,
) -> CreatorRoleTemplate:
    """Admin/Staff 处置模板并保留恢复所需状态。"""
    return disable_creator_role_template_by_admin(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        admin_user_id=admin_user_id,
        reason=reason,
        changed_at=_now(now).strftime(DATETIME_FORMAT),
    )


def admin_enable_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    admin_user_id: str,
    now: Optional[datetime] = None,
) -> CreatorRoleTemplate:
    """仅 Admin/Staff 恢复未过期模板到停用前状态；不会重算有效期。"""
    current = _now(now)
    template = get_creator_role_template(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
    )
    if template is None:
        raise CreatorRoleTemplateError("creator_role_template_not_found")
    expires_at = parse_db_timestamp(template.expires_at)
    if expires_at is not None and current >= expires_at:
        raise CreatorRoleTemplateError("creator_role_template_expired")
    return enable_creator_role_template_by_admin(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        admin_user_id=admin_user_id,
        changed_at=current.strftime(DATETIME_FORMAT),
    )


__all__ = [
    "CREATOR_ROLE_TEMPLATE_VALIDITY_DAYS",
    "admin_disable_creator_role_template",
    "admin_enable_creator_role_template",
    "create_creator_role_template_version",
    "disable_creator_role_template",
    "effective_creator_role_template_status",
    "enable_creator_role_template",
    "get_creator_personal_invite_code",
    "publish_creator_role_template",
    "require_creator_role_template_eligibility",
]
