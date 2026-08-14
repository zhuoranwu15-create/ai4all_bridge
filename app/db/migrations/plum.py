"""Plum 产品相关的 schema 迁移。

函数体自 ``app/db/_core.py`` 原样搬出；顺序仍由 ``_core._MIGRATIONS`` 决定。
"""
from __future__ import annotations

import re

from app.db._backend import Connection
from app.db._schema_utils import _ensure_column, _table_exists


def _ensure_constraint(
    conn: Connection, table: str, name: str, definition: str
) -> None:
    """Add one named PostgreSQL constraint when a replay has not created it."""

    if not all(re.fullmatch(r"[a-z_][a-z0-9_]*", value) for value in (table, name)):
        raise RuntimeError("unsafe Plum schema identifier")
    exists = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(?) AND conname=?",
        (table, name),
    ).fetchone()
    if exists is None:
        conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")


def _ensure_not_null(conn: Connection, table: str, column: str) -> None:
    """Set NOT NULL only when a replay still sees a nullable column."""

    row = conn.execute(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=? AND column_name=?
        """,
        (table, column),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing column for NOT NULL: {table}.{column}")
    if row["is_nullable"] == "YES":
        conn.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")


def _ensure_default(
    conn: Connection, table: str, column: str, default: str
) -> None:
    """Install a column default only when a replay has not installed one."""

    row = conn.execute(
        """
        SELECT column_default FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=? AND column_name=?
        """,
        (table, column),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing column for default: {table}.{column}")
    if row["column_default"] is None:
        conn.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default}"
        )


def _ensure_character_version_immutability(conn: Connection) -> None:
    """Reject in-place update/delete of approved Character version snapshots."""

    conn.execute(
        """
        CREATE OR REPLACE FUNCTION plum_reject_character_version_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'plum_character_versions rows are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    exists = conn.execute(
        """
        SELECT 1 FROM pg_trigger
        WHERE tgrelid=to_regclass('plum_character_versions')
          AND tgname='trg_plum_character_versions_immutable'
          AND NOT tgisinternal
        """
    ).fetchone()
    if exists is None:
        conn.execute(
            """
            CREATE TRIGGER trg_plum_character_versions_immutable
            BEFORE UPDATE OR DELETE ON plum_character_versions
            FOR EACH ROW EXECUTE FUNCTION plum_reject_character_version_mutation()
            """
        )


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
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
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
            heat_count BIGINT NOT NULL DEFAULT 0,
            avatar_ref TEXT,
            cover_ref TEXT,
            accent_color TEXT NOT NULL DEFAULT '#8b5cf6',
            persona_prompt TEXT NOT NULL,
            scenario_prompt TEXT NOT NULL DEFAULT '',
            speaking_style TEXT NOT NULL DEFAULT '',
            prompt_version BIGINT NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft',
            sort_order BIGINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_characters_feed
            ON fibre_characters(status, sort_order, id);

        CREATE TABLE IF NOT EXISTS fibre_character_bindings (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            runtime_account_id TEXT NOT NULL,
            character_prompt_version BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, character_id),
            UNIQUE(runtime_account_id)
        );

        CREATE TABLE IF NOT EXISTS fibre_model_profiles (
            profile TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL,
            coin_cost_micros BIGINT NOT NULL,
            enabled BIGINT NOT NULL DEFAULT 1,
            is_default BIGINT NOT NULL DEFAULT 0,
            config_version BIGINT NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fibre_model_profiles_default
            ON fibre_model_profiles(is_default) WHERE enabled = 1 AND is_default = 1;

        CREATE TABLE IF NOT EXISTS fibre_conversations (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            runtime_account_id TEXT NOT NULL,
            runtime_session_id BIGINT NOT NULL,
            model_profile TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            archived_at TEXT
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
    # Migration replay may encounter columns repaired outside the version chain.
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
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
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
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );
        CREATE TABLE IF NOT EXISTS fibre_character_badge_assignments (
            character_id TEXT NOT NULL,
            badge_id TEXT NOT NULL,
            sort_order BIGINT NOT NULL DEFAULT 0,
            starts_at TEXT,
            ends_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(character_id, badge_id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_character_badges_character
            ON fibre_character_badge_assignments(character_id, sort_order);

        CREATE TABLE IF NOT EXISTS fibre_character_stats (
            character_id TEXT PRIMARY KEY,
            interaction_count BIGINT NOT NULL DEFAULT 0 CHECK(interaction_count >= 0),
            connector_count BIGINT NOT NULL DEFAULT 0 CHECK(connector_count >= 0),
            comment_count BIGINT NOT NULL DEFAULT 0 CHECK(comment_count >= 0),
            memory_count BIGINT NOT NULL DEFAULT 0 CHECK(memory_count >= 0),
            like_count BIGINT NOT NULL DEFAULT 0 CHECK(like_count >= 0),
            favorite_count BIGINT NOT NULL DEFAULT 0 CHECK(favorite_count >= 0),
            stats_version BIGINT NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );

        CREATE TABLE IF NOT EXISTS fibre_user_character_relationships (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'connected',
            relationship_level BIGINT NOT NULL DEFAULT 0 CHECK(relationship_level >= 0),
            relationship_xp BIGINT NOT NULL DEFAULT 0 CHECK(relationship_xp >= 0),
            completed_turn_count BIGINT NOT NULL DEFAULT 0 CHECK(completed_turn_count >= 0),
            first_connected_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            last_interacted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, character_id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_relationships_character
            ON fibre_user_character_relationships(character_id, state, last_interacted_at);

        CREATE TABLE IF NOT EXISTS fibre_character_likes (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, character_id)
        );
        CREATE INDEX IF NOT EXISTS ix_fibre_character_likes_character
            ON fibre_character_likes(character_id, created_at);

        CREATE TABLE IF NOT EXISTS fibre_character_favorites (
            platform_user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, character_id)
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
            like_count BIGINT NOT NULL DEFAULT 0 CHECK(like_count >= 0),
            is_featured BIGINT NOT NULL DEFAULT 0,
            featured_rank BIGINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            deleted_at TEXT
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
            message_count BIGINT NOT NULL DEFAULT 0 CHECK(message_count >= 0),
            engagement_count BIGINT NOT NULL DEFAULT 0 CHECK(engagement_count >= 0),
            visibility TEXT NOT NULL DEFAULT 'private',
            moderation_status TEXT NOT NULL DEFAULT 'pending',
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
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
            is_default BIGINT NOT NULL DEFAULT 0,
            version BIGINT NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fibre_personas_default
            ON fibre_user_personas(platform_user_id, is_default)
            WHERE status = 'active' AND is_default = 1;

        CREATE TABLE IF NOT EXISTS fibre_conversation_pins (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            content_snapshot TEXT NOT NULL,
            sort_order BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
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
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
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


def _migration_0072_plum_character_content_foundation(conn: Connection) -> None:
    """建立 Plum Work、Character Version、受控 Tag 与 Persona 锁定基础。"""

    # 内置内容没有真人 owner。使用明确的系统主体承接存量，避免根据角色名称、
    # creator profile 或历史聊天猜测所有权。
    conn.execute(
        """
        INSERT INTO platform_users(id, phone, display_name, status)
        VALUES ('pusr_plum_system', 'plum-system@local.invalid',
                'Plum System', 'active')
        ON CONFLICT(id) DO NOTHING
        """
    )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_works (
            id TEXT PRIMARY KEY,
            owner_platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            creator_profile_id TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'active'
                CHECK(lifecycle_status IN ('active', 'archived')),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );
        CREATE INDEX IF NOT EXISTS ix_plum_works_owner
            ON plum_works(owner_platform_user_id, lifecycle_status, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_works_id_owner
            ON plum_works(id, owner_platform_user_id);
        """
    )

    character_columns = (
        ("work_id", "TEXT"),
        ("gender", "TEXT"),
        ("portrait_media_id", "TEXT"),
        ("portrait_position_x", "INTEGER"),
        ("portrait_position_y", "INTEGER"),
        ("avatar_position_x", "INTEGER"),
        ("avatar_position_y", "INTEGER"),
        ("example_dialogues", "TEXT"),
        ("content_version", "BIGINT"),
        ("creator_declared_rating", "TEXT"),
        ("platform_effective_rating", "TEXT"),
        ("access_policy_version", "TEXT"),
        ("moderation_decision_id", "TEXT"),
        ("visibility", "TEXT"),
        ("published_at", "TEXT"),
    )
    for column, definition in character_columns:
        _ensure_column(conn, "plum_characters", column, definition)

    persona_columns = (
        ("avatar_media_id", "TEXT"),
        ("locked_at", "TEXT"),
        ("derived_from_persona_id", "TEXT"),
    )
    for column, definition in persona_columns:
        _ensure_column(conn, "plum_user_personas", column, definition)

    # 先恢复各新增投影的合法默认值，再增加约束，兼容曾被手工补过空列的库。
    conn.execute(
        """
        UPDATE plum_characters
        SET portrait_position_x=COALESCE(portrait_position_x, 50),
            portrait_position_y=COALESCE(portrait_position_y, 50),
            avatar_position_x=COALESCE(avatar_position_x, 50),
            avatar_position_y=COALESCE(avatar_position_y, 50),
            example_dialogues=COALESCE(example_dialogues, ''),
            content_version=COALESCE(content_version, 1),
            creator_declared_rating=COALESCE(
                NULLIF(BTRIM(creator_declared_rating), ''),
                NULLIF(BTRIM(content_rating), ''),
                'general'
            ),
            platform_effective_rating=COALESCE(
                NULLIF(BTRIM(platform_effective_rating), ''),
                NULLIF(BTRIM(content_rating), ''),
                'general'
            ),
            access_policy_version=COALESCE(
                NULLIF(BTRIM(access_policy_version), ''), 'plum-rating-v1'
            ),
            visibility=COALESCE(NULLIF(BTRIM(visibility), ''), 'public'),
            published_at=CASE
                WHEN status='active' THEN COALESCE(published_at, created_at)
                ELSE published_at
            END
        WHERE portrait_position_x IS NULL
           OR portrait_position_y IS NULL
           OR avatar_position_x IS NULL
           OR avatar_position_y IS NULL
           OR example_dialogues IS NULL
           OR content_version IS NULL
           OR creator_declared_rating IS NULL
           OR BTRIM(creator_declared_rating)=''
           OR platform_effective_rating IS NULL
           OR BTRIM(platform_effective_rating)=''
           OR access_policy_version IS NULL
           OR BTRIM(access_policy_version)=''
           OR visibility IS NULL
           OR BTRIM(visibility)=''
           OR (status='active' AND published_at IS NULL)
        """
    )
    conn.execute("UPDATE plum_user_personas SET version=1 WHERE version<>1")

    conn.execute(
        """
        INSERT INTO plum_works(
            id, owner_platform_user_id, creator_profile_id,
            lifecycle_status, created_at, updated_at
        )
        SELECT 'work_for_' || id,
               'pusr_plum_system',
               COALESCE(NULLIF(BTRIM(creator_profile_id), ''), 'fprof_creator_plum'),
               CASE WHEN status='archived' THEN 'archived' ELSE 'active' END,
               created_at,
               updated_at
        FROM plum_characters
        WHERE work_id IS NULL
        ON CONFLICT(id) DO NOTHING
        """
    )
    conn.execute(
        "UPDATE plum_characters SET work_id='work_for_' || id WHERE work_id IS NULL"
    )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_character_versions (
            character_id TEXT NOT NULL REFERENCES plum_characters(id),
            version_number BIGINT NOT NULL CHECK(version_number > 0),
            prompt_version BIGINT NOT NULL CHECK(prompt_version > 0),
            display_name TEXT NOT NULL,
            gender TEXT CHECK(gender IS NULL OR gender IN ('male', 'female', 'non_binary')),
            portrait_media_id TEXT REFERENCES media_assets(id),
            portrait_position_x INTEGER NOT NULL DEFAULT 50 CHECK(portrait_position_x BETWEEN 0 AND 100),
            portrait_position_y INTEGER NOT NULL DEFAULT 50 CHECK(portrait_position_y BETWEEN 0 AND 100),
            avatar_position_x INTEGER NOT NULL DEFAULT 50 CHECK(avatar_position_x BETWEEN 0 AND 100),
            avatar_position_y INTEGER NOT NULL DEFAULT 50 CHECK(avatar_position_y BETWEEN 0 AND 100),
            intro TEXT NOT NULL,
            opening_scene TEXT NOT NULL,
            character_settings TEXT NOT NULL,
            scenario_prompt TEXT NOT NULL DEFAULT '',
            example_dialogues TEXT NOT NULL DEFAULT '',
            response_rules TEXT NOT NULL DEFAULT '',
            creator_declared_rating TEXT NOT NULL CHECK(BTRIM(creator_declared_rating) <> ''),
            platform_effective_rating TEXT NOT NULL CHECK(BTRIM(platform_effective_rating) <> ''),
            access_policy_version TEXT NOT NULL CHECK(BTRIM(access_policy_version) <> ''),
            moderation_decision_id TEXT,
            visibility TEXT NOT NULL CHECK(visibility IN ('public', 'private')),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(character_id, version_number)
        );
        CREATE INDEX IF NOT EXISTS ix_plum_character_versions_created
            ON plum_character_versions(character_id, created_at);

        CREATE TABLE IF NOT EXISTS plum_tags (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'disabled')),
            sort_order BIGINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );

        CREATE TABLE IF NOT EXISTS plum_character_version_tags (
            character_id TEXT NOT NULL,
            version_number BIGINT NOT NULL,
            tag_id TEXT NOT NULL REFERENCES plum_tags(id),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(character_id, version_number, tag_id),
            FOREIGN KEY(character_id, version_number)
                REFERENCES plum_character_versions(character_id, version_number)
        );
        CREATE INDEX IF NOT EXISTS ix_plum_character_version_tags_tag
            ON plum_character_version_tags(tag_id, character_id, version_number);
        """
    )

    # 历史 tags_json 是自由字符串，不能未经产品词表确认就伪装成受控 Tag；
    # 因而只回填完整 Version 1，版本-Tag 关系暂时为空。
    conn.execute(
        """
        INSERT INTO plum_character_versions(
            character_id, version_number, prompt_version, display_name, gender,
            portrait_media_id, portrait_position_x, portrait_position_y,
            avatar_position_x, avatar_position_y, intro, opening_scene,
            character_settings, scenario_prompt, example_dialogues,
            response_rules, creator_declared_rating, platform_effective_rating,
            access_policy_version, moderation_decision_id, visibility, created_at
        )
        SELECT id, 1, prompt_version, display_name, gender,
               portrait_media_id, portrait_position_x, portrait_position_y,
               avatar_position_x, avatar_position_y, intro, greeting,
               persona_prompt, scenario_prompt, example_dialogues,
               speaking_style, creator_declared_rating,
               platform_effective_rating, access_policy_version,
               moderation_decision_id, visibility, updated_at
        FROM plum_characters
        ON CONFLICT(character_id, version_number) DO NOTHING
        """
    )

    orphan = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM plum_characters ch
        LEFT JOIN plum_works w ON w.id=ch.work_id
        WHERE ch.work_id IS NULL OR w.id IS NULL
        """
    ).fetchone()
    missing_version = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM plum_characters ch
        LEFT JOIN plum_character_versions cv
          ON cv.character_id=ch.id AND cv.version_number=ch.content_version
        WHERE cv.character_id IS NULL
        """
    ).fetchone()
    if int(orphan["n"] or 0) or int(missing_version["n"] or 0):
        raise RuntimeError(
            "m0072 Plum character backfill failed: "
            f"orphan_work={int(orphan['n'] or 0)}, "
            f"missing_version={int(missing_version['n'] or 0)}"
        )

    required_character_columns = (
        "work_id",
        "portrait_position_x",
        "portrait_position_y",
        "avatar_position_x",
        "avatar_position_y",
        "example_dialogues",
        "content_version",
        "creator_declared_rating",
        "platform_effective_rating",
        "access_policy_version",
        "visibility",
    )
    for column in required_character_columns:
        _ensure_not_null(conn, "plum_characters", column)

    character_defaults = (
        ("portrait_position_x", "50"),
        ("portrait_position_y", "50"),
        ("avatar_position_x", "50"),
        ("avatar_position_y", "50"),
        ("example_dialogues", "''"),
        ("content_version", "1"),
        ("creator_declared_rating", "'general'"),
        ("platform_effective_rating", "'general'"),
        ("access_policy_version", "'plum-rating-v1'"),
        ("visibility", "'public'"),
    )
    for column, default in character_defaults:
        _ensure_default(conn, "plum_characters", column, default)

    _ensure_constraint(
        conn, "plum_characters", "fk_plum_characters_work",
        "FOREIGN KEY(work_id) REFERENCES plum_works(id)",
    )
    _ensure_constraint(
        conn, "plum_characters", "fk_plum_characters_portrait_media",
        "FOREIGN KEY(portrait_media_id) REFERENCES media_assets(id)",
    )
    _ensure_constraint(
        conn, "plum_characters", "fk_plum_characters_current_version",
        "FOREIGN KEY(id, content_version) "
        "REFERENCES plum_character_versions(character_id, version_number) "
        "DEFERRABLE INITIALLY DEFERRED",
    )
    _ensure_constraint(
        conn, "plum_characters", "ck_plum_characters_gender",
        "CHECK(gender IS NULL OR gender IN ('male', 'female', 'non_binary'))",
    )
    _ensure_constraint(
        conn, "plum_characters", "ck_plum_characters_crop_positions",
        "CHECK(portrait_position_x BETWEEN 0 AND 100 "
        "AND portrait_position_y BETWEEN 0 AND 100 "
        "AND avatar_position_x BETWEEN 0 AND 100 "
        "AND avatar_position_y BETWEEN 0 AND 100)",
    )
    _ensure_constraint(
        conn, "plum_characters", "ck_plum_characters_content_version",
        "CHECK(content_version > 0 AND prompt_version > 0)",
    )
    _ensure_constraint(
        conn, "plum_characters", "ck_plum_characters_visibility",
        "CHECK(visibility IN ('public', 'private'))",
    )
    _ensure_constraint(
        conn, "plum_characters", "ck_plum_characters_lifecycle_status",
        "CHECK(status IN ('draft', 'active', 'taken_down', 'archived'))",
    )
    _ensure_constraint(
        conn, "plum_characters", "ck_plum_characters_rating_fields",
        "CHECK(BTRIM(creator_declared_rating) <> '' "
        "AND BTRIM(platform_effective_rating) <> '' "
        "AND BTRIM(access_policy_version) <> '' "
        "AND content_rating=platform_effective_rating "
        "AND (moderation_decision_id IS NULL "
        "OR BTRIM(moderation_decision_id) <> ''))",
    )
    _ensure_constraint(
        conn, "media_assets", "uq_media_assets_id_owner",
        "UNIQUE(id, owner_platform_user_id)",
    )
    _ensure_constraint(
        conn, "plum_user_personas", "uq_plum_personas_id_owner",
        "UNIQUE(id, platform_user_id)",
    )
    _ensure_constraint(
        conn, "plum_user_personas", "fk_plum_personas_platform_user",
        "FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)",
    )
    _ensure_constraint(
        conn, "plum_user_personas", "fk_plum_personas_avatar_owner",
        "FOREIGN KEY(avatar_media_id, platform_user_id) "
        "REFERENCES media_assets(id, owner_platform_user_id)",
    )
    _ensure_constraint(
        conn, "plum_user_personas", "fk_plum_personas_derived_owner",
        "FOREIGN KEY(derived_from_persona_id, platform_user_id) "
        "REFERENCES plum_user_personas(id, platform_user_id)",
    )
    _ensure_constraint(
        conn, "plum_user_personas", "ck_plum_personas_version_frozen",
        "CHECK(version=1)",
    )

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_plum_characters_work
            ON plum_characters(work_id, status, visibility, updated_at);
        CREATE INDEX IF NOT EXISTS ix_plum_personas_owner_status
            ON plum_user_personas(platform_user_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS ix_plum_personas_derived
            ON plum_user_personas(derived_from_persona_id)
            WHERE derived_from_persona_id IS NOT NULL;
        """
    )
    _ensure_character_version_immutability(conn)


def _ensure_persona_lock_guards(conn: Connection) -> None:
    """Enforce permanent Persona identity locking at the database boundary."""

    conn.execute(
        """
        CREATE OR REPLACE FUNCTION plum_reject_locked_persona_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.locked_at IS NOT NULL AND (
                NEW.locked_at IS NULL
                OR NEW.display_name IS DISTINCT FROM OLD.display_name
                OR NEW.avatar_ref IS DISTINCT FROM OLD.avatar_ref
                OR NEW.avatar_media_id IS DISTINCT FROM OLD.avatar_media_id
                OR NEW.description IS DISTINCT FROM OLD.description
                OR NEW.prompt_text IS DISTINCT FROM OLD.prompt_text
                OR NEW.derived_from_persona_id IS DISTINCT FROM OLD.derived_from_persona_id
            ) THEN
                RAISE EXCEPTION 'locked Plum Persona identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    exists = conn.execute(
        """
        SELECT 1 FROM pg_trigger
        WHERE tgrelid=to_regclass('plum_user_personas')
          AND tgname='trg_plum_user_personas_locked_immutable'
          AND NOT tgisinternal
        """
    ).fetchone()
    if exists is None:
        conn.execute(
            """
            CREATE TRIGGER trg_plum_user_personas_locked_immutable
            BEFORE UPDATE ON plum_user_personas
            FOR EACH ROW EXECUTE FUNCTION plum_reject_locked_persona_mutation()
            """
        )

    conn.execute(
        """
        CREATE OR REPLACE FUNCTION plum_guard_new_connection()
        RETURNS trigger AS $$
        DECLARE
            persona_status TEXT;
            character_status TEXT;
        BEGIN
            SELECT status INTO persona_status
            FROM plum_user_personas
            WHERE id=NEW.persona_id AND platform_user_id=NEW.platform_user_id
            FOR UPDATE;
            IF persona_status IS NULL OR persona_status <> 'active' THEN
                RAISE EXCEPTION 'active owned Plum Persona is required';
            END IF;

            UPDATE plum_user_personas
            SET locked_at=COALESCE(
                    locked_at,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                            'YYYY-MM-DD HH24:MI:SS')
                )
            WHERE id=NEW.persona_id AND platform_user_id=NEW.platform_user_id;

            SELECT status INTO character_status
            FROM plum_characters WHERE id=NEW.character_id;
            IF character_status IS NULL THEN
                RAISE EXCEPTION 'Plum Character is required';
            END IF;
            IF NEW.status='active' AND character_status <> 'active' THEN
                RAISE EXCEPTION 'active Plum Connection requires active Character';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    exists = conn.execute(
        """
        SELECT 1 FROM pg_trigger
        WHERE tgrelid=to_regclass('plum_connections')
          AND tgname='trg_plum_connections_guard_insert'
          AND NOT tgisinternal
        """
    ).fetchone()
    if exists is None:
        conn.execute(
            """
            CREATE TRIGGER trg_plum_connections_guard_insert
            BEFORE INSERT ON plum_connections
            FOR EACH ROW EXECUTE FUNCTION plum_guard_new_connection()
            """
        )


def _migration_0073_plum_connection_foundation(conn: Connection) -> None:
    """建立 Persona–Character Connection，并迁移旧关系与 Runtime 归属。"""

    _ensure_column(conn, "plum_conversations", "connection_id", "TEXT")
    _ensure_constraint(
        conn,
        "runtime_ownerships",
        "uq_runtime_ownerships_account_owner",
        "UNIQUE(runtime_account_id, platform_user_id)",
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_connections (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            persona_id TEXT NOT NULL,
            character_id TEXT NOT NULL REFERENCES plum_characters(id),
            current_character_version BIGINT NOT NULL
                CHECK(current_character_version > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'archived', 'blocked')),
            blocked_reason TEXT,
            creation_reason TEXT NOT NULL
                CHECK(creation_reason IN ('initial', 'restart', 'backfill')),
            creation_idempotency_key TEXT,
            replaces_connection_id TEXT,
            migration_source TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            archived_at TEXT,
            UNIQUE(id, platform_user_id),
            UNIQUE(id, character_id),
            UNIQUE(id, platform_user_id, character_id),
            FOREIGN KEY(persona_id, platform_user_id)
                REFERENCES plum_user_personas(id, platform_user_id),
            FOREIGN KEY(character_id, current_character_version)
                REFERENCES plum_character_versions(character_id, version_number),
            FOREIGN KEY(replaces_connection_id, platform_user_id)
                REFERENCES plum_connections(id, platform_user_id),
            CHECK((status='blocked') = (blocked_reason IS NOT NULL)),
            CHECK((status='archived') = (archived_at IS NOT NULL)),
            CHECK(creation_idempotency_key IS NULL
                  OR (CHAR_LENGTH(BTRIM(creation_idempotency_key))
                      BETWEEN 8 AND 128)),
            CHECK(migration_source IS NULL OR BTRIM(migration_source) <> '')
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_connections_active_persona_character
            ON plum_connections(persona_id, character_id)
            WHERE status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_connections_user_idempotency
            ON plum_connections(platform_user_id, creation_idempotency_key)
            WHERE creation_idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_plum_connections_owner_status
            ON plum_connections(platform_user_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS ix_plum_connections_character_status
            ON plum_connections(character_id, status, updated_at);

        CREATE TABLE IF NOT EXISTS plum_connection_character_adoptions (
            id TEXT PRIMARY KEY,
            connection_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            from_content_version BIGINT,
            to_content_version BIGINT NOT NULL CHECK(to_content_version > 0),
            from_prompt_version BIGINT,
            to_prompt_version BIGINT NOT NULL CHECK(to_prompt_version > 0),
            trigger TEXT NOT NULL
                CHECK(trigger IN ('created', 'character_published', 'backfill')),
            status TEXT NOT NULL
                CHECK(status IN ('pending', 'applied', 'failed')),
            failure_code TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            applied_at TEXT,
            FOREIGN KEY(connection_id, character_id)
                REFERENCES plum_connections(id, character_id),
            FOREIGN KEY(character_id, from_content_version)
                REFERENCES plum_character_versions(character_id, version_number),
            FOREIGN KEY(character_id, to_content_version)
                REFERENCES plum_character_versions(character_id, version_number),
            CHECK(from_content_version IS NULL OR from_content_version > 0),
            CHECK(from_prompt_version IS NULL OR from_prompt_version > 0),
            CHECK((status='failed') = (failure_code IS NOT NULL)),
            CHECK((status='applied') = (applied_at IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS ix_plum_connection_adoptions_connection
            ON plum_connection_character_adoptions(connection_id, created_at);

        CREATE TABLE IF NOT EXISTS plum_connection_relationship_state (
            connection_id TEXT PRIMARY KEY REFERENCES plum_connections(id),
            state TEXT NOT NULL DEFAULT 'connected'
                CHECK(state IN ('connected', 'paused', 'ended')),
            relationship_level BIGINT NOT NULL DEFAULT 0
                CHECK(relationship_level >= 0),
            relationship_xp BIGINT NOT NULL DEFAULT 0
                CHECK(relationship_xp >= 0),
            completed_turn_count BIGINT NOT NULL DEFAULT 0
                CHECK(completed_turn_count >= 0),
            first_connected_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            last_interacted_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );

        CREATE TABLE IF NOT EXISTS plum_connection_runtime_bindings (
            connection_id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            runtime_account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id),
            applied_content_version BIGINT NOT NULL
                CHECK(applied_content_version > 0),
            applied_prompt_version BIGINT NOT NULL
                CHECK(applied_prompt_version > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'inactive')),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            FOREIGN KEY(connection_id, platform_user_id)
                REFERENCES plum_connections(id, platform_user_id),
            FOREIGN KEY(runtime_account_id, platform_user_id)
                REFERENCES runtime_ownerships(runtime_account_id, platform_user_id)
        );
        CREATE INDEX IF NOT EXISTS ix_plum_connection_runtime_owner
            ON plum_connection_runtime_bindings(platform_user_id, status);
        """
    )
    _ensure_persona_lock_guards(conn)

    # 历史表没有 Persona 引用。只采用当前 active 默认 Persona；缺失时创建
    # 明确的迁移身份，绝不从会话文本推断用户扮演的身份。
    conn.execute(
        """
        WITH legacy_pairs AS (
            SELECT rel.platform_user_id, rel.character_id
            FROM plum_user_character_relationships rel
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connections pc
                WHERE pc.platform_user_id=rel.platform_user_id
                  AND pc.character_id=rel.character_id
            )
            UNION
            SELECT c.platform_user_id, c.character_id
            FROM plum_conversations c
            WHERE c.connection_id IS NULL
            UNION
            SELECT b.platform_user_id, b.character_id
            FROM plum_character_bindings b
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connections pc
                WHERE pc.platform_user_id=b.platform_user_id
                  AND pc.character_id=b.character_id
            )
        ), users_without_default AS (
            SELECT DISTINCT lp.platform_user_id
            FROM legacy_pairs lp
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_user_personas p
                WHERE p.platform_user_id=lp.platform_user_id
                  AND p.status='active' AND p.is_default=1
            )
        )
        INSERT INTO plum_user_personas(
            id, platform_user_id, display_name, description, prompt_text,
            status, is_default, version, updated_at
        )
        SELECT 'fpersona_migration_' || md5(platform_user_id),
               platform_user_id, 'Migrated Persona',
               '由历史 Plum 关系迁移生成；原系统未记录当时使用的 Persona。',
               '使用迁移身份进入故事；不要根据历史聊天反推或补写身份设定。',
               'active', 1, 1,
               to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                       'YYYY-MM-DD HH24:MI:SS')
        FROM users_without_default
        ON CONFLICT(id) DO NOTHING
        """
    )

    conn.execute(
        """
        WITH legacy_pairs AS (
            SELECT rel.platform_user_id, rel.character_id
            FROM plum_user_character_relationships rel
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connections pc
                WHERE pc.platform_user_id=rel.platform_user_id
                  AND pc.character_id=rel.character_id
            )
            UNION
            SELECT c.platform_user_id, c.character_id
            FROM plum_conversations c
            WHERE c.connection_id IS NULL
            UNION
            SELECT b.platform_user_id, b.character_id
            FROM plum_character_bindings b
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connections pc
                WHERE pc.platform_user_id=b.platform_user_id
                  AND pc.character_id=b.character_id
            )
        ), selected_personas AS (
            SELECT DISTINCT ON (p.platform_user_id)
                   p.platform_user_id, p.id AS persona_id
            FROM plum_user_personas p
            JOIN legacy_pairs lp ON lp.platform_user_id=p.platform_user_id
            WHERE p.status='active' AND p.is_default=1
            ORDER BY p.platform_user_id, p.created_at, p.id
        )
        INSERT INTO plum_connections(
            id, platform_user_id, persona_id, character_id,
            current_character_version, status, blocked_reason,
            creation_reason, migration_source, created_at, updated_at
        )
        SELECT 'fconn_backfill_' || md5(
                   lp.platform_user_id || ':' || lp.character_id
               ),
               lp.platform_user_id, sp.persona_id, lp.character_id,
               ch.content_version,
               CASE WHEN ch.status='active' THEN 'active' ELSE 'blocked' END,
               CASE WHEN ch.status='active' THEN NULL
                    ELSE 'character_taken_down' END,
               'backfill', 'm0073_legacy_user_character',
               COALESCE(rel.created_at, conv.created_at,
                        to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                                'YYYY-MM-DD HH24:MI:SS')),
               COALESCE(rel.updated_at, conv.updated_at,
                        to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                                'YYYY-MM-DD HH24:MI:SS'))
        FROM legacy_pairs lp
        JOIN selected_personas sp
          ON sp.platform_user_id=lp.platform_user_id
        JOIN plum_characters ch ON ch.id=lp.character_id
        LEFT JOIN plum_user_character_relationships rel
          ON rel.platform_user_id=lp.platform_user_id
         AND rel.character_id=lp.character_id
        LEFT JOIN LATERAL (
            SELECT created_at, updated_at
            FROM plum_conversations c
            WHERE c.platform_user_id=lp.platform_user_id
              AND c.character_id=lp.character_id
            ORDER BY (c.status='active') DESC, c.updated_at DESC, c.id
            LIMIT 1
        ) conv ON TRUE
        ON CONFLICT(id) DO NOTHING
        """
    )

    conn.execute(
        """
        INSERT INTO plum_connection_relationship_state(
            connection_id, state, relationship_level, relationship_xp,
            completed_turn_count, first_connected_at, last_interacted_at,
            updated_at
        )
        SELECT pc.id,
               CASE WHEN pc.status='blocked' THEN 'paused'
                    WHEN rel.state IN ('connected', 'paused', 'ended')
                        THEN rel.state
                    ELSE 'connected' END,
               COALESCE(rel.relationship_level, 0),
               COALESCE(rel.relationship_xp, 0),
               COALESCE(rel.completed_turn_count, 0),
               COALESCE(rel.first_connected_at, pc.created_at),
               rel.last_interacted_at,
               COALESCE(rel.updated_at, pc.updated_at)
        FROM plum_connections pc
        LEFT JOIN plum_user_character_relationships rel
          ON rel.platform_user_id=pc.platform_user_id
         AND rel.character_id=pc.character_id
        WHERE pc.migration_source='m0073_legacy_user_character'
        ON CONFLICT(connection_id) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO plum_connection_character_adoptions(
            id, connection_id, character_id,
            from_content_version, to_content_version,
            from_prompt_version, to_prompt_version,
            trigger, status, created_at, applied_at
        )
        SELECT 'fadopt_backfill_' || md5(pc.id), pc.id, pc.character_id,
               NULL, pc.current_character_version, NULL, cv.prompt_version,
               'backfill', 'applied', pc.created_at, pc.created_at
        FROM plum_connections pc
        JOIN plum_character_versions cv
          ON cv.character_id=pc.character_id
         AND cv.version_number=pc.current_character_version
        WHERE pc.migration_source='m0073_legacy_user_character'
        ON CONFLICT(id) DO NOTHING
        """
    )

    # 旧 Binding 优先；若缺失则采用该组最新会话的 Runtime。只有 ownership
    # 与 User/App 一致的 Runtime 才允许进入新 Binding。
    binding_candidates = """
        WITH candidates AS (
            SELECT b.platform_user_id, b.character_id, b.runtime_account_id,
                   b.character_prompt_version AS prompt_version,
                   b.updated_at, 0 AS priority
            FROM plum_character_bindings b
            WHERE b.status='active' AND NOT EXISTS (
                SELECT 1 FROM plum_connection_runtime_bindings rb
                WHERE rb.runtime_account_id=b.runtime_account_id
            )
            UNION ALL
            SELECT c.platform_user_id, c.character_id, c.runtime_account_id,
                   ch.prompt_version, c.updated_at, 1 AS priority
            FROM plum_conversations c
            JOIN plum_characters ch ON ch.id=c.character_id
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connection_runtime_bindings rb
                WHERE rb.runtime_account_id=c.runtime_account_id
            )
        ), chosen AS (
            SELECT DISTINCT ON (platform_user_id, character_id)
                   platform_user_id, character_id, runtime_account_id,
                   prompt_version, updated_at
            FROM candidates
            ORDER BY platform_user_id, character_id, priority,
                     (updated_at IS NULL), updated_at DESC, runtime_account_id
        )
    """
    conn.execute(
        binding_candidates
        + """
        UPDATE runtime_ownerships ro
        SET source_type='plum_connection', source_id=pc.id,
            updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                               'YYYY-MM-DD HH24:MI:SS')
        FROM chosen c
        JOIN plum_connections pc
          ON pc.platform_user_id=c.platform_user_id
         AND pc.character_id=c.character_id
         AND pc.migration_source='m0073_legacy_user_character'
        WHERE ro.runtime_account_id=c.runtime_account_id
          AND ro.platform_user_id=c.platform_user_id
          AND ro.app_id='plum'
        """
    )
    conn.execute(
        binding_candidates
        + """
        INSERT INTO runtime_ownerships(
            runtime_account_id, platform_user_id, app_id, owner_kind,
            source_type, source_id, status, updated_at
        )
        SELECT c.runtime_account_id, c.platform_user_id, 'plum', 'user',
               'plum_connection', pc.id, 'active',
               to_char((now() AT TIME ZONE 'Asia/Shanghai'),
                       'YYYY-MM-DD HH24:MI:SS')
        FROM chosen c
        JOIN plum_connections pc
          ON pc.platform_user_id=c.platform_user_id
         AND pc.character_id=c.character_id
         AND pc.migration_source='m0073_legacy_user_character'
        JOIN accounts a ON a.id=c.runtime_account_id AND a.app_id='plum'
        WHERE NOT EXISTS (
            SELECT 1 FROM runtime_ownerships ro
            WHERE ro.runtime_account_id=c.runtime_account_id
        )
        ON CONFLICT(runtime_account_id) DO NOTHING
        """
    )
    conn.execute(
        binding_candidates
        + """
        INSERT INTO plum_connection_runtime_bindings(
            connection_id, platform_user_id, runtime_account_id,
            applied_content_version, applied_prompt_version,
            status, created_at, updated_at
        )
        SELECT pc.id, pc.platform_user_id, c.runtime_account_id,
               pc.current_character_version, c.prompt_version,
               CASE WHEN pc.status='active' THEN 'active' ELSE 'inactive' END,
               pc.created_at, COALESCE(c.updated_at, pc.updated_at)
        FROM chosen c
        JOIN plum_connections pc
          ON pc.platform_user_id=c.platform_user_id
         AND pc.character_id=c.character_id
         AND pc.migration_source='m0073_legacy_user_character'
        JOIN runtime_ownerships ro
          ON ro.runtime_account_id=c.runtime_account_id
         AND ro.platform_user_id=c.platform_user_id
         AND ro.app_id='plum' AND ro.status='active'
        ON CONFLICT(connection_id) DO NOTHING
        """
    )

    conn.execute(
        """
        UPDATE plum_conversations c
        SET connection_id='fconn_backfill_' || md5(
                c.platform_user_id || ':' || c.character_id
            )
        WHERE c.connection_id IS NULL
        """
    )

    invalid = conn.execute(
        """
        WITH legacy_pairs AS (
            SELECT rel.platform_user_id, rel.character_id
            FROM plum_user_character_relationships rel
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connections existing
                WHERE existing.platform_user_id=rel.platform_user_id
                  AND existing.character_id=rel.character_id
            )
            UNION
            SELECT c.platform_user_id, c.character_id
            FROM plum_conversations c
            WHERE c.connection_id IS NULL
            UNION
            SELECT b.platform_user_id, b.character_id
            FROM plum_character_bindings b
            WHERE NOT EXISTS (
                SELECT 1 FROM plum_connections existing
                WHERE existing.platform_user_id=b.platform_user_id
                  AND existing.character_id=b.character_id
            )
        )
        SELECT
            COUNT(*) FILTER (WHERE pu.id IS NULL OR ch.id IS NULL) AS bad_pair,
            COUNT(*) FILTER (WHERE pc.id IS NULL) AS missing_connection,
            (SELECT COUNT(*) FROM plum_conversations
             WHERE connection_id IS NULL) AS missing_conversation,
            (SELECT COUNT(*)
             FROM plum_connections x
             JOIN plum_user_personas p ON p.id=x.persona_id
             WHERE p.locked_at IS NULL) AS unlocked_persona,
            (SELECT COUNT(*)
             FROM plum_connection_runtime_bindings rb
             LEFT JOIN runtime_ownerships ro
               ON ro.runtime_account_id=rb.runtime_account_id
              AND ro.platform_user_id=rb.platform_user_id
              AND ro.app_id='plum'
             WHERE ro.runtime_account_id IS NULL) AS bad_runtime_owner,
            (SELECT COUNT(*)
             FROM plum_conversations c
             LEFT JOIN plum_connection_runtime_bindings rb
               ON rb.connection_id=c.connection_id
              AND rb.runtime_account_id=c.runtime_account_id
             WHERE c.status='active' AND rb.connection_id IS NULL
            ) AS unbound_active_conversation,
            (SELECT COUNT(*)
             FROM plum_character_bindings b
             JOIN plum_connections pc
               ON pc.platform_user_id=b.platform_user_id
              AND pc.character_id=b.character_id
              AND pc.migration_source='m0073_legacy_user_character'
             LEFT JOIN runtime_ownerships ro
               ON ro.runtime_account_id=b.runtime_account_id
              AND ro.platform_user_id=b.platform_user_id
              AND ro.app_id='plum'
             WHERE b.status='active' AND ro.runtime_account_id IS NULL
            ) AS bad_legacy_binding_owner
        FROM legacy_pairs lp
        LEFT JOIN platform_users pu ON pu.id=lp.platform_user_id
        LEFT JOIN plum_characters ch ON ch.id=lp.character_id
        LEFT JOIN plum_connections pc
          ON pc.id='fconn_backfill_' || md5(
              lp.platform_user_id || ':' || lp.character_id
          )
        """
    ).fetchone()
    counts = {key: int(invalid[key] or 0) for key in invalid.keys()}
    if any(counts.values()):
        raise RuntimeError(f"m0073 Plum Connection backfill failed: {counts}")

    _ensure_not_null(conn, "plum_conversations", "connection_id")
    _ensure_constraint(
        conn,
        "plum_conversations",
        "fk_plum_conversations_connection_owner_character",
        "FOREIGN KEY(connection_id, platform_user_id, character_id) "
        "REFERENCES plum_connections(id, platform_user_id, character_id)",
    )
    conn.executescript(
        """
        DROP INDEX IF EXISTS ux_fibre_conversations_active;
        DROP INDEX IF EXISTS ux_plum_conversations_active;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_conversations_active_connection
            ON plum_conversations(connection_id) WHERE status='active';
        CREATE INDEX IF NOT EXISTS ix_plum_conversations_connection_status
            ON plum_conversations(connection_id, status, updated_at);
        """
    )


def _migration_0074_plum_storyline_foundation(conn: Connection) -> None:
    """建立 Connection 下的 Storyline/State，并回填 Conversation 归属。"""

    _ensure_column(conn, "plum_conversations", "storyline_id", "TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_storylines (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            ordinal BIGINT NOT NULL CHECK(ordinal > 0),
            opening_character_version BIGINT NOT NULL
                CHECK(opening_character_version > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'archived')),
            migration_source TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            archived_at TEXT,
            UNIQUE(id, platform_user_id),
            UNIQUE(id, connection_id),
            UNIQUE(id, platform_user_id, connection_id, character_id),
            UNIQUE(connection_id, ordinal),
            FOREIGN KEY(connection_id, platform_user_id, character_id)
                REFERENCES plum_connections(id, platform_user_id, character_id),
            FOREIGN KEY(character_id, opening_character_version)
                REFERENCES plum_character_versions(character_id, version_number),
            CHECK((status='archived') = (archived_at IS NOT NULL)),
            CHECK(migration_source IS NULL OR BTRIM(migration_source) <> '')
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_plum_storylines_active_connection
            ON plum_storylines(connection_id) WHERE status='active';
        CREATE INDEX IF NOT EXISTS ix_plum_storylines_owner_status
            ON plum_storylines(platform_user_id, status, updated_at);

        CREATE TABLE IF NOT EXISTS plum_storyline_state (
            storyline_id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            current_chapter_no BIGINT NOT NULL DEFAULT 1
                CHECK(current_chapter_no >= 1),
            state_schema_version BIGINT NOT NULL DEFAULT 1
                CHECK(state_schema_version > 0),
            plot_state_json JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK(jsonb_typeof(plot_state_json)='object'),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            FOREIGN KEY(storyline_id, platform_user_id)
                REFERENCES plum_storylines(id, platform_user_id),
            FOREIGN KEY(storyline_id, connection_id)
                REFERENCES plum_storylines(id, connection_id)
        );
        CREATE INDEX IF NOT EXISTS ix_plum_storyline_state_owner
            ON plum_storyline_state(platform_user_id, connection_id);
        """
    )

    # 旧模型只有 Conversation.current_chapter_no。一个既有 Connection 只回填
    # 一条初始 Storyline，不根据会话文本或归档次数猜测历史剧情线边界。
    conn.execute(
        """
        INSERT INTO plum_storylines(
            id, platform_user_id, connection_id, character_id, ordinal,
            opening_character_version, status, migration_source,
            created_at, updated_at, archived_at
        )
        SELECT 'fstory_backfill_' || md5(pc.id),
               pc.platform_user_id, pc.id, pc.character_id, 1,
               pc.current_character_version,
               CASE WHEN pc.status='archived' THEN 'archived' ELSE 'active' END,
               'm0074_connection_backfill', pc.created_at, pc.updated_at,
               CASE WHEN pc.status='archived'
                    THEN COALESCE(pc.archived_at, pc.updated_at)
                    ELSE NULL END
        FROM plum_connections pc
        WHERE NOT EXISTS (
            SELECT 1 FROM plum_storylines ps
            WHERE ps.connection_id=pc.id
        )
        ON CONFLICT(id) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO plum_storyline_state(
            storyline_id, platform_user_id, connection_id,
            current_chapter_no, state_schema_version,
            plot_state_json, updated_at
        )
        SELECT ps.id, ps.platform_user_id, ps.connection_id,
               GREATEST(COALESCE(MAX(c.current_chapter_no), 1), 1),
               1, '{}'::jsonb, ps.updated_at
        FROM plum_storylines ps
        LEFT JOIN plum_conversations c ON c.connection_id=ps.connection_id
        WHERE ps.migration_source='m0074_connection_backfill'
        GROUP BY ps.id, ps.platform_user_id, ps.connection_id, ps.updated_at
        ON CONFLICT(storyline_id) DO NOTHING
        """
    )
    conn.execute(
        """
        UPDATE plum_conversations c
        SET storyline_id='fstory_backfill_' || md5(c.connection_id)
        WHERE c.storyline_id IS NULL
        """
    )

    invalid = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM plum_connections pc
             LEFT JOIN plum_storylines ps ON ps.connection_id=pc.id
             WHERE ps.id IS NULL) AS connection_without_storyline,
            (SELECT COUNT(*) FROM plum_storylines ps
             LEFT JOIN plum_storyline_state st ON st.storyline_id=ps.id
             WHERE st.storyline_id IS NULL) AS storyline_without_state,
            (SELECT COUNT(*) FROM plum_conversations
             WHERE storyline_id IS NULL) AS conversation_without_storyline,
            (SELECT COUNT(*)
             FROM plum_conversations c
             JOIN plum_storylines ps ON ps.id=c.storyline_id
             WHERE ps.connection_id<>c.connection_id
                OR ps.platform_user_id<>c.platform_user_id
                OR ps.character_id<>c.character_id
            ) AS conversation_storyline_mismatch
        """
    ).fetchone()
    counts = {key: int(invalid[key] or 0) for key in invalid.keys()}
    if any(counts.values()):
        raise RuntimeError(f"m0074 Plum Storyline backfill failed: {counts}")

    _ensure_not_null(conn, "plum_conversations", "storyline_id")
    _ensure_constraint(
        conn,
        "plum_conversations",
        "fk_plum_conversations_storyline_scope",
        "FOREIGN KEY(storyline_id, platform_user_id, connection_id, character_id) "
        "REFERENCES plum_storylines(id, platform_user_id, connection_id, character_id)",
    )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_plum_conversations_storyline_status
            ON plum_conversations(storyline_id, status, updated_at);
        """
    )


def _migration_0075_plum_system_work_ownership(conn: Connection) -> None:
    """Separate platform-owned Works from human ``platform_users``."""

    _ensure_column(conn, "plum_works", "owner_kind", "TEXT")
    conn.execute(
        """
        UPDATE plum_works
        SET owner_kind=CASE
                WHEN owner_platform_user_id='pusr_plum_system'
                    THEN 'system'
                ELSE 'platform_user'
            END
        WHERE owner_kind IS NULL
        """
    )
    # System-owned catalog content has no human rights subject. Keep the owner
    # discriminator explicit instead of manufacturing a platform_users row.
    conn.execute(
        "ALTER TABLE plum_works ALTER COLUMN owner_platform_user_id DROP NOT NULL"
    )
    conn.execute(
        """
        UPDATE plum_works
        SET owner_platform_user_id=NULL
        WHERE owner_kind='system'
          AND owner_platform_user_id='pusr_plum_system'
        """
    )
    _ensure_default(conn, "plum_works", "owner_kind", "'platform_user'")
    _ensure_not_null(conn, "plum_works", "owner_kind")
    _ensure_constraint(
        conn,
        "plum_works",
        "ck_plum_works_owner_scope",
        "CHECK((owner_kind='platform_user' "
        "AND owner_platform_user_id IS NOT NULL) "
        "OR (owner_kind='system' AND owner_platform_user_id IS NULL))",
    )
    conn.execute(
        """
        DELETE FROM platform_users
        WHERE id='pusr_plum_system'
          AND NOT EXISTS (
              SELECT 1 FROM plum_works
              WHERE owner_platform_user_id='pusr_plum_system'
          )
        """
    )


def _migration_0076_plum_character_create_idempotency(conn: Connection) -> None:
    """Persist successful user Character creates under an owner-scoped key."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_character_create_requests (
            platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            idempotency_key TEXT NOT NULL CHECK(BTRIM(idempotency_key) <> ''),
            request_hash TEXT NOT NULL CHECK(LENGTH(request_hash)=64),
            work_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, idempotency_key),
            UNIQUE(work_id),
            UNIQUE(character_id),
            FOREIGN KEY(work_id, platform_user_id)
                REFERENCES plum_works(id, owner_platform_user_id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(character_id)
                REFERENCES plum_characters(id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX IF NOT EXISTS ix_plum_character_create_requests_owner_created
            ON plum_character_create_requests(platform_user_id, created_at);
        """
    )


def _migration_0077_plum_guest_identity_foundation(conn: Connection) -> None:
    """Add provisional Plum subjects, Guest sessions, profiles and quota state."""

    conn.execute("ALTER TABLE platform_users ALTER COLUMN phone DROP NOT NULL")
    _ensure_column(
        conn,
        "platform_users",
        "subject_kind",
        "TEXT NOT NULL DEFAULT 'member'",
    )
    _ensure_column(conn, "platform_users", "merged_into_platform_user_id", "TEXT")
    _ensure_column(conn, "platform_users", "merged_at", "TEXT")
    _ensure_constraint(
        conn,
        "platform_users",
        "ck_platform_users_subject_kind",
        "CHECK(subject_kind IN ('guest', 'member', 'merged'))",
    )
    _ensure_constraint(
        conn,
        "platform_users",
        "ck_platform_users_merge_target",
        "CHECK((subject_kind='merged') = (merged_into_platform_user_id IS NOT NULL))",
    )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_guest_sessions (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE CHECK(LENGTH(token_hash)=64),
            platform_user_id TEXT NOT NULL UNIQUE REFERENCES platform_users(id),
            csrf_hash TEXT NOT NULL CHECK(LENGTH(csrf_hash)=64),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'promoted', 'revoked', 'expired')),
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_ip_hash TEXT,
            user_agent_hash TEXT,
            promoted_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            CHECK((status='promoted') = (promoted_at IS NOT NULL)),
            CHECK((status='revoked') = (revoked_at IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS ix_plum_guest_sessions_expiry
            ON plum_guest_sessions(status, expires_at);

        CREATE TABLE IF NOT EXISTS plum_guest_profiles (
            platform_user_id TEXT PRIMARY KEY REFERENCES platform_users(id),
            adult_confirmed_at TEXT NOT NULL,
            pronouns TEXT NOT NULL CHECK(BTRIM(pronouns) <> ''),
            relationship_preference TEXT,
            genres_json JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK(jsonb_typeof(genres_json)='array'),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );

        CREATE TABLE IF NOT EXISTS plum_guest_usage (
            platform_user_id TEXT PRIMARY KEY REFERENCES platform_users(id),
            typed_accepted BIGINT NOT NULL DEFAULT 0 CHECK(typed_accepted >= 0),
            continue_accepted BIGINT NOT NULL DEFAULT 0 CHECK(continue_accepted >= 0),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        );

        CREATE TABLE IF NOT EXISTS plum_guest_character_usage (
            platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            character_id TEXT NOT NULL REFERENCES plum_characters(id),
            continue_accepted BIGINT NOT NULL DEFAULT 0 CHECK(continue_accepted >= 0),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, character_id)
        );

        CREATE TABLE IF NOT EXISTS plum_guest_action_receipts (
            platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            client_action_id TEXT NOT NULL CHECK(BTRIM(client_action_id) <> ''),
            action_kind TEXT NOT NULL CHECK(action_kind IN ('message', 'continue')),
            conversation_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected', 'completed', 'failed')),
            response_json JSONB,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, client_action_id)
        );
        CREATE INDEX IF NOT EXISTS ix_plum_guest_action_receipts_conversation
            ON plum_guest_action_receipts(conversation_id, created_at)
            WHERE conversation_id IS NOT NULL;
        """
    )


def _migration_0078_plum_external_identity_challenges(conn: Connection) -> None:
    """External identities and one-time login challenges for formal Plum auth."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS platform_external_identities (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            provider TEXT NOT NULL CHECK(provider IN ('email', 'google', 'apple')),
            provider_subject TEXT NOT NULL CHECK(BTRIM(provider_subject) <> ''),
            normalized_email TEXT,
            email_verified BIGINT NOT NULL DEFAULT 0 CHECK(email_verified IN (0, 1)),
            profile_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(profile_json)='object'),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled')),
            last_authenticated_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            UNIQUE(provider, provider_subject)
        );
        CREATE INDEX IF NOT EXISTS ix_platform_external_identities_owner
            ON platform_external_identities(platform_user_id, status);
        CREATE INDEX IF NOT EXISTS ix_platform_external_identities_email
            ON platform_external_identities(normalized_email, status)
            WHERE normalized_email IS NOT NULL;

        CREATE TABLE IF NOT EXISTS plum_identity_challenges (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('email_otp', 'oauth_state')),
            provider TEXT NOT NULL CHECK(provider IN ('email', 'google', 'apple')),
            target_hash TEXT,
            secret_hash TEXT NOT NULL CHECK(LENGTH(secret_hash)=64),
            guest_platform_user_id TEXT REFERENCES platform_users(id),
            attempt_count BIGINT NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            max_attempts BIGINT NOT NULL CHECK(max_attempts > 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'consumed', 'expired', 'failed')),
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(metadata_json)='object'),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            CHECK((status='consumed') = (consumed_at IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS ix_plum_identity_challenges_target
            ON plum_identity_challenges(provider, target_hash, status, expires_at);
        CREATE INDEX IF NOT EXISTS ix_plum_identity_challenges_guest
            ON plum_identity_challenges(guest_platform_user_id, status, created_at)
            WHERE guest_platform_user_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS plum_identity_merge_runs (
            id TEXT PRIMARY KEY,
            guest_platform_user_id TEXT NOT NULL UNIQUE REFERENCES platform_users(id),
            target_platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            identity_id TEXT NOT NULL REFERENCES platform_external_identities(id),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed', 'failed')),
            result_json JSONB,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            CHECK((status='completed') = (completed_at IS NOT NULL))
        );
        """
    )


def _migration_0079_plum_identity_merge_constraints(conn: Connection) -> None:
    """Allow one transaction to transfer an entire Guest aggregate owner."""

    constraints = (
        ("plum_connections", "FOREIGN KEY (persona_id, platform_user_id)"),
        (
            "plum_connection_runtime_bindings",
            "FOREIGN KEY (connection_id, platform_user_id)",
        ),
        (
            "plum_connection_runtime_bindings",
            "FOREIGN KEY (runtime_account_id, platform_user_id)",
        ),
        (
            "plum_storylines",
            "FOREIGN KEY (connection_id, platform_user_id, character_id)",
        ),
        (
            "plum_storyline_state",
            "FOREIGN KEY (storyline_id, platform_user_id)",
        ),
        (
            "plum_conversations",
            "FOREIGN KEY (connection_id, platform_user_id, character_id)",
        ),
        (
            "plum_conversations",
            "FOREIGN KEY (storyline_id, platform_user_id, connection_id, character_id)",
        ),
    )
    for table, definition_prefix in constraints:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise RuntimeError("unsafe Plum constraint identifier")
        row = conn.execute(
            """
            SELECT conname, condeferrable FROM pg_constraint
            WHERE conrelid=to_regclass(?) AND contype='f'
              AND pg_get_constraintdef(oid) LIKE ?
            """,
            (table, f"{definition_prefix}%"),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"missing Plum merge constraint: {table}.{definition_prefix}"
            )
        if not bool(row["condeferrable"]):
            name = str(row["conname"])
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                raise RuntimeError("unsafe Plum constraint identifier")
            conn.execute(
                f"ALTER TABLE {table} ALTER CONSTRAINT {name} "
                "DEFERRABLE INITIALLY DEFERRED"
            )


def _migration_0080_plum_creation_drafts(conn: Connection) -> None:
    """Persist owner-scoped Creation drafts separately from published versions."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plum_creation_drafts (
            work_id TEXT PRIMARY KEY,
            owner_platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            revision BIGINT NOT NULL DEFAULT 1 CHECK(revision > 0),
            content_json TEXT NOT NULL,
            portrait_media_id TEXT,
            lifecycle_status TEXT NOT NULL DEFAULT 'active'
                CHECK(lifecycle_status IN ('active', 'archived')),
            moderation_status TEXT NOT NULL DEFAULT 'not_submitted'
                CHECK(moderation_status IN ('not_submitted', 'pending_review', 'approved', 'rejected')),
            moderation_categories_json TEXT NOT NULL DEFAULT '[]',
            moderation_provider_reference TEXT,
            published_character_id TEXT,
            submitted_at TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            UNIQUE(work_id, owner_platform_user_id),
            FOREIGN KEY(portrait_media_id, owner_platform_user_id)
                REFERENCES media_assets(id, owner_platform_user_id)
        );
        CREATE INDEX IF NOT EXISTS ix_plum_creation_drafts_owner_updated
            ON plum_creation_drafts(owner_platform_user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS ix_plum_creation_drafts_owner_moderation
            ON plum_creation_drafts(owner_platform_user_id, moderation_status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS plum_character_version_publish_requests (
            platform_user_id TEXT NOT NULL REFERENCES platform_users(id),
            idempotency_key TEXT NOT NULL CHECK(BTRIM(idempotency_key) <> ''),
            request_hash TEXT NOT NULL CHECK(LENGTH(request_hash)=64),
            work_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            version_number BIGINT NOT NULL CHECK(version_number > 1),
            created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY(platform_user_id, idempotency_key),
            UNIQUE(character_id, version_number),
            FOREIGN KEY(work_id, platform_user_id)
                REFERENCES plum_works(id, owner_platform_user_id),
            FOREIGN KEY(character_id, version_number)
                REFERENCES plum_character_versions(character_id, version_number)
                DEFERRABLE INITIALLY DEFERRED
        );
        """
    )


