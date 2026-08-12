"""Plum m0073 Connection 基础、账号隔离与唯一重开测试。"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from psycopg.errors import RaiseException

import app.db as db
from app.db._backend import IntegrityError
from app.db._core import (
    _MIGRATIONS,
    _migration_0073_plum_connection_foundation,
    _migration_0076_plum_character_create_idempotency,
    _migration_0078_plum_external_identity_challenges,
)
from app.products.plum.infrastructure import repository


def _configure_plum(monkeypatch, fresh_db):
    config = SimpleNamespace(
        **fresh_db.__dict__,
        plum_test_user_id="user_plum_test",
        plum_test_phone="plum-test@local.invalid",
        plum_fast_provider_id="deepseek",
        plum_balanced_provider_id="chatgpt",
        plum_immersive_provider_id="deepseek-v4-pro",
    )
    monkeypatch.setattr(repository, "settings", config)
    return config


def test_m0073_backfills_legacy_pair_persona_runtime_and_conversation(
    test_settings, empty_pg_database
):
    """旧关系/会话/Binding 的并集被原子迁到一个锁定 Persona Connection。"""

    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=72, expected_current_version=0)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_legacy_conn', 'legacy-conn@local.invalid', 'Legacy')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_works(
                    id, owner_platform_user_id, creator_profile_id
                ) VALUES ('work_legacy_conn', 'pusr_legacy_conn', 'profile_legacy')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_characters(
                    id, work_id, display_name, tagline, intro, greeting,
                    persona_prompt, scenario_prompt, speaking_style,
                    prompt_version, content_version, status
                )
                VALUES ('char_legacy_conn', 'work_legacy_conn', 'Legacy Char',
                        'tagline', 'intro', 'hello', 'settings', 'scene',
                        'style', 2, 1, 'active')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_character_versions(
                    character_id, version_number, prompt_version,
                    display_name, intro, opening_scene, character_settings,
                    scenario_prompt, response_rules,
                    creator_declared_rating, platform_effective_rating,
                    access_policy_version, visibility
                )
                VALUES ('char_legacy_conn', 1, 2, 'Legacy Char', 'intro',
                        'hello', 'settings', 'scene', 'style', 'general',
                        'general', 'plum-rating-v1', 'public')
                """
            )
            conn.execute(
                """
                INSERT INTO accounts(id, display_name, app_id)
                VALUES ('aid_legacy_conn', 'Legacy Runtime', 'plum')
                """
            )
            conn.execute(
                """
                INSERT INTO runtime_ownerships(
                    runtime_account_id, platform_user_id, app_id,
                    source_type, source_id
                )
                VALUES ('aid_legacy_conn', 'pusr_legacy_conn', 'plum',
                        'character_binding',
                        'pusr_legacy_conn:char_legacy_conn')
                """
            )
            session = conn.execute(
                """
                INSERT INTO sessions(account_id, session_key, sender_id)
                VALUES ('aid_legacy_conn', '__app_active__', 'pusr_legacy_conn')
                RETURNING id
                """
            ).fetchone()
            conn.execute(
                """
                INSERT INTO plum_user_character_relationships(
                    platform_user_id, character_id, relationship_level,
                    relationship_xp, completed_turn_count
                )
                VALUES ('pusr_legacy_conn', 'char_legacy_conn', 3, 40, 9)
                """
            )
            conn.execute(
                """
                INSERT INTO plum_character_bindings(
                    platform_user_id, character_id, runtime_account_id,
                    character_prompt_version
                )
                VALUES ('pusr_legacy_conn', 'char_legacy_conn',
                        'aid_legacy_conn', 2)
                """
            )
            conn.execute(
                """
                INSERT INTO plum_conversations(
                    id, platform_user_id, character_id, runtime_account_id,
                    runtime_session_id, model_profile
                )
                VALUES ('fconv_legacy_conn', 'pusr_legacy_conn',
                        'char_legacy_conn', 'aid_legacy_conn', ?, 'balanced')
                """,
                (session["id"],),
            )

            _migration_0073_plum_connection_foundation(conn)
            _migration_0073_plum_connection_foundation(conn)

            connection = conn.execute(
                "SELECT * FROM plum_connections"
            ).fetchone()
            assert connection["creation_reason"] == "backfill"
            assert connection["migration_source"] == "m0073_legacy_user_character"
            persona = conn.execute(
                "SELECT * FROM plum_user_personas WHERE id=?",
                (connection["persona_id"],),
            ).fetchone()
            assert persona["display_name"] == "Migrated Persona"
            assert persona["locked_at"] is not None

            state = conn.execute(
                "SELECT * FROM plum_connection_relationship_state"
            ).fetchone()
            assert state["relationship_level"] == 3
            assert state["relationship_xp"] == 40
            assert state["completed_turn_count"] == 9
            binding = conn.execute(
                "SELECT * FROM plum_connection_runtime_bindings"
            ).fetchone()
            assert binding["runtime_account_id"] == "aid_legacy_conn"
            assert binding["platform_user_id"] == "pusr_legacy_conn"
            ownership = conn.execute(
                "SELECT * FROM runtime_ownerships WHERE runtime_account_id=?",
                (binding["runtime_account_id"],),
            ).fetchone()
            assert ownership["source_type"] == "plum_connection"
            assert ownership["source_id"] == connection["id"]
            conversation = conn.execute(
                "SELECT connection_id FROM plum_conversations"
            ).fetchone()
            assert conversation["connection_id"] == connection["id"]
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM plum_connection_character_adoptions"
            ).fetchone()["n"] == 1
    db.close_pg_pool()


