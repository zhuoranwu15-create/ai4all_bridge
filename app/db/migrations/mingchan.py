"""鸣蝉（mingchan）产品相关的 schema 迁移。

函数体自 ``app/db/_core.py`` 原样搬出；顺序仍由 ``_core._MIGRATIONS`` 决定。
"""
from __future__ import annotations

import re

from app.db._backend import Connection
from app.db._schema_utils import _ensure_column


def _migration_0028_companion_world_core(conn: Connection) -> None:
    """M2-A：朝夕相伴 P1 多居民核心四表（universe/template/resident/conversation）。

    见 companion_world_p1_backend_spec.md §2.1–2.4。一真人一 home world（universes
    UNIQUE(owner_platform_user_id) 幂等 bootstrap 依赖）；模板≠runtime account（同模板进两
    世界=两 resident 两 account）；容量真相 = universe_residents.status='active' 计数（D-07，
    world row lock 下校验 active≤10），偏唯一索引保证一 runtime account 至多一 resident；
    ai_conversations 提供跨 session 稳定会话 ID（≠数值 session.id），owner 校验锚防越权。

    空表迁移、无回填、幂等（IF NOT EXISTS）。本刀不接线，无 live 读写。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS universes (
            id TEXT PRIMARY KEY,                          -- 内部世界 ID，非可分享公开码
            owner_platform_user_id TEXT NOT NULL UNIQUE,  -- 一真人一 home world（幂等 bootstrap 依赖）
            legacy_primary_account_id TEXT,               -- 老用户迁移/计费锚点（D-08 legacy 映射）
            status TEXT NOT NULL DEFAULT 'active',         -- active | disabled
            onboarding_state TEXT NOT NULL DEFAULT 'preparing',  -- preparing | selecting | confirmed
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id)
        );

        CREATE TABLE IF NOT EXISTS character_templates (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,                    -- official | operations | user_created | generated
            owner_platform_user_id TEXT,                  -- 自建时非空；官方/运营为空
            name TEXT NOT NULL,
            avatar_ref TEXT,
            summary TEXT,
            tags_json TEXT,
            persona_seed_json TEXT,                        -- 实例化时写入 runtime account 的 SOUL/IDENTITY 种子；不经 App DTO 下发
            persona_version TEXT NOT NULL DEFAULT 'v1',    -- 版本；运营更新不静默改写既有关系
            status TEXT NOT NULL DEFAULT 'active',          -- active | retired
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_character_templates_source ON character_templates(source_type, status);
        CREATE INDEX IF NOT EXISTS ix_character_templates_owner ON character_templates(owner_platform_user_id);

        CREATE TABLE IF NOT EXISTS universe_residents (
            id TEXT PRIMARY KEY,
            universe_id TEXT NOT NULL,
            character_template_id TEXT NOT NULL,
            template_version TEXT NOT NULL,               -- 确认时刻钉住的模板版本
            runtime_account_id TEXT,                       -- 激活后指向 account；candidate 期为空
            origin TEXT NOT NULL,                          -- preset | custom | mailbox | legacy
            status TEXT NOT NULL DEFAULT 'candidate',       -- candidate | active | offline | dismissed
            joined_at TEXT,
            offline_at TEXT,
            departure_event_id TEXT,                        -- M4 用，P1 留列
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(runtime_account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS ix_universe_residents_universe_status ON universe_residents(universe_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_residents_runtime
            ON universe_residents(runtime_account_id) WHERE runtime_account_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS ai_conversations (
            id TEXT PRIMARY KEY,                           -- 客户端长期稳定会话 ID（≠ 数值 session.id）
            universe_id TEXT NOT NULL,
            resident_id TEXT NOT NULL,
            owner_platform_user_id TEXT NOT NULL,          -- owner 校验锚（防越权）
            runtime_account_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',            -- active | read_only（resident offline 后原子切换）
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(resident_id) REFERENCES universe_residents(id),
            FOREIGN KEY(runtime_account_id) REFERENCES accounts(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_conversations_resident ON ai_conversations(resident_id);
        CREATE INDEX IF NOT EXISTS ix_ai_conversations_owner_state ON ai_conversations(owner_platform_user_id, state);
        """
    )


def _migration_0029_universe_memory_l3(conn: Connection) -> None:
    """M2-A：L3 共享沉淀记忆承载表 universe_memory_facts（append-only typed fact）。

    见 companion_world_p1_backend_spec.md §2.5 + ADR §6.4/D-05/D-06。L3 锚 universe_id（非
    account）；各 resident 只 INSERT 带 provenance 的结构化事实行、永不就地改写，天然规避
    last-writer-wins；compact 由单 writer 打 status='superseded'+superseded_by。payload_json
    是结构化事实体、非逐字原文（D-05 不共享聊天原文）。

    空表迁移、无回填、幂等（IF NOT EXISTS）。本刀只落存储，读注入/sink 路由属 M2-B。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS universe_memory_facts (
            id TEXT PRIMARY KEY,
            universe_id TEXT NOT NULL,                     -- L3 锚点（D-06），非 account
            fact_type TEXT NOT NULL,                       -- §1 L3 类枚举
            payload_json TEXT NOT NULL,                    -- 结构化事实体（非逐字原文，D-05）
            source_account_id TEXT,                        -- 来源 resident 的 runtime account
            source_resident_id TEXT,
            source_message_id TEXT,
            occurred_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',           -- active | superseded
            superseded_by TEXT,                              -- compact 合并后新行的 id
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(universe_id) REFERENCES universes(id)
        );
        CREATE INDEX IF NOT EXISTS ix_universe_memory_facts_read
            ON universe_memory_facts(universe_id, fact_type, status);
        CREATE INDEX IF NOT EXISTS ix_universe_memory_facts_universe_time
            ON universe_memory_facts(universe_id, created_at);
        """
    )


