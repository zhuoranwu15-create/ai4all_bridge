"""Plum m0072 Work/Character Version/Persona 基础迁移测试。"""

from unittest.mock import patch

import pytest
from psycopg.errors import RaiseException

import app.db as db
from app.db._backend import IntegrityError
from app.db._core import (
    _MIGRATIONS,
    _migration_0072_plum_character_content_foundation,
    _migration_0075_plum_system_work_ownership,
    _migration_0076_plum_character_create_idempotency,
    _migration_0077_plum_guest_identity_foundation,
)


def test_m0072_backfills_legacy_character_and_persona_idempotently(
    test_settings, empty_pg_database
):
    """存量投影获得确定 Work/Version，Persona 停止伪版本递增。"""

    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=71, expected_current_version=0)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_legacy', 'legacy@local.invalid', 'Legacy User')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_user_personas(
                    id, platform_user_id, display_name, description,
                    prompt_text, status, is_default, version
                )
                VALUES ('persona_legacy', 'pusr_legacy', 'Traveler',
                        'A traveler', 'Act as a traveler', 'active', 1, 9)
                """
            )
            conn.execute(
                """
                INSERT INTO plum_characters(
                    id, display_name, tagline, intro, greeting, tags_json,
                    persona_prompt, scenario_prompt, speaking_style,
                    prompt_version, status, creator_profile_id, content_rating
                )
                VALUES ('char_legacy', 'Legacy', 'Legacy intro', 'Legacy intro',
                        'The door opens.', '["romance"]', 'Character settings',
                        'Shared observatory', 'Keep replies concise', 3,
                        'active', 'fprof_legacy', 'mature')
                """
            )

            _migration_0072_plum_character_content_foundation(conn)
            _migration_0072_plum_character_content_foundation(conn)

            character = conn.execute(
                """
                SELECT work_id, content_version, creator_declared_rating,
                       platform_effective_rating, access_policy_version,
                       moderation_decision_id, visibility, published_at
                FROM plum_characters WHERE id='char_legacy'
                """
            ).fetchone()
            assert character["work_id"] == "work_for_char_legacy"
            assert character["content_version"] == 1
            assert character["creator_declared_rating"] == "mature"
            assert character["platform_effective_rating"] == "mature"
            assert character["access_policy_version"] == "plum-rating-v1"
            assert character["moderation_decision_id"] is None
            assert character["visibility"] == "public"
            assert character["published_at"] is not None

            work = conn.execute(
                """
                SELECT owner_platform_user_id, creator_profile_id
                FROM plum_works WHERE id=?
                """,
                (character["work_id"],),
            ).fetchone()
            assert work["owner_platform_user_id"] == "pusr_plum_system"
            assert work["creator_profile_id"] == "fprof_legacy"

            version = conn.execute(
                """
                SELECT version_number, prompt_version, opening_scene,
                       character_settings, scenario_prompt, response_rules,
                       platform_effective_rating, moderation_decision_id
                FROM plum_character_versions WHERE character_id='char_legacy'
                """
            ).fetchone()
            assert dict(zip(version.keys(), version)) == {
                "version_number": 1,
                "prompt_version": 3,
                "opening_scene": "The door opens.",
                "character_settings": "Character settings",
                "scenario_prompt": "Shared observatory",
                "response_rules": "Keep replies concise",
                "platform_effective_rating": "mature",
                "moderation_decision_id": None,
            }
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM plum_character_versions"
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM plum_character_version_tags"
            ).fetchone()["n"] == 0

            persona = conn.execute(
                """
                SELECT version, locked_at, avatar_media_id,
                       derived_from_persona_id
                FROM plum_user_personas WHERE id='persona_legacy'
                """
            ).fetchone()
            assert persona["version"] == 1
            assert persona["locked_at"] is None
            assert persona["avatar_media_id"] is None
            assert persona["derived_from_persona_id"] is None


def test_m0072_constraints_reject_invalid_projection_and_cross_owner_copy(
    test_settings,
):
    """数据库约束守住版本、取景、Work 引用和 Persona owner 隔离。"""

    with patch("app.db.settings", test_settings):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_a', 'a@local.invalid', 'A'),
                       ('pusr_b', 'b@local.invalid', 'B')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_user_personas(
                    id, platform_user_id, display_name, status, is_default, version
                )
                VALUES ('persona_a', 'pusr_a', 'A', 'active', 1, 1),
                       ('persona_b', 'pusr_b', 'B', 'active', 1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO plum_works(
                    id, owner_platform_user_id, creator_profile_id
                )
                VALUES ('work_a', 'pusr_a', 'profile_a')
                """
            )
            conn.execute(
                """
                INSERT INTO media_assets(
                    id, owner_platform_user_id, kind, mime, bytes,
                    sha256, storage_path
                )
                VALUES ('media_a', 'pusr_a', 'image', 'image/webp', 10,
                        'sha-a', 'aa/media-a.webp')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_characters(
                    id, work_id, display_name, tagline, intro, greeting,
                    persona_prompt, status
                )
                VALUES ('char_a', 'work_a', 'A', 'A', 'A', 'Hello',
                        'Settings', 'active')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_character_versions(
                    character_id, version_number, prompt_version,
                    display_name, intro, opening_scene, character_settings,
                    creator_declared_rating, platform_effective_rating,
                    access_policy_version, visibility
                )
                VALUES ('char_a', 1, 1, 'A', 'A', 'Hello', 'Settings',
                        'general', 'general', 'plum-rating-v1', 'public')
                """
            )

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    "UPDATE plum_characters SET portrait_position_x=101 WHERE id='char_a'"
                )

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    "UPDATE plum_characters SET work_id='missing' WHERE id='char_a'"
                )

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE plum_user_personas
                    SET derived_from_persona_id='persona_a'
                    WHERE id='persona_b'
                    """
                )

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE plum_user_personas
                    SET avatar_media_id='media_a'
                    WHERE id='persona_b'
                    """
                )

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    "UPDATE plum_user_personas SET version=2 WHERE id='persona_a'"
                )

        with pytest.raises(RaiseException, match="immutable"):
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE plum_character_versions
                    SET display_name='Changed'
                    WHERE character_id='char_a' AND version_number=1
                    """
                )

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO plum_characters(
                        id, work_id, display_name, tagline, intro, greeting,
                        persona_prompt, status
                    )
                    VALUES ('char_without_version', 'work_a', 'B', 'B', 'B',
                            'Hello', 'Settings', 'active')
                    """
                )
    db.close_pg_pool()