def _migration_0081_plum_avatar_zoom(conn: Connection) -> None:
    """Persist the creator-selected avatar crop zoom on every published version."""

    definition = "INTEGER NOT NULL DEFAULT 100 CHECK(avatar_zoom BETWEEN 100 AND 200)"
    _ensure_column(conn, "plum_characters", "avatar_zoom", definition)
    _ensure_column(conn, "plum_character_versions", "avatar_zoom", definition)


def _migration_0082_plum_portrait_zoom(conn: Connection) -> None:
    """Persist the creator-selected full portrait crop zoom."""

    definition = "INTEGER NOT NULL DEFAULT 100 CHECK(portrait_zoom BETWEEN 100 AND 200)"
    _ensure_column(conn, "plum_characters", "portrait_zoom", definition)
    _ensure_column(conn, "plum_character_versions", "portrait_zoom", definition)


__all__ = [
    "_migration_0064_fibre_mvp",
    "_migration_0065_fibre_character_experience",
    "_migration_0066_fibre_public_test_auth",
    "_migration_0067_plum_product_rename",
    "_migration_0072_plum_character_content_foundation",
    "_migration_0073_plum_connection_foundation",
    "_migration_0074_plum_storyline_foundation",
    "_migration_0075_plum_system_work_ownership",
    "_migration_0076_plum_character_create_idempotency",
    "_migration_0077_plum_guest_identity_foundation",
    "_migration_0078_plum_external_identity_challenges",
    "_migration_0079_plum_identity_merge_constraints",
    "_migration_0080_plum_creation_drafts",
    "_migration_0081_plum_avatar_zoom",
    "_migration_0082_plum_portrait_zoom",
]