def _migration_0030_companion_world_candidates(conn: Connection) -> None:
    """M2-C：冻结初始候选目录顺序与同世界模板关系唯一性。

    ``initial_candidate_rank`` 只约束 active 模板的非空 rank，允许 retired 历史版本保留
    原 rank；同一世界的非 legacy resident 不得重复引用同一模板。legacy backfill 的多个
    既有 account 共用一条哨兵模板，因此明确从后一个偏唯一索引豁免。
    """
    _ensure_column(conn, "character_templates", "initial_candidate_rank", "INTEGER")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_character_templates_active_initial_rank
            ON character_templates(initial_candidate_rank)
            WHERE status = 'active' AND initial_candidate_rank IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_residents_template_nonlegacy
            ON universe_residents(universe_id, character_template_id)
            WHERE origin <> 'legacy';
        """
    )


def _migration_0033_companion_world_m3_content(conn: Connection) -> None:
    """M3：文字 Feed/outbox 与 App 拉取式通知的加性数据基座。

    Feed 归属锚为 universe/platform user，不复用 account-scoped moderation；M3 默认直接
    发布，审核策略后续单独设计。通知按 platform user 隔离，reserved 行同时承载真人级
    App-only 触达的并发 claim。三表均为空表迁移，不回填、不修改既有 Runtime 行为。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS universe_posts (
            id TEXT PRIMARY KEY,
            universe_id TEXT NOT NULL,
            author_type TEXT NOT NULL,                 -- human | resident
            author_platform_user_id TEXT,
            author_resident_id TEXT,
            source_type TEXT NOT NULL,                 -- user_post | ai_feed
            content_type TEXT NOT NULL DEFAULT 'text',
            text TEXT,
            status TEXT NOT NULL,                      -- generating | published | skipped | deleted
            client_request_id TEXT,
            request_fingerprint TEXT,
            ai_local_date TEXT,
            ai_slot TEXT,                              -- morning | evening
            slot_window_end_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT,
            claim_token TEXT,
            next_attempt_at TEXT,
            terminal_reason TEXT,
            published_at TEXT,
            deleted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(author_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(author_resident_id) REFERENCES universe_residents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_user_request
            ON universe_posts(universe_id, author_platform_user_id, client_request_id)
            WHERE source_type = 'user_post';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_ai_slot
            ON universe_posts(universe_id, ai_local_date, ai_slot)
            WHERE source_type = 'ai_feed';
        CREATE INDEX IF NOT EXISTS ix_universe_posts_feed
            ON universe_posts(universe_id, status, published_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_universe_posts_ai_claim
            ON universe_posts(source_type, status, next_attempt_at, claimed_at);

        CREATE TABLE IF NOT EXISTS companion_world_outbox (
            id TEXT PRIMARY KEY,
            universe_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',     -- pending | processing | delivered | dead
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            claimed_at TEXT,
            claim_token TEXT,
            last_error TEXT,
            delivered_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(post_id) REFERENCES universe_posts(id)
        );
        CREATE INDEX IF NOT EXISTS ix_companion_world_outbox_claim
            ON companion_world_outbox(status, available_at, claimed_at, id);
        CREATE INDEX IF NOT EXISTS ix_companion_world_outbox_post
            ON companion_world_outbox(post_id, event_type);

        CREATE TABLE IF NOT EXISTS app_notifications (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            universe_id TEXT NOT NULL,
            resident_id TEXT,
            scope TEXT NOT NULL,                         -- resident | human
            category TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            delivery_status TEXT NOT NULL,               -- reserved | visible | cancelled
            title TEXT,
            body_text TEXT,
            target_type TEXT NOT NULL DEFAULT 'none',
            target_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            claim_token TEXT,
            claim_expires_at TEXT,
            delivered_at TEXT,
            read_at TEXT,
            expires_at TEXT,
            cancelled_at TEXT,
            terminal_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(resident_id) REFERENCES universe_residents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_app_notifications_user_idempotency
            ON app_notifications(platform_user_id, idempotency_key);
        CREATE INDEX IF NOT EXISTS ix_app_notifications_list
            ON app_notifications(platform_user_id, delivery_status, delivered_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_app_notifications_unread
            ON app_notifications(platform_user_id, delivery_status, read_at, expires_at);
        CREATE INDEX IF NOT EXISTS ix_app_notifications_human_window
            ON app_notifications(platform_user_id, scope, delivery_status, delivered_at, claim_expires_at);
        CREATE INDEX IF NOT EXISTS ix_app_notifications_cleanup
            ON app_notifications(delivery_status, expires_at, claim_expires_at, id);
        """
    )


def _migration_0034_companion_world_lifecycle_mailbox(conn: Connection) -> None:
    """M4：resident lifecycle 审计、唯一 farewell 与私密 mailbox 数据基座。

    本迁移只增加表、列和索引，不接入 scheduler/API，也不改变既有 resident、conversation
    或 Feed 行为。历史 M3 post 统一以 ``post_type='normal'`` 兼容读取。
    """
    _ensure_column(
        conn,
        "universe_posts",
        "post_type",
        "TEXT NOT NULL DEFAULT 'normal'",
    )
    _ensure_column(conn, "universe_posts", "departure_event_id", "TEXT")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_departure_event
            ON universe_posts(departure_event_id)
            WHERE departure_event_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS resident_lifecycle_events (
            id TEXT PRIMARY KEY,
            owner_platform_user_id TEXT NOT NULL,
            universe_id TEXT NOT NULL,
            resident_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            evidence_window_start TEXT NOT NULL,
            evidence_window_end TEXT NOT NULL,
            evidence_count INTEGER NOT NULL,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            cooldown_until TEXT,
            crisis_freeze_until TEXT,
            last_resident_exception_requested INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,
            farewell_text TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT,
            terminal_reason TEXT,
            committed_at TEXT,
            farewell_post_id TEXT,
            correction_status TEXT NOT NULL DEFAULT 'none',
            corrected_by TEXT,
            corrected_at TEXT,
            correction_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(resident_id) REFERENCES universe_residents(id),
            FOREIGN KEY(farewell_post_id) REFERENCES universe_posts(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_resident_open
            ON resident_lifecycle_events(resident_id)
            WHERE status IN ('cooling_down', 'review_pending');
        CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_resident_committed
            ON resident_lifecycle_events(resident_id)
            WHERE status = 'committed';
        CREATE INDEX IF NOT EXISTS ix_lifecycle_review_queue
            ON resident_lifecycle_events(status, cooldown_until, created_at, id);
        CREATE INDEX IF NOT EXISTS ix_lifecycle_owner
            ON resident_lifecycle_events(owner_platform_user_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_universe_residents_lifecycle_scan
            ON universe_residents(status, id);
        CREATE INDEX IF NOT EXISTS ix_messages_account_inbound_created
            ON messages(account_id, direction, role, created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS resident_lifecycle_event_actions (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(event_id) REFERENCES resident_lifecycle_events(id)
        );
        CREATE INDEX IF NOT EXISTS ix_lifecycle_actions_event
            ON resident_lifecycle_event_actions(event_id, created_at, id);

        CREATE TABLE IF NOT EXISTS character_letter_catalog (
            id TEXT PRIMARY KEY,
            character_key TEXT NOT NULL,
            character_template_id TEXT NOT NULL,
            template_version TEXT NOT NULL,
            letter_body TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            available_from TEXT,
            available_until TEXT,
            created_by TEXT NOT NULL,
            retired_by TEXT,
            retired_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(character_template_id) REFERENCES character_templates(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_letter_catalog_character_version
            ON character_letter_catalog(character_key, template_version);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_letter_catalog_active_character
            ON character_letter_catalog(character_key)
            WHERE status = 'active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_letter_catalog_template
            ON character_letter_catalog(character_template_id);
        CREATE INDEX IF NOT EXISTS ix_letter_catalog_selection
            ON character_letter_catalog(status, priority DESC, id);

        CREATE TABLE IF NOT EXISTS character_letters (
            id TEXT PRIMARY KEY,
            owner_platform_user_id TEXT NOT NULL,
            universe_id TEXT NOT NULL,
            catalog_id TEXT NOT NULL,
            character_key TEXT NOT NULL,
            character_template_id TEXT NOT NULL,
            template_version TEXT NOT NULL,
            body_text TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,
            eligibility_snapshot_json TEXT NOT NULL DEFAULT '{}',
            policy_version TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            read_at TEXT,
            deferred_at TEXT,
            handled_at TEXT,
            expires_at TEXT NOT NULL,
            accepted_resident_id TEXT,
            terminal_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(catalog_id) REFERENCES character_letter_catalog(id),
            FOREIGN KEY(character_template_id) REFERENCES character_templates(id),
            FOREIGN KEY(accepted_resident_id) REFERENCES universe_residents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_character_letters_open_world
            ON character_letters(universe_id)
            WHERE status IN ('unread', 'read', 'deferred');
        CREATE UNIQUE INDEX IF NOT EXISTS ux_character_letters_world_character
            ON character_letters(universe_id, character_key);
        CREATE INDEX IF NOT EXISTS ix_character_letters_owner_list
            ON character_letters(owner_platform_user_id, delivered_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_character_letters_expiry
            ON character_letters(status, expires_at, id);
        """
    )


def _migration_0035_companion_world_visit_human_chat(conn: Connection) -> None:
    """M5：限时 visit、独立真人聊天、拉黑与举报证据的加性数据基座。

    本迁移只增加空表和索引，不注册 API/scheduler，也不把真人消息接入 Runtime。
    owner 世界三个 slot 由后续事务在 world lock 内按需幂等补齐。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS universe_visit_slots (
            universe_id TEXT NOT NULL,
            slot_no INTEGER NOT NULL,
            occupant_type TEXT,
            occupant_id TEXT,
            occupied_at TEXT,
            PRIMARY KEY (universe_id, slot_no),
            UNIQUE (occupant_type, occupant_id),
            FOREIGN KEY(universe_id) REFERENCES universes(id)
        );
        CREATE INDEX IF NOT EXISTS ix_universe_visit_slots_occupant
            ON universe_visit_slots(occupant_type, occupant_id);

        CREATE TABLE IF NOT EXISTS universe_invites (
            id TEXT PRIMARY KEY,
            universe_id TEXT NOT NULL,
            owner_platform_user_id TEXT NOT NULL,
            code_hash TEXT NOT NULL UNIQUE,
            code_prefix TEXT NOT NULL,
            status TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            redeemed_by_platform_user_id TEXT,
            redeemed_visit_id TEXT,
            redeemed_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(redeemed_by_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_universe_invites_owner
            ON universe_invites(owner_platform_user_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_universe_invites_expiry
            ON universe_invites(status, expires_at, id);

        CREATE TABLE IF NOT EXISTS universe_visits (
            id TEXT PRIMARY KEY,
            invite_id TEXT NOT NULL UNIQUE,
            universe_id TEXT NOT NULL,
            owner_platform_user_id TEXT NOT NULL,
            visitor_platform_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            pending_expires_at TEXT NOT NULL,
            accepted_at TEXT,
            expires_at TEXT,
            terminal_at TEXT,
            terminal_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(invite_id) REFERENCES universe_invites(id),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(visitor_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_visits_open_pair
            ON universe_visits(universe_id, visitor_platform_user_id)
            WHERE status IN ('pending', 'active');
        CREATE INDEX IF NOT EXISTS ix_universe_visits_visitor
            ON universe_visits(visitor_platform_user_id, status, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_universe_visits_owner
            ON universe_visits(owner_platform_user_id, status, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_universe_visits_expiry
            ON universe_visits(status, pending_expires_at, expires_at, id);

        CREATE TABLE IF NOT EXISTS human_conversations (
            id TEXT PRIMARY KEY,
            visit_id TEXT NOT NULL UNIQUE,
            owner_platform_user_id TEXT NOT NULL,
            visitor_platform_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            owner_hidden_at TEXT,
            visitor_hidden_at TEXT,
            owner_last_read_at TEXT,
            visitor_last_read_at TEXT,
            last_message_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(visit_id) REFERENCES universe_visits(id),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(visitor_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_human_conversations_owner
            ON human_conversations(owner_platform_user_id, last_message_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_human_conversations_visitor
            ON human_conversations(visitor_platform_user_id, last_message_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS human_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sender_platform_user_id TEXT NOT NULL,
            client_message_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            body_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES human_conversations(id),
            FOREIGN KEY(sender_platform_user_id) REFERENCES platform_users(id),
            UNIQUE(conversation_id, sender_platform_user_id, client_message_id),
            UNIQUE(conversation_id, sequence_no)
        );
        CREATE INDEX IF NOT EXISTS ix_human_messages_list
            ON human_messages(conversation_id, sequence_no DESC);

        CREATE TABLE IF NOT EXISTS platform_user_blocks (
            blocker_platform_user_id TEXT NOT NULL,
            blocked_platform_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(blocker_platform_user_id, blocked_platform_user_id),
            FOREIGN KEY(blocker_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(blocked_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_platform_user_blocks_blocked
            ON platform_user_blocks(blocked_platform_user_id, blocker_platform_user_id);

        CREATE TABLE IF NOT EXISTS human_chat_reports (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            reporter_platform_user_id TEXT NOT NULL,
            reported_platform_user_id TEXT NOT NULL,
            reported_message_id TEXT,
            reason_code TEXT NOT NULL,
            details_text TEXT,
            evidence_snapshot_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            retained_until TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            FOREIGN KEY(conversation_id) REFERENCES human_conversations(id),
            FOREIGN KEY(reporter_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(reported_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(reported_message_id) REFERENCES human_messages(id)
        );
        CREATE INDEX IF NOT EXISTS ix_human_chat_reports_queue
            ON human_chat_reports(status, created_at, id);
        """
    )


def _migration_0047_legacy_template_display_name(conn: Connection) -> None:
    """把 legacy 哨兵模板的占位名换成面向用户的默认展示名。

    legacy 居民的展示名走 ``COALESCE(profiles.display_name, character_templates.name)``。
    微信侧从未起过名的账号会回落到哨兵串 ``'legacy'`` 并直接暴露到 App 界面。这里只在
    模板名仍是原始哨兵值时改写，运营手工改过的名字不覆盖。
    """
    conn.execute(
        """
        UPDATE character_templates
        SET name = ?
        WHERE id = 'tmpl_legacy' AND name = 'legacy'
        """,
        ("来自微信的Bot",),
    )


def _migration_0048_companion_world_resident_drafts(conn: Connection) -> None:
    """结构化自建角色（CUSTOM-001）+ 两步式草稿（SEC-001/D-B）+ 幂等键（IDEM-001）。

    ``character_templates`` 加 4 个可空列（``persona_key`` 跨模板版本稳定的人设身份、
    ``long_summary`` 运营长介绍、``relationship_type`` / ``personality_traits_json``
    结构化设定）；新增 ``resident_drafts`` 承载 preview → 消费的短期草稿，行内存的是
    **已清洗** 文本与已渲染的 persona seed，保证「所见即所存」。

    纯加列 + 新表，无回填、无锁表风险；两后端均幂等。
    """
    _ensure_column(conn, "character_templates", "persona_key", "TEXT")
    _ensure_column(conn, "character_templates", "long_summary", "TEXT")
    _ensure_column(conn, "character_templates", "relationship_type", "TEXT")
    _ensure_column(conn, "character_templates", "personality_traits_json", "TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS resident_drafts (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,          -- 草稿绑定真人，跨用户消费一律 not_found
            draft_token TEXT NOT NULL UNIQUE,        -- 不可枚举、单次消费
            name TEXT NOT NULL,                      -- 已过清洗器
            avatar_key TEXT NOT NULL,                -- 受控取值
            relationship_type TEXT NOT NULL,         -- 受控取值
            relationship_label TEXT,                 -- custom 关系的自由文本，已过清洗器
            personality_traits_json TEXT NOT NULL,   -- 受控取值数组
            style_note TEXT,                         -- 自由文本，已过清洗器
            normalized_summary TEXT NOT NULL,        -- 预览摘要，与持久化同一段渲染代码产出
            persona_seed_json TEXT NOT NULL,         -- 服务端模板化渲染的 SOUL/IDENTITY
            safety_json TEXT,                        -- 清洗器判定留痕（verdict/风险分类）
            status TEXT NOT NULL DEFAULT 'open',     -- open | consumed
            client_request_id TEXT,                  -- 消费时写入，承载 IDEM-001
            resident_id TEXT,                        -- 消费结果，幂等重放直接回放
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_resident_drafts_owner
            ON resident_drafts(platform_user_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_resident_drafts_client_request
            ON resident_drafts(platform_user_id, client_request_id)
            WHERE client_request_id IS NOT NULL;
        """
    )


def _migration_0049_companion_world_naming(conn: Connection) -> None:
    """候选实例名快照（NAME-001）+ 候选稳定身份（CAND-001）。

    ``character_templates`` 加运营名池：``name_pool_json``（3–5 个已审核候选名）与
    ``name_pool_version``（改名池必须换版本号，否则新老快照无法区分来源）。
    ``universe_residents`` 加 ``suggested_display_name`` / ``naming_version``：
    首次快照候选时确定性选名并写入，之后**只读回**——重复 bootstrap、换设备、重装
    都拿到同一个名字，且不随选名算法升级静默变化。

    纯加列，无回填：既有候选行两列为 NULL，DTO 侧表现为 ``naming_status=unavailable``，
    与「模板未配名池」同一条退化路径，不影响 bootstrap 成功。
    """
    _ensure_column(conn, "character_templates", "name_pool_json", "TEXT")
    _ensure_column(conn, "character_templates", "name_pool_version", "TEXT")
    _ensure_column(conn, "universe_residents", "suggested_display_name", "TEXT")
    _ensure_column(conn, "universe_residents", "naming_version", "TEXT")


def _migration_0050_ai_conversation_read_cursor(conn: Connection) -> None:
    """AI 会话最小 read cursor（CONV-002 方案 B）。

    ``last_read_message_id`` 存该会话已读到的 ``messages.id``；未读数 = App scope 内
    ``id > 游标`` 的 assistant 消息数。不建新表、不改 turn 链路。

    纯加列无回填：既有会话为 NULL，等价于「一条都没读过」，未读数即全部 AI 消息数。
    """
    _ensure_column(conn, "ai_conversations", "last_read_message_id", "INTEGER")


def _migration_0052_human_conversation_read_cursor(conn: Connection) -> None:
    """真人会话 read cursor（M5-CONV-001），沿用 m0050 给 AI 会话定下的形状。

    原先未读只能靠 ``owner/visitor_last_read_at`` 时间戳推算，而 read marker 与消息
    ``created_at`` 都是**秒**精度：与标记已读同一秒到达的对方消息会被判成已读并永久
    漏计——是漏不是多，用户根本不知道有消息没看到。改用 ``human_messages.sequence_no``
    做游标后比较的是序号而非墙钟，同秒问题从根上消失。

    ``last_read_at`` 保留：它仍是客户端的展示字段，只是不再承担未读计算。

    纯加列无回填：既有会话为 NULL，等价于「一条都没读过」，未读数即对方全部消息数。
    """
    _ensure_column(
        conn, "human_conversations", "owner_last_read_sequence", "INTEGER"
    )
    _ensure_column(
        conn, "human_conversations", "visitor_last_read_sequence", "INTEGER"
    )


def _migration_0053_resident_intro_post(conn: Connection) -> None:
    """CONTENT-002：一位居民最多一条自我介绍动态。

    只加一个部分唯一索引，不加表不加列。``resident_intro`` 与既有 ``ai_feed`` /
    ``user_post`` 的两个部分唯一索引互不相交（各自带 ``WHERE source_type=...``），
    所以新 source_type 不会撞上 AI 每日双档位的槽位唯一约束。

    幂等锚落在库上而不是应用层：确认候选是一次性操作，重放/并发只应有一条介绍动态，
    而 ``client_request_id`` 那条索引限定 ``source_type='user_post'``，管不到这里。
    """
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_resident_intro
            ON universe_posts(universe_id, author_resident_id)
            WHERE source_type = 'resident_intro';
        """
    )


def _migration_0055_universe_post_media(conn: Connection) -> None:
    """v1.5 图文动态：一条动态最多挂 4 张图（``universe_post_media``）。

    为什么另开一张表而不是在 ``universe_posts`` 上加 4 个列：顺序是产品可见的（客户端按
    ``position`` 排版），而"第 N 张"这种列名做不出稳定的插入/删除语义；多对一独立成行后
    回收 job 与引用计数也只需扫一张窄表。

    两条唯一约束各管一件事：
    - ``PRIMARY KEY(post_id, position)`` —— 同一条动态里位次不重复（发布是一次性写入，
      重放靠 ``ON CONFLICT DO NOTHING`` 收敛）；
    - ``ux_universe_post_media_media`` —— **一份资产全局只能挂一条动态**，与聊天侧
      ``media_assets.status`` 的一次性语义同构。跨 post 复用会在这里撞唯一键，
      连同发布事务一起回滚，对外收敛成 ``media_ref_invalid``。

    ``media_id`` 不加 FK（同 m0054 的理由）：完整性由发布事务内的原子认领与状态位保证。
    纯加表，无回填。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS universe_post_media (
            post_id TEXT NOT NULL,
            media_id TEXT NOT NULL,
            position INTEGER NOT NULL,               -- 0..3，客户端按此排版
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY (post_id, position),
            FOREIGN KEY(post_id) REFERENCES universe_posts(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_post_media_media
            ON universe_post_media(media_id);
        """
    )


def _migration_0057_resident_wish_drafts(conn: Connection) -> None:
    """v1.5 许愿创建（WISH-001/005）：草稿加来源与许愿幂等键。

    ``source``：``form``（结构化表单，m0048 起的既有路径）| ``wish``（一句话许愿，LLM 生成）。
    存量行按 ``form`` 回落——它们全部来自表单路径，语义准确。

    ``wish_request_id``：**preview 阶段**的幂等键，与既有 ``client_request_id``（消费阶段的
    IDEM-001 键）刻意分列两列。合用一列会让"预览重试"与"确认创建"抢同一个唯一约束：
    preview 先占了 (user, id)，随后 create 再往同一行写同一个 id 就分不清是重放还是新请求。
    分列之后语义清晰——同一个 ``wish_request_id`` 恒等于同一份草稿，重试不会产生第二份草稿、
    也不会产生第二次 LLM 计费。

    ``ix_resident_drafts_wish_window`` 服务于日额度计数（按 owner + source 数时间窗内的许愿
    次数）。计数恒带 ``platform_user_id`` 等值条件，账号隔离由查询与索引共同保证。

    纯加列 + 加索引，无回填、无锁表风险，两后端幂等。
    """
    _ensure_column(conn, "resident_drafts", "source", "TEXT NOT NULL DEFAULT 'form'")
    _ensure_column(conn, "resident_drafts", "wish_request_id", "TEXT")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_resident_drafts_wish_request
            ON resident_drafts(platform_user_id, wish_request_id)
            WHERE wish_request_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_resident_drafts_wish_window
            ON resident_drafts(platform_user_id, source, created_at);
        """
    )


def _migration_0058_async_resident_wishes(conn: Connection) -> None:
    """v1.5 异步许愿：独立 wish/job 状态、持久任务与 mailbox 关联。

    愿望原文只在尚需生成时保留；投递、收回或最终无法满足后由应用层置空。job 只保存
    受控角色生成物，不复用 Feed outbox，避免两种重试/投递语义互相污染。
    """
    _ensure_column(
        conn, "character_letter_catalog", "source", "TEXT NOT NULL DEFAULT 'organic'"
    )
    _ensure_column(
        conn, "character_letters", "source", "TEXT NOT NULL DEFAULT 'organic'"
    )
    _ensure_column(conn, "character_letters", "wish_id", "TEXT")
    conn.executescript(
        """
        DROP INDEX IF EXISTS ux_character_letters_open_world;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_character_letters_open_world
            ON character_letters(universe_id)
            WHERE status IN ('unread', 'read', 'deferred') AND source = 'organic';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_character_letters_wish
            ON character_letters(wish_id)
            WHERE wish_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_letter_catalog_source_selection
            ON character_letter_catalog(source, status, priority DESC, id);

        CREATE TABLE IF NOT EXISTS resident_wishes (
            id TEXT PRIMARY KEY,
            owner_platform_user_id TEXT NOT NULL,
            universe_id TEXT NOT NULL,
            client_request_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            wish_text TEXT,
            input_safety_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            submitted_at TEXT NOT NULL,
            deliver_not_before TEXT NOT NULL,
            deliver_by TEXT NOT NULL,
            letter_id TEXT,
            closed_at TEXT,
            terminal_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(universe_id) REFERENCES universes(id),
            FOREIGN KEY(letter_id) REFERENCES character_letters(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_resident_wishes_owner_request
            ON resident_wishes(owner_platform_user_id, client_request_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_resident_wishes_owner_open
            ON resident_wishes(owner_platform_user_id)
            WHERE closed_at IS NULL;
        CREATE INDEX IF NOT EXISTS ix_resident_wishes_owner_current
            ON resident_wishes(owner_platform_user_id, submitted_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_resident_wishes_window
            ON resident_wishes(owner_platform_user_id, submitted_at);

        CREATE TABLE IF NOT EXISTS resident_wish_jobs (
            id TEXT PRIMARY KEY,
            wish_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            claim_token TEXT,
            lease_expires_at TEXT,
            generation_json TEXT,
            safety_json TEXT,
            last_error_code TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(wish_id) REFERENCES resident_wishes(id)
        );
        CREATE INDEX IF NOT EXISTS ix_resident_wish_jobs_claim
            ON resident_wish_jobs(status, next_attempt_at, lease_expires_at, id);
        """
    )


def _migration_0059_creator_role_templates(conn: Connection) -> None:
    """建立用户角色模板、版本审核、注册快照与审计事件表。

    ``urt_`` 是用户模板活动码的保留命名空间。迁移前先检查运营活码存量，避免两类
    code 在统一 ``campaign_code`` 参数中出现无法确定来源的冲突。
    """
    reserved = conn.execute(
        "SELECT code FROM campaign_codes "
        "WHERE substr(lower(code), 1, 4) = 'urt_' LIMIT 1"
    ).fetchone()
    if reserved is not None:
        raise RuntimeError(
            "migration 59 blocked: campaign_codes contains reserved urt_ prefix"
        )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS creator_role_templates (
            id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL DEFAULT 'zhaoxi' CHECK (app_id = 'zhaoxi'),
            creator_platform_user_id TEXT NOT NULL,
            slot_no INTEGER NOT NULL CHECK (slot_no BETWEEN 1 AND 3),
            campaign_code TEXT NOT NULL
                CHECK (
                    substr(campaign_code, 1, 4) = 'urt_'
                    AND length(campaign_code) BETWEEN 26 AND 64
                ),
            status TEXT NOT NULL DEFAULT 'pending_review'
                CHECK (status IN (
                    'pending_review', 'approved', 'active', 'rejected',
                    'disabled_creator', 'disabled_admin', 'deleted'
                )),
            used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
            activated_at TEXT,
            expires_at TEXT,
            disabled_by_admin_user_id TEXT,
            disabled_reason TEXT,
            status_before_admin_disable TEXT CHECK (
                status_before_admin_disable IS NULL OR status_before_admin_disable IN (
                    'pending_review', 'approved', 'active', 'rejected', 'disabled_creator'
                )
            ),
            deleted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(creator_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(disabled_by_admin_user_id) REFERENCES admin_users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_creator_role_templates_owner_slot_live
            ON creator_role_templates(creator_platform_user_id, app_id, slot_no)
            WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_creator_role_templates_campaign_code
            ON creator_role_templates(campaign_code);
        CREATE INDEX IF NOT EXISTS ix_creator_role_templates_owner_list
            ON creator_role_templates(
                creator_platform_user_id, app_id, deleted_at, updated_at DESC
            );

        CREATE TABLE IF NOT EXISTS creator_role_template_versions (
            id TEXT PRIMARY KEY,
            creator_role_template_id TEXT NOT NULL,
            version_no INTEGER NOT NULL CHECK (version_no >= 1),
            ai_name TEXT NOT NULL,
            personality_text TEXT NOT NULL,
            mission_text TEXT NOT NULL,
            opening_line TEXT,
            generated_summary TEXT,
            public_summary TEXT,
            summary_edit_status TEXT NOT NULL DEFAULT 'unavailable',
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (review_status IN ('pending', 'reviewing', 'passed', 'rejected')),
            is_published INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
            review_categories_json TEXT NOT NULL DEFAULT '[]',
            review_reason TEXT,
            reviewed_at TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(creator_role_template_id) REFERENCES creator_role_templates(id),
            UNIQUE(creator_role_template_id, version_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_creator_role_template_versions_published
            ON creator_role_template_versions(creator_role_template_id)
            WHERE is_published = 1;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_creator_role_template_versions_open_review
            ON creator_role_template_versions(creator_role_template_id)
            WHERE review_status IN ('pending', 'reviewing');
        CREATE INDEX IF NOT EXISTS ix_creator_role_template_versions_history
            ON creator_role_template_versions(creator_role_template_id, version_no DESC);

        CREATE TABLE IF NOT EXISTS creator_role_template_review_runs (
            id TEXT PRIMARY KEY,
            creator_role_template_version_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'passed', 'rejected', 'error')),
            model TEXT,
            provider TEXT,
            latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
            categories_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT,
            error_code TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY(creator_role_template_version_id)
                REFERENCES creator_role_template_versions(id),
            UNIQUE(creator_role_template_version_id, attempt_no)
        );
        CREATE INDEX IF NOT EXISTS ix_creator_role_template_review_runs_version
            ON creator_role_template_review_runs(
                creator_role_template_version_id, attempt_no DESC
            );

        CREATE TABLE IF NOT EXISTS account_creator_role_template_attribution (
            account_id TEXT PRIMARY KEY,
            creator_role_template_id TEXT NOT NULL,
            creator_role_template_version_id TEXT NOT NULL,
            creator_platform_user_id TEXT NOT NULL,
            campaign_code TEXT NOT NULL,
            ai_name_snapshot TEXT NOT NULL,
            personality_snapshot TEXT NOT NULL,
            mission_snapshot TEXT NOT NULL,
            opening_line_snapshot TEXT,
            attributed_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(creator_role_template_id) REFERENCES creator_role_templates(id),
            FOREIGN KEY(creator_role_template_version_id)
                REFERENCES creator_role_template_versions(id),
            FOREIGN KEY(creator_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_account_creator_role_template_source
            ON account_creator_role_template_attribution(
                creator_role_template_id, attributed_at
            );
        CREATE INDEX IF NOT EXISTS ix_account_creator_role_template_campaign
            ON account_creator_role_template_attribution(campaign_code);

        CREATE TABLE IF NOT EXISTS creator_role_template_events (
            id TEXT PRIMARY KEY,
            creator_role_template_id TEXT NOT NULL,
            creator_role_template_version_id TEXT,
            event_type TEXT NOT NULL CHECK (event_type IN (
                'created', 'review_started', 'review_passed', 'review_rejected',
                'review_error', 'version_activated', 'disabled_by_creator',
                'enabled', 'disabled_by_admin', 'deleted', 'attribution_applied'
            )),
            actor_type TEXT NOT NULL CHECK (actor_type IN ('creator', 'admin', 'system')),
            actor_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(creator_role_template_id) REFERENCES creator_role_templates(id),
            FOREIGN KEY(creator_role_template_version_id)
                REFERENCES creator_role_template_versions(id)
        );
        CREATE INDEX IF NOT EXISTS ix_creator_role_template_events_history
            ON creator_role_template_events(
                creator_role_template_id, created_at DESC, id DESC
            );
        """
    )


def _migration_0060_creator_role_template_opening_and_summary(conn: Connection) -> None:
    """增加自定义开场白、公开简介和一次性简介审核审计。"""
    _ensure_column(conn, "creator_role_template_versions", "opening_line", "TEXT")
    _ensure_column(conn, "creator_role_template_versions", "generated_summary", "TEXT")
    _ensure_column(conn, "creator_role_template_versions", "public_summary", "TEXT")
    _ensure_column(
        conn,
        "creator_role_template_versions",
        "summary_edit_status",
        "TEXT NOT NULL DEFAULT 'unavailable'",
    )
    _ensure_column(
        conn,
        "account_creator_role_template_attribution",
        "opening_line_snapshot",
        "TEXT",
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS creator_role_template_summary_review_runs (
            id TEXT PRIMARY KEY,
            creator_role_template_version_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
            submitted_summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'passed', 'rejected', 'error')),
            model TEXT,
            provider TEXT,
            latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
            categories_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT,
            error_code TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY(creator_role_template_version_id)
                REFERENCES creator_role_template_versions(id),
            UNIQUE(creator_role_template_version_id, attempt_no)
        );
        CREATE INDEX IF NOT EXISTS ix_creator_role_template_summary_runs_version
            ON creator_role_template_summary_review_runs(
                creator_role_template_version_id, attempt_no DESC
            );
        """
    )


def _migration_0061_companion_world_product_scope(conn: Connection) -> None:
    """给历史 Companion World 根实体补产品锚，并把模板目录唯一性收缩到产品内。

    存量 World/App 数据属于拆分前的朝夕产品，因此两列统一回填 ``zhaoxi``。鸣蝉只会
    显式写入/读取 ``mingchan``。``universes.owner_platform_user_id`` 的历史全局唯一约束
    暂不在自动迁移中重建：启用鸣蝉前必须先通过 MC-05 precheck/cleanup 清除旧 App 测试
    World，避免 SQLite 大范围父子表重建与 PostgreSQL 约束切换混入常规启动迁移。
    """
    _ensure_column(
        conn,
        "universes",
        "app_id",
        "TEXT NOT NULL DEFAULT 'zhaoxi'",
    )
    _ensure_column(
        conn,
        "character_templates",
        "app_id",
        "TEXT NOT NULL DEFAULT 'zhaoxi'",
    )
    conn.execute("UPDATE universes SET app_id = 'zhaoxi' WHERE app_id IS NULL")
    conn.execute(
        "UPDATE character_templates SET app_id = 'zhaoxi' WHERE app_id IS NULL"
    )
    conn.executescript(
        """
        DROP INDEX IF EXISTS ux_character_templates_active_initial_rank;
        CREATE INDEX IF NOT EXISTS ix_universes_app_owner
            ON universes(app_id, owner_platform_user_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_character_templates_app_active_initial_rank
            ON character_templates(app_id, initial_candidate_rank)
            WHERE status = 'active' AND initial_candidate_rank IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_character_templates_app_source
            ON character_templates(app_id, source_type, status);
        CREATE INDEX IF NOT EXISTS ix_character_templates_app_owner
            ON character_templates(app_id, owner_platform_user_id);
        """
    )


def _migration_0062_mingchan_notification_product_scope(conn: Connection) -> None:
    """给 App 通知补产品锚，并建立可并存的产品级通知偏好表。

    旧 ``app_notification_preferences`` 的真人主键无法表达多产品，保持为朝夕 legacy
    数据等待 cleanup；新表从一开始以 ``(platform_user_id, app_id)`` 为主键。存量通知
    同样属于拆分前朝夕 App，新增列默认并回填为 ``zhaoxi``。
    """
    _ensure_column(
        conn,
        "app_notifications",
        "app_id",
        "TEXT NOT NULL DEFAULT 'zhaoxi'",
    )
    conn.execute("UPDATE app_notifications SET app_id = 'zhaoxi' WHERE app_id IS NULL")
    conn.executescript(
        """
        DROP INDEX IF EXISTS ux_app_notifications_user_idempotency;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_app_notifications_app_user_idempotency
            ON app_notifications(app_id, platform_user_id, idempotency_key);
        CREATE INDEX IF NOT EXISTS ix_app_notifications_app_list
            ON app_notifications(
                app_id, platform_user_id, delivery_status, delivered_at DESC, id DESC
            );
        CREATE INDEX IF NOT EXISTS ix_app_notifications_app_unread
            ON app_notifications(
                app_id, platform_user_id, delivery_status, read_at, expires_at
            );

        CREATE TABLE IF NOT EXISTS product_notification_preferences (
            platform_user_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            quiet_level TEXT NOT NULL DEFAULT 'standard',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(platform_user_id, app_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        """
    )


def _migration_0063_companion_world_owner_product_unique(conn: Connection) -> None:
    """把 home World 唯一性从真人全局收缩为 ``(app_id, owner)``。

    m0061 已完成 ``app_id`` 回填；本迁移只改变根表唯一契约，不移动或删除任何
    legacy World。SQLite 在关闭外键检查的单个 ``executescript`` 中重建根表，随后
    立即运行 ``foreign_key_check``；PostgreSQL 只删除精确覆盖 owner 单列的 UNIQUE
    constraint。两端最后都建立产品级唯一索引。
    """
    duplicate = conn.execute(
        """
        SELECT app_id, owner_platform_user_id
        FROM universes
        GROUP BY app_id, owner_platform_user_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError("m0063 duplicate universe app/owner rows")

    rows = conn.execute(
        """
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN LATERAL (
            SELECT array_agg(a.attname ORDER BY key_col.ordinality) AS columns
            FROM unnest(c.conkey) WITH ORDINALITY AS key_col(attnum, ordinality)
            JOIN pg_attribute a
              ON a.attrelid = t.oid AND a.attnum = key_col.attnum
        ) names ON TRUE
        WHERE n.nspname = current_schema()
          AND t.relname = 'universes'
          AND c.contype = 'u'
          AND names.columns = ARRAY['owner_platform_user_id']::name[]
        """
    ).fetchall()
    for row in rows:
        name = str(row["conname"])
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RuntimeError("unexpected universes unique constraint name")
        conn.execute(f'ALTER TABLE universes DROP CONSTRAINT "{name}"')

    conn.executescript(
        """
        DROP INDEX IF EXISTS ix_universes_app_owner;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_universes_app_owner
            ON universes(app_id, owner_platform_user_id);
        """
    )


__all__ = [
    "_migration_0028_companion_world_core",
    "_migration_0029_universe_memory_l3",
    "_migration_0030_companion_world_candidates",
    "_migration_0033_companion_world_m3_content",
    "_migration_0034_companion_world_lifecycle_mailbox",
    "_migration_0035_companion_world_visit_human_chat",
    "_migration_0047_legacy_template_display_name",
    "_migration_0048_companion_world_resident_drafts",
    "_migration_0049_companion_world_naming",
    "_migration_0050_ai_conversation_read_cursor",
    "_migration_0052_human_conversation_read_cursor",
    "_migration_0053_resident_intro_post",
    "_migration_0055_universe_post_media",
    "_migration_0057_resident_wish_drafts",
    "_migration_0058_async_resident_wishes",
    "_migration_0059_creator_role_templates",
    "_migration_0060_creator_role_template_opening_and_summary",
    "_migration_0061_companion_world_product_scope",
    "_migration_0062_mingchan_notification_product_scope",
    "_migration_0063_companion_world_owner_product_unique",
]
