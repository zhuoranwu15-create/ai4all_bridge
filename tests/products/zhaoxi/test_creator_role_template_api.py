"""CRT-05：创建者 API、公共 preview 与 owner-scoped 统计。"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

import app.db as db
from app.db import SessionPrincipal
from app.products.zhaoxi.api import creator_role_templates as api
from app.products.zhaoxi.api.admin_campaigns import admin_campaign_code_stats
from app.products.zhaoxi.application import creator_role_template_review as review_app
from app.products.zhaoxi.infrastructure.persistence.campaign_analytics import (
    record_campaign_visit,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    get_account_creator_role_template_attribution,
)
from app.time_utils import beijing_naive_now, beijing_now


def _principal(user_id: str) -> SessionPrincipal:
    return SessionPrincipal(
        session_id="session-test",
        platform_user_id=user_id,
        app_id="zhaoxi",
        expires_at="2099-01-01 00:00:00",
    )


def _eligible_creator(phone: str, *, onboarding_state: str = "complete"):
    registration = db.register_platform_user_with_referral(
        phone=phone, app_id="zhaoxi"
    )
    user_id = registration["platform_user"]["id"]
    invite_code = db.get_or_create_personal_referral_code_for_user(
        platform_user_id=user_id,
        app_id="zhaoxi",
    )["code"]
    account = db.get_or_create_default_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user_id
    )
    db.set_account_onboarding_state(
        account_id=account["account"]["id"],
        state=onboarding_state,
    )
    return _principal(user_id), invite_code, account["account"]["id"]


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


def _reject_review(monkeypatch) -> None:
    monkeypatch.setattr(
        review_app,
        "generate_completion",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "reject",
                "field_results": {
                    "ai_name": "pass",
                    "personality_text": "reject",
                    "mission_text": "pass",
                    "opening_line": "pass",
                },
                "categories": ["prompt_injection"],
                "reason": "tries to override platform rules",
                "public_summary": "",
            }
        ),
    )


def _create(principal: SessionPrincipal):
    return api.creator_role_template_create(
        api.CreatorRoleTemplateCreateRequest(
            ai_name="朝朝",
            personality_text="温柔坦诚，也敢于提醒边界。",
            mission_text="陪用户更清楚地认识自己，并找到生活节奏。",
            opening_line="我是朝朝，很高兴认识你。以后想聊什么都可以告诉我。",
        ),
        principal,
    )["creator_role_template"]


def _request(ip: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/preview",
            "headers": [],
            "query_string": b"",
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_creator_api_full_lifecycle_and_no_account_switch_surface(
    fresh_db, monkeypatch
):
    principal, invite_code, creator_account_id = _eligible_creator("13820001001")
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)

    created = _create(principal)
    assert created["effective_status"] == "approved"
    assert created["effective_status_display"] == "审核通过，待发布"
    assert created["registration_url"] is None
    assert created["latest_version"]["review_status"] == "passed"
    assert created["latest_version"]["review_status_display"] == "审核通过"
    assert created["latest_version"]["review_reason_display"] == ""

    published = api.creator_role_template_publish(
        created["id"],
        api.CreatorRoleTemplatePublishRequest(
            version_id=created["latest_version"]["id"]
        ),
        principal,
    )["creator_role_template"]
    assert published["effective_status"] == "active"
    assert published["registration_url"] == (
        f"/?invite_code={invite_code}&campaign_code={published['campaign_code']}"
    )
    assert get_account_creator_role_template_attribution(
        account_id=creator_account_id
    ) is None

    edited = api.creator_role_template_update(
        created["id"],
        api.CreatorRoleTemplateUpdateRequest(personality_text="新版温柔而坚定。"),
        principal,
    )["creator_role_template"]
    assert edited["published_version"]["id"] == published["published_version"]["id"]
    assert edited["latest_version"]["review_status"] == "passed"
    republished = api.creator_role_template_publish(
        created["id"],
        api.CreatorRoleTemplatePublishRequest(
            version_id=edited["latest_version"]["id"]
        ),
        principal,
    )["creator_role_template"]
    assert republished["activated_at"] == published["activated_at"]
    assert republished["expires_at"] == published["expires_at"]

    disabled = api.creator_role_template_disable(created["id"], principal)[
        "creator_role_template"
    ]
    assert disabled["effective_status"] == "disabled_creator"
    enabled = api.creator_role_template_enable(created["id"], principal)[
        "creator_role_template"
    ]
    assert enabled["effective_status"] == "active"
    assert len(api.creator_role_template_list(principal)["creator_role_templates"]) == 1

    deleted = api.creator_role_template_delete(created["id"], principal)[
        "creator_role_template"
    ]
    assert deleted["effective_status"] == "deleted"
    assert api.creator_role_template_list(principal)["creator_role_templates"] == []

    route_paths = {route.path for route in api.router.routes}
    assert not any(
        token in path for path in route_paths for token in ("trial", "debug", "switch", "apply")
    )


def test_rejected_review_hides_raw_audit_reason_and_returns_chinese_display(
    fresh_db, monkeypatch
):
    principal, _, _ = _eligible_creator("13820001008")
    fresh_db.creator_role_templates_enabled = True
    _reject_review(monkeypatch)

    created = _create(principal)

    assert created["effective_status"] == "rejected"
    assert created["effective_status_display"] == "审核未通过"
    latest = created["latest_version"]
    assert latest["review_status"] == "rejected"
    assert latest["review_status_display"] == "审核未通过"
    assert "review_reason" not in latest
    assert latest["review_reason_display"] == (
        "角色设定包含试图绕过或覆盖平台规则的内容，请删除相关指令后重试。"
    )


def test_owner_guard_feature_gate_eligibility_and_extra_fields(fresh_db, monkeypatch):
    owner, _, _ = _eligible_creator("13820001002")
    outsider, _, _ = _eligible_creator("13820001003")
    pending, _, _ = _eligible_creator(
        "13820001004", onboarding_state="step1_sent"
    )
    _pass_review(monkeypatch)

    with pytest.raises(HTTPException) as disabled:
        _create(owner)
    assert disabled.value.status_code == 503
    assert disabled.value.detail == "creator_role_templates_disabled"

    fresh_db.creator_role_templates_enabled = True
    with pytest.raises(HTTPException) as ineligible:
        _create(pending)
    assert ineligible.value.status_code == 403

    created = _create(owner)
    with pytest.raises(HTTPException) as hidden:
        api.creator_role_template_detail(created["id"], outsider)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as hidden_stats:
        api.creator_role_template_stats(created["id"], None, None, outsider)
    assert hidden_stats.value.status_code == 404

    with pytest.raises(ValidationError):
        api.CreatorRoleTemplateCreateRequest(
            ai_name="朝朝",
            personality_text="性格",
            mission_text="使命",
            opening_line="开场白",
            account_id="forbidden",
        )


def test_public_preview_minimal_fields_owner_match_and_expiry(fresh_db, monkeypatch):
    owner, invite_code, _ = _eligible_creator("13820001005")
    _, other_invite, _ = _eligible_creator("13820001006")
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    created = _create(owner)
    active = api.creator_role_template_publish(
        created["id"],
        api.CreatorRoleTemplatePublishRequest(
            version_id=created["latest_version"]["id"]
        ),
        owner,
    )["creator_role_template"]

    preview = api.creator_role_template_public_preview(
        active["campaign_code"],
        _request("10.0.0.1"),
        invite_code,
    )
    assert set(preview) == {"valid", "role"}
    assert set(preview["role"]) == {
        "name",
        "summary",
        "expires_at",
    }
    assert preview["role"]["summary"] == "朝朝，温柔坦诚的陪伴者"
    assert preview["role"]["name"] == "朝朝"
    mismatch = api.creator_role_template_public_preview(
        active["campaign_code"],
        _request("10.0.0.2"),
        other_invite,
    )
    assert mismatch == {"valid": False, "reason": "creator_mismatch"}

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
    expired = api.creator_role_template_public_preview(
        active["campaign_code"],
        _request("10.0.0.3"),
        invite_code,
    )
    assert expired == {"valid": False, "reason": "expired"}


def test_public_preview_is_closed_by_feature_flag(fresh_db, monkeypatch):
    owner, invite_code, _ = _eligible_creator("13820001008")
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    created = _create(owner)
    active = api.creator_role_template_publish(
        created["id"],
        api.CreatorRoleTemplatePublishRequest(
            version_id=created["latest_version"]["id"]
        ),
        owner,
    )["creator_role_template"]

    fresh_db.creator_role_templates_enabled = False
    preview = api.creator_role_template_public_preview(
        active["campaign_code"],
        _request("10.0.0.4"),
        invite_code,
    )
    assert preview == {"valid": False, "reason": "capability_disabled"}


def test_summary_edit_api_returns_specific_rejection_and_keeps_default(
    fresh_db, monkeypatch
):
    owner, _, _ = _eligible_creator("13820001009")
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    created = _create(owner)
    default_summary = created["latest_version"]["public_summary"]

    monkeypatch.setattr(
        review_app,
        "generate_completion",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "reject",
                "categories": ["professional_deception"],
                "reason": "存在未经支持的专业能力声明",
            },
            ensure_ascii=False,
        ),
    )
    response = api.creator_role_template_summary_edit(
        created["id"],
        api.CreatorRoleTemplateSummaryEditRequest(
            version_id=created["latest_version"]["id"],
            summary="朝朝，最专业的医生伙伴",
        ),
        owner,
    )
    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["detail"] == "creator_role_template_summary_rejected"
    assert body["reason_categories"] == ["professional_deception"]
    assert "专业" in body["message"]
    assert body["current_summary"] == default_summary
    refreshed = api.creator_role_template_detail(created["id"], owner)[
        "creator_role_template"
    ]
    assert refreshed["latest_version"]["public_summary"] == default_summary
    assert refreshed["latest_version"]["summary_edit_status"] == "rejected"
    assert "专业" in refreshed["latest_version"]["summary_review_reason_display"]


def test_creator_and_admin_stats_share_exact_range_and_payload(fresh_db, monkeypatch):
    owner, _, account_id = _eligible_creator("13820001007")
    fresh_db.creator_role_templates_enabled = True
    _pass_review(monkeypatch)
    created = _create(owner)
    today = beijing_now().date().isoformat()
    record_campaign_visit(
        campaign_code=created["campaign_code"],
        visitor_token="visitor-one",
    )
    with db.connect() as conn:
        now = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO account_campaign_attribution(
                account_id, campaign_code, attributed_at, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (account_id, created["campaign_code"], now, now),
        )

    creator_stats = api.creator_role_template_stats(
        created["id"], today, today, owner
    )
    admin_stats = admin_campaign_code_stats(
        created["campaign_code"], today, today, {"id": "admin"}
    )
    assert creator_stats == admin_stats
    assert creator_stats["totals"]["pv"] == 1
    assert creator_stats["totals"]["registered"] == 1
