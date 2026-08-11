"""Plum 注册登录编排与用户 Character 原子创建准备。"""

import pytest
from pydantic import ValidationError

import app.db as db
from app.platform.media.persistence import insert_media_asset
from app.products.plum.api.contracts import CreateCharacterRequest
from app.products.plum.application.identity import create_plum_login_session
from app.products.plum.infrastructure.repository import (
    PlumConflictError,
    create_or_get_conversation,
    list_characters,
    publish_created_character,
    seed_plum_catalog,
)


def _insert_user(user_id: str, phone: str, display_name: str) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, status)
            VALUES (?, ?, ?, 'active')
            """,
            (user_id, phone, display_name),
        )


def _insert_tags() -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO plum_tags(id, code, display_name, sort_order)
            VALUES ('tag_romance', 'romance', 'Romance', 20),
                   ('tag_fantasy', 'fantasy', 'Fantasy', 10)
            """
        )


def _insert_portrait(*, media_id: str, owner_id: str) -> None:
    insert_media_asset(
        media_id=media_id,
        owner_platform_user_id=owner_id,
        kind="image",
        mime="image/png",
        bytes_len=12,
        sha256=f"sha-{media_id}",
        storage_path=f"tests/{media_id}.png",
        width=12,
        height=18,
        expires_at="2099-01-01 00:00:00",
    )


def _publish(owner_id: str, media_id: str, **overrides):
    payload = {
        "platform_user_id": owner_id,
        "idempotency_key": "create-character-001",
        "display_name": "Luna",
        "gender": "female",
        "portrait_media_id": media_id,
        "intro": "A guarded stargazer.",
        "opening_scene": "The observatory door opens.",
        "character_settings": "You are Luna, an observant astronomer.",
        "tag_ids": ["tag_romance", "tag_fantasy"],
        "creator_declared_rating": "general",
        "approved_moderation_decision_id": "moddec_verified_001",
        "platform_effective_rating": "general",
        "visibility": "private",
    }
    payload.update(overrides)
    return publish_created_character(**payload)


def test_verified_user_login_provisions_plum_once_and_issues_audience_session(
    fresh_db,
):
    _insert_user("user_login_ready", "login-ready@local.invalid", "Alice")

    first = create_plum_login_session(
        verified_platform_user_id="user_login_ready", days=14
    )
    second = create_plum_login_session(
        verified_platform_user_id="user_login_ready", days=14
    )

    assert first["is_new_membership"] is True
    assert second["is_new_membership"] is False
    assert first["session"]["app_id"] == "plum"
    assert first["provisioning"]["entry_account_id"] == second["provisioning"][
        "entry_account_id"
    ]
    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM product_memberships
            WHERE platform_user_id='user_login_ready' AND app_id='plum'
            """
        ).fetchone()["n"] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM runtime_ownerships
            WHERE platform_user_id='user_login_ready' AND app_id='plum'
              AND source_type='product_entry' AND status='active'
            """
        ).fetchone()["n"] == 1
    wallet = db.get_wallet_summary(
        account_id=first["provisioning"]["entry_account_id"]
    )
    assert wallet["wallet"]["balance_shell_micros"] == 1_000_000_000


def test_disabled_membership_is_not_reactivated_or_provisioned(fresh_db):
    _insert_user("user_login_disabled", "login-disabled@local.invalid", "Blocked")
    db.ensure_product_membership(
        platform_user_id="user_login_disabled", app_id="plum"
    )
    db.update_product_membership_status(
        platform_user_id="user_login_disabled", app_id="plum", status="disabled"
    )

    with pytest.raises(ValueError, match="product_membership_disabled"):
        create_plum_login_session(
            verified_platform_user_id="user_login_disabled"
        )

    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM runtime_ownerships
            WHERE platform_user_id='user_login_disabled' AND app_id='plum'
            """
        ).fetchone()["n"] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM platform_user_sessions
            WHERE platform_user_id='user_login_disabled' AND app_id='plum'
            """
        ).fetchone()["n"] == 0


