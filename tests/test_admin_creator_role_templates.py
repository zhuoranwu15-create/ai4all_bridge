"""CRT-06：Admin/Staff 用户角色模板查询、处置与后台静态契约。"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.db as db
from app.db import SessionPrincipal
from app.products.zhaoxi.api import admin_creator_role_templates as admin_api
from app.products.zhaoxi.api import creator_role_templates as creator_api
from app.products.zhaoxi.application import creator_role_template_review as review_app
from app.products.zhaoxi.infrastructure.persistence.campaign import (
    apply_campaign_code_attribution,
)
from app.routers.deps import require_admin_or_staff_user
from app.time_utils import beijing_naive_now


def _principal(user_id: str) -> SessionPrincipal:
    return SessionPrincipal(
        session_id="session-admin-template-test",
        platform_user_id=user_id,
        app_id="zhaoxi",
        expires_at="2099-01-01 00:00:00",
    )


def _eligible_creator(phone: str) -> SessionPrincipal:
    registration = db.register_platform_user_with_referral(
        phone=phone, app_id="zhaoxi"
    )
    user_id = registration["platform_user"]["id"]
    db.get_or_create_personal_referral_code_for_user(
        platform_user_id=user_id,
        app_id="zhaoxi",
    )
    account = db.get_or_create_default_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user_id
    )
    db.set_account_onboarding_state(
        account_id=account["account"]["id"],
        state="complete",
    )
    return _principal(user_id)


def _pass_review(monkeypatch) -> None:
    monkeypatch.setattr(
        review_app,
        "generate_completion",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "pass",
                "field_results": {
                    "ai_name": "pass",
                    "personality_text": "pass",
                    "mission_text": "pass",
                    "opening_line": "pass",
                },
                "categories": [],
                "reason": "",
                "public_summary": "朝朝，温柔坦诚的陪伴者",
            }
        ),
    )


def _admin_user(user_id: str, role: str) -> dict:
    """模拟真实鉴权 dependency 会先 upsert 的 Admin/Staff 主体。"""
    return db.upsert_admin_user(
        admin_user_id=user_id,
        role=role,
        display_name=user_id.title(),
    )


def _create_template(principal: SessionPrincipal) -> dict:
    return creator_api.creator_role_template_create(
        creator_api.CreatorRoleTemplateCreateRequest(
            ai_name="朝朝",
            personality_text="温柔坦诚，能尊重边界，也会提供清晰建议。",
            mission_text="陪伴用户更清楚地认识自己，并逐步找到适合自己的生活节奏。",
            opening_line="我是朝朝，很高兴认识你。以后想聊什么都可以告诉我。",
        ),
        principal,
    )["creator_role_template"]


def _publish_template(principal: SessionPrincipal, template: dict) -> dict:
    return creator_api.creator_role_template_publish(
        template["id"],
        creator_api.CreatorRoleTemplatePublishRequest(
            version_id=template["latest_version"]["id"]
        ),
        principal,
    )["creator_role_template"]


def test_admin_staff_list_detail_disable_enable_and_creator_lock(
    fresh_db, monkeypatch
):
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    owner = _eligible_creator("13820001101")
    active = _publish_template(owner, _create_template(owner))
    staff = _admin_user("staff", "staff")
    admin = _admin_user("admin", "admin")
    invitee = db.register_platform_user_with_referral(
        phone="13920001101", app_id="zhaoxi"
    )
    invitee_account = db.get_or_create_default_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=invitee["platform_user"]["id"]
    )
    assert apply_campaign_code_attribution(
        account_id=invitee_account["account"]["id"],
        campaign_code=active["campaign_code"],
        expected_creator_platform_user_id=owner.platform_user_id,
        is_new_membership=True,
        creator_role_templates_enabled=True,
    )["applied"] is True

    listed = admin_api.admin_list_creator_role_templates(
        creator=owner.platform_user_id,
        status="active",
        review_status="passed",
        date_from=None,
        date_to=None,
        limit=50,
        offset=0,
        _=staff,
    )
    assert listed["pagination"]["total"] == 1
    assert listed["creator_role_templates"][0]["id"] == active["id"]
    assert listed["creator_role_templates"][0]["latest_version"]["ai_name"] == "朝朝"
    assert listed["creator_role_templates"][0]["effective_status_display"] == "已发布"
    assert (
        listed["creator_role_templates"][0]["latest_version"][
            "review_status_display"
        ]
        == "审核通过"
    )

    detail = admin_api.admin_creator_role_template_detail(
        active["id"], None, None, admin
    )
    assert detail["creator_role_template"]["creator_platform_user_id"] == owner.platform_user_id
    assert len(detail["creator_role_template"]["versions"]) == 1
    assert len(detail["review_runs"]) == 1
    assert detail["review_runs"][0]["status_display"] == "审核通过"
    assert set(detail["stats"]) == {"campaign_code", "range", "totals", "rates", "by_day"}
    attribution_event = next(
        event
        for event in detail["events"]
        if event["event_type"] == "attribution_applied"
    )
    assert "account_id" not in json.loads(attribution_event["metadata_json"])

    disabled = admin_api.admin_disable_creator_role_template_link(
        active["id"],
        admin_api.AdminDisableCreatorRoleTemplateRequest(reason="违规风险复核"),
        staff,
    )["creator_role_template"]
    assert disabled["effective_status"] == "disabled_admin"
    assert disabled["disabled_by_admin_user_id"] == "staff"
    assert disabled["disabled_reason"] == "违规风险复核"

    with pytest.raises(HTTPException) as creator_restore:
        creator_api.creator_role_template_enable(active["id"], owner)
    assert creator_restore.value.status_code == 409
    assert creator_restore.value.detail == "creator_role_template_disabled_by_admin"

    disabled_detail = admin_api.admin_creator_role_template_detail(
        active["id"], None, None, staff
    )
    disabled_event = next(
        event
        for event in disabled_detail["events"]
        if event["event_type"] == "disabled_by_admin"
    )
    metadata = json.loads(disabled_event["metadata_json"])
    assert disabled_event["actor_id"] == "staff"
    assert metadata == {"previous_status": "active", "reason": "违规风险复核"}

    enabled = admin_api.admin_enable_creator_role_template_link(
        active["id"], admin
    )["creator_role_template"]
    assert enabled["effective_status"] == "active"
    enabled_detail = admin_api.admin_creator_role_template_detail(
        active["id"], None, None, admin
    )
    enabled_event = next(
        event
        for event in enabled_detail["events"]
        if event["event_type"] == "enabled" and event["actor_type"] == "admin"
    )
    assert enabled_event["actor_id"] == "admin"
    assert json.loads(enabled_event["metadata_json"])["restored_status"] == "active"


def test_admin_recovery_restores_unpublished_state_but_rejects_expired(
    fresh_db, monkeypatch
):
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    owner = _eligible_creator("13820001102")
    admin = _admin_user("admin", "admin")
    staff = _admin_user("staff", "staff")

    approved = _create_template(owner)
    admin_api.admin_disable_creator_role_template_link(
        approved["id"],
        admin_api.AdminDisableCreatorRoleTemplateRequest(reason="发布前风险检查"),
        admin,
    )
    restored = admin_api.admin_enable_creator_role_template_link(
        approved["id"], admin
    )["creator_role_template"]
    assert restored["effective_status"] == "approved"

    second_owner = _eligible_creator("13820001103")
    active = _publish_template(second_owner, _create_template(second_owner))
    admin_api.admin_disable_creator_role_template_link(
        active["id"],
        admin_api.AdminDisableCreatorRoleTemplateRequest(reason="临时停用"),
        staff,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE creator_role_templates SET expires_at = ? WHERE id = ?",
            (
                (beijing_naive_now() - timedelta(seconds=1)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                active["id"],
            ),
        )
    with pytest.raises(HTTPException) as expired:
        admin_api.admin_enable_creator_role_template_link(
            active["id"], staff
        )
    assert expired.value.status_code == 409
    assert expired.value.detail == "creator_role_template_expired"


def test_admin_filters_pagination_product_scope_and_date_validation(
    fresh_db, monkeypatch
):
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    first_owner = _eligible_creator("13820001104")
    second_owner = _eligible_creator("13820001105")
    first = _create_template(first_owner)
    _create_template(second_owner)

    owner_page = admin_api.admin_list_creator_role_templates(
        creator=first_owner.platform_user_id,
        status="approved",
        review_status="passed",
        date_from=None,
        date_to=None,
        limit=1,
        offset=0,
        _={"id": "admin", "role": "admin"},
    )
    assert owner_page["pagination"] == {"limit": 1, "offset": 0, "total": 1}
    assert [item["id"] for item in owner_page["creator_role_templates"]] == [first["id"]]

    all_page = admin_api.admin_list_creator_role_templates(
        creator=None,
        status=None,
        review_status=None,
        date_from=None,
        date_to=None,
        limit=1,
        offset=1,
        _={"id": "staff", "role": "staff"},
    )
    assert all_page["pagination"]["total"] == 2
    assert len(all_page["creator_role_templates"]) == 1

    with pytest.raises(HTTPException) as bad_date:
        admin_api.admin_list_creator_role_templates(
            creator=None,
            status=None,
            review_status=None,
            date_from="2026-13-01",
            date_to=None,
            limit=50,
            offset=0,
            _={"id": "admin", "role": "admin"},
        )
    assert bad_date.value.status_code == 400

    with pytest.raises(HTTPException) as not_found:
        admin_api.admin_creator_role_template_detail(
            "crtpl_unknown", None, None, {"id": "admin", "role": "admin"}
        )
    assert not_found.value.status_code == 404


def test_admin_routes_permissions_and_no_content_mutation_surface():
    route_methods = {
        (route.path, tuple(sorted(route.methods or []))) for route in admin_api.router.routes
    }
    assert route_methods == {
        ("/admin/creator-role-templates", ("GET",)),
        ("/admin/creator-role-templates/{template_id}", ("GET",)),
        ("/admin/creator-role-templates/{template_id}/disable", ("POST",)),
        ("/admin/creator-role-templates/{template_id}/enable", ("POST",)),
    }
    for route in admin_api.router.routes:
        assert any(
            dependency.call is require_admin_or_staff_user
            for dependency in route.dependant.dependencies
        )

    for role in ("reviewer", "user"):
        with pytest.raises(HTTPException) as rejected:
            require_admin_or_staff_user({"id": role, "role": role})
        assert rejected.value.status_code == 403

    with pytest.raises(ValidationError):
        admin_api.AdminDisableCreatorRoleTemplateRequest(
            reason="风险", ai_name="后台禁止改写"
        )


def test_admin_ui_separates_operator_campaigns_and_user_templates():
    html = Path("app/static/campaign_codes_admin.html").read_text(encoding="utf-8")

    for marker in (
        'id="operator-view"',
        'id="role-template-view"',
        "用户角色模板链接",
        "/admin/creator-role-templates",
        "仅支持查看、统计、平台停用和恢复",
        "后台不可改写",
        "t.effective_status_display",
        "latest.review_status_display",
        "v.review_reason_display",
        "r.status_display",
        "r.reason_display",
    ):
        assert marker in html
    assert "+ esc(v.review_status)" not in html
    assert "+ esc(r.status)" not in html
    assert "v.review_reason ||" not in html
    assert "内部状态码" not in html
    assert "createRoleTemplate" not in html
    assert "editRoleTemplate" not in html
    assert "trial" not in html.lower()
    assert "debugRoleTemplate" not in html
    # 运营活码原有创建、编辑、启停和统计入口仍完整保留。
    for marker in (
        "createCampaignCode()",
        "saveCampaignCodeEdit()",
        "toggleStatus(code",
        "loadStats()",
    ):
        assert marker in html