def test_connection_create_or_get_is_concurrent_and_locks_persona(
    fresh_db, monkeypatch
):
    """并发普通开聊只创建一个 active Connection/Runtime/Conversation。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()

    def create():
        return repository.create_or_get_conversation(
            platform_user_id="user_plum_test",
            character_id="char_ref_after_hours",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))

    assert results[0]["id"] == results[1]["id"]
    assert results[0]["connection_id"] == results[1]["connection_id"]
    with db.connect() as conn:
        persona = conn.execute(
            """
            SELECT * FROM plum_user_personas
            WHERE platform_user_id='user_plum_test' AND is_default=1
            """
        ).fetchone()
        assert persona["locked_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM plum_connections WHERE status='active'"
        ).fetchone()["n"] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM runtime_ownerships
            WHERE source_type='plum_connection'
              AND source_id=? AND status='active'
            """,
            (results[0]["connection_id"],),
        ).fetchone()["n"] == 1

    with pytest.raises(RaiseException, match="immutable"):
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE plum_user_personas SET display_name='Changed'
                WHERE id=?
                """,
                (persona["id"],),
            )


def test_connection_owner_constraints_and_multiple_personas(
    fresh_db, monkeypatch
):
    """Persona 隔离由数据库兜底，同 User 的两个 Persona 可各自建连。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name)
            VALUES ('pusr_other_plum', 'other-plum@local.invalid', 'Other')
            """
        )
        conn.execute(
            """
            INSERT INTO plum_user_personas(
                id, platform_user_id, display_name, status, is_default, version
            ) VALUES ('fpersona_other_owner', 'pusr_other_plum',
                      'Other', 'active', 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO plum_user_personas(
                id, platform_user_id, display_name, status, is_default, version
            ) VALUES ('fpersona_plum_second', 'user_plum_test',
                      'Second', 'active', 0, 1)
            """
        )

    first = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
    )
    second = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
        persona_id="fpersona_plum_second",
    )
    assert first["connection_id"] != second["connection_id"]
    assert first["runtime_account_id"] != second["runtime_account_id"]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM plum_user_character_relationships"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM plum_character_bindings"
        ).fetchone()["n"] == 0

    with pytest.raises((IntegrityError, RaiseException)):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO plum_connections(
                    id, platform_user_id, persona_id, character_id,
                    current_character_version, status, creation_reason
                )
                VALUES ('fconn_cross_owner', 'user_plum_test',
                        'fpersona_other_owner', 'char_ref_after_hours',
                        1, 'active', 'initial')
                """
            )


