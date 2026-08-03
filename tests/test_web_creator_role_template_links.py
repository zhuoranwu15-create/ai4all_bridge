"""CRT-04：Web 新成员可信双归因与角色模板原子实例化。"""
from __future__ import annotations

import concurrent.futures
from datetime import timedelta
from unittest.mock import patch

import app.db as db
import pytest
from fastapi import HTTPException
from app.agent_runtime.persistence import profile_storage
from app.products.zhaoxi.application.creator_role_template_links import (
    publish_creator_role_template,
)
from app.products.zhaoxi.application.account_deletion import (
    execute_account_deletion,
)
from app.products.zhaoxi.domain.creator_role_templates import ZHAOXI_APP_ID
from app.products.zhaoxi.infrastructure.persistence.campaign import (
    apply_campaign_code_attribution,
    campaign_code_exists,
    get_campaign_attribution,
)
from app.products.zhaoxi.infrastructure.persistence.campaign_analytics import (
    get_campaign_funnel,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    create_creator_role_template,
    get_account_creator_role_template_attribution,
    get_creator_role_template,
    list_creator_role_template_events,
    list_creator_role_templates,
)
from app.products.zhaoxi.infrastructure.profiles import (
    render_creator_role_template_identity,
    render_creator_role_template_mission,
    render_creator_role_template_soul,
)
from app.routers.web import (
    WebLoginRequest,
    WebRegisterAndBindingIntentRequest,
    web_login,
    web_register_and_binding_intent,
)
from app.time_utils import beijing_naive_now, beijing_now


@pytest.fixture(autouse=True)
def _enable_creator_role_templates(fresh_db):
    """CRT-04 主路径默认在能力开启状态验证；关闭行为由专门用例覆盖。"""
    fresh_db.creator_role_templates_enabled = True
    return fresh_db


def _verified_token(phone: str) -> str:
    db.create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    verification = db.get_latest_active_verification(phone)
    return db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]


