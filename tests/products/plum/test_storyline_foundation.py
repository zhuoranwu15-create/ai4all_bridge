"""Plum m0074 Storyline 基础、归属隔离与唯一重开测试。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.db as db
from app.db._backend import IntegrityError
from app.db._core import _migration_0074_plum_storyline_foundation
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


def test_m0074_backfills_one_archived_storyline_and_max_chapter(
    test_settings, empty_pg_database
):
    """每个存量 Connection 只回填一条 Storyline，并保留最大章节进度。"""

    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=73, expected_current_version=0)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_storyline_legacy',
                        'storyline-legacy@local.invalid', 'Legacy')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_works(
                    id, owner_platform_user_id, creator_profile_id
                ) VALUES ('work_storyline_legacy',
                          'pusr_storyline_legacy', 'profile_storyline_legacy')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_characters(
                    id, work_id, display_name, tagline, intro, greeting,
                    persona_prompt, scenario_prompt, speaking_style,
                    prompt_version, content_version, status
                )
                VALUES ('char_storyline_legacy', 'work_storyline_legacy',
                        'Legacy Char', 'tagline', 'intro', 'hello',
                        'settings', 'scene', 'style', 1, 1, 'active')
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
                VALUES ('char_storyline_legacy', 1, 1, 'Legacy Char',
                        'intro', 'hello', 'settings', 'scene', 'style',
                        'general', 'general', 'plum-rating-v1', 'public')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_user_personas(
                    id, platform_user_id, display_name, status,
                    is_default, version, locked_at
                )
                VALUES ('fpersona_storyline_legacy',
                        'pusr_storyline_legacy', 'Legacy Persona',
                        'active', 1, 1,
                        to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                                'YYYY-MM-DD HH24:MI:SS'))
                """
            )
            conn.execute(
                """
                INSERT INTO plum_connections(
                    id, platform_user_id, persona_id, character_id,
                    current_character_version, status, creation_reason,
                    archived_at
                )
                VALUES ('fconn_storyline_legacy', 'pusr_storyline_legacy',
                        'fpersona_storyline_legacy', 'char_storyline_legacy',
                        1, 'archived', 'backfill',
                        to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                                'YYYY-MM-DD HH24:MI:SS'))
                """
            )
            conn.execute(
                """
                INSERT INTO accounts(id, display_name, app_id)
                VALUES ('aid_storyline_legacy', 'Legacy Runtime', 'plum')
                """
            )
            session = conn.execute(
                """
                INSERT INTO sessions(account_id, session_key, sender_id)
                VALUES ('aid_storyline_legacy', 'legacy-storyline',
                        'pusr_storyline_legacy')
                RETURNING id
                """
            ).fetchone()
            for suffix, chapter in (("one", 2), ("two", 7)):
                conn.execute(
                    """
                    INSERT INTO plum_conversations(
                        id, platform_user_id, character_id, connection_id,
                        runtime_account_id, runtime_session_id, model_profile,
                        current_chapter_no, status, archived_at
                    )
                    VALUES (?, 'pusr_storyline_legacy',
                            'char_storyline_legacy', 'fconn_storyline_legacy',
                            'aid_storyline_legacy', ?, 'balanced', ?,
                            'archived',
                            to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                                    'YYYY-MM-DD HH24:MI:SS'))
                    """,
                    (f"fconv_storyline_{suffix}", session["id"], chapter),
                )

            _migration_0074_plum_storyline_foundation(conn)
            _migration_0074_plum_storyline_foundation(conn)

            storyline = conn.execute(
                "SELECT * FROM plum_storylines"
            ).fetchone()
            assert storyline["connection_id"] == "fconn_storyline_legacy"
            assert storyline["ordinal"] == 1
            assert storyline["opening_character_version"] == 1
            assert storyline["status"] == "archived"
            assert storyline["migration_source"] == "m0074_connection_backfill"
            state = conn.execute(
                "SELECT * FROM plum_storyline_state"
            ).fetchone()
            assert state["storyline_id"] == storyline["id"]
            assert state["current_chapter_no"] == 7
            assert state["state_schema_version"] == 1
            assert state["plot_state_json"] == {}
            conversations = conn.execute(
                "SELECT storyline_id FROM plum_conversations ORDER BY id"
            ).fetchall()
            assert {row["storyline_id"] for row in conversations} == {
                storyline["id"]
            }
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM plum_storylines"
            ).fetchone()["n"] == 1
    db.close_pg_pool()


