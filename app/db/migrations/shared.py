"""跨产品共享的 schema 迁移。

函数体自 ``app/db/_core.py`` 原样搬出：函数名、版本号、逻辑均未改动。
迁移顺序仍由 ``_core._MIGRATIONS`` 这一份全局有序列表唯一决定——**分文件不等于分链**。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from app.db._backend import Connection, is_postgres
from app.db._schema_utils import _ensure_column, product_quota_subject

# 与 _core 同名，logging.getLogger 返回同一实例，日志 channel 不变。
logger = logging.getLogger("ai4all.db")


def _migration_0001_baseline(conn: Connection) -> None:
    """基线迁移：建立当前全量 schema（幂等，可在已有库上重复执行）。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            channel TEXT,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT,
            onboarding_state TEXT NOT NULL DEFAULT 'pending',
            onboarding_updated_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE TABLE IF NOT EXISTS platform_users (
            id TEXT PRIMARY KEY,
            phone TEXT NOT NULL UNIQUE,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );

        CREATE INDEX IF NOT EXISTS ix_subscriptions_user
        ON subscriptions(platform_user_id, updated_at);

        CREATE TABLE IF NOT EXISTS entitlement_wallets (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL UNIQUE,
            platform_user_id TEXT NOT NULL,
            balance_shell_micros INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );

        CREATE INDEX IF NOT EXISTS ix_entitlement_wallets_user
        ON entitlement_wallets(platform_user_id, status);

        CREATE TABLE IF NOT EXISTS entitlement_ledger (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            platform_user_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            amount_shell_micros INTEGER NOT NULL,
            balance_after_shell_micros INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(wallet_id) REFERENCES entitlement_wallets(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );

        CREATE INDEX IF NOT EXISTS ix_entitlement_ledger_wallet_created
        ON entitlement_ledger(wallet_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_entitlement_ledger_account_created
        ON entitlement_ledger(account_id, created_at);

        CREATE TABLE IF NOT EXISTS cost_events (
            id TEXT PRIMARY KEY,
            wallet_id TEXT,
            account_id TEXT NOT NULL,
            platform_user_id TEXT,
            cost_type TEXT NOT NULL,
            cost_owner TEXT NOT NULL DEFAULT 'user',
            billable_to_user INTEGER NOT NULL DEFAULT 1,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            billable_tokens INTEGER,
            model_price_multiplier_micros INTEGER NOT NULL DEFAULT 1000000,
            computed_shell_micros INTEGER NOT NULL DEFAULT 0,
            entitlement_ledger_id TEXT,
            source_type TEXT,
            source_id TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(wallet_id) REFERENCES entitlement_wallets(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(entitlement_ledger_id) REFERENCES entitlement_ledger(id)
        );

        CREATE INDEX IF NOT EXISTS ix_cost_events_account_created
        ON cost_events(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_cost_events_wallet_created
        ON cost_events(wallet_id, created_at);

        -- owner_binding = 微信接入（形态 A）独有的产物：记录「微信渠道把某 account 绑到某真人」，
        -- 非通用「用户账号」机制。朝夕相伴居民（form-B runtime account）不发 binding，经世界归属解析到
        -- 真人（accounts.resolve_owner_platform_user_id）。详见 docs/archive/deliveries/companion_world/companion_world_account_model_reconciliation.md。
        CREATE TABLE IF NOT EXISTS account_owner_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            binding_method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            verified_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(platform_user_id, account_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_account_owner_bindings_account
        ON account_owner_bindings(account_id);

        CREATE TABLE IF NOT EXISTS referral_codes (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT,
            code TEXT NOT NULL UNIQUE,
            code_type TEXT NOT NULL DEFAULT 'personal',
            status TEXT NOT NULL DEFAULT 'active',
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_by_admin_user_id TEXT,
            disabled_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_referral_codes_personal_user
        ON referral_codes(platform_user_id, code_type)
        WHERE code_type = 'personal' AND platform_user_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS ix_referral_codes_user_status
        ON referral_codes(platform_user_id, status);

        CREATE TABLE IF NOT EXISTS referral_relationships (
            id TEXT PRIMARY KEY,
            inviter_platform_user_id TEXT NOT NULL,
            invitee_platform_user_id TEXT NOT NULL UNIQUE,
            referral_code_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'registered',
            meaningful_message_count INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            reward_ledger_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            rewarded_at TEXT,
            FOREIGN KEY(inviter_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(invitee_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(referral_code_id) REFERENCES referral_codes(id),
            FOREIGN KEY(reward_ledger_id) REFERENCES entitlement_ledger(id)
        );

        CREATE INDEX IF NOT EXISTS ix_referral_relationships_inviter_created
        ON referral_relationships(inviter_platform_user_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_referral_relationships_status
        ON referral_relationships(status, review_status);

        CREATE TABLE IF NOT EXISTS meaningful_message_reviews (
            id TEXT PRIMARY KEY,
            referral_relationship_id TEXT NOT NULL,
            invitee_platform_user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            message_ids_json TEXT NOT NULL DEFAULT '[]',
            reviewer_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(referral_relationship_id) REFERENCES referral_relationships(id),
            FOREIGN KEY(invitee_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_meaningful_reviews_relationship_reviewer
        ON meaningful_message_reviews(referral_relationship_id, reviewer_type);

        CREATE TABLE IF NOT EXISTS binding_intents (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            openclaw_login_session_key TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'openclaw-weixin',
            status TEXT NOT NULL DEFAULT 'created',
            channel_account_id TEXT,
            qr_data_url TEXT,
            manual_login_command TEXT,
            raw_result_json TEXT NOT NULL DEFAULT '{}',
            expires_at TEXT,
            completed_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_binding_intents_user_created
        ON binding_intents(platform_user_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_binding_intents_account_created
        ON binding_intents(account_id, created_at);

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            sender_id TEXT,
            chat_id TEXT,
            sender_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            ended_at TEXT,
            close_reason TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            business_day TEXT,
            session_summary TEXT,
            carryover_summary TEXT,
            summary_model TEXT,
            summary_prompt_version TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(account_id, session_key),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL UNIQUE,
            display_name TEXT,
            style TEXT,
            preferences_json TEXT NOT NULL DEFAULT '{}',
            system_prompt TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            message_id TEXT,
            reply_to_message_id TEXT,
            direction TEXT NOT NULL,
            role TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'text',
            content TEXT,
            raw_json TEXT,
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_account_message
        ON messages(account_id, message_id)
        WHERE message_id IS NOT NULL AND message_id != '';

        CREATE INDEX IF NOT EXISTS ix_messages_session_created
        ON messages(session_id, id);

        CREATE TABLE IF NOT EXISTS daily_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            date TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(account_id, date),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS channel_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            session_key TEXT NOT NULL,
            channel_account_id TEXT,
            sender_id TEXT,
            chat_id TEXT,
            raw_identity_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(account_id, channel, session_key),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_channel_bindings_account_seen
        ON channel_bindings(account_id, last_seen_at);

        CREATE TABLE IF NOT EXISTS outbound_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            channel_account_id TEXT,
            to_user_id TEXT NOT NULL,
            session_key TEXT,
            source TEXT NOT NULL,
            text TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            gateway_message_id TEXT,
            quota_date TEXT NOT NULL,
            product_category TEXT,
            policy_version TEXT,
            policy_reason TEXT,
            scheduled_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_outbound_messages_account_date
        ON outbound_messages(account_id, quota_date, status);

        CREATE INDEX IF NOT EXISTS ix_outbound_messages_status_created
        ON outbound_messages(status, created_at);

        CREATE TABLE IF NOT EXISTS content_moderation_tasks (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            session_id INTEGER,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            message_db_id INTEGER,
            outbound_message_id INTEGER,
            direction TEXT NOT NULL,
            content_kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            risk_level TEXT NOT NULL DEFAULT 'unknown',
            risk_categories_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            content_hash TEXT,
            snapshot_text TEXT,
            media_json TEXT NOT NULL DEFAULT '{}',
            sampling_reason TEXT,
            sample_rate_percent INTEGER,
            policy_version TEXT NOT NULL,
            prompt_version TEXT,
            machine_attempts INTEGER NOT NULL DEFAULT 0,
            machine_claimed_at TEXT,
            machine_completed_at TEXT,
            assigned_admin_user_id TEXT,
            reviewed_by_admin_user_id TEXT,
            reviewed_at TEXT,
            last_error TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(message_db_id) REFERENCES messages(id),
            FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id)
        );

        CREATE INDEX IF NOT EXISTS ix_content_moderation_tasks_account_created
        ON content_moderation_tasks(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_content_moderation_tasks_status_created
        ON content_moderation_tasks(status, created_at);

        CREATE INDEX IF NOT EXISTS ix_content_moderation_tasks_source
        ON content_moderation_tasks(source_type, source_id);

        CREATE INDEX IF NOT EXISTS ix_content_moderation_tasks_review_queue
        ON content_moderation_tasks(status, risk_level, created_at);

        CREATE TABLE IF NOT EXISTS content_moderation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            reviewer_type TEXT NOT NULL,
            engine TEXT,
            engine_version TEXT,
            result_level TEXT NOT NULL,
            categories_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            matched_terms_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT,
            raw_result_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(task_id) REFERENCES content_moderation_tasks(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_content_moderation_results_task
        ON content_moderation_results(task_id, id);

        CREATE INDEX IF NOT EXISTS ix_content_moderation_results_account_created
        ON content_moderation_results(account_id, created_at);

        CREATE TABLE IF NOT EXISTS content_moderation_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            admin_user_id TEXT,
            action TEXT NOT NULL,
            previous_status TEXT,
            next_status TEXT,
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(task_id) REFERENCES content_moderation_tasks(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_content_moderation_actions_task_created
        ON content_moderation_actions(task_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_content_moderation_actions_account_created
        ON content_moderation_actions(account_id, created_at);

        CREATE TABLE IF NOT EXISTS content_moderation_exports (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            admin_user_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            artifact_path TEXT,
            artifact_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(task_id) REFERENCES content_moderation_tasks(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_content_moderation_exports_account_created
        ON content_moderation_exports(account_id, created_at);

        CREATE TABLE IF NOT EXISTS moderation_account_risk_state (
            account_id TEXT PRIMARY KEY,
            risk_level TEXT NOT NULL DEFAULT 'normal',
            risk_score INTEGER NOT NULL DEFAULT 0,
            sample_multiplier REAL NOT NULL DEFAULT 1.0,
            proactive_blocked_until TEXT,
            conversation_blocked_until TEXT,
            last_risk_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            channel_account_id TEXT,
            to_user_id TEXT NOT NULL,
            session_key TEXT,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            outbound_message_id INTEGER,
            error TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            claimed_at TEXT,
            sent_at TEXT,
            cancelled_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id)
        );

        CREATE INDEX IF NOT EXISTS ix_reminders_status_due
        ON reminders(status, due_at);

        CREATE INDEX IF NOT EXISTS ix_reminders_account_due
        ON reminders(account_id, due_at);

        CREATE TABLE IF NOT EXISTS proactive_commitments (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            session_id INTEGER,
            source_message_id TEXT,
            source_reply_message_id TEXT,
            dedupe_key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            confidence REAL,
            reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            outbound_message_id INTEGER,
            error TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            claimed_at TEXT,
            sent_at TEXT,
            cancelled_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id)
        );

        CREATE INDEX IF NOT EXISTS ix_proactive_commitments_status_due
        ON proactive_commitments(status, due_at);

        CREATE INDEX IF NOT EXISTS ix_proactive_commitments_account_due
        ON proactive_commitments(account_id, due_at);

        CREATE TABLE IF NOT EXISTS proactive_account_state (
            account_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_scan_at TEXT,
            last_scan_at TEXT,
            last_proactive_sent_at TEXT,
            cooldown_until TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_proactive_account_state_due
        ON proactive_account_state(enabled, next_scan_at, cooldown_until);

        CREATE TABLE IF NOT EXISTS llm_runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        -- 账号级主动消息偏好（source of truth）。稀疏存储：未显式设置的
        -- override 列为 NULL / 空容器，读取层 merge 全局配置后才是有效值，
        -- 以此区分"未设置=继承全局"与"用户显式设置"。
        CREATE TABLE IF NOT EXISTS proactive_message_settings (
            account_id TEXT PRIMARY KEY,
            master_enabled INTEGER NOT NULL DEFAULT 1,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            quiet_hours_json TEXT,                              -- NULL = 继承全局 quiet hours
            allowed_windows_json TEXT NOT NULL DEFAULT '[]',    -- 预留，Phase 2
            frequency_json TEXT NOT NULL DEFAULT '{}',          -- 预留，Phase 2
            category_settings_json TEXT NOT NULL DEFAULT '{}',
            muted_until TEXT,                                   -- NULL = 未临时静默
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        -- 主动消息设置变更审计：每次 tool/admin 修改都记录 before/patch/after。
        CREATE TABLE IF NOT EXISTS proactive_message_setting_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            source TEXT NOT NULL,                              -- tool | admin | system | migration
            tool_invocation_id INTEGER,
            previous_settings_json TEXT,
            patch_json TEXT NOT NULL,
            next_settings_json TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(tool_invocation_id) REFERENCES tool_invocations(id)
        );

        CREATE INDEX IF NOT EXISTS ix_proactive_message_setting_events_account
        ON proactive_message_setting_events(account_id, created_at);

        CREATE TABLE IF NOT EXISTS content_invitations (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            invitation_text TEXT NOT NULL,
            title_items_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate',
            scheduled_at TEXT,
            invited_at TEXT,
            responded_at TEXT,
            expires_at TEXT,
            outbound_message_id INTEGER,
            trigger_message_id TEXT,
            tool_invocation_id INTEGER,
            source_task_id TEXT,
            policy_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id),
            FOREIGN KEY(tool_invocation_id) REFERENCES tool_invocations(id)
        );

        CREATE INDEX IF NOT EXISTS ix_content_invitations_status_due
        ON content_invitations(status, scheduled_at, expires_at);

        CREATE INDEX IF NOT EXISTS ix_content_invitations_account_status
        ON content_invitations(account_id, status, updated_at);

        CREATE TABLE IF NOT EXISTS content_invitation_preferences (
            account_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'allowed',
            cooldown_until TEXT,
            last_feedback_at TEXT,
            feedback_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(account_id, topic),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_content_invitation_preferences_cooldown
        ON content_invitation_preferences(account_id, status, cooldown_until);

        CREATE TABLE IF NOT EXISTS dreaming_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_session_id INTEGER,
            source_business_day TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            prompt_version TEXT NOT NULL,
            llm_model TEXT,
            input_hash TEXT,
            output_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            token_input INTEGER,
            token_output INTEGER,
            actor_type TEXT NOT NULL DEFAULT 'system',
            actor_id TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(source_session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS ix_dreaming_runs_account_created
        ON dreaming_runs(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_dreaming_runs_source_session
        ON dreaming_runs(source_session_id);

        CREATE TABLE IF NOT EXISTS dreaming_memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            dreaming_run_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            source_session_id INTEGER,
            source_daily_note_date TEXT,
            operation TEXT NOT NULL DEFAULT 'add',
            target_file TEXT NOT NULL DEFAULT 'MEMORY.md',
            category TEXT NOT NULL DEFAULT 'other',
            memory_text TEXT NOT NULL,
            base_text_hash TEXT,
            diff_json TEXT NOT NULL DEFAULT '{}',
            importance TEXT NOT NULL DEFAULT 'medium',
            confidence REAL NOT NULL DEFAULT 0,
            sensitivity TEXT NOT NULL DEFAULT 'normal',
            apply_status TEXT NOT NULL DEFAULT 'generated',
            skip_reason TEXT,
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            applied_at TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(dreaming_run_id) REFERENCES dreaming_runs(id),
            FOREIGN KEY(source_session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS ix_dreaming_memory_items_run
        ON dreaming_memory_items(dreaming_run_id, id);

        CREATE INDEX IF NOT EXISTS ix_dreaming_memory_items_account_status
        ON dreaming_memory_items(account_id, apply_status, created_at);

        CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            memory_item_id INTEGER,
            event_type TEXT NOT NULL,
            actor_type TEXT NOT NULL DEFAULT 'system',
            actor_id TEXT,
            before_text TEXT,
            after_text TEXT,
            diff_text TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(memory_item_id) REFERENCES dreaming_memory_items(id)
        );

        CREATE INDEX IF NOT EXISTS ix_memory_events_account_created
        ON memory_events(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_memory_events_item
        ON memory_events(memory_item_id, created_at);

        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            event_name TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            source TEXT,
            properties_json TEXT NOT NULL DEFAULT '{}',
            event_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_analytics_events_name_time
        ON analytics_events(event_name, event_time);

        CREATE INDEX IF NOT EXISTS ix_analytics_events_account
        ON analytics_events(account_id, event_time);

        CREATE TABLE IF NOT EXISTS debug_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            message_id TEXT,
            source TEXT NOT NULL,
            llm_model TEXT,
            system_prompt TEXT,
            messages_json TEXT NOT NULL DEFAULT '[]',
            reply TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS ix_debug_traces_account_created
        ON debug_traces(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_debug_traces_session_created
        ON debug_traces(session_id, created_at);

        CREATE TABLE IF NOT EXISTS tool_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            session_id INTEGER,
            message_id TEXT,
            tool_call_id TEXT,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            args_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            finished_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS ix_tool_invocations_account_created
        ON tool_invocations(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_tool_invocations_tool_status
        ON tool_invocations(tool_name, status, created_at);

        CREATE TABLE IF NOT EXISTS search_provider_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_invocation_id INTEGER,
            task_id TEXT,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'running',
            request_json TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            finished_at TEXT,
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(tool_invocation_id) REFERENCES tool_invocations(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_search_provider_runs_account_created
        ON search_provider_runs(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_search_provider_runs_invocation
        ON search_provider_runs(tool_invocation_id, id);

        CREATE TABLE IF NOT EXISTS admin_users (
            id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            role TEXT NOT NULL DEFAULT 'staff',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE INDEX IF NOT EXISTS ix_admin_users_role_status
        ON admin_users(role, status);

        CREATE TABLE IF NOT EXISTS admin_access_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id TEXT,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            account_id TEXT,
            plaintext INTEGER NOT NULL DEFAULT 0,
            grant_id INTEGER,
            reason TEXT,
            request_path TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE INDEX IF NOT EXISTS ix_admin_access_events_account_created
        ON admin_access_events(account_id, created_at);

        CREATE INDEX IF NOT EXISTS ix_admin_access_events_plaintext_created
        ON admin_access_events(plaintext, created_at);

        CREATE TABLE IF NOT EXISTS admin_plaintext_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_admin_user_id TEXT NOT NULL,
            approver_admin_user_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            account_scope_json TEXT NOT NULL DEFAULT '[]',
            resource_scope_json TEXT NOT NULL DEFAULT '[]',
            time_scope_start TEXT,
            time_scope_end TEXT,
            approved_at TEXT,
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE INDEX IF NOT EXISTS ix_admin_plaintext_grants_requester_status
        ON admin_plaintext_grants(requester_admin_user_id, status, expires_at);

        CREATE INDEX IF NOT EXISTS ix_admin_plaintext_grants_status_created
        ON admin_plaintext_grants(status, created_at);

        CREATE TABLE IF NOT EXISTS phone_verifications (
            id TEXT PRIMARY KEY,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            verify_attempts INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT,
            verified_token TEXT,
            token_expires_at TEXT,
            token_consumed_at TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE INDEX IF NOT EXISTS ix_phone_verifications_phone_created
        ON phone_verifications(phone, created_at);

        CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
            service TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );

        CREATE TABLE IF NOT EXISTS faq_messages (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            author_name TEXT,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            moderation_status TEXT NOT NULL DEFAULT 'pending',
            moderation_reason TEXT,
            moderation_categories_json TEXT NOT NULL DEFAULT '[]',
            like_count INTEGER NOT NULL DEFAULT 0,
            reply_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(parent_id) REFERENCES faq_messages(id)
        );

        CREATE INDEX IF NOT EXISTS ix_faq_messages_parent_status_created
        ON faq_messages(parent_id, status, created_at);

        CREATE INDEX IF NOT EXISTS ix_faq_messages_status_created
        ON faq_messages(status, created_at);

        CREATE TABLE IF NOT EXISTS faq_message_likes (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            voter_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(message_id, voter_key),
            FOREIGN KEY(message_id) REFERENCES faq_messages(id)
        );

        CREATE INDEX IF NOT EXISTS ix_faq_message_likes_message
        ON faq_message_likes(message_id, created_at);
        """
    )
    # Ensure new columns exist on accounts (for DBs created before this change)
    _ensure_column(conn, "accounts", "status", "TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "accounts", "notes", "TEXT")
    _ensure_column(conn, "accounts", "daily_limit", "INTEGER")
    _ensure_column(conn, "accounts", "rpm_limit", "INTEGER")
    _ensure_column(conn, "sessions", "ended_at", "TEXT")
    _ensure_column(conn, "sessions", "close_reason", "TEXT")
    _ensure_column(conn, "sessions", "turn_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sessions", "business_day", "TEXT")
    _ensure_column(conn, "sessions", "session_summary", "TEXT")
    _ensure_column(conn, "sessions", "carryover_summary", "TEXT")
    _ensure_column(conn, "sessions", "summary_model", "TEXT")
    _ensure_column(conn, "sessions", "summary_prompt_version", "TEXT")
    _ensure_column(conn, "sessions", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "messages", "latency_ms", "INTEGER")
    _ensure_column(conn, "messages", "error", "TEXT")
    _ensure_column(conn, "binding_intents", "qr_data_url", "TEXT")
    _ensure_column(conn, "binding_intents", "manual_login_command", "TEXT")
    _ensure_column(conn, "binding_intents", "error", "TEXT")
    _ensure_column(conn, "binding_intents", "channel_account_id", "TEXT")
    # 多机接入:绑定 intent 创建时定好目标节点(登录会话钉死在该节点 = 该出口 IP)。
    _ensure_column(conn, "binding_intents", "node_id", "TEXT")
    _ensure_column(conn, "accounts", "onboarding_state", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(conn, "accounts", "onboarding_updated_at", "TEXT")
    _ensure_column(conn, "accounts", "is_debug", "INTEGER NOT NULL DEFAULT 0")
    # 多机接入:账号 → 节点归属(路由属性,不参与隔离)。见 multi_node_access_refactor.md §5。
    _ensure_column(conn, "accounts", "assigned_node_id", "TEXT")
    _ensure_column(conn, "outbound_messages", "product_category", "TEXT")
    _ensure_column(conn, "outbound_messages", "policy_version", "TEXT")
    _ensure_column(conn, "outbound_messages", "policy_reason", "TEXT")
    _ensure_column(conn, "outbound_messages", "scheduled_at", "TEXT")
    # 多机接入:出站按节点认领。node_id=enqueue 时解析的归属节点;claimed_at=sending 抢占时间(stale 回收)。
    _ensure_column(conn, "outbound_messages", "node_id", "TEXT")
    _ensure_column(conn, "outbound_messages", "claimed_at", "TEXT")
    _ensure_column(conn, "reminders", "recur_rule", "TEXT")
    _ensure_column(conn, "reminders", "sent_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "reminders", "last_sent_at", "TEXT")
    # 多机接入:接入节点登记表(MVP 仅登记 + 心跳),中心据 base_url push 登录。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_nodes (
            node_id TEXT PRIMARY KEY,
            base_url TEXT,
            egress_ip TEXT,
            last_heartbeat_at TEXT,
            session_count INTEGER NOT NULL DEFAULT 0,
            max_sessions INTEGER,
            status TEXT NOT NULL DEFAULT 'online',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        )
        """
    )
    # 节点出站认领索引:WHERE node_id=? AND status=? ORDER BY scheduled_at。
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_outbound_messages_node_dispatch
        ON outbound_messages(node_id, status, scheduled_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_invitations (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            invitation_text TEXT NOT NULL,
            title_items_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate',
            scheduled_at TEXT,
            invited_at TEXT,
            responded_at TEXT,
            expires_at TEXT,
            outbound_message_id INTEGER,
            trigger_message_id TEXT,
            tool_invocation_id INTEGER,
            source_task_id TEXT,
            policy_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id),
            FOREIGN KEY(tool_invocation_id) REFERENCES tool_invocations(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_content_invitations_status_due
        ON content_invitations(status, scheduled_at, expires_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_content_invitations_account_status
        ON content_invitations(account_id, status, updated_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_invitation_preferences (
            account_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'allowed',
            cooldown_until TEXT,
            last_feedback_at TEXT,
            feedback_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY(account_id, topic),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_content_invitation_preferences_cooldown
        ON content_invitation_preferences(account_id, status, cooldown_until)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_outbound_messages_account_category_date
        ON outbound_messages(account_id, product_category, quota_date, status)
        """
    )
    conn.execute(
        """
        UPDATE outbound_messages
        SET product_category = CASE
            WHEN source IN ('reminder', 'reminder_change_confirmation') THEN 'user_reminder'
            WHEN source IN ('commitment', 'account_check', 'heartbeat') THEN 'companion_followup'
            WHEN source = 'content_invitation' THEN 'content_invitation'
            WHEN source IN ('content_invitation_titles', 'content_invitation_feedback') THEN 'content_invitation_response'
            WHEN source = 'async_task_result' THEN 'task_result'
            ELSE product_category
        END
        WHERE product_category IS NULL
        """
    )
    conn.execute(
        "UPDATE accounts SET display_name = NULL WHERE display_name = ?",
        ("AI4ALL 助手",),
    )
    conn.execute(
        "UPDATE profiles SET display_name = NULL WHERE display_name = ?",
        ("AI4ALL 助手",),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_user_sessions (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_platform_user_sessions_token
        ON platform_user_sessions(token)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
            service TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS faq_messages (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            author_name TEXT,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            moderation_status TEXT NOT NULL DEFAULT 'pending',
            moderation_reason TEXT,
            moderation_categories_json TEXT NOT NULL DEFAULT '[]',
            like_count INTEGER NOT NULL DEFAULT 0,
            reply_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(parent_id) REFERENCES faq_messages(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_faq_messages_parent_status_created
        ON faq_messages(parent_id, status, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_faq_messages_status_created
        ON faq_messages(status, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS faq_message_likes (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            voter_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(message_id, voter_key),
            FOREIGN KEY(message_id) REFERENCES faq_messages(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_faq_message_likes_message
        ON faq_message_likes(message_id, created_at)
        """
    )


def _migration_0002_llm_runtime_config(conn: Connection) -> None:
    """Add global runtime LLM provider selection storage."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        )
        """
    )


def _migration_0003_user_meta(conn: Connection) -> None:
    """Add account-level user meta current and daily snapshot tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_user_meta (
            account_id TEXT PRIMARY KEY,
            registered_at TEXT NOT NULL,
            message_intensity_level INTEGER NOT NULL DEFAULT 0,
            companion_primary_type TEXT,
            companion_secondary_types TEXT NOT NULL DEFAULT '[]',
            companion_type_confidence REAL,
            companion_type_last_evaluated_at TEXT,
            companion_type_source TEXT NOT NULL DEFAULT 'auto',
            companion_type_expires_at TEXT,
            companion_type_reasoning TEXT,
            safety_risk_trigger_count_30d INTEGER NOT NULL DEFAULT 0,
            last_evaluated_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS account_user_meta_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            message_intensity_level INTEGER NOT NULL DEFAULT 0,
            companion_primary_type TEXT,
            companion_secondary_types TEXT NOT NULL DEFAULT '[]',
            companion_type_confidence REAL,
            companion_type_last_evaluated_at TEXT,
            companion_type_source TEXT NOT NULL DEFAULT 'auto',
            companion_type_expires_at TEXT,
            companion_type_reasoning TEXT,
            safety_risk_trigger_count_30d INTEGER NOT NULL DEFAULT 0,
            last_evaluated_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(account_id, snapshot_date),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_account_user_meta_daily_account_date
        ON account_user_meta_daily(account_id, snapshot_date DESC);
        """
    )


def _migration_0004_account_profile_files(conn: Connection) -> None:
    """账号级 profile 文件内容入库（厚节点改造 P2，见 thick_node_postgres_refactor.md §5）。

    把 SOUL/IDENTITY/USER/MEMORY/legacy user_profile.md 及 memory/YYYY-MM-DD.md daily notes
    的内容从本地文件系统收敛进此表，作为唯一真相、供多节点共享读取。filename 为账号 profile
    目录内的相对路径（如 'SOUL.md'、'memory/2026-06-20.md'），(account_id, filename) 为主键，
    其前缀天然覆盖 account_id 范围扫描（列举/wipe 用），无需额外索引。不设 DB 级 FK：账号隔离
    由 app 层 WHERE account_id 保证，且本表为无子表的叶子表，省去建表/删除顺序约束。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_profile_files (
            account_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            PRIMARY KEY (account_id, filename)
        );
        """
    )


def _migration_0005_rpm_hits(conn: Connection) -> None:
    """RPM 滑动窗口命中记录表（厚节点改造 P3，见 thick_node_postgres_refactor.md §5）。

    hit_at 存 Unix epoch 秒（REAL），方便在 Python 侧与 time.time() 直接做算术比较。
    check_rpm 在单一事务内清过期行、计数、未达限则插入，无进程内 deque，
    多节点共享同一 PG 库时天然隔离正确。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rpm_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            hit_at DOUBLE PRECISION NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rpm_hits_account_hit ON rpm_hits(account_id, hit_at);
        """
    )


def _migration_0006_merge_reactivation_categories(conn: Connection) -> None:
    """拉活分类合并入 companion_followup / content_invitation。

    OutboundCategory 从 8 个收敛到 5 个：reactivation_topic_followup 并入
    companion_followup，reactivation_content_invitation 并入 content_invitation。
    历史 outbound_messages 行的 product_category 一并迁移，使新的按分类计数口径
    （get_outbound_daily_usage/count_outbound_in_window）覆盖这些行。拉活来源仍由
    metadata_json.reactivation 标志识别，不依赖 product_category，故迁移不影响拉活节流。
    幂等：旧值不存在时 UPDATE 影响 0 行。
    """
    conn.execute(
        """
        UPDATE outbound_messages
        SET product_category = 'companion_followup'
        WHERE product_category = 'reactivation_topic_followup'
        """
    )
    conn.execute(
        """
        UPDATE outbound_messages
        SET product_category = 'content_invitation'
        WHERE product_category = 'reactivation_content_invitation'
        """
    )


def _migration_0007_merge_reactivation_settings_keys(conn: Connection) -> None:
    """把存量用户偏好里的旧分类键并入新分类（配合 0006 的消息行迁移）。

    8→5 收敛后，proactive_message_settings 的两个 JSON 列里可能残留旧键：
      - category_settings_json: reactivation_topic_followup → companion_followup,
        reactivation_content_invitation → content_invitation
      - frequency_json: 旧 reactivation 桶 → content_invitation
    0006 只迁了 outbound_messages.product_category，未动偏好行，导致升级后用户此前
    设置的关闭/频次偏好被新口径静默忽略。此迁移按键改名补齐。

    合并策略：目标新键已存在时保留现有值（不被旧键覆盖），仅丢弃旧键，避免回退用户
    后来用新键设置的偏好。幂等：无旧键时不产生变更。
    """
    cat_rename = {
        "reactivation_topic_followup": "companion_followup",
        "reactivation_content_invitation": "content_invitation",
    }
    freq_rename = {"reactivation": "content_invitation"}

    def _apply_rename(raw: Optional[str], rename: Dict[str, str]):
        if not raw:
            return None, False
        try:
            obj = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None, False
        if not isinstance(obj, dict):
            return None, False
        if not (set(obj.keys()) & set(rename.keys())):
            return None, False  # 无旧键 → 不动（幂等）
        # 两遍：先拷所有非旧键（保证新键值优先、不被旧键覆盖），再用旧键仅补缺失目标。
        # 单遍按 JSON 原始顺序会在旧键排前时让旧值先落、新值被丢，故必须分两遍。
        new_obj: Dict[str, Any] = {
            key: value for key, value in obj.items() if key not in rename
        }
        for key, value in obj.items():
            if key not in rename:
                continue
            target = rename[key]
            if target in new_obj:
                continue  # 目标新键已存在 → 保留现值，丢弃旧键
            new_obj[target] = value
        return json.dumps(new_obj, ensure_ascii=False), True

    rows = conn.execute(
        "SELECT account_id, category_settings_json, frequency_json "
        "FROM proactive_message_settings"
    ).fetchall()
    for row in rows:
        account_id = row["account_id"]
        new_cat, cat_changed = _apply_rename(row["category_settings_json"], cat_rename)
        new_freq, freq_changed = _apply_rename(row["frequency_json"], freq_rename)
        if not (cat_changed or freq_changed):
            continue
        # 一条 UPDATE 写两列；未变更的列写回原值（同值回写在 SQLite/PG 均为 no-op）。
        conn.execute(
            "UPDATE proactive_message_settings "
            "SET category_settings_json = ?, frequency_json = ? WHERE account_id = ?",
            (
                new_cat if cat_changed else row["category_settings_json"],
                new_freq if freq_changed else row["frequency_json"],
                account_id,
            ),
        )


def _migration_0008_relationship_state(conn: Connection) -> None:
    """关系阶段与 Agent 需求满足状态字段落库（见 docs/architecture/products/zhaoxi/relationship_state_design.md §3/§4）。

    给 account_user_meta（当前快照）和 account_user_meta_daily（每日历史）同步补四列。
    仅落结构与默认值，确定性/LLM 更新由后续阶段接入。各列均为 NOT NULL + 常量默认，
    存量行回填为默认值（历史快照不可追溯，属已知局限）。
    """
    for table in ("account_user_meta", "account_user_meta_daily"):
        _ensure_column(conn, table, "relationship_stage", "TEXT NOT NULL DEFAULT 'icebreaking'")
        _ensure_column(conn, table, "agent_need_survival_status", "TEXT NOT NULL DEFAULT 'cooling'")
        _ensure_column(conn, table, "agent_need_trust_status", "TEXT NOT NULL DEFAULT 'building'")
        _ensure_column(conn, table, "agent_need_growth_status", "TEXT NOT NULL DEFAULT 'not_started'")


def _migration_0009_messages_account_id_index(conn: Connection) -> None:
    """补 messages(account_id, id) 索引，支撑短期历史热点查询。

    list_recent_messages_for_account 走 `WHERE account_id=? ORDER BY id DESC LIMIT N`，
    既有索引 ux_messages_account_message(account_id, message_id) 因 message_id 非有序无法服务
    该 ORDER BY id，会退化成"扫该账号全部消息再排序"。本索引让其走有序覆盖、O(LIMIT) 取回。
    纯增益、幂等，SQLite/PG 双后端通用。
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_account_id ON messages(account_id, id)"
    )


def _migration_0010_sessions_rolling_summary(conn: Connection) -> None:
    """sessions 增滚动摘要两列（Token 压力 intra-session 摘要，灰度默认关）。

    rolling_summary：本会话已滑出窗口的头部消息的滚动摘要文本。
    rolling_summary_upto_id：水位线，标记摘要已覆盖到哪条 message.id，避免重复摘要 / 与 kept
    window 重叠。见 docs/architecture/agent-runtime/context_window_token_budget_design.md §6。
    """
    _ensure_column(conn, "sessions", "rolling_summary", "TEXT")
    _ensure_column(conn, "sessions", "rolling_summary_upto_id", "INTEGER")


def _migration_0011_proactive_global_candidates(conn: Connection) -> None:
    """全局（无主）主动消息候选池——首个真·全局召回（近期热点 hot_topic）的落地表。

    刻意**无 account_id**：存的是"所有账号只读共享的无主候选池"（如近 24h 热点主题），
    不是任何账号的数据，因此不受"按 account_id 隔离"这条核心不变量约束——这是该不变量
    唯一的、显式的例外（详见
    app/products/zhaoxi/proactive/store/global_candidates.py 模块说明）。池→账号
    的绑定发生在**选择层**：每账号 LLM 打分选中 top1 时才盖上 account_id，写进该账号自己的
    reactivation 候选（proactive_account_state.metadata）。本表本身绝不写任何账号维度数据。

    UNIQUE(kind, generated_date, dedupe_key) 天然实现"当日同主题只入一次 + 跨天历史去重依据"；
    expires_at 存北京 naive 时间字符串，读活跃池时按 expires_at > now 过滤（被动 TTL，不主动清表）。
    strftime 默认值 / AUTOINCREMENT / INSERT OR IGNORE 由 _backend 方言翻译层适配 PG，双后端通用。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proactive_global_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            topic TEXT,
            text TEXT NOT NULL,
            generated_date TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_global_candidates_kind_date_key
            ON proactive_global_candidates(kind, generated_date, dedupe_key);
        CREATE INDEX IF NOT EXISTS ix_global_candidates_kind_expires
            ON proactive_global_candidates(kind, expires_at);
        """
    )


def _migration_0012_agent_mission(conn: Connection) -> None:
    """使命子系统：账号级使命分配 + 记录的瞬间（agent_mission_and_orchestration_design.md §3）。

    account_mission 一账号一行，mission_id 只写一次——不可更改性由 app 层"没有 update
    函数"保证（见 products/zhaoxi/infrastructure/persistence/mission.py），DB 层只用
    INSERT ... ON CONFLICT(account_id)
    DO NOTHING 兜底防覆盖，不是唯一防线。

    mission_moments 是追加型内容集合，进度 = COUNT(*)（派生量，不另建计数字段，避免
    与真实内容漂移）；mission_id 冗余存储在每条瞬间上，钉住记录时的使命版本，不受未来
    模板措辞调整影响。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_mission (
            account_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS mission_moments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id TEXT,
            message_id TEXT,
            recorded_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE INDEX IF NOT EXISTS ix_mission_moments_account
            ON mission_moments(account_id, mission_id);
        """
    )


def _migration_0013_campaign_codes(conn: Connection) -> None:
    """内容创意营销活码：campaign_codes（可编辑配置）+ account_campaign_attribution（注册时策略快照）。

    见 docs/architecture/products/zhaoxi/campaign_codes_technical_design.md §1。account_campaign_attribution
    存的是注册时刻解析出的策略快照，不实时 join campaign_codes——活码后续被编辑/下线不影响
    已归因账号，只影响新注册；这与 account_mission「一经分配不可更改」的不可变语义不同，
    这里可变的是 campaign_codes 本身，不可变的只是归因快照这张表。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS campaign_codes (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            campaign_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            valid_from TEXT,
            expires_at TEXT,
            mission_id TEXT,
            onboarding_script_variant TEXT,
            soul_preset_key TEXT,
            used_count INTEGER NOT NULL DEFAULT 0,
            created_by_admin_user_id TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );
        CREATE INDEX IF NOT EXISTS ix_campaign_codes_status ON campaign_codes(status);

        CREATE TABLE IF NOT EXISTS account_campaign_attribution (
            account_id TEXT PRIMARY KEY,
            campaign_code TEXT NOT NULL,
            mission_id TEXT,
            onboarding_script_variant TEXT,
            soul_preset_key TEXT,
            attributed_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS ix_account_campaign_attribution_code ON account_campaign_attribution(campaign_code);
        """
    )


def _migration_0019_campaign_ai_name_preset(conn: Connection) -> None:
    """活码新增"AI 称呼（名字）"预设列 ai_name_preset。

    campaign_codes（可编辑配置）与 account_campaign_attribution（注册快照）各加一列：
    活码设定该值时，建号即把 AI 名字写入 IDENTITY.md，onboarding 不再问用户"想怎么称呼 AI"
    （见 campaign_codes_technical_design.md §4.4）。与 soul_preset_key 并列的第四个策略旋钮，
    双后端通用（SQLite/PG 均支持 ADD COLUMN）。

    编号说明：14–18 曾被未合并实验分支预留，现明确保留为空号/废弃，不再回填。
    迁移框架按 `version > MAX(已应用)` 判定，版本号不要求连续；本迁移固定为 19，
    后续新增迁移从 20 继续，避免已应用 19 的库再遇到 14–18 时被静默跳过。
    """
    _ensure_column(conn, "campaign_codes", "ai_name_preset", "TEXT")
    _ensure_column(conn, "account_campaign_attribution", "ai_name_preset", "TEXT")


def _migration_0020_campaign_visits(conn: Connection) -> None:
    """营销活码落地页曝光埋点：campaign_visits（匿名 PV/UV，账号创建之前）。

    见 docs/architecture/products/zhaoxi/campaign_funnel_analytics_technical_design.md §3.1。这是漏斗 S0
    曝光层：用户点营销链接进落地页时，前端上报 campaign_code + 匿名 visitor_token。
    刻意 campaign-scoped、无 account_id——曝光发生在注册建号之前，此时没有账号；表内不含
    任何用户正文/PII，故不违反账号隔离不变量（该不变量约束的是账号级用户数据）。
    S1–S5 漏斗仍以 account_campaign_attribution 按 account_id 归组，与本表不 join。
    双后端通用 DDL（AUTOINCREMENT/TEXT/默认值 SQLite+PG 均支持）。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS campaign_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_code TEXT NOT NULL,
            visitor_token TEXT,
            page TEXT,
            referrer TEXT,
            user_agent TEXT,
            visit_date TEXT NOT NULL,
            event_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        );
        CREATE INDEX IF NOT EXISTS ix_campaign_visits_code_date ON campaign_visits(campaign_code, visit_date);
        """
    )


def _migration_0021_dynamic_reminders(conn: Connection) -> None:
    """动态提醒（例行简报）：提醒新增履约方式 + 履约留痕表。

    见 docs/architecture/products/zhaoxi/dynamic_reminder_scheduled_content_design.md。提醒分两种履约：
    - fulfillment='fixed'（默认，现状零回归）：到点发 reminders.text 固定文案；
    - fulfillment='dynamic'：到点跑一次「无用户输入的合成轮次」（专用 prompt + 可配工具集），
      检索并生成一条带来源的内容再发。dynamic 专属参数（topic/max_items/tool_policy/上次成功
      时间等）存 content_meta_json，避免污染 text 的「固定文案」语义。

    reminder_content_runs 记录每次 dynamic 履约：UNIQUE(reminder_id, scheduled_for) 是防
    「同一周期重复搜索/重复发送」的第一道闸；status 覆盖 pending/running/enqueued/sent/
    skipped/failed，其中 enqueued 专为远程账号（出站 pull 由归属节点完成）设，配合对账翻终态。
    account_id 冗余存储以满足账号隔离查询。
    """
    _ensure_column(conn, "reminders", "fulfillment", "TEXT NOT NULL DEFAULT 'fixed'")
    _ensure_column(conn, "reminders", "content_meta_json", "TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reminder_content_runs (
            id TEXT PRIMARY KEY,
            reminder_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            generated_text TEXT,
            outbound_message_id INTEGER,
            search_ok INTEGER NOT NULL DEFAULT 0,
            search_trace_json TEXT,
            error TEXT,
            metadata_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_reminder_content_runs_reminder_sched
            ON reminder_content_runs(reminder_id, scheduled_for);
        CREATE INDEX IF NOT EXISTS ix_reminder_content_runs_status
            ON reminder_content_runs(status);
        CREATE INDEX IF NOT EXISTS ix_reminder_content_runs_account
            ON reminder_content_runs(account_id, created_at);
        """
    )


def _migration_0022_account_app_id(conn: Connection) -> None:
    """App(产品)层身份打底:accounts / account_owner_bindings 落 app_id 列。

    见 docs/plans/products/zhaoxi/app_account_convergence_and_channel_persona.md §9。App 是"分组单元"
    (一手机号 × 一 App = 一 account);当前全部资产属唯一 App「朝夕相伴」,故存量 backfill 为
    'zhaoxi'。app_id 是账号不可变属性(账号一旦属于某 App 永不改),因此把它冗余到
    account_owner_bindings 是安全的(创建时写入、永不漂移),使"每 (platform_user, app) 一个
    active 账号"的收敛不变量能用单条部分唯一索引(m0023)表达。

    双后端通用:ADD COLUMN ... NOT NULL DEFAULT 会自动把存量行回填为 'zhaoxi'(SQLite/PG 均支持)。
    owner_bindings.app_id 再用相关子查询从 accounts 精确对齐(当前均为 zhaoxi,为混合 App 未来预留正确性)。
    """
    _ensure_column(conn, "accounts", "app_id", "TEXT NOT NULL DEFAULT 'zhaoxi'")
    _ensure_column(conn, "account_owner_bindings", "app_id", "TEXT NOT NULL DEFAULT 'zhaoxi'")
    # 用账号真实 app_id 对齐 owner_binding 冗余列(相关子查询,SQLite/PG 通用)。
    conn.execute(
        """
        UPDATE account_owner_bindings
        SET app_id = (
            SELECT a.app_id FROM accounts a WHERE a.id = account_owner_bindings.account_id
        )
        WHERE EXISTS (
            SELECT 1 FROM accounts a WHERE a.id = account_owner_bindings.account_id
        )
        """
    )


def _migration_0023_owner_binding_active_unique(conn: Connection) -> None:
    """A 收敛的 DB 层唯一保证:每个 (platform_user, app) 最多一个 active owner_binding。

    见 §9.3 A2。部分唯一索引仅约束 status='active' 行,归档/停用行不占名额。SQLite/PG 均支持
    带 WHERE 的部分索引(范式同 ux_messages_account_message)。当前单 App 下 app_id 恒为 'zhaoxi',
    (platform_user_id, app_id) 索引行为等价于 (platform_user_id),但形态已是多 App 终态。
    生产 S1 审计已确认无重复(0 多账号 user),建索引不会因存量冲突失败。
    """
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_owner_binding_active_user_app
        ON account_owner_bindings(platform_user_id, app_id)
        WHERE status='active'
        """
    )


def _migration_0024_rename_channel_app_to_native(conn: Connection) -> None:
    """channel 命名 'app' → 'native':区分 App(产品层) 与 channel(传输层)。

    见 §9.2 Q5 / §9.3 B。CHANNEL_APP 常量值由 'app' 改为 'native'(app/channels.py)后,存量
    channel_bindings 中的 'app' 值需一并迁移,否则旧绑定解析不到。生产审计:全库仅 channel_bindings
    有 1 行 'app',其余含 channel 列的表(accounts/binding_intents/outbound_messages/reminders)均无。
    幂等:无 'app' 行时 UPDATE 影响 0 行。
    """
    conn.execute("UPDATE channel_bindings SET channel='native' WHERE channel='app'")


def _migration_0025_wallet_unique_platform_user(conn: Connection) -> None:
    """D-14 M1-1：钱包唯一性从 account_id 上迁到 platform_user（一真人一 active 钱包）。

    见 ADR docs/architecture/products/mingchan/companion_world_3_0_refactor_design.md §D-14。多居民（朝夕相伴）
    上线前，把 entitlement_wallets 的「一 account 一钱包」上迁为「一真人一钱包、全部居民共用
    一份余额」。本迁移刻意**保留** UNIQUE(account_id)：billing 改按 platform_user
    get-or-create 后永不会为同一真人插入第二个钱包行，account_id 事实上仍唯一、保留无害，据此
    完全避开 SQLite 表重建 / PG DROP CONSTRAINT 的高风险后端分叉（决策见 §D-14 item3）。只做：

      1) 合并存量多钱包老用户（建号允许每真人 ≤10 account，历史上可能已有多钱包）：
         选主钱包 = 该真人「最早 active binding 对应 account」的 active 钱包；余额求和入主钱包、
         ledger/cost_events.wallet_id 归并到主、其余钱包置 status='merged'（保留审计，不删行避免 FK 冲突）。
      2) 加局部唯一索引 ux_entitlement_wallets_user_active（合并后可满足；SQLite/PG 同一 DDL，无需分支）。

    可重复执行（幂等）：再跑时每真人仅 1 active 钱包，不再进入合并分支。生产执行前须先跑
    scripts/precheck_wallet_migration.py（四条阻断全过）；本迁移假定 primary 有定义、无歧义/
    孤儿/漂移，万一取不到主钱包则告警跳过、不静默改数。
    """
    multi_wallet_users = conn.execute(
        """
        SELECT platform_user_id
        FROM entitlement_wallets
        WHERE status = 'active'
        GROUP BY platform_user_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in multi_wallet_users:
        user_id = row["platform_user_id"]
        # 选主：该真人「最早 active binding 对应 account」的 active 钱包（冻结规则，与预检一致）。
        primary = conn.execute(
            """
            SELECT w.id AS id
            FROM entitlement_wallets w
            JOIN account_owner_bindings b
              ON b.account_id = w.account_id
             AND b.status = 'active'
             AND b.platform_user_id = w.platform_user_id
            WHERE w.status = 'active' AND w.platform_user_id = ?
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if primary is None:
            logger.warning(
                "m0025 skip merge: primary wallet undefined for platform_user=%s "
                "(precheck should have blocked this)",
                user_id,
            )
            continue
        primary_id = primary["id"]
        # 求和须在置 merged 之前（此刻全部候选钱包仍 active）。
        total = conn.execute(
            """
            SELECT COALESCE(SUM(balance_shell_micros), 0) AS total
            FROM entitlement_wallets
            WHERE status = 'active' AND platform_user_id = ?
            """,
            (user_id,),
        ).fetchone()["total"]
        conn.execute(
            """
            UPDATE entitlement_ledger SET wallet_id = ?
            WHERE wallet_id IN (
                SELECT id FROM entitlement_wallets
                WHERE status = 'active' AND platform_user_id = ? AND id <> ?
            )
            """,
            (primary_id, user_id, primary_id),
        )
        conn.execute(
            """
            UPDATE cost_events SET wallet_id = ?
            WHERE wallet_id IN (
                SELECT id FROM entitlement_wallets
                WHERE status = 'active' AND platform_user_id = ? AND id <> ?
            )
            """,
            (primary_id, user_id, primary_id),
        )
        conn.execute(
            """
            UPDATE entitlement_wallets SET status = 'merged'
            WHERE status = 'active' AND platform_user_id = ? AND id <> ?
            """,
            (user_id, primary_id),
        )
        conn.execute(
            "UPDATE entitlement_wallets SET balance_shell_micros = ? WHERE id = ?",
            (int(total), primary_id),
        )
    # 合并后每真人至多 1 active 钱包，局部唯一索引可满足。
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_wallets_user_active
        ON entitlement_wallets(platform_user_id)
        WHERE status = 'active';
        """
    )


def _migration_0026_daily_usage_platform_user(conn: Connection) -> None:
    """D-09 M1-3/M1-4：daily 配额计数键从 account_id 上迁到 platform_user（一真人一套配额）。

    见 ADR docs/architecture/products/mingchan/companion_world_3_0_refactor_design.md §D-09。多居民（朝夕相伴）
    上线前，把 daily_usage 的「一 account 一套额度」上迁为「一真人一套、全部居民共享」。同 D-14
    钱包上迁刻意**保留** UNIQUE(account_id,date)：daily 三函数改按 platform_user get-or-create
    后每 (真人,date) 至多一行、account_id = 当日首个号，旧唯一仍满足，据此完全避开 SQLite 表重建 /
    PG DROP CONSTRAINT 的后端分叉（决策见 §D-09 落地说明）。只做：

      1) 新增 platform_user_id 列（无 FK，容 fallback 值）+ 从最早 active binding 回填。
      2) 合并同 (真人,date) 多行（历史多号可能各有当日行）：message_count 求和入主行（MIN(id)）、
         删其余行（daily_usage 无被引用，直接删，无需 status 保留）。须在建唯一索引前。
      3) 加唯一索引 ux_daily_usage_user_date(platform_user_id, date)（合并后可满足；SQLite/PG
         同一 DDL，NULL 行互不相等不冲突，无需分支）。

    可重复执行（幂等）：再跑时每 (真人,date) 仅 1 行、回填只补 NULL、索引 IF NOT EXISTS。daily/rpm
    是瞬态计数（无历史余额可损坏），故无需 precheck（owner 解析不变式已由 D-14 precheck 作同一 M1
    发布闸覆盖）。无 active binding 的孤儿行**回退 platform_user_id=account_id**（镜像运行时
    _resolve_quota_subject 的孤儿回退），保证写路径 ON CONFLICT(platform_user_id,date) 命中同一行、
    不与保留的 UNIQUE(account_id,date) 冲突（否则孤儿号次日 increment 触 IntegrityError / 读取静默清零）。
    """
    # 1) 加列（幂等）+ 从最早 active binding 回填（冻结解析规则，与 get_platform_user_id_for_account
    #    及 D-14 预检一致：ORDER BY b.created_at ASC, b.id ASC LIMIT 1）。
    _ensure_column(conn, "daily_usage", "platform_user_id", "TEXT")
    conn.execute(
        """
        UPDATE daily_usage
        SET platform_user_id = (
            SELECT b.platform_user_id
            FROM account_owner_bindings b
            WHERE b.account_id = daily_usage.account_id
              AND b.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
        )
        WHERE platform_user_id IS NULL
        """
    )
    # 1b) 无 active binding 的孤儿行：回退 platform_user_id=account_id（同 _resolve_quota_subject 的
    #     孤儿回退）。否则该行 platform_user_id 恒为 NULL，运行时 increment 以 subject=account_id 写入
    #     时 ON CONFLICT(platform_user_id,date) 命不中 NULL 行、转而撞 UNIQUE(account_id,date) 无 arbiter
    #     处理 → IntegrityError；读取按 platform_user_id 亦查不到 → 配额静默清零。幂等：仅补 NULL。
    conn.execute(
        "UPDATE daily_usage SET platform_user_id = account_id WHERE platform_user_id IS NULL"
    )
    # 2) 合并同 (真人,date) 多行（多号≈0，near-no-op；索引安全必需，须在建索引前）。
    dup_groups = conn.execute(
        """
        SELECT platform_user_id, date
        FROM daily_usage
        WHERE platform_user_id IS NOT NULL
        GROUP BY platform_user_id, date
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for grp in dup_groups:
        pu = grp["platform_user_id"]
        date = grp["date"]
        primary_id = conn.execute(
            "SELECT MIN(id) AS id FROM daily_usage WHERE platform_user_id = ? AND date = ?",
            (pu, date),
        ).fetchone()["id"]
        total = conn.execute(
            "SELECT COALESCE(SUM(message_count), 0) AS total "
            "FROM daily_usage WHERE platform_user_id = ? AND date = ?",
            (pu, date),
        ).fetchone()["total"]
        conn.execute(
            "DELETE FROM daily_usage WHERE platform_user_id = ? AND date = ? AND id <> ?",
            (pu, date, primary_id),
        )
        conn.execute(
            "UPDATE daily_usage SET message_count = ? WHERE id = ?",
            (int(total), primary_id),
        )
    # 3) 合并后每 (真人,date) 至多 1 行，唯一索引可满足。
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_usage_user_date
        ON daily_usage(platform_user_id, date);
        """
    )


def _migration_0027_daily_quota_reservations(conn: Connection) -> None:
    """D-09 下半刀：daily 配额原子预占的存储载体（每 reservation 一行 + TTL）。

    见 ADR docs/architecture/products/mingchan/companion_world_3_0_refactor_design.md §D-09（item 3–6）+
    P1 §2.7（锁序 L3 = pg_advisory_xact_lock('quota:'||platform_user_id)）。多居民聚合到真人后，
    turn_service 的「读—处理—+1」有 TOCTOU；本表承载「预占（reserve）→确认(confirm)/回滚(rollback)」：
    reserve 在 advisory 锁下按 message_count + 活跃 reservation 数校验 cap 后插一行；confirm/rollback/
    TTL 一律 DELETE，故行只在「已预占未结算」态存在、不设 status 列。daily_usage.message_count 语义
    不变（=已确认成功计费的计数），展示口径零改动。

    空表迁移、无回填、幂等（IF NOT EXISTS）。platform_user_id 无 FK（容孤儿号 fallback=account_id）。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_quota_reservations (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            account_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_qres_pu_date ON daily_quota_reservations(platform_user_id, date);
        CREATE INDEX IF NOT EXISTS ix_qres_expires ON daily_quota_reservations(expires_at);
        """
    )


def _migration_0031_platform_user_quota_overrides(conn: Connection) -> None:
    """D-09：把 daily/RPM override 的 canonical 来源上迁到真人。

    仅自动回填所有归属 account（owner binding + World resident）取值完全一致的非空
    override；冲突值保留在 accounts 供运行时兼容 resolver 以最严格值收口，避免迁移时
    静默改变配额。后续 Admin 写入由 accounts.update_account 统一写真人并传播副本。
    """
    _ensure_column(conn, "platform_users", "daily_limit", "INTEGER")
    _ensure_column(conn, "platform_users", "rpm_limit", "INTEGER")
    users = conn.execute("SELECT id FROM platform_users ORDER BY id").fetchall()
    for user in users:
        platform_user_id = str(user["id"])
        rows = conn.execute(
            """
            SELECT a.daily_limit, a.rpm_limit
            FROM accounts a
            WHERE a.id IN (
                SELECT b.account_id
                FROM account_owner_bindings b
                WHERE b.platform_user_id = ? AND b.status = 'active'
                UNION
                SELECT r.runtime_account_id
                FROM universe_residents r
                JOIN universes u ON u.id = r.universe_id
                WHERE u.owner_platform_user_id = ?
                  AND r.runtime_account_id IS NOT NULL
            )
            """,
            (platform_user_id, platform_user_id),
        ).fetchall()
        daily_values = {row["daily_limit"] for row in rows}
        rpm_values = {row["rpm_limit"] for row in rows}
        daily = next(iter(daily_values)) if len(daily_values) == 1 else None
        rpm = next(iter(rpm_values)) if len(rpm_values) == 1 else None
        if daily is not None or rpm is not None:
            conn.execute(
                "UPDATE platform_users SET daily_limit = ?, rpm_limit = ?, "
                "updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) "
                "WHERE id = ?",
                (daily, rpm, platform_user_id),
            )


def _migration_0032_rpm_hit_double_precision(conn: Connection) -> None:
    """PG：把 Unix epoch 命中时间从单精度 REAL 升级为双精度。

    PostgreSQL REAL 只有 24 位尾数，2026 epoch 的量化步长约 128 秒，会让 60 秒 RPM
    窗口随机误删刚写入的命中；SQLite REAL 本就是 8 字节浮点，无需迁移。
    """
    if is_postgres():
        conn.execute(
            "ALTER TABLE rpm_hits ALTER COLUMN hit_at TYPE DOUBLE PRECISION "
            "USING hit_at::double precision"
        )


def _migration_0036_repair_account_app_id(conn: Connection) -> None:
    """修复旧分支迁移编号碰撞导致的账号 App schema 漂移。

    部分已升级数据库曾在旧 Companion World 分支把 22–24 登记为另一组迁移，
    因而合并后的 m0022–m0024 会被版本表误判为已执行。使用新的前向版本幂等
    重放三步，避免删除或改写历史 migration 记录。
    """
    _migration_0022_account_app_id(conn)
    _migration_0023_owner_binding_active_unique(conn)
    _migration_0024_rename_channel_app_to_native(conn)


def _migration_0037_product_memberships(conn: Connection) -> None:
    """建立产品 membership 规范锚，并把存量真人回填为 active zhaoxi 成员。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS product_memberships (
            platform_user_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'disabled')),
            daily_limit INTEGER,
            rpm_limit INTEGER,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            UNIQUE(platform_user_id, app_id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_product_memberships_app_status
        ON product_memberships(app_id, status);
        """
    )
    conn.execute(
        """
        INSERT INTO product_memberships(
            platform_user_id, app_id, status, daily_limit, rpm_limit, updated_at
        )
        SELECT pu.id, 'zhaoxi', 'active', pu.daily_limit, pu.rpm_limit,
               strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        FROM platform_users pu
        WHERE NOT EXISTS (
            SELECT 1 FROM product_memberships pm
            WHERE pm.platform_user_id=pu.id AND pm.app_id='zhaoxi'
        )
        """
    )


def _migration_0038_session_app_id(conn: Connection) -> None:
    """把 opaque platform session 固定到服务端签发的产品 audience。"""
    _ensure_column(
        conn,
        "platform_user_sessions",
        "app_id",
        "TEXT NOT NULL DEFAULT 'zhaoxi'",
    )
    conn.execute(
        "UPDATE platform_user_sessions SET app_id='zhaoxi' "
        "WHERE app_id IS NULL OR app_id=''"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_platform_user_sessions_app_user
        ON platform_user_sessions(app_id, platform_user_id)
        """
    )


def _migration_0039_billing_app_id_expand(conn: Connection) -> None:
    """为计费子树补产品归属，并收敛存量重复 active subscription。

    这是 MP-02 expand 阶段：默认值保证尚未下线的旧 writer 仍只会写入 zhaoxi；
    ledger/cost 的冗余列再从其权威 wallet/account 对齐，供 contract 前 reconcile。
    """
    for table in (
        "subscriptions",
        "entitlement_wallets",
        "entitlement_ledger",
        "cost_events",
    ):
        _ensure_column(conn, table, "app_id", "TEXT NOT NULL DEFAULT 'zhaoxi'")

    # 钱包的创建来源账号是产品归属锚；流水必须与钱包一致；cost 优先跟钱包，
    # 无 wallet 的平台成本记录则跟 account。所有存量当前均应收敛到 zhaoxi。
    conn.execute(
        """
        UPDATE entitlement_wallets
        SET app_id = (
            SELECT a.app_id FROM accounts a
            WHERE a.id = entitlement_wallets.account_id
        )
        WHERE EXISTS (
            SELECT 1 FROM accounts a
            WHERE a.id = entitlement_wallets.account_id
              AND a.app_id IS NOT NULL
              AND a.app_id <> ''
        )
        """
    )
    conn.execute(
        """
        UPDATE entitlement_ledger
        SET app_id = (
            SELECT w.app_id FROM entitlement_wallets w
            WHERE w.id = entitlement_ledger.wallet_id
        )
        WHERE EXISTS (
            SELECT 1 FROM entitlement_wallets w
            WHERE w.id = entitlement_ledger.wallet_id
              AND w.app_id IS NOT NULL
              AND w.app_id <> ''
        )
        """
    )
    conn.execute(
        """
        UPDATE cost_events
        SET app_id = COALESCE(
            (SELECT w.app_id FROM entitlement_wallets w WHERE w.id = cost_events.wallet_id),
            (SELECT a.app_id FROM accounts a WHERE a.id = cost_events.account_id),
            app_id
        )
        """
    )

    # 状态历史模型：正常 cancelled/expired 历史原样保留；每个产品只把重复 active
    # 中较旧的行标成 superseded，稳定排序规则与 ADR 一致。
    duplicate_groups = conn.execute(
        """
        SELECT platform_user_id, app_id
        FROM subscriptions
        WHERE status = 'active'
        GROUP BY platform_user_id, app_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in duplicate_groups:
        active_rows = conn.execute(
            """
            SELECT id
            FROM subscriptions
            WHERE platform_user_id = ? AND app_id = ? AND status = 'active'
            ORDER BY updated_at DESC, id DESC
            """,
            (group["platform_user_id"], group["app_id"]),
        ).fetchall()
        for stale in active_rows[1:]:
            conn.execute(
                """
                UPDATE subscriptions
                SET status = 'superseded',
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND status = 'active'
                """,
                (stale["id"],),
            )

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_subscriptions_user_app_updated
        ON subscriptions(platform_user_id, app_id, updated_at);
        CREATE INDEX IF NOT EXISTS ix_entitlement_wallets_user_app_status
        ON entitlement_wallets(platform_user_id, app_id, status);
        CREATE INDEX IF NOT EXISTS ix_entitlement_ledger_user_app_created
        ON entitlement_ledger(platform_user_id, app_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_cost_events_account_app_created
        ON cost_events(account_id, app_id, created_at);
        """
    )


def _billing_contract_violation_counts(conn: Connection) -> Dict[str, int]:
    """返回 m0040 contract 的聚合阻断计数，不读取或输出业务明细。"""
    checks = {
        "null_app_id": """
            SELECT id FROM subscriptions WHERE app_id IS NULL OR app_id=''
            UNION ALL SELECT id FROM entitlement_wallets WHERE app_id IS NULL OR app_id=''
            UNION ALL SELECT id FROM entitlement_ledger WHERE app_id IS NULL OR app_id=''
            UNION ALL SELECT id FROM cost_events WHERE app_id IS NULL OR app_id=''
        """,
        "duplicate_active_subscription": """
            SELECT platform_user_id, app_id
            FROM subscriptions WHERE status='active'
            GROUP BY platform_user_id, app_id HAVING COUNT(*) > 1
        """,
        "duplicate_active_wallet": """
            SELECT platform_user_id, app_id
            FROM entitlement_wallets WHERE status='active'
            GROUP BY platform_user_id, app_id HAVING COUNT(*) > 1
        """,
        "subscription_membership_drift": """
            SELECT s.id
            FROM subscriptions s
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=s.platform_user_id AND pm.app_id=s.app_id
            WHERE pm.platform_user_id IS NULL
        """,
        "wallet_scope_drift": """
            SELECT w.id
            FROM entitlement_wallets w
            LEFT JOIN accounts a ON a.id=w.account_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=w.platform_user_id AND pm.app_id=w.app_id
            WHERE a.id IS NULL OR a.app_id<>w.app_id OR pm.platform_user_id IS NULL
        """,
        "ledger_scope_drift": """
            SELECT l.id
            FROM entitlement_ledger l
            LEFT JOIN entitlement_wallets w ON w.id=l.wallet_id
            LEFT JOIN accounts a ON a.id=l.account_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=l.platform_user_id AND pm.app_id=l.app_id
            WHERE w.id IS NULL OR a.id IS NULL OR pm.platform_user_id IS NULL
               OR l.app_id<>w.app_id OR l.app_id<>a.app_id
               OR l.platform_user_id<>w.platform_user_id
        """,
        "cost_scope_drift": """
            SELECT ce.id
            FROM cost_events ce
            LEFT JOIN accounts a ON a.id=ce.account_id
            LEFT JOIN entitlement_wallets w ON w.id=ce.wallet_id
            LEFT JOIN entitlement_ledger l ON l.id=ce.entitlement_ledger_id
            WHERE a.id IS NULL OR ce.app_id<>a.app_id
               OR (ce.wallet_id IS NOT NULL AND (w.id IS NULL OR ce.app_id<>w.app_id))
               OR (ce.entitlement_ledger_id IS NOT NULL AND (l.id IS NULL OR ce.app_id<>l.app_id))
        """,
        "wallet_ledger_balance_mismatch": """
            SELECT w.id
            FROM entitlement_wallets w
            LEFT JOIN entitlement_ledger l
              ON l.wallet_id=w.id AND l.app_id=w.app_id
            GROUP BY w.id, w.balance_shell_micros
            HAVING w.balance_shell_micros<>COALESCE(SUM(l.amount_shell_micros), 0)
        """,
    }
    counts: Dict[str, int] = {}
    for name, sql in checks.items():
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({sql}) billing_contract_rows"
        ).fetchone()
        counts[name] = int(row["n"])
    return counts


def _migration_0040_billing_app_id_contract(conn: Connection) -> None:
    """切换计费唯一约束到产品维度；执行前必须排空全部旧 writer。"""
    violations = _billing_contract_violation_counts(conn)
    blocking = {name: total for name, total in violations.items() if total > 0}
    if blocking:
        summary = ", ".join(f"{name}={total}" for name, total in sorted(blocking.items()))
        raise RuntimeError(f"m0040 billing reconcile failed: {summary}")

    # 旧代码仍使用 ON CONFLICT(platform_user_id)；删除该 arbiter 前生产必须已 drain
    # 所有旧 API/scheduler writer。新代码只使用下面的产品级 partial unique index。
    conn.execute("DROP INDEX IF EXISTS ux_entitlement_wallets_user_active")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_wallets_user_app_active
        ON entitlement_wallets(platform_user_id, app_id)
        WHERE status = 'active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_subscriptions_user_app_active
        ON subscriptions(platform_user_id, app_id)
        WHERE status = 'active';
        """
    )


def _migration_0041_quota_app_id_expand(conn: Connection) -> None:
    """为 daily/reservation 补产品归属，同时保留旧真人级唯一 arbiter。"""
    _ensure_column(
        conn,
        "daily_usage",
        "app_id",
        "TEXT NOT NULL DEFAULT 'zhaoxi'",
    )
    _ensure_column(
        conn,
        "daily_quota_reservations",
        "app_id",
        "TEXT NOT NULL DEFAULT 'zhaoxi'",
    )
    for table in ("daily_usage", "daily_quota_reservations"):
        conn.execute(
            f"""
            UPDATE {table}
            SET app_id = (
                SELECT a.app_id FROM accounts a WHERE a.id = {table}.account_id
            )
            WHERE EXISTS (
                SELECT 1 FROM accounts a
                WHERE a.id = {table}.account_id
                  AND a.app_id IS NOT NULL
                  AND a.app_id <> ''
            )
            """
        )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_daily_usage_user_app_date
        ON daily_usage(platform_user_id, app_id, date);
        CREATE INDEX IF NOT EXISTS ix_qres_user_app_date
        ON daily_quota_reservations(platform_user_id, app_id, date);
        CREATE INDEX IF NOT EXISTS ix_qres_app_expires
        ON daily_quota_reservations(app_id, expires_at);
        """
    )


def _quota_contract_violation_counts(conn: Connection) -> Dict[str, int]:
    """返回 m0042 contract 的聚合阻断计数，不读取或输出业务明细。"""
    owner_matches = """
        EXISTS (
            SELECT 1 FROM account_owner_bindings b
            WHERE b.account_id={alias}.account_id
              AND b.platform_user_id={alias}.platform_user_id
              AND b.app_id={alias}.app_id
        )
        OR EXISTS (
            SELECT 1
            FROM universe_residents r
            JOIN universes u ON u.id=r.universe_id
            JOIN accounts resident_account ON resident_account.id=r.runtime_account_id
            WHERE r.runtime_account_id={alias}.account_id
              AND u.owner_platform_user_id={alias}.platform_user_id
              AND resident_account.app_id={alias}.app_id
        )
    """
    checks = {
        "quota_null_app_id": """
            SELECT CAST(id AS TEXT) AS id
            FROM daily_usage WHERE app_id IS NULL OR app_id=''
            UNION ALL
            SELECT id FROM daily_quota_reservations WHERE app_id IS NULL OR app_id=''
        """,
        "quota_owner_fallback": """
            SELECT CAST(d.id AS TEXT) AS id
            FROM daily_usage d
            LEFT JOIN platform_users pu ON pu.id=d.platform_user_id
            WHERE pu.id IS NULL
            UNION ALL
            SELECT q.id
            FROM daily_quota_reservations q
            LEFT JOIN platform_users pu ON pu.id=q.platform_user_id
            WHERE pu.id IS NULL
        """,
        "daily_scope_drift": f"""
            SELECT CAST(d.id AS TEXT) AS id
            FROM daily_usage d
            LEFT JOIN accounts a ON a.id=d.account_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=d.platform_user_id AND pm.app_id=d.app_id
            WHERE a.id IS NULL OR a.app_id<>d.app_id OR pm.platform_user_id IS NULL
               OR NOT ({owner_matches.format(alias='d')})
        """,
        "reservation_scope_drift": f"""
            SELECT q.id
            FROM daily_quota_reservations q
            LEFT JOIN accounts a ON a.id=q.account_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=q.platform_user_id AND pm.app_id=q.app_id
            WHERE a.id IS NULL OR a.app_id<>q.app_id OR pm.platform_user_id IS NULL
               OR NOT ({owner_matches.format(alias='q')})
        """,
        "duplicate_daily_usage": """
            SELECT platform_user_id, app_id, date
            FROM daily_usage
            GROUP BY platform_user_id, app_id, date
            HAVING COUNT(*) > 1
        """,
    }
    counts: Dict[str, int] = {}
    for name, sql in checks.items():
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({sql}) quota_contract_rows"
        ).fetchone()
        counts[name] = int(row["n"])
    return counts


def _migration_0042_quota_app_id_contract(conn: Connection) -> None:
    """切换 daily/RPM 到产品维度；执行前必须排空全部旧 writer。"""
    violations = _quota_contract_violation_counts(conn)
    blocking = {name: total for name, total in violations.items() if total > 0}
    if blocking:
        summary = ", ".join(f"{name}={total}" for name, total in sorted(blocking.items()))
        raise RuntimeError(f"m0042 quota reconcile failed: {summary}")

    # contract 后新代码使用产品化 RPM subject。旧 writer 已停写，此处把尚在窗口内的
    # zhaoxi 真人级命中原位改键，确保切换瞬间不会因换 key 放宽限额。
    zhaoxi_users = conn.execute(
        """
        SELECT DISTINCT h.account_id AS platform_user_id
        FROM rpm_hits h
        JOIN product_memberships pm
          ON pm.platform_user_id=h.account_id AND pm.app_id='zhaoxi'
        """
    ).fetchall()
    for row in zhaoxi_users:
        platform_user_id = str(row["platform_user_id"])
        conn.execute(
            "UPDATE rpm_hits SET account_id=? WHERE account_id=?",
            (
                product_quota_subject(
                    platform_user_id=platform_user_id,
                    app_id="zhaoxi",
                ),
                platform_user_id,
            ),
        )
    legacy_account_subjects = conn.execute(
        """
        SELECT DISTINCT h.account_id
        FROM rpm_hits h
        JOIN accounts a ON a.id=h.account_id
        WHERE a.app_id='zhaoxi'
        """
    ).fetchall()
    for row in legacy_account_subjects:
        account_id = str(row["account_id"])
        owner = conn.execute(
            """
            SELECT platform_user_id
            FROM account_owner_bindings
            WHERE account_id=? AND status='active' AND app_id='zhaoxi'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if owner is None:
            owner = conn.execute(
                """
                SELECT u.owner_platform_user_id AS platform_user_id
                FROM universe_residents r
                JOIN universes u ON u.id=r.universe_id
                WHERE r.runtime_account_id=?
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        subject = str(owner["platform_user_id"]) if owner is not None else account_id
        conn.execute(
            "UPDATE rpm_hits SET account_id=? WHERE account_id=?",
            (
                product_quota_subject(
                    platform_user_id=subject,
                    app_id="zhaoxi",
                ),
                account_id,
            ),
        )

    # 删除旧 ON CONFLICT(platform_user_id,date) arbiter 前必须 drain 旧 writer。
    conn.execute("DROP INDEX IF EXISTS ux_daily_usage_user_date")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_usage_user_app_date
        ON daily_usage(platform_user_id, app_id, date);
        """
    )


def _migration_0043_referral_app_id_expand(conn: Connection) -> None:
    """为邀请、关系和有效消息审核补产品归属，保留旧 invitee 全局唯一约束。"""
    for table in (
        "referral_codes",
        "referral_relationships",
        "meaningful_message_reviews",
    ):
        _ensure_column(conn, table, "app_id", "TEXT NOT NULL DEFAULT 'zhaoxi'")

    conn.execute(
        """
        UPDATE referral_relationships
        SET app_id = (
            SELECT rc.app_id FROM referral_codes rc
            WHERE rc.id=referral_relationships.referral_code_id
        )
        WHERE EXISTS (
            SELECT 1 FROM referral_codes rc
            WHERE rc.id=referral_relationships.referral_code_id
              AND rc.app_id IS NOT NULL AND rc.app_id<>''
        )
        """
    )
    conn.execute(
        """
        UPDATE meaningful_message_reviews
        SET app_id = (
            SELECT rr.app_id FROM referral_relationships rr
            WHERE rr.id=meaningful_message_reviews.referral_relationship_id
        )
        WHERE EXISTS (
            SELECT 1 FROM referral_relationships rr
            WHERE rr.id=meaningful_message_reviews.referral_relationship_id
              AND rr.app_id IS NOT NULL AND rr.app_id<>''
        )
        """
    )

    # 新旧 writer 均可命中产品级 personal 唯一约束；code 字符串的表级全局 UNIQUE 保留。
    conn.execute("DROP INDEX IF EXISTS ux_referral_codes_personal_user")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_referral_codes_personal_user_app
        ON referral_codes(platform_user_id, app_id, code_type)
        WHERE code_type='personal' AND platform_user_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_referral_codes_app_status
        ON referral_codes(app_id, status);
        CREATE INDEX IF NOT EXISTS ix_referral_relationships_inviter_app_created
        ON referral_relationships(inviter_platform_user_id, app_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_referral_relationships_app_status
        ON referral_relationships(app_id, status, review_status);
        CREATE INDEX IF NOT EXISTS ix_meaningful_reviews_app_relationship
        ON meaningful_message_reviews(app_id, referral_relationship_id);
        """
    )


def _referral_contract_violation_counts(conn: Connection) -> Dict[str, int]:
    """返回 m0044 contract 的聚合阻断计数，不读取或输出业务明细。"""
    checks = {
        "referral_null_app_id": """
            SELECT id FROM referral_codes WHERE app_id IS NULL OR app_id=''
            UNION ALL
            SELECT id FROM referral_relationships WHERE app_id IS NULL OR app_id=''
            UNION ALL
            SELECT id FROM meaningful_message_reviews WHERE app_id IS NULL OR app_id=''
        """,
        "duplicate_personal_code": """
            SELECT platform_user_id, app_id, code_type
            FROM referral_codes
            WHERE code_type='personal' AND platform_user_id IS NOT NULL
            GROUP BY platform_user_id, app_id, code_type
            HAVING COUNT(*) > 1
        """,
        "duplicate_invitee_membership": """
            SELECT invitee_platform_user_id, app_id
            FROM referral_relationships
            GROUP BY invitee_platform_user_id, app_id
            HAVING COUNT(*) > 1
        """,
        "referral_code_scope_drift": """
            SELECT rc.id
            FROM referral_codes rc
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=rc.platform_user_id AND pm.app_id=rc.app_id
            WHERE rc.platform_user_id IS NOT NULL AND pm.platform_user_id IS NULL
        """,
        "referral_relationship_scope_drift": """
            SELECT rr.id
            FROM referral_relationships rr
            LEFT JOIN referral_codes rc ON rc.id=rr.referral_code_id
            LEFT JOIN product_memberships inviter
              ON inviter.platform_user_id=rr.inviter_platform_user_id
             AND inviter.app_id=rr.app_id
            LEFT JOIN product_memberships invitee
              ON invitee.platform_user_id=rr.invitee_platform_user_id
             AND invitee.app_id=rr.app_id
            LEFT JOIN entitlement_ledger reward ON reward.id=rr.reward_ledger_id
            WHERE rc.id IS NULL OR rc.app_id<>rr.app_id
               OR inviter.platform_user_id IS NULL
               OR invitee.platform_user_id IS NULL
               OR rr.inviter_platform_user_id=rr.invitee_platform_user_id
               OR (rr.reward_ledger_id IS NOT NULL
                   AND (reward.id IS NULL OR reward.app_id<>rr.app_id))
        """,
        "meaningful_review_scope_drift": """
            SELECT mr.id
            FROM meaningful_message_reviews mr
            LEFT JOIN referral_relationships rr ON rr.id=mr.referral_relationship_id
            LEFT JOIN accounts a ON a.id=mr.account_id
            WHERE rr.id IS NULL OR a.id IS NULL
               OR mr.app_id<>rr.app_id OR mr.app_id<>a.app_id
               OR mr.invitee_platform_user_id<>rr.invitee_platform_user_id
        """,
    }
    counts: Dict[str, int] = {}
    for name, sql in checks.items():
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({sql}) referral_contract_rows"
        ).fetchone()
        counts[name] = int(row["n"])
    return counts


def _rebuild_referral_relationships_sqlite(conn: Connection) -> None:
    """SQLite 同步重建 relationship 及其 review 子表，移除 invitee 全局 UNIQUE。"""
    conn.executescript(
        """
        DROP TABLE IF EXISTS referral_relationships_mp04;
        DROP TABLE IF EXISTS meaningful_message_reviews_mp04_backup;
        CREATE TABLE meaningful_message_reviews_mp04_backup AS
        SELECT id, referral_relationship_id, invitee_platform_user_id,
               account_id, message_ids_json, reviewer_type, status, reason,
               metadata_json, created_at, app_id
        FROM meaningful_message_reviews;
        DROP TABLE meaningful_message_reviews;
        CREATE TABLE referral_relationships_mp04 (
            id TEXT PRIMARY KEY,
            inviter_platform_user_id TEXT NOT NULL,
            invitee_platform_user_id TEXT NOT NULL,
            referral_code_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'registered',
            meaningful_message_count INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            reward_ledger_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            rewarded_at TEXT,
            app_id TEXT NOT NULL DEFAULT 'zhaoxi',
            FOREIGN KEY(inviter_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(invitee_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(referral_code_id) REFERENCES referral_codes(id),
            FOREIGN KEY(reward_ledger_id) REFERENCES entitlement_ledger(id)
        );
        INSERT INTO referral_relationships_mp04(
            id, inviter_platform_user_id, invitee_platform_user_id,
            referral_code_id, status, meaningful_message_count, review_status,
            reward_ledger_id, metadata_json, created_at, updated_at, rewarded_at, app_id
        )
        SELECT id, inviter_platform_user_id, invitee_platform_user_id,
               referral_code_id, status, meaningful_message_count, review_status,
               reward_ledger_id, metadata_json, created_at, updated_at, rewarded_at, app_id
        FROM referral_relationships;
        DROP TABLE referral_relationships;
        ALTER TABLE referral_relationships_mp04 RENAME TO referral_relationships;

        CREATE TABLE meaningful_message_reviews (
            id TEXT PRIMARY KEY,
            referral_relationship_id TEXT NOT NULL,
            invitee_platform_user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            message_ids_json TEXT NOT NULL DEFAULT '[]',
            reviewer_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            app_id TEXT NOT NULL DEFAULT 'zhaoxi',
            FOREIGN KEY(referral_relationship_id) REFERENCES referral_relationships(id),
            FOREIGN KEY(invitee_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        INSERT INTO meaningful_message_reviews(
            id, referral_relationship_id, invitee_platform_user_id,
            account_id, message_ids_json, reviewer_type, status, reason,
            metadata_json, created_at, app_id
        )
        SELECT id, referral_relationship_id, invitee_platform_user_id,
               account_id, message_ids_json, reviewer_type, status, reason,
               metadata_json, created_at, app_id
        FROM meaningful_message_reviews_mp04_backup;
        DROP TABLE meaningful_message_reviews_mp04_backup;
        CREATE UNIQUE INDEX ux_meaningful_reviews_relationship_reviewer
        ON meaningful_message_reviews(referral_relationship_id, reviewer_type);
        CREATE INDEX ix_meaningful_reviews_app_relationship
        ON meaningful_message_reviews(app_id, referral_relationship_id);
        """
    )


def _migration_0044_referral_app_id_contract(conn: Connection) -> None:
    """切换 invitee 唯一性到产品维度；执行前必须排空全部旧 writer。"""
    violations = _referral_contract_violation_counts(conn)
    blocking = {name: total for name, total in violations.items() if total > 0}
    if blocking:
        summary = ", ".join(f"{name}={total}" for name, total in sorted(blocking.items()))
        raise RuntimeError(f"m0044 referral reconcile failed: {summary}")

    if is_postgres():
        constraints = conn.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid='referral_relationships'::regclass
              AND contype='u'
            """
        ).fetchall()
        for constraint in constraints:
            name = str(constraint["conname"])
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise RuntimeError("unexpected referral unique constraint name")
            conn.execute(f'ALTER TABLE referral_relationships DROP CONSTRAINT "{name}"')
    else:
        _rebuild_referral_relationships_sqlite(conn)

    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_referral_relationships_invitee_app
        ON referral_relationships(invitee_platform_user_id, app_id);
        CREATE INDEX IF NOT EXISTS ix_referral_relationships_inviter_created
        ON referral_relationships(inviter_platform_user_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_referral_relationships_status
        ON referral_relationships(status, review_status);
        CREATE INDEX IF NOT EXISTS ix_referral_relationships_inviter_app_created
        ON referral_relationships(inviter_platform_user_id, app_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_referral_relationships_app_status
        ON referral_relationships(app_id, status, review_status);
        """
    )


def _identity_contract_violation_counts(conn: Connection) -> Dict[str, int]:
    """返回 Phase 1 身份/session/runtime 锚点的最终聚合阻断计数。"""
    checks = {
        "identity_null_app_id": """
            SELECT CAST(id AS TEXT) AS id
            FROM accounts WHERE app_id IS NULL OR app_id=''
            UNION ALL
            SELECT CAST(id AS TEXT) FROM account_owner_bindings
            WHERE app_id IS NULL OR app_id=''
            UNION ALL
            SELECT CAST(id AS TEXT) FROM platform_user_sessions
            WHERE app_id IS NULL OR app_id=''
            UNION ALL
            SELECT CAST(platform_user_id AS TEXT) FROM product_memberships
            WHERE app_id IS NULL OR app_id=''
        """,
        "binding_app_drift": """
            SELECT CAST(b.id AS TEXT) AS id
            FROM account_owner_bindings b
            LEFT JOIN accounts a ON a.id=b.account_id
            LEFT JOIN platform_users pu ON pu.id=b.platform_user_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=b.platform_user_id AND pm.app_id=b.app_id
            WHERE a.id IS NULL OR pu.id IS NULL OR pm.platform_user_id IS NULL
               OR b.app_id<>a.app_id
        """,
        "session_scope_drift": """
            SELECT CAST(s.id AS TEXT) AS id
            FROM platform_user_sessions s
            LEFT JOIN platform_users pu ON pu.id=s.platform_user_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=s.platform_user_id AND pm.app_id=s.app_id
            WHERE pu.id IS NULL OR pm.platform_user_id IS NULL
        """,
        "resident_scope_drift": """
            SELECT CAST(r.id AS TEXT) AS id
            FROM universe_residents r
            JOIN universes u ON u.id=r.universe_id
            LEFT JOIN accounts a ON a.id=r.runtime_account_id
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=u.owner_platform_user_id AND pm.app_id=a.app_id
            WHERE r.runtime_account_id IS NOT NULL
              AND (a.id IS NULL OR pm.platform_user_id IS NULL)
        """,
        "message_account_drift": """
            SELECT CAST(m.id AS TEXT) AS id
            FROM messages m
            LEFT JOIN sessions s ON s.id=m.session_id
            LEFT JOIN accounts a ON a.id=m.account_id
            WHERE s.id IS NULL OR a.id IS NULL OR s.account_id<>m.account_id
        """,
        "duplicate_active_entry_account": """
            SELECT platform_user_id, app_id
            FROM account_owner_bindings
            WHERE status='active'
            GROUP BY platform_user_id, app_id
            HAVING COUNT(*) > 1
        """,
        "duplicate_active_account_owner": """
            SELECT account_id
            FROM account_owner_bindings
            WHERE status='active'
            GROUP BY account_id
            HAVING COUNT(*) > 1
        """,
    }
    counts: Dict[str, int] = {}
    for name, sql in checks.items():
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({sql}) identity_contract_rows"
        ).fetchone()
        counts[name] = int(row["n"])
    return counts


def _phase1_contract_violation_counts(conn: Connection) -> Dict[str, int]:
    """返回 MP-05 最终 contract 的完整隔离 reconcile 计数。"""
    counts = _identity_contract_violation_counts(conn)
    counts.update(_billing_contract_violation_counts(conn))
    counts.update(_billing_idempotency_contract_violation_counts(conn))
    counts.update(_quota_contract_violation_counts(conn))
    counts.update(_referral_contract_violation_counts(conn))
    return counts


def _migration_0045_multi_product_phase1_contract(conn: Connection) -> None:
    """Phase 1 最终 contract：只校验终态并固化索引，不重写业务数据。"""
    violations = _phase1_contract_violation_counts(conn)
    blocking = {name: total for name, total in violations.items() if total > 0}
    if blocking:
        summary = ", ".join(f"{name}={total}" for name, total in sorted(blocking.items()))
        raise RuntimeError(f"m0045 phase1 reconcile failed: {summary}")

    app_id_tables = (
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
        "referral_codes",
        "referral_relationships",
        "meaningful_message_reviews",
    )
    if is_postgres():
        # PG 的 SET NOT NULL 是最终 schema contract；前置聚合校验已保证不会因 NULL 失败。
        for table in app_id_tables:
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN app_id SET NOT NULL")
    else:
        # SQLite expand 时列已直接以 NOT NULL 添加。若历史分支留下 nullable schema，拒绝
        # 在最终 contract 偷偷重建业务大表，要求先显式修复该异常迁移路径。
        nullable_tables = []
        for table in app_id_tables:
            columns = {
                str(row["name"]): int(row["notnull"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if columns.get("app_id") != 1:
                nullable_tables.append(table)
        if nullable_tables:
            raise RuntimeError(
                "m0045 nullable app_id schema: " + ", ".join(nullable_tables)
            )

    # 幂等确认终态查询/唯一 arbiter，避免 contract 阶段重写业务数据或 SQLite 重建。
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_accounts_app_status
        ON accounts(app_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_owner_binding_active_user_app
        ON account_owner_bindings(platform_user_id, app_id)
        WHERE status='active';
        CREATE INDEX IF NOT EXISTS ix_platform_user_sessions_app_user
        ON platform_user_sessions(app_id, platform_user_id);
        CREATE INDEX IF NOT EXISTS ix_product_memberships_app_status
        ON product_memberships(app_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_wallets_user_app_active
        ON entitlement_wallets(platform_user_id, app_id)
        WHERE status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_subscriptions_user_app_active
        ON subscriptions(platform_user_id, app_id)
        WHERE status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_usage_user_app_date
        ON daily_usage(platform_user_id, app_id, date);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_referral_codes_personal_user_app
        ON referral_codes(platform_user_id, app_id, code_type)
        WHERE code_type='personal' AND platform_user_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_referral_relationships_invitee_app
        ON referral_relationships(invitee_platform_user_id, app_id);
        """
    )


def _billing_idempotency_contract_violation_counts(conn: Connection) -> Dict[str, int]:
    """返回 MP-06 billing 幂等键切换前的重复组合键计数。"""
    checks = {
        "duplicate_ledger_app_idempotency": """
            SELECT app_id, idempotency_key
            FROM entitlement_ledger
            GROUP BY app_id, idempotency_key
            HAVING COUNT(*) > 1
        """,
        "duplicate_cost_app_idempotency": """
            SELECT app_id, idempotency_key
            FROM cost_events
            GROUP BY app_id, idempotency_key
            HAVING COUNT(*) > 1
        """,
    }
    counts: Dict[str, int] = {}
    for name, sql in checks.items():
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({sql}) billing_idempotency_rows"
        ).fetchone()
        counts[name] = int(row["n"])
    return counts


def _rebuild_billing_idempotency_sqlite(conn: Connection) -> None:
    """SQLite 重建 billing 及引用子表，移除列级全局幂等 UNIQUE。"""
    conn.executescript(
        """
        DROP TABLE IF EXISTS meaningful_message_reviews_mp06_backup;
        DROP TABLE IF EXISTS referral_relationships_mp06_backup;
        DROP TABLE IF EXISTS cost_events_mp06_backup;
        DROP TABLE IF EXISTS entitlement_ledger_mp06_backup;

        CREATE TABLE meaningful_message_reviews_mp06_backup AS
        SELECT * FROM meaningful_message_reviews;
        DROP TABLE meaningful_message_reviews;
        CREATE TABLE referral_relationships_mp06_backup AS
        SELECT * FROM referral_relationships;
        DROP TABLE referral_relationships;
        CREATE TABLE cost_events_mp06_backup AS
        SELECT * FROM cost_events;
        DROP TABLE cost_events;
        CREATE TABLE entitlement_ledger_mp06_backup AS
        SELECT * FROM entitlement_ledger;
        DROP TABLE entitlement_ledger;

        CREATE TABLE entitlement_ledger (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            platform_user_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            amount_shell_micros INTEGER NOT NULL,
            balance_after_shell_micros INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            app_id TEXT NOT NULL DEFAULT 'zhaoxi',
            FOREIGN KEY(wallet_id) REFERENCES entitlement_wallets(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        INSERT INTO entitlement_ledger(
            id, wallet_id, account_id, platform_user_id, entry_type,
            source_type, source_id, amount_shell_micros,
            balance_after_shell_micros, idempotency_key, metadata_json,
            created_at, app_id
        )
        SELECT id, wallet_id, account_id, platform_user_id, entry_type,
               source_type, source_id, amount_shell_micros,
               balance_after_shell_micros, idempotency_key, metadata_json,
               created_at, app_id
        FROM entitlement_ledger_mp06_backup;
        DROP TABLE entitlement_ledger_mp06_backup;

        CREATE TABLE cost_events (
            id TEXT PRIMARY KEY,
            wallet_id TEXT,
            account_id TEXT NOT NULL,
            platform_user_id TEXT,
            cost_type TEXT NOT NULL,
            cost_owner TEXT NOT NULL DEFAULT 'user',
            billable_to_user INTEGER NOT NULL DEFAULT 1,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            billable_tokens INTEGER,
            model_price_multiplier_micros INTEGER NOT NULL DEFAULT 1000000,
            computed_shell_micros INTEGER NOT NULL DEFAULT 0,
            entitlement_ledger_id TEXT,
            source_type TEXT,
            source_id TEXT,
            idempotency_key TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            app_id TEXT NOT NULL DEFAULT 'zhaoxi',
            FOREIGN KEY(wallet_id) REFERENCES entitlement_wallets(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(entitlement_ledger_id) REFERENCES entitlement_ledger(id)
        );
        INSERT INTO cost_events(
            id, wallet_id, account_id, platform_user_id, cost_type,
            cost_owner, billable_to_user, model, input_tokens, output_tokens,
            billable_tokens, model_price_multiplier_micros,
            computed_shell_micros, entitlement_ledger_id, source_type,
            source_id, idempotency_key, metadata_json, created_at, app_id
        )
        SELECT id, wallet_id, account_id, platform_user_id, cost_type,
               cost_owner, billable_to_user, model, input_tokens, output_tokens,
               billable_tokens, model_price_multiplier_micros,
               computed_shell_micros, entitlement_ledger_id, source_type,
               source_id, idempotency_key, metadata_json, created_at, app_id
        FROM cost_events_mp06_backup;
        DROP TABLE cost_events_mp06_backup;

        CREATE TABLE referral_relationships (
            id TEXT PRIMARY KEY,
            inviter_platform_user_id TEXT NOT NULL,
            invitee_platform_user_id TEXT NOT NULL,
            referral_code_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'registered',
            meaningful_message_count INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            reward_ledger_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            rewarded_at TEXT,
            app_id TEXT NOT NULL DEFAULT 'zhaoxi',
            FOREIGN KEY(inviter_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(invitee_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(referral_code_id) REFERENCES referral_codes(id),
            FOREIGN KEY(reward_ledger_id) REFERENCES entitlement_ledger(id)
        );
        INSERT INTO referral_relationships(
            id, inviter_platform_user_id, invitee_platform_user_id,
            referral_code_id, status, meaningful_message_count, review_status,
            reward_ledger_id, metadata_json, created_at, updated_at, rewarded_at,
            app_id
        )
        SELECT id, inviter_platform_user_id, invitee_platform_user_id,
               referral_code_id, status, meaningful_message_count, review_status,
               reward_ledger_id, metadata_json, created_at, updated_at, rewarded_at,
               app_id
        FROM referral_relationships_mp06_backup;
        DROP TABLE referral_relationships_mp06_backup;

        CREATE TABLE meaningful_message_reviews (
            id TEXT PRIMARY KEY,
            referral_relationship_id TEXT NOT NULL,
            invitee_platform_user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            message_ids_json TEXT NOT NULL DEFAULT '[]',
            reviewer_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            app_id TEXT NOT NULL DEFAULT 'zhaoxi',
            FOREIGN KEY(referral_relationship_id) REFERENCES referral_relationships(id),
            FOREIGN KEY(invitee_platform_user_id) REFERENCES platform_users(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        INSERT INTO meaningful_message_reviews(
            id, referral_relationship_id, invitee_platform_user_id,
            account_id, message_ids_json, reviewer_type, status, reason,
            metadata_json, created_at, app_id
        )
        SELECT id, referral_relationship_id, invitee_platform_user_id,
               account_id, message_ids_json, reviewer_type, status, reason,
               metadata_json, created_at, app_id
        FROM meaningful_message_reviews_mp06_backup;
        DROP TABLE meaningful_message_reviews_mp06_backup;

        CREATE INDEX ix_entitlement_ledger_wallet_created
        ON entitlement_ledger(wallet_id, created_at);
        CREATE INDEX ix_entitlement_ledger_account_created
        ON entitlement_ledger(account_id, created_at);
        CREATE INDEX ix_entitlement_ledger_user_app_created
        ON entitlement_ledger(platform_user_id, app_id, created_at);
        CREATE UNIQUE INDEX ux_entitlement_ledger_app_idempotency
        ON entitlement_ledger(app_id, idempotency_key);
        CREATE INDEX ix_cost_events_account_created
        ON cost_events(account_id, created_at);
        CREATE INDEX ix_cost_events_wallet_created
        ON cost_events(wallet_id, created_at);
        CREATE INDEX ix_cost_events_account_app_created
        ON cost_events(account_id, app_id, created_at);
        CREATE UNIQUE INDEX ux_cost_events_app_idempotency
        ON cost_events(app_id, idempotency_key);
        CREATE UNIQUE INDEX ux_referral_relationships_invitee_app
        ON referral_relationships(invitee_platform_user_id, app_id);
        CREATE INDEX ix_referral_relationships_inviter_created
        ON referral_relationships(inviter_platform_user_id, created_at);
        CREATE INDEX ix_referral_relationships_status
        ON referral_relationships(status, review_status);
        CREATE INDEX ix_referral_relationships_inviter_app_created
        ON referral_relationships(inviter_platform_user_id, app_id, created_at);
        CREATE INDEX ix_referral_relationships_app_status
        ON referral_relationships(app_id, status, review_status);
        CREATE UNIQUE INDEX ux_meaningful_reviews_relationship_reviewer
        ON meaningful_message_reviews(referral_relationship_id, reviewer_type);
        CREATE INDEX ix_meaningful_reviews_app_relationship
        ON meaningful_message_reviews(app_id, referral_relationship_id);
        """
    )
    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise RuntimeError("m0046 SQLite billing rebuild produced foreign key drift")


def _drop_pg_global_billing_idempotency_constraints(conn: Connection) -> None:
    """删除两张 billing 表只覆盖 idempotency_key 的旧列级唯一约束。"""
    rows = conn.execute(
        """
        SELECT tc.table_name, tc.constraint_name,
               COUNT(*) AS column_count,
               MAX(kcu.column_name) AS column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_schema=tc.constraint_schema
         AND kcu.constraint_name=tc.constraint_name
         AND kcu.table_name=tc.table_name
        WHERE tc.constraint_schema=current_schema()
          AND tc.constraint_type='UNIQUE'
          AND tc.table_name IN ('entitlement_ledger', 'cost_events')
        GROUP BY tc.table_name, tc.constraint_name
        """
    ).fetchall()
    for row in rows:
        if int(row["column_count"]) != 1 or row["column_name"] != "idempotency_key":
            continue
        table = str(row["table_name"])
        constraint = str(row["constraint_name"])
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise RuntimeError("unexpected billing table name")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", constraint):
            raise RuntimeError("unexpected billing unique constraint name")
        conn.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint}"')


def _migration_0046_billing_idempotency_contract(conn: Connection) -> None:
    """把 ledger/cost 幂等唯一性从全局键切换为产品组合键。"""
    violations = _billing_idempotency_contract_violation_counts(conn)
    blocking = {name: total for name, total in violations.items() if total > 0}
    if blocking:
        summary = ", ".join(f"{name}={total}" for name, total in sorted(blocking.items()))
        raise RuntimeError(f"m0046 billing idempotency reconcile failed: {summary}")

    if is_postgres():
        _drop_pg_global_billing_idempotency_constraints(conn)
    else:
        _rebuild_billing_idempotency_sqlite(conn)
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entitlement_ledger_app_idempotency
        ON entitlement_ledger(app_id, idempotency_key);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_events_app_idempotency
        ON cost_events(app_id, idempotency_key);
        """
    )


def _migration_0051_app_me_tab(conn: Connection) -> None:
    """「我的」Tab 收尾：用户 Profile（ME-01）、注销申请（ME-06/07）、通知偏好（ME-10）。

    ``platform_users.avatar_key`` 存受控头像 key（不存 URL——资产前缀由
    ``COMPANION_WORLD_ASSET_BASE_URL`` 决定，换 CDN 不用改数据）；昵称沿用既有
    ``display_name`` 列，不新建。

    ``account_deletion_requests`` 是注销的**执行流水**。产品口径为「注销立即删聊天记录
    和相关记忆」（Q14，2026-07-26 拍板），没有冷静期、没有待办状态，所以本表记的是
    「谁在什么时候删了什么」而不是「谁申请了删除」。刻意建表而不是给 ``platform_users``
    加列：注销后手机号可重新注册，加列会被下一次注销覆盖掉，合规追溯要的是完整历史。
    ``purge_stats_json`` 存本次实际删除的行数快照，供运营核对清除范围。

    ``app_notification_preferences`` 一行一个真人；缺行等价于默认值 ``standard``，
    因此无需回填。

    纯加列 + 两张新表，无回填、无锁表风险；两后端均幂等。
    """
    _ensure_column(conn, "platform_users", "avatar_key", "TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_deletion_requests (
            id TEXT PRIMARY KEY,
            platform_user_id TEXT NOT NULL,
            app_id TEXT NOT NULL,                    -- 发起注销的产品，便于运营分流
            status TEXT NOT NULL DEFAULT 'executed', -- 当前只有 executed：注销即时生效
            reason_code TEXT,                        -- 受控取值，非自由文本
            executed_at TEXT NOT NULL,               -- 实际完成清除的时刻
            purge_stats_json TEXT,                   -- 本次删除行数快照，供运营核对
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        -- 同一真人可多次注销（注销后手机号仍可重新注册），因此**不**加唯一约束。
        CREATE INDEX IF NOT EXISTS ix_account_deletion_requests_owner
            ON account_deletion_requests(platform_user_id, executed_at);

        CREATE TABLE IF NOT EXISTS app_notification_preferences (
            platform_user_id TEXT PRIMARY KEY,
            quiet_level TEXT NOT NULL DEFAULT 'standard',  -- standard | quiet
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
        );
        """
    )


def _migration_0054_media_assets(conn: Connection) -> None:
    """v1.5 媒体地基：一张 ``media_assets`` + 消息侧三个可空列（MEDIA-* 系列）。

    ``media_assets`` 是**上传与消息解耦**的中间态：客户端先传拿到 ``media_ref``，之后才在
    发消息/发动态时引用它。因此 ``status`` 只有两态——``pending``（已上传未引用，
    ``expires_at`` 到点连行带文件一起回收）与 ``referenced``（已被引用，不再过期）。
    刻意不做反向扫描（"有没有消息指向我"）：引用与状态翻转在同一事务内完成，
    状态位就是唯一真相，比每小时 JOIN 三张表便宜得多。

    ``storage_path`` 存相对路径（``<sha256[0:2]>/<sha256[2:4]>/<media_id>``），
    不存绝对路径也不存 URL——存储根由 ``settings.media_storage_dir`` 决定，将来换对象存储
    只需换解析函数，库里的数据不用动。

    ``owner_platform_user_id`` 是账号隔离的锚：读端点除了验签名，还要复核 scope 与 owner
    的关系，任何不带 owner 约束的媒体查询都是 bug。

    消息侧只加列不改约束：
    - ``messages.content_json`` 存 D-1 判别联合的**可持久化部分**（``messages.content``
      继续存 LLM 上下文用的纯文本，两者口径不同，刻意不合并）。S2 定稿只落
      ``{"type", "text"}``：URL 是短 TTL 签名的、宽高/时长/转写在 ``media_assets`` 里，
      存第二份必然漂移，所以库里只留不可再生的 caption，其余读时现取；
    - ``messages.media_id`` / ``human_messages.media_id`` 单列引用，**不加 FK**——
      SQLite 无法用 ALTER 补 FK，为一个可空列重建带 2 个 UNIQUE + 2 个 FK 的
      ``human_messages`` 表不值当，完整性由应用层与回收 job 的状态位保证；
    - ``human_messages.body_text`` 保持 ``NOT NULL``（同上，重建风险 > 收益），
      纯媒体消息写空串，"文本或媒体至少有一个"在 API 层校验。

    纯加表 + 加列，无回填、无锁表风险，两后端幂等。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media_assets (
            id TEXT PRIMARY KEY,                             -- mda_<token_urlsafe(24)>，不透明
            owner_platform_user_id TEXT NOT NULL,            -- 账号隔离锚，读写都必须带上
            kind TEXT NOT NULL,                              -- image | voice
            mime TEXT NOT NULL,                              -- 重编码后的真实 mime，非客户端声明
            bytes INTEGER NOT NULL,                           -- 落盘字节数（重编码后）
            width INTEGER,                                   -- 图片；语音为 NULL
            height INTEGER,
            duration_ms INTEGER,                             -- 语音；图片为 NULL
            sha256 TEXT NOT NULL,                            -- 落盘内容摘要，也是分片目录来源
            storage_path TEXT NOT NULL,                      -- 相对 media_storage_dir 的路径
            transcript TEXT,                                 -- 语音同步转写结果；失败或图片为 NULL
            status TEXT NOT NULL DEFAULT 'pending',          -- pending | referenced
            -- D-7 定稿口径：默认 skipped=本资产没有过审流程（S4 接阿里云前恒为此值）；
            -- S4 上线后入队时才置 pending，终态 passed / rejected。
            moderation_status TEXT NOT NULL DEFAULT 'skipped',  -- skipped | pending | passed | rejected
            moderation_task_id TEXT,                         -- S4 接阿里云内容安全后回填
            expires_at TEXT,                                 -- pending 的回收截止；referenced 置 NULL
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id)
        );
        CREATE INDEX IF NOT EXISTS ix_media_assets_owner
            ON media_assets(owner_platform_user_id, created_at);
        -- 回收 job 的唯一扫描路径：status='pending' AND expires_at <= now。
        CREATE INDEX IF NOT EXISTS ix_media_assets_reclaim
            ON media_assets(status, expires_at);
        """
    )
    _ensure_column(conn, "messages", "content_json", "TEXT")
    _ensure_column(conn, "messages", "media_id", "TEXT")
    _ensure_column(conn, "human_messages", "media_id", "TEXT")


def _migration_0056_media_moderation_scan_index(conn: Connection) -> None:
    """v1.5 图片机审：待审资产的扫描索引 + 重试次数列。

    索引：m0054 建表时只索引了 owner 与回收路径；S4 的批处理按 ``moderation_status`` 取待审
    资产，没有索引就是全表扫。绝大多数行恒为 ``skipped``（未开机审时全部如此），因此索引前导列
    选择性很低——但查询恒带等值条件 ``= 'pending'``，两个后端都能走索引只扫这一小段。
    刻意不用 partial index：谓词一旦与查询不完全匹配就静默退化成全表扫，收益不值这个脆弱性。

    ``moderation_attempts``：云调用失败的资产会留在 ``pending`` 等下一轮，没有计数就会对着
    一个坏配置无限重试。达到上限后按**先发后审的 fail-open 口径**记 ``skipped`` 放过，
    而不是当成命中红线误删用户内容。

    纯加索引 + 带默认值加列，无回填、无锁表风险，两后端幂等。
    """
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_media_assets_moderation
            ON media_assets(moderation_status, created_at);
        """
    )
    _ensure_column(conn, "media_assets", "moderation_attempts", "INTEGER NOT NULL DEFAULT 0")


def _migration_0068_runtime_turn_runs(conn: Connection) -> None:
    """Add provider-neutral streaming run state and active-session/idempotency guards."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_turn_runs (
            id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            client_message_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL,
            provider_id TEXT,
            model_ref TEXT,
            assistant_message_id TEXT,
            first_delta_at TEXT,
            finish_reason TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            completed_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_turn_runs_idempotency
            ON runtime_turn_runs(app_id, account_id, idempotency_key);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_turn_runs_active_session
            ON runtime_turn_runs(app_id, account_id, session_id)
            WHERE status IN ('accepted', 'running');
        CREATE INDEX IF NOT EXISTS ix_runtime_turn_runs_stale
            ON runtime_turn_runs(status, updated_at);
        """
    )


def _migration_0069_runtime_turn_cancellation(conn: Connection) -> None:
    """Persist cross-worker cancellation requests for active streaming turns."""
    _ensure_column(
        conn,
        "runtime_turn_runs",
        "cancel_requested_at",
        "TEXT",
    )


__all__ = [
    "_billing_contract_violation_counts",
    "_billing_idempotency_contract_violation_counts",
    "_drop_pg_global_billing_idempotency_constraints",
    "_identity_contract_violation_counts",
    "_migration_0001_baseline",
    "_migration_0002_llm_runtime_config",
    "_migration_0003_user_meta",
    "_migration_0004_account_profile_files",
    "_migration_0005_rpm_hits",
    "_migration_0006_merge_reactivation_categories",
    "_migration_0007_merge_reactivation_settings_keys",
    "_migration_0008_relationship_state",
    "_migration_0009_messages_account_id_index",
    "_migration_0010_sessions_rolling_summary",
    "_migration_0011_proactive_global_candidates",
    "_migration_0012_agent_mission",
    "_migration_0013_campaign_codes",
    "_migration_0019_campaign_ai_name_preset",
    "_migration_0020_campaign_visits",
    "_migration_0021_dynamic_reminders",
    "_migration_0022_account_app_id",
    "_migration_0023_owner_binding_active_unique",
    "_migration_0024_rename_channel_app_to_native",
    "_migration_0025_wallet_unique_platform_user",
    "_migration_0026_daily_usage_platform_user",
    "_migration_0027_daily_quota_reservations",
    "_migration_0031_platform_user_quota_overrides",
    "_migration_0032_rpm_hit_double_precision",
    "_migration_0036_repair_account_app_id",
    "_migration_0037_product_memberships",
    "_migration_0038_session_app_id",
    "_migration_0039_billing_app_id_expand",
    "_migration_0040_billing_app_id_contract",
    "_migration_0041_quota_app_id_expand",
    "_migration_0042_quota_app_id_contract",
    "_migration_0043_referral_app_id_expand",
    "_migration_0044_referral_app_id_contract",
    "_migration_0045_multi_product_phase1_contract",
    "_migration_0046_billing_idempotency_contract",
    "_migration_0051_app_me_tab",
    "_migration_0054_media_assets",
    "_migration_0056_media_moderation_scan_index",
    "_migration_0068_runtime_turn_runs",
    "_migration_0069_runtime_turn_cancellation",
    "_phase1_contract_violation_counts",
    "_quota_contract_violation_counts",
    "_rebuild_billing_idempotency_sqlite",
    "_rebuild_referral_relationships_sqlite",
    "_referral_contract_violation_counts",
]
