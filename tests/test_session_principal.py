"""MP-01 session audience 与 SessionPrincipal。"""
import pytest

from app.bootstrap.product_registry import build_test_product_registry

import app.db as db


def test_session_resolves_to_product_principal(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037201")
    session = db.create_platform_user_session(platform_user_id=user["id"], days=7)

    principal = db.resolve_session_principal(
        token=session["token"], expected_app_id="zhaoxi"
    )
    assert principal is not None
    assert principal.session_id == session["id"]
    assert principal.platform_user_id == user["id"]
    assert principal.app_id == "zhaoxi"


def test_disabled_or_missing_membership_invalidates_existing_token(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037202")
    disabled_session = db.create_platform_user_session(platform_user_id=user["id"])
    db.update_product_membership_status(
        platform_user_id=user["id"], app_id="zhaoxi", status="disabled"
    )
    assert db.resolve_session_principal(token=disabled_session["token"]) is None

    db.update_product_membership_status(
        platform_user_id=user["id"], app_id="zhaoxi", status="active"
    )
    missing_session = db.create_platform_user_session(platform_user_id=user["id"])
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM product_memberships WHERE platform_user_id=? AND app_id='zhaoxi'",
            (user["id"],),
        )
    assert db.resolve_session_principal(token=missing_session["token"]) is None
    with pytest.raises(ValueError, match="active product membership required"):
        db.create_platform_user_session(platform_user_id=user["id"])


def test_test_product_token_cannot_resolve_for_legacy_audience(fresh_db):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037203")
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    session = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id="test_product",
        registry=registry,
    )

    principal = db.resolve_session_principal(
        token=session["token"],
        expected_app_id="test_product",
        registry=registry,
    )
    assert principal is not None and principal.app_id == "test_product"
    assert (
        db.resolve_session_principal(
            token=session["token"], expected_app_id="zhaoxi", registry=registry
        )
        is None
    )


def test_require_product_session_uses_fixed_server_audience(fresh_db):
    from fastapi import HTTPException
    from app.routers.deps import require_product_session

    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037206")
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    zhaoxi_session = db.create_platform_user_session(platform_user_id=user["id"])
    test_session = db.create_platform_user_session(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    dependency = require_product_session("test_product", registry=registry)

    principal = dependency(authorization=f"Bearer {test_session['token']}")
    assert principal.app_id == "test_product"
    with pytest.raises(HTTPException) as exc:
        dependency(authorization=f"Bearer {zhaoxi_session['token']}")
    assert exc.value.status_code == 401


def test_test_product_token_cannot_access_legacy_web_or_v1_routes(client):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037205")
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    session = db.create_platform_user_session(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    headers = {"Authorization": f"Bearer {session['token']}"}

    assert client.get("/web/me", headers=headers).status_code == 401
    assert client.get("/v1/me", headers=headers).status_code == 401
    assert (
        client.get("/api/v1/products/zhaoxi/me", headers=headers).status_code == 401
    )


def test_m0038_backfills_existing_sessions_without_changing_count(fresh_db):
    from app.db._core import _migration_0038_session_app_id

    user = db.create_or_get_platform_user_by_phone(phone="13800037204")
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="迁移存量账号"
    )["account"]
    db.set_account_onboarding_state(account_id=account["id"], state="completed")
    session = db.create_platform_user_session(platform_user_id=user["id"])
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM platform_user_sessions").fetchone()["n"]
        accounts_before = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        conn.execute("DROP INDEX IF EXISTS ix_platform_user_sessions_app_user")
        conn.execute("ALTER TABLE platform_user_sessions DROP COLUMN app_id")
        _migration_0038_session_app_id(conn)
        _migration_0038_session_app_id(conn)
        after = conn.execute("SELECT COUNT(*) AS n FROM platform_user_sessions").fetchone()["n"]
        row = conn.execute(
            "SELECT app_id FROM platform_user_sessions WHERE id=?", (session["id"],)
        ).fetchone()
        onboarding = conn.execute(
            "SELECT onboarding_state FROM accounts WHERE id=?", (account["id"],)
        ).fetchone()["onboarding_state"]
        accounts_after = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]

    assert after == before
    assert accounts_after == accounts_before
    assert onboarding == "completed"
    assert row["app_id"] == "zhaoxi"