def test_restart_is_idempotent_and_uses_blank_isolated_runtime(
    fresh_db, monkeypatch
):
    """唯一重开不复用旧 Runtime，也不继承 Connection 关系状态。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    original = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE plum_connection_relationship_state
            SET relationship_level=4, relationship_xp=88,
                completed_turn_count=12
            WHERE connection_id=?
            """,
            (original["connection_id"],),
        )

    restarted = repository.restart_conversation(
        conversation_id=original["id"],
        platform_user_id="user_plum_test",
        creation_idempotency_key="restart-foundation-1",
    )
    replay = repository.restart_conversation(
        conversation_id=original["id"],
        platform_user_id="user_plum_test",
        creation_idempotency_key="restart-foundation-1",
    )
    assert replay["id"] == restarted["id"]
    assert replay["connection_id"] == restarted["connection_id"]
    assert restarted["connection_id"] != original["connection_id"]
    assert restarted["runtime_account_id"] != original["runtime_account_id"]
    assert restarted["runtime_session_id"] != original["runtime_session_id"]

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, status, replaces_connection_id
            FROM plum_connections
            WHERE platform_user_id='user_plum_test'
            ORDER BY created_at, id
            """
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        assert by_id[original["connection_id"]]["status"] == "archived"
        assert by_id[restarted["connection_id"]]["status"] == "active"
        assert (
            by_id[restarted["connection_id"]]["replaces_connection_id"]
            == original["connection_id"]
        )
        old_binding = conn.execute(
            """
            SELECT status FROM plum_connection_runtime_bindings
            WHERE connection_id=?
            """,
            (original["connection_id"],),
        ).fetchone()
        assert old_binding["status"] == "inactive"
        old_ownership = conn.execute(
            """
            SELECT status FROM runtime_ownerships
            WHERE runtime_account_id=?
            """,
            (original["runtime_account_id"],),
        ).fetchone()
        assert old_ownership["status"] == "inactive"
        old_account = conn.execute(
            "SELECT status FROM accounts WHERE id=?",
            (original["runtime_account_id"],),
        ).fetchone()
        assert old_account["status"] == "disabled"
        new_runtime = conn.execute(
            """
            SELECT a.status AS account_status, ro.status AS owner_status
            FROM accounts a
            JOIN runtime_ownerships ro ON ro.runtime_account_id=a.id
            WHERE a.id=?
            """,
            (restarted["runtime_account_id"],),
        ).fetchone()
        assert new_runtime["account_status"] == "active"
        assert new_runtime["owner_status"] == "active"
        new_state = conn.execute(
            """
            SELECT * FROM plum_connection_relationship_state
            WHERE connection_id=?
            """,
            (restarted["connection_id"],),
        ).fetchone()
        assert new_state["relationship_level"] == 0
        assert new_state["relationship_xp"] == 0
        assert new_state["completed_turn_count"] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM plum_connections
            WHERE persona_id=(SELECT persona_id FROM plum_connections WHERE id=?)
              AND character_id='char_ref_after_hours' AND status='active'
            """,
            (restarted["connection_id"],),
        ).fetchone()["n"] == 1


def test_taken_down_character_blocks_existing_connection_and_restart(
    fresh_db, monkeypatch
):
    """Character 下架后已有 Connection 不能继续，也不能通过重开绕过。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    conversation = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE plum_characters SET status='taken_down'
            WHERE id='char_ref_after_hours'
            """
        )

    assert repository.get_conversation(
        conversation_id=conversation["id"],
        platform_user_id="user_plum_test",
    ) is None
    with pytest.raises(ValueError, match="character not found"):
        repository.create_or_get_conversation(
            platform_user_id="user_plum_test",
            character_id="char_ref_after_hours",
        )
    with pytest.raises(ValueError, match="character not found"):
        repository.restart_conversation(
            conversation_id=conversation["id"],
            platform_user_id="user_plum_test",
            creation_idempotency_key="restart-taken-down-1",
        )


def test_m0073_foundation_remains_available_at_current_schema_head(fresh_db):
    assert (76, _migration_0076_plum_character_create_idempotency) in _MIGRATIONS
    assert _MIGRATIONS[-1] == (78, _migration_0078_plum_external_identity_challenges)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()["v"] == 78
        for table in (
            "plum_connections",
            "plum_connection_character_adoptions",
            "plum_connection_relationship_state",
            "plum_connection_runtime_bindings",
        ):
            conn.execute(f"SELECT 1 FROM {table} WHERE 1=0").fetchall()