def _seed_creator(phone: str, suffix: str):
    registration = db.register_platform_user_with_referral(phone=phone)
    creator_id = registration["platform_user"]["id"]
    invite_code = db.get_or_create_personal_referral_code_for_user(
        platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
    )["code"]
    created = create_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        ai_name=f"朝朝{suffix}",
        personality_text=f"温柔坦诚的底色{suffix}",
        mission_text=f"陪用户找到自己的生活节奏{suffix}",
        opening_line=f"我是朝朝{suffix}，很高兴认识你。",
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE creator_role_template_versions
            SET review_status = 'passed', reviewed_at = created_at,
                generated_summary = ai_name, public_summary = ai_name,
                summary_edit_status = 'available'
            WHERE id = ?
            """,
            (created.version.id,),
        )
        conn.execute(
            "UPDATE creator_role_templates SET status = 'approved' WHERE id = ?",
            (created.template.id,),
        )
    published = publish_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=created.template.id,
        version_id=created.version.id,
        now=beijing_naive_now() - timedelta(minutes=1),
    )
    return creator_id, invite_code, published


def _web_login(*, phone: str, invite_code: str | None, campaign_code: str | None):
    payload = WebLoginRequest(
        phone=phone,
        verified_token=_verified_token(phone),
        invite_code=invite_code,
        campaign_code=campaign_code,
    )
    with patch(
        "app.routers.web._start_openclaw_qr_for_binding",
        side_effect=lambda binding_intent: binding_intent,
    ):
        return web_login(payload)


def test_web_new_member_applies_referral_and_template_snapshot_atomically(fresh_db):
    creator_id, invite_code, published = _seed_creator(
        "13810001001", "甲"
    )

    response = _web_login(
        phone="13910001001",
        invite_code=invite_code,
        campaign_code=published.template.campaign_code,
    )

    account_id = response["account"]["id"]
    result = response["role_template_link_result"]
    assert result == {"applied": True}

    relationships = db.list_referral_relationships(
        inviter_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
    )
    assert len(relationships) == 1
    assert relationships[0]["invitee_platform_user_id"] == response["platform_user"]["id"]

    common = get_campaign_attribution(account_id=account_id)
    snapshot = get_account_creator_role_template_attribution(account_id=account_id)
    assert common["campaign_code"] == published.template.campaign_code
    assert all(
        common[field] is None
        for field in (
            "mission_id",
            "onboarding_script_variant",
            "soul_preset_key",
            "ai_name_preset",
        )
    )
    assert snapshot["creator_platform_user_id"] == creator_id
    assert snapshot["creator_role_template_version_id"] == published.version.id
    assert profile_storage.read_file(account_id, "IDENTITY.md") == (
        render_creator_role_template_identity(published.version.ai_name)
    )
    assert profile_storage.read_file(account_id, "SOUL.md") == (
        render_creator_role_template_soul(
            published.version.ai_name,
            published.version.personality_text,
        )
    )
    assert profile_storage.read_file(account_id, "MISSION.md") == (
        render_creator_role_template_mission(published.version.mission_text)
    )

    stored = get_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert stored.used_count == 1
    events = list_creator_role_template_events(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert sum(event["event_type"] == "attribution_applied" for event in events) == 1
    today = beijing_now().date().isoformat()
    funnel = get_campaign_funnel(
        campaign_code=published.template.campaign_code,
        date_from=today,
        date_to=today,
    )
    assert funnel["totals"]["registered"] == 1


def test_mixed_invite_and_template_keeps_referral_but_falls_back_to_default(fresh_db):
    creator_a, invite_a, _ = _seed_creator("13810001002", "甲")
    creator_b, _, template_b = _seed_creator("13810001003", "乙")

    response = _web_login(
        phone="13910001002",
        invite_code=invite_a,
        campaign_code=template_b.template.campaign_code,
    )

    account_id = response["account"]["id"]
    assert response["role_template_link_result"] == {
        "applied": False,
        "reason": "creator_mismatch",
    }
    assert get_campaign_attribution(account_id=account_id) is None
    assert get_account_creator_role_template_attribution(account_id=account_id) is None
    assert db.list_referral_relationships(
        inviter_platform_user_id=creator_a,
        app_id=ZHAOXI_APP_ID,
    )[0]["invitee_platform_user_id"] == response["platform_user"]["id"]
    assert db.list_referral_relationships(
        inviter_platform_user_id=creator_b,
        app_id=ZHAOXI_APP_ID,
    ) == []


def test_register_and_binding_intent_preserves_trusted_referral_context(fresh_db):
    creator_id, invite_code, published = _seed_creator("13810001010", "绑定")
    phone = "13910001010"
    payload = WebRegisterAndBindingIntentRequest(
        phone=phone,
        otp_token=_verified_token(phone),
        invite_code=invite_code,
        campaign_code=published.template.campaign_code,
    )
    with patch(
        "app.routers.web._start_openclaw_qr_for_binding",
        side_effect=lambda binding_intent: binding_intent,
    ):
        response = web_register_and_binding_intent(payload)

    assert response["role_template_link_result"]["applied"] is True
    snapshot = get_account_creator_role_template_attribution(
        account_id=response["account"]["id"]
    )
    assert snapshot["creator_platform_user_id"] == creator_id


def test_invalid_invite_cannot_use_valid_template_as_owner_proof(fresh_db):
    creator_id, _, published = _seed_creator("13810001012", "无邀请")

    with pytest.raises(HTTPException) as exc_info:
        _web_login(
            phone="13910001012",
            invite_code="INVALID_INVITE",
            campaign_code=published.template.campaign_code,
        )

    assert exc_info.value.status_code == 400
    stored = get_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert stored.used_count == 0


def test_existing_member_and_creator_current_ai_never_consume_template(fresh_db):
    creator_id, _, published = _seed_creator("13810001004", "守护")
    creator_account = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=creator_id,
        campaign_code=published.template.campaign_code,
        expected_creator_platform_user_id=creator_id,
        is_new_membership=False,
    )

    account_id = creator_account["account"]["id"]
    assert creator_account["role_template_link_result"]["reason"] == (
        "not_new_membership"
    )
    assert get_campaign_attribution(account_id=account_id) is None
    assert get_account_creator_role_template_attribution(account_id=account_id) is None

    # 重复登录直接复用 existing account，不再进入任何 campaign dispatcher。
    repeated = _web_login(
        phone="13810001004",
        invite_code=None,
        campaign_code=published.template.campaign_code,
    )
    assert repeated["account"]["id"] == account_id
    assert repeated["role_template_link_result"] is None
    assert get_account_creator_role_template_attribution(account_id=account_id) is None


def test_template_attribution_failure_rolls_back_all_template_side_effects(
    fresh_db,
):
    creator_id, _, published = _seed_creator("13810001005", "回滚")
    invitee = db.register_platform_user_with_referral(phone="13910001005")
    account = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=invitee["platform_user"]["id"]
    )
    account_id = account["account"]["id"]

    with patch(
        "app.products.zhaoxi.infrastructure.persistence.creator_role_templates."
        "write_creator_role_template_snapshot",
        side_effect=RuntimeError("profile write failed"),
    ):
        result = apply_campaign_code_attribution(
            account_id=account_id,
            campaign_code=published.template.campaign_code,
            expected_creator_platform_user_id=creator_id,
            is_new_membership=True,
        )

    assert result["applied"] is False
    assert result["reason"] == "error"
    assert get_campaign_attribution(account_id=account_id) is None
    assert get_account_creator_role_template_attribution(account_id=account_id) is None
    assert profile_storage.read_file(account_id, "IDENTITY.md") is None
    stored = get_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert stored.used_count == 0


def test_template_dispatcher_is_idempotent_and_debug_context_cannot_consume(fresh_db):
    creator_id, _, published = _seed_creator("13810001006", "幂等")
    invitee = db.register_platform_user_with_referral(phone="13910001006")
    account = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=invitee["platform_user"]["id"]
    )
    account_id = account["account"]["id"]

    rejected = apply_campaign_code_attribution(
        account_id=account_id,
        campaign_code=published.template.campaign_code,
        increment_usage=False,
    )
    assert rejected["reason"] == "not_new_membership"

    first = apply_campaign_code_attribution(
        account_id=account_id,
        campaign_code=published.template.campaign_code,
        expected_creator_platform_user_id=creator_id,
        is_new_membership=True,
    )
    second = apply_campaign_code_attribution(
        account_id=account_id,
        campaign_code=published.template.campaign_code,
        expected_creator_platform_user_id=creator_id,
        is_new_membership=True,
    )
    assert first["applied"] is True
    assert second["reason"] == "already_attributed"
    stored = get_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert stored.used_count == 1


def test_concurrent_template_attribution_counts_exactly_once(fresh_db):
    creator_id, _, published = _seed_creator("13810001013", "并发")
    invitee = db.register_platform_user_with_referral(phone="13910001013")
    account = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=invitee["platform_user"]["id"]
    )
    account_id = account["account"]["id"]

    def apply_once():
        return apply_campaign_code_attribution(
            account_id=account_id,
            campaign_code=published.template.campaign_code,
            expected_creator_platform_user_id=creator_id,
            is_new_membership=True,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: apply_once(), range(2)))

    assert sum(result["applied"] is True for result in results) == 1
    assert sorted(
        result.get("reason", "applied") for result in results
    ) == ["already_attributed", "applied"]
    stored = get_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert stored.used_count == 1


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("status", "disabled_creator", "disabled"),
        ("expires_at", "2000-01-01 00:00:00", "expired"),
    ),
)
def test_inactive_template_falls_back_without_partial_attribution(
    fresh_db,
    field,
    value,
    reason,
):
    creator_id, _, published = _seed_creator("13810001011", reason)
    invitee = db.register_platform_user_with_referral(phone="13910001011")
    account = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=invitee["platform_user"]["id"]
    )
    with db.connect() as conn:
        conn.execute(
            f"UPDATE creator_role_templates SET {field} = ? WHERE id = ?",
            (value, published.template.id),
        )

    result = apply_campaign_code_attribution(
        account_id=account["account"]["id"],
        campaign_code=published.template.campaign_code,
        expected_creator_platform_user_id=creator_id,
        is_new_membership=True,
    )

    assert result["reason"] == reason
    assert get_campaign_attribution(account_id=account["account"]["id"]) is None
    assert get_account_creator_role_template_attribution(
        account_id=account["account"]["id"]
    ) is None


def test_template_code_exists_for_campaign_visit_even_when_disabled(fresh_db):
    creator_id, _, published = _seed_creator("13810001007", "曝光")
    assert campaign_code_exists(code=published.template.campaign_code) is True
    with db.connect() as conn:
        conn.execute(
            "UPDATE creator_role_templates SET status = 'disabled_creator' WHERE id = ?",
            (published.template.id,),
        )
    assert campaign_code_exists(code=published.template.campaign_code) is True
    assert campaign_code_exists(code="urt_missing") is False


def test_wipe_deletes_template_snapshot_but_preserves_common_funnel_anchor(fresh_db):
    creator_id, _, published = _seed_creator("13810001008", "清理")
    invitee = db.register_platform_user_with_referral(phone="13910001008")
    account = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=invitee["platform_user"]["id"]
    )
    account_id = account["account"]["id"]
    applied = apply_campaign_code_attribution(
        account_id=account_id,
        campaign_code=published.template.campaign_code,
        expected_creator_platform_user_id=creator_id,
        is_new_membership=True,
    )
    assert applied["applied"] is True

    stats = db.wipe_account_data(account_id=account_id)

    assert stats["account_creator_role_template_attribution_deleted"] == 1
    assert get_account_creator_role_template_attribution(account_id=account_id) is None
    assert get_campaign_attribution(account_id=account_id) is not None
    assert profile_storage.read_file(account_id, "IDENTITY.md") is None


def test_creator_account_deletion_soft_deletes_owned_templates(fresh_db):
    creator_id, _, published = _seed_creator("13810001009", "注销")

    stats = execute_account_deletion(
        platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        now=beijing_naive_now(),
    )

    assert stats["creator_role_templates_deleted"] == 1
    templates = list_creator_role_templates(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        include_deleted=True,
    )
    assert len(templates) == 1
    assert templates[0].id == published.template.id
    assert templates[0].status == "deleted"
    assert templates[0].deleted_at is not None


def test_feature_flag_stops_new_instances_but_preserves_existing_snapshot(fresh_db):
    creator_id, invite_code, published = _seed_creator("13810001012", "灰度")

    first = _web_login(
        phone="13910001012",
        invite_code=invite_code,
        campaign_code=published.template.campaign_code,
    )
    first_account_id = first["account"]["id"]
    first_snapshot = get_account_creator_role_template_attribution(
        account_id=first_account_id
    )
    assert first["role_template_link_result"] == {"applied": True}
    assert first_snapshot is not None
    identity_before = profile_storage.read_file(first_account_id, "IDENTITY.md")

    fresh_db.creator_role_templates_enabled = False
    second = _web_login(
        phone="13910001013",
        invite_code=invite_code,
        campaign_code=published.template.campaign_code,
    )
    second_account_id = second["account"]["id"]

    assert second["role_template_link_result"] == {
        "applied": False,
        "reason": "capability_disabled",
    }
    assert get_account_creator_role_template_attribution(
        account_id=second_account_id
    ) is None
    # 邀请关系仍成功，能力开关只关闭模板实例化。
    with db.connect() as conn:
        relationship = conn.execute(
            """
            SELECT inviter_platform_user_id FROM referral_relationships
            WHERE invitee_platform_user_id = ? AND app_id = ?
            """,
            (second["platform_user"]["id"], ZHAOXI_APP_ID),
        ).fetchone()
    assert relationship["inviter_platform_user_id"] == creator_id

    # 已实例化账号继续使用自己的不可变快照/profile，不依赖 live flag。
    assert get_account_creator_role_template_attribution(
        account_id=first_account_id
    ) == first_snapshot
    assert profile_storage.read_file(first_account_id, "IDENTITY.md") == identity_before
    current = get_creator_role_template(
        creator_platform_user_id=creator_id,
        app_id=ZHAOXI_APP_ID,
        template_id=published.template.id,
    )
    assert current.used_count == 1