def test_new_connection_creates_scoped_storyline_and_empty_state(
    fresh_db, monkeypatch
):
    """建连事务同时创建初始 Storyline、空 State 并绑定 Conversation。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    conversation = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
    )

    with db.connect() as conn:
        storyline = conn.execute(
            "SELECT * FROM plum_storylines WHERE connection_id=?",
            (conversation["connection_id"],),
        ).fetchone()
        state = conn.execute(
            "SELECT * FROM plum_storyline_state WHERE storyline_id=?",
            (storyline["id"],),
        ).fetchone()
        stored_conversation = conn.execute(
            "SELECT * FROM plum_conversations WHERE id=?",
            (conversation["id"],),
        ).fetchone()

    assert storyline["platform_user_id"] == "user_plum_test"
    assert storyline["character_id"] == "char_ref_after_hours"
    assert storyline["status"] == "active"
    assert storyline["ordinal"] == 1
    assert state["platform_user_id"] == "user_plum_test"
    assert state["connection_id"] == conversation["connection_id"]
    assert state["current_chapter_no"] == 1
    assert state["state_schema_version"] == 1
    assert state["plot_state_json"] == {}
    assert stored_conversation["storyline_id"] == storyline["id"]


def test_restart_archives_old_storyline_and_is_idempotent(fresh_db, monkeypatch):
    """重开归档旧 Storyline，并只创建一套全新的 Connection 剧情状态。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    original = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
    )
    restarted = repository.restart_conversation(
        conversation_id=original["id"],
        platform_user_id="user_plum_test",
        creation_idempotency_key="restart-storyline-1",
    )
    replay = repository.restart_conversation(
        conversation_id=original["id"],
        platform_user_id="user_plum_test",
        creation_idempotency_key="restart-storyline-1",
    )

    assert replay["id"] == restarted["id"]
    with db.connect() as conn:
        old_storyline = conn.execute(
            "SELECT * FROM plum_storylines WHERE connection_id=?",
            (original["connection_id"],),
        ).fetchone()
        new_storylines = conn.execute(
            "SELECT * FROM plum_storylines WHERE connection_id=?",
            (restarted["connection_id"],),
        ).fetchall()
        new_state = conn.execute(
            """
            SELECT st.* FROM plum_storyline_state st
            JOIN plum_storylines ps ON ps.id=st.storyline_id
            WHERE ps.connection_id=?
            """,
            (restarted["connection_id"],),
        ).fetchone()

    assert old_storyline["status"] == "archived"
    assert old_storyline["archived_at"] is not None
    assert len(new_storylines) == 1
    assert new_storylines[0]["status"] == "active"
    assert new_storylines[0]["id"] != old_storyline["id"]
    assert new_state["current_chapter_no"] == 1
    assert new_state["plot_state_json"] == {}

    with pytest.raises(ValueError, match="conversation not found"):
        repository.update_conversation_model(
            conversation_id=original["id"],
            platform_user_id="user_plum_test",
            model_profile="fast",
        )
    with db.connect() as conn:
        conn.execute(
            "UPDATE plum_conversations SET updated_at='2000-01-01 00:00:00' WHERE id=?",
            (original["id"],),
        )
    repository.touch_conversation(
        conversation_id=original["id"], platform_user_id="user_plum_test"
    )
    with db.connect() as conn:
        assert conn.execute(
            "SELECT updated_at FROM plum_conversations WHERE id=?",
            (original["id"],),
        ).fetchone()["updated_at"] == "2000-01-01 00:00:00"


def test_storyline_constraints_reject_cross_connection_and_second_active(
    fresh_db, monkeypatch
):
    """Conversation 不能串线，且一个 Connection 不能有第二条 active Storyline。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    first = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO plum_user_personas(
                id, platform_user_id, display_name, status, is_default, version
            ) VALUES ('fpersona_storyline_second', 'user_plum_test',
                      'Second', 'active', 0, 1)
            """
        )
    second = repository.create_or_get_conversation(
        platform_user_id="user_plum_test",
        character_id="char_ref_after_hours",
        persona_id="fpersona_storyline_second",
    )

    with db.connect() as conn:
        first_storyline = conn.execute(
            "SELECT * FROM plum_storylines WHERE connection_id=?",
            (first["connection_id"],),
        ).fetchone()
        second_storyline = conn.execute(
            "SELECT * FROM plum_storylines WHERE connection_id=?",
            (second["connection_id"],),
        ).fetchone()

    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "UPDATE plum_conversations SET storyline_id=? WHERE id=?",
                (second_storyline["id"], first["id"]),
            )

    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO plum_storylines(
                    id, platform_user_id, connection_id, character_id,
                    ordinal, opening_character_version, status
                )
                VALUES ('fstory_second_active', 'user_plum_test', ?,
                        'char_ref_after_hours', 2, ?, 'active')
                """,
                (
                    first["connection_id"],
                    first_storyline["opening_character_version"],
                ),
            )
