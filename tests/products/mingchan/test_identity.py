"""鸣蝉 membership、session audience 与居民 runtime account 身份基座。"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

import app.db as db
from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    ZHAOXI_APP_ID,
    build_test_product_registry,
)
from app.products.mingchan.application.identity import create_mingchan_login_session
from app.products.mingchan.api.deps import build_mingchan_session_dependency
from app.products.mingchan.infrastructure.accounts import (
    create_mingchan_resident_runtime_account,
)
from app.routers.deps import require_product_session


def test_disabled_mingchan_dependency_loads_and_fails_closed(fresh_db):
    """鸣蝉禁用时模块可以加载，但任何请求都在解析 token 前被拒绝。"""

    user = db.create_or_get_platform_user_by_phone(phone="13800037901")
    session = db.create_platform_user_session(platform_user_id=user["id"])
    require_mingchan_session = build_mingchan_session_dependency(
        build_test_product_registry(mingchan_enabled=False)
    )

    with pytest.raises(HTTPException) as exc:
        require_mingchan_session(authorization=f"Bearer {session['token']}")

    assert exc.value.status_code == 401
    assert exc.value.detail == "产品暂不可用"


def test_mingchan_and_zhaoxi_session_audiences_are_bidirectionally_isolated(
    fresh_db,
):
    """同一真人可加入两产品，但两个 session dependency 只能接受自己的 token。"""

    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037902")
    db.ensure_product_membership(
        platform_user_id=user["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    zhaoxi_session = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    mingchan_session = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    zhaoxi_dependency = require_product_session(ZHAOXI_APP_ID, registry=registry)
    mingchan_dependency = require_product_session(MINGCHAN_APP_ID, registry=registry)

    assert zhaoxi_dependency(
        authorization=f"Bearer {zhaoxi_session['token']}"
    ).app_id == ZHAOXI_APP_ID
    assert mingchan_dependency(
        authorization=f"Bearer {mingchan_session['token']}"
    ).app_id == MINGCHAN_APP_ID
    with pytest.raises(HTTPException) as zhaoxi_rejects_mingchan:
        zhaoxi_dependency(authorization=f"Bearer {mingchan_session['token']}")
    with pytest.raises(HTTPException) as mingchan_rejects_zhaoxi:
        mingchan_dependency(authorization=f"Bearer {zhaoxi_session['token']}")
    assert zhaoxi_rejects_mingchan.value.status_code == 401
    assert mingchan_rejects_zhaoxi.value.status_code == 401


def test_mingchan_login_creates_only_mingchan_membership_and_session(fresh_db):
    """全新手机号登录鸣蝉时不应被旧默认逻辑顺带加入朝夕。"""

    registry = build_test_product_registry()
    phone = "13800037904"
    verification = db.create_phone_verification(
        phone=phone,
        code="999999",
        expires_minutes=10,
    )
    verified = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )

    result = create_mingchan_login_session(
        phone=phone,
        verified_token=verified["verified_token"],
        registry=registry,
    )
    platform_user = result["registration"]["platform_user"]

    assert result["session"]["app_id"] == MINGCHAN_APP_ID
    assert [
        membership["app_id"]
        for membership in db.list_product_memberships(
            platform_user_id=platform_user["id"]
        )
    ] == [MINGCHAN_APP_ID]


def test_mingchan_resident_account_is_created_in_mingchan_scope(fresh_db):
    """居民 factory 必须显式接收产品，并把鸣蝉账号落到鸣蝉 membership 下。"""

    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037903")
    db.ensure_product_membership(
        platform_user_id=user["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    world = db.get_or_create_home_universe(
        platform_user_id=user["id"], app_id=MINGCHAN_APP_ID
    )
    template = db.create_character_template(
        app_id=MINGCHAN_APP_ID,
        source_type="official",
        name="鸣蝉居民",
    )

    runtime = create_mingchan_resident_runtime_account(
        universe_id=world["id"],
        character_template_id=template["id"],
        display_name="鸣蝉居民",
        registry=registry,
    )

    assert runtime["account"]["app_id"] == MINGCHAN_APP_ID
    with db.connect() as conn:
        bindings = conn.execute(
            "SELECT COUNT(*) AS n FROM account_owner_bindings WHERE account_id = ?",
            (runtime["account"]["id"],),
        ).fetchone()["n"]
    assert bindings == 0


def test_resident_account_factories_have_no_implicit_product_default():
    """防止后续调用遗漏 app_id 后静默回落朝夕。"""

    for factory in (
        db.insert_resident_runtime_account,
        db.create_resident_runtime_account,
    ):
        assert (
            inspect.signature(factory).parameters["app_id"].default
            is inspect.Parameter.empty
        )