def test_m0075_replaces_fake_platform_user_with_explicit_system_work_owner(
    test_settings, empty_pg_database
):
    """平台内置 Work 不伪装成注册 User，真人 Work 仍要求真实 owner。"""

    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=74, expected_current_version=0)
        with db.connect() as conn:
            assert conn.execute(
                "SELECT 1 FROM platform_users WHERE id='pusr_plum_system'"
            ).fetchone() is not None
            conn.execute(
                """
                INSERT INTO plum_works(
                    id, owner_platform_user_id, creator_profile_id
                ) VALUES ('work_legacy_system', 'pusr_plum_system',
                          'profile_system')
                """
            )

            _migration_0075_plum_system_work_ownership(conn)
            _migration_0075_plum_system_work_ownership(conn)

            assert conn.execute(
                "SELECT 1 FROM platform_users WHERE id='pusr_plum_system'"
            ).fetchone() is None
            system_work = conn.execute(
                """
                SELECT owner_kind, owner_platform_user_id
                FROM plum_works WHERE id='work_legacy_system'
                """
            ).fetchone()
            assert system_work["owner_kind"] == "system"
            assert system_work["owner_platform_user_id"] is None

            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_work_owner', 'work-owner@local.invalid', 'Owner')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_works(
                    id, owner_platform_user_id, creator_profile_id
                ) VALUES ('work_human', 'pusr_work_owner', 'profile_human')
                """
            )
            human_work = conn.execute(
                "SELECT owner_kind FROM plum_works WHERE id='work_human'"
            ).fetchone()
            assert human_work["owner_kind"] == "platform_user"

        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO plum_works(
                        id, owner_kind, owner_platform_user_id,
                        creator_profile_id
                    ) VALUES ('work_invalid_system', 'system',
                              'pusr_work_owner', 'profile_system')
                    """
                )
    db.close_pg_pool()


def test_m0077_is_registered_as_current_schema_head(test_settings):
    assert _MIGRATIONS[-1] == (77, _migration_0077_plum_guest_identity_foundation)
    with patch("app.db.settings", test_settings):
        with db.connect() as conn:
            assert conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()["v"] == 77
            for table in (
                "plum_works",
                "plum_character_versions",
                "plum_tags",
                "plum_character_version_tags",
                "plum_character_create_requests",
            ):
                conn.execute(f"SELECT 1 FROM {table} WHERE 1=0").fetchall()
    db.close_pg_pool()