def test_publish_created_character_is_atomic_private_and_idempotent(fresh_db):
    _insert_user("user_creator_ready", "creator-ready@local.invalid", "Creator")
    create_plum_login_session(verified_platform_user_id="user_creator_ready")
    seed_plum_catalog()
    _insert_tags()
    _insert_portrait(media_id="media_creator_ready", owner_id="user_creator_ready")

    created = _publish("user_creator_ready", "media_creator_ready")
    replay = _publish(
        "user_creator_ready",
        "media_creator_ready",
        approved_moderation_decision_id="moddec_retry_should_not_replace_original",
    )

    assert replay["character_id"] == created["character_id"]
    assert replay["work_id"] == created["work_id"]
    assert replay["moderation_decision_id"] == "moddec_verified_001"
    assert replay["tag_ids"] == ["tag_fantasy", "tag_romance"]
    assert all(item["id"] != created["character_id"] for item in list_characters())
    assert create_or_get_conversation(
        platform_user_id="user_creator_ready",
        character_id=created["character_id"],
    )["character_id"] == created["character_id"]

    _insert_user("user_private_viewer", "private-viewer@local.invalid", "Viewer")
    create_plum_login_session(verified_platform_user_id="user_private_viewer")
    with pytest.raises(ValueError, match="character not found"):
        create_or_get_conversation(
            platform_user_id="user_private_viewer",
            character_id=created["character_id"],
        )
    with db.connect() as conn:
        work = conn.execute(
            """
            SELECT owner_kind, owner_platform_user_id
            FROM plum_works WHERE id=?
            """,
            (created["work_id"],),
        ).fetchone()
        assert (work["owner_kind"], work["owner_platform_user_id"]) == (
            "platform_user",
            "user_creator_ready",
        )
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM plum_character_versions
            WHERE character_id=? AND version_number=1
            """,
            (created["character_id"],),
        ).fetchone()["n"] == 1
        assert conn.execute(
            """
            SELECT status FROM media_assets WHERE id='media_creator_ready'
            """
        ).fetchone()["status"] == "referenced"
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM plum_character_create_requests
            WHERE platform_user_id='user_creator_ready'
            """
        ).fetchone()["n"] == 1

    with pytest.raises(
        PlumConflictError, match="character_create_idempotency_conflict"
    ):
        _publish(
            "user_creator_ready",
            "media_creator_ready",
            intro="Different content under the same key.",
        )


def test_character_create_rejects_cross_owner_media_without_partial_rows(fresh_db):
    _insert_user("user_creator_a", "creator-a@local.invalid", "A")
    _insert_user("user_creator_b", "creator-b@local.invalid", "B")
    create_plum_login_session(verified_platform_user_id="user_creator_a")
    create_plum_login_session(verified_platform_user_id="user_creator_b")
    _insert_tags()
    _insert_portrait(media_id="media_creator_a", owner_id="user_creator_a")

    with pytest.raises(ValueError, match="creator_media_not_claimable"):
        _publish("user_creator_b", "media_creator_a")

    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM plum_character_create_requests
            WHERE platform_user_id='user_creator_b'
            """
        ).fetchone()["n"] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM plum_works
            WHERE owner_platform_user_id='user_creator_b'
            """
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT status FROM media_assets WHERE id='media_creator_a'"
        ).fetchone()["status"] == "pending"


def test_create_character_contract_rejects_client_owned_review_fields():
    with pytest.raises(ValidationError):
        CreateCharacterRequest(
            idempotency_key="character-contract-001",
            display_name="Luna",
            gender="female",
            portrait_media_id="media_contract",
            intro="Intro",
            opening_scene="Opening",
            character_settings="Settings",
            tag_ids=["tag_romance"],
            creator_declared_rating="general",
            visibility="private",
            moderation_decision_id="client-forged",
        )
