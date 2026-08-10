"""Plum 产品相关的 schema 迁移。

函数体自 ``app/db/_core.py`` 原样搬出；顺序仍由 ``_core._MIGRATIONS`` 决定。
"""
from __future__ import annotations

import re

from app.db._backend import Connection, is_postgres
from app.db._schema_utils import _ensure_column, _table_exists


def _migration_0064_fibre_mvp(conn: Connection) -> None:
    """建立通用 runtime 归属投影与 Fibre MVP 产品私有数据表。"""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_ownerships (
            runtime_account_id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            owner_kind TEXT NOT NULL DEFAULT 'user',
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(runtime_account_id) REFERENCES accounts(id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_ownerships_source
            ON runtime_ownerships(app_id, source_type, source_id)
            WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS ix_runtime_ownerships_owner
            ON runtime_ownerships(platform_user_id, app_id, status);

        CREATE TABLE IF NOT EXISTS fibre_characters (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            tagline TEXT NOT NULL,
            intro TEXT NOT NULL,
            greeting TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            heat_count INTEGER NOT NULL DEFAULT 0,
            avatar_ref TEXT,
            cover_ref TEXT,
            accent_color TEXT NOT NULL DEFAULT '#8b5cf6',
            persona_prompt TEXT NOT NULL,
            scenario_prompt TEXT NOT NULL DEFAULT '',
            speaking_style TEXT NOT NULL DEFAULT '',
            prompt_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_characters_feed
            ON fibre_characters(status, sort_order, id);

        CREATE TABLE IF NOT EXISTS fibre_character_bindings (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            runtime_account_id TEXT NOT NULL,
            character_prompt_version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(platform_user_id, character_id),
            UNIQUE(runtime_account_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id),
            FOREIGN KEY(runtime_account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS fibre_model_profiles (
            profile TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL,
            coin_cost_micros INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            is_default INTEGER NOT NULL DEFAULT 0,
            config_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fibre_model_profiles_default
            ON fibre_model_profiles(is_default) WHERE enabled = 1 AND is_default = 1;

        CREATE TABLE IF NOT EXISTS fibre_conversations (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            runtime_account_id TEXT NOT NULL,
            runtime_session_id INTEGER NOT NULL,
            model_profile TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            archived_at TEXT,
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id),
            FOREIGN KEY(runtime_account_id) REFERENCES accounts(id),
            FOREIGN KEY(runtime_session_id) REFERENCES sessions(id),
            FOREIGN KEY(model_profile) REFERENCES fibre_model_profiles(profile)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fibre_conversations_active
            ON fibre_conversations(platform_user_id, character_id)
            WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS ix_fibre_conversations_owner
            ON fibre_conversations(platform_user_id, status, updated_at);
        """
    )


def _migration_0065_fibre_character_experience(conn: Connection) -> None:
    """补齐 Fibre Feed/Profile、viewer state 与会话工具的产品私有数据。"""

    # Migration replay tests and repaired legacy databases may already contain
    # one or more of these columns even when version 65 is absent. Keep the
    # migration idempotent across SQLite and PostgreSQL.
    _ensure_column(conn, "fibre_characters", "creator_profile_id", "TEXT")
    _ensure_column(
        conn,
        "fibre_characters",
        "content_rating",
        "TEXT NOT NULL DEFAULT 'general'",
    )
    _ensure_column(
        conn,
        "fibre_characters",
        "capabilities_json",
        "TEXT NOT NULL DEFAULT '{\"text\":true,\"voice\":false}'",
    )
    _ensure_column(conn, "fibre_characters", "fixture_version", "TEXT")
    _ensure_column(
        conn,
        "fibre_conversations",
        "current_chapter_no",
        "INTEGER NOT NULL DEFAULT 1",
    )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fibre_public_profiles (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT,
            handle TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            avatar_ref TEXT,
            profile_type TEXT NOT NULL DEFAULT 'creator',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fibre_public_profiles_platform_user
            ON fibre_public_profiles(platform_user_id) WHERE platform_user_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS fibre_character_badges (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            icon_ref TEXT,
            style_token TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );
        CREATE TABLE IF NOT EXISTS fibre_character_badge_assignments (
            character_id TEXT NOT NULL,
            badge_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            starts_at TEXT,
            ends_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(character_id, badge_id),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id),
            FOREIGN KEY(badge_id) REFERENCES fibre_character_badges(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_character_badges_character
            ON fibre_character_badge_assignments(character_id, sort_order);

        CREATE TABLE IF NOT EXISTS fibre_character_stats (
            character_id TEXT PRIMARY KEY,
            interaction_count INTEGER NOT NULL DEFAULT 0 CHECK(interaction_count >= 0),
            connector_count INTEGER NOT NULL DEFAULT 0 CHECK(connector_count >= 0),
            comment_count INTEGER NOT NULL DEFAULT 0 CHECK(comment_count >= 0),
            memory_count INTEGER NOT NULL DEFAULT 0 CHECK(memory_count >= 0),
            like_count INTEGER NOT NULL DEFAULT 0 CHECK(like_count >= 0),
            favorite_count INTEGER NOT NULL DEFAULT 0 CHECK(favorite_count >= 0),
            stats_version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id)
        );

        CREATE TABLE IF NOT EXISTS fibre_user_character_relationships (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'connected',
            relationship_level INTEGER NOT NULL DEFAULT 0 CHECK(relationship_level >= 0),
            relationship_xp INTEGER NOT NULL DEFAULT 0 CHECK(relationship_xp >= 0),
            completed_turn_count INTEGER NOT NULL DEFAULT 0 CHECK(completed_turn_count >= 0),
            first_connected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            last_interacted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(platform_user_id, character_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_relationships_character
            ON fibre_user_character_relationships(character_id, state, last_interacted_at);

        CREATE TABLE IF NOT EXISTS fibre_character_likes (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(platform_user_id, character_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_character_likes_character
            ON fibre_character_likes(character_id, created_at);

        CREATE TABLE IF NOT EXISTS fibre_character_favorites (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(platform_user_id, character_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_character_favorites_character
            ON fibre_character_favorites(character_id, created_at);

        CREATE TABLE IF NOT EXISTS fibre_character_comments (
            id TEXT PRIMARY KEY,
            character_id TEXT NOT NULL,
            author_profile_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source_locale TEXT NOT NULL DEFAULT 'en',
            status TEXT NOT NULL DEFAULT 'visible',
            like_count INTEGER NOT NULL DEFAULT 0 CHECK(like_count >= 0),
            is_featured INTEGER NOT NULL DEFAULT 0,
            featured_rank INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            deleted_at TEXT,
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id),
            FOREIGN KEY(author_profile_id) REFERENCES fibre_public_profiles(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_comments_profile
            ON fibre_character_comments(character_id, status, is_featured, featured_rank, created_at);

        CREATE TABLE IF NOT EXISTS fibre_character_memories (
            id TEXT PRIMARY KEY,
            character_id TEXT NOT NULL,
            owner_profile_id TEXT NOT NULL,
            fibre_conversation_id TEXT,
            origin TEXT NOT NULL DEFAULT 'seed',
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            cover_ref TEXT,
            message_count INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0),
            engagement_count INTEGER NOT NULL DEFAULT 0 CHECK(engagement_count >= 0),
            visibility TEXT NOT NULL DEFAULT 'private',
            moderation_status TEXT NOT NULL DEFAULT 'pending',
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(character_id) REFERENCES fibre_characters(id),
            FOREIGN KEY(owner_profile_id) REFERENCES fibre_public_profiles(id),
            FOREIGN KEY(fibre_conversation_id) REFERENCES fibre_conversations(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_memories_profile
            ON fibre_character_memories(character_id, visibility, moderation_status, published_at);

        CREATE TABLE IF NOT EXISTS fibre_user_personas (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            avatar_ref TEXT,
            description TEXT NOT NULL DEFAULT '',
            prompt_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            is_default INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fibre_personas_default
            ON fibre_user_personas(platform_user_id, is_default)
            WHERE status = 'active' AND is_default = 1;

        CREATE TABLE IF NOT EXISTS fibre_conversation_pins (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            content_snapshot TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(conversation_id) REFERENCES fibre_conversations(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_conversation_pins_active
            ON fibre_conversation_pins(conversation_id, status, sort_order);
        """
    )


def _migration_0066_fibre_public_test_auth(conn: Connection) -> None:
    """Add one-person-one-code credentials for the Fibre public test."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fibre_access_invites (
            id TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            platform_user_id TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'revoked')),
            expires_at TEXT,
            claimed_at TEXT,
            last_used_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_access_invites_status_expiry
            ON fibre_access_invites(status, expires_at);
        """
    )


def _migration_0067_plum_product_rename(conn: Connection) -> None:
    """Rename the Fibre product schema and shared ownership rows to Plum.

    Migrations 64-66 stay immutable so an existing deployment can prove which
    schema it originally applied.  This forward migration is deliberately
    idempotent and refuses to guess when both the legacy and Plum table for the
    same entity exist.
    """

    legacy_prefix = "fi" + "bre"
    plum_prefix = "plum"
    table_suffixes = (
        "access_invites",
        "character_badge_assignments",
        "character_badges",
        "character_bindings",
        "character_comments",
        "character_favorites",
        "character_likes",
        "character_memories",
        "character_stats",
        "characters",
        "conversation_pins",
        "conversations",
        "model_profiles",
        "public_profiles",
        "user_character_relationships",
        "user_personas",
    )
    table_pairs = tuple(
        (f"{legacy_prefix}_{suffix}", f"{plum_prefix}_{suffix}")
        for suffix in table_suffixes
    )
    for legacy_table, plum_table in table_pairs:
        if _table_exists(conn, legacy_table) and _table_exists(conn, plum_table):
            raise RuntimeError(
                "m0067 refuses to merge coexisting product tables: "
                f"{legacy_table}, {plum_table}"
            )

    legacy_indexes = (
        "ix_characters_feed",
        "ux_model_profiles_default",
        "ux_conversations_active",
        "ix_conversations_owner",
        "ux_public_profiles_platform_user",
        "ix_character_badges_character",
        "ix_relationships_character",
        "ix_character_likes_character",
        "ix_character_favorites_character",
        "ix_comments_profile",
        "ix_memories_profile",
        "ux_personas_default",
        "ix_conversation_pins_active",
        "ix_access_invites_status_expiry",
    )
    for index_suffix in legacy_indexes:
        conn.execute(
            f"DROP INDEX IF EXISTS "
            f"{index_suffix[:3]}_{legacy_prefix}_{index_suffix[3:]}"
        )

    for legacy_table, plum_table in table_pairs:
        if _table_exists(conn, legacy_table):
            conn.execute(f"ALTER TABLE {legacy_table} RENAME TO {plum_table}")

    memories_table = "plum_character_memories"
    legacy_conversation_column = f"{legacy_prefix}_conversation_id"
    plum_conversation_column = "plum_conversation_id"
    if _table_exists(conn, memories_table):
        if is_postgres():
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name=? AND column_name IN (?, ?)
                """,
                (
                    memories_table,
                    legacy_conversation_column,
                    plum_conversation_column,
                ),
            ).fetchall()
            columns = {str(row["column_name"]) for row in rows}
        else:
            columns = {
                str(row["name"])
                for row in conn.execute(
                    f"PRAGMA table_info({memories_table})"
                ).fetchall()
            }
        if (
            legacy_conversation_column in columns
            and plum_conversation_column in columns
        ):
            raise RuntimeError(
                "m0067 refuses to merge coexisting conversation reference columns"
            )
        if legacy_conversation_column in columns:
            conn.execute(
                f"ALTER TABLE {memories_table} "
                f"RENAME COLUMN {legacy_conversation_column} "
                f"TO {plum_conversation_column}"
            )

    shared_app_tables = (
        "accounts",
        "account_owner_bindings",
        "platform_user_sessions",
        "product_memberships",
        "subscriptions",
        "entitlement_wallets",
        "entitlement_ledger",
        "cost_events",
        "daily_usage",
        "daily_quota_reservations",
        "runtime_ownerships",
    )
    for table in shared_app_tables:
        if _table_exists(conn, table):
            conn.execute(
                f"UPDATE {table} SET app_id=? WHERE app_id=?",
                (plum_prefix, legacy_prefix),
            )

    if is_postgres():
        for _legacy_table, plum_table in table_pairs:
            if not _table_exists(conn, plum_table):
                continue
            constraints = conn.execute(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conrelid=to_regclass(?) AND conname LIKE ?
                """,
                (plum_table, f"{legacy_prefix}_%"),
            ).fetchall()
            for row in constraints:
                old_name = str(row["conname"])
                new_name = old_name.replace(legacy_prefix, plum_prefix, 1)
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", old_name):
                    raise RuntimeError(f"unsafe legacy constraint name: {old_name!r}")
                conn.execute(
                    f"ALTER TABLE {plum_table} "
                    f"RENAME CONSTRAINT {old_name} TO {new_name}"
                )

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_plum_characters_feed
            ON plum_characters(status, sort_order, id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_model_profiles_default
            ON plum_model_profiles(is_default) WHERE enabled = 1 AND is_default = 1;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_conversations_active
            ON plum_conversations(platform_user_id, character_id)
            WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS ix_plum_conversations_owner
            ON plum_conversations(platform_user_id, status, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_public_profiles_platform_user
            ON plum_public_profiles(platform_user_id) WHERE platform_user_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_plum_character_badges_character
            ON plum_character_badge_assignments(character_id, sort_order);
        CREATE INDEX IF NOT EXISTS ix_plum_relationships_character
            ON plum_user_character_relationships(character_id, state, last_interacted_at);
        CREATE INDEX IF NOT EXISTS ix_plum_character_likes_character
            ON plum_character_likes(character_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_plum_character_favorites_character
            ON plum_character_favorites(character_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_plum_comments_profile
            ON plum_character_comments(
                character_id, status, is_featured, featured_rank, created_at
            );
        CREATE INDEX IF NOT EXISTS ix_plum_memories_profile
            ON plum_character_memories(
                character_id, visibility, moderation_status, published_at
            );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_personas_default
            ON plum_user_personas(platform_user_id, is_default)
            WHERE status = 'active' AND is_default = 1;
        CREATE INDEX IF NOT EXISTS ix_plum_conversation_pins_active
            ON plum_conversation_pins(conversation_id, status, sort_order);
        CREATE INDEX IF NOT EXISTS ix_plum_access_invites_status_expiry
            ON plum_access_invites(status, expires_at);
        """
    )


__all__ = [
    "_migration_0064_fibre_mvp",
    "_migration_0065_fibre_character_experience",
    "_migration_0066_fibre_public_test_auth",
    "_migration_0067_plum_product_rename",
]
