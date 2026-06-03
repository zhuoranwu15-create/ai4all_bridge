import json
import logging
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings

logger = logging.getLogger("ai4all.db")

_UNSET = object()
_NON_CONTEXT_ASSISTANT_REPLY = "我这边刚刚有点卡住了，你可以稍后再发我一次。"
ACCOUNT_ACTIVE_SESSION_KEY = "__account_active__"
_LEGACY_DEFAULT_ASSISTANT_NAMES = {"AI4ALL 助手"}
SHELL_MICROS_PER_SHELL = 1_000_000
SHELL_BILLABLE_TOKENS_PER_SHELL = 1000
NEW_USER_GRANT_SHELLS = 1000
NEW_USER_GRANT_SHELL_MICROS = NEW_USER_GRANT_SHELLS * SHELL_MICROS_PER_SHELL
_ACCOUNT_ID_RANDOM_MIN = 100_000_000
_ACCOUNT_ID_RANDOM_SPACE = 900_000_000
_ACCOUNT_ID_GENERATION_RETRIES = 20


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _new_account_id() -> str:
    return f"aid_{_ACCOUNT_ID_RANDOM_MIN + uuid.uuid4().int % _ACCOUNT_ID_RANDOM_SPACE}"


def _clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_default_account_display_name(value: Optional[str]) -> Optional[str]:
    cleaned = _clean_text(value)
    if cleaned in _LEGACY_DEFAULT_ASSISTANT_NAMES:
        return None
    return cleaned


def _format_shell_amount(amount_shell_micros: int) -> str:
    sign = "-" if amount_shell_micros < 0 else ""
    amount = abs(int(amount_shell_micros))
    whole = amount // SHELL_MICROS_PER_SHELL
    fraction = amount % SHELL_MICROS_PER_SHELL
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _channel_account_id_aliases(value: str) -> List[str]:
    """Return known OpenClaw/provider account id variants for lookup.

    openclaw-weixin's QR wait result returns the raw ilink bot id such as
    `abc@im.bot`, while OpenClaw runtime context commonly uses its normalized
    account id form `abc-im-bot`.
    """
    cleaned = _clean_text(value)
    if not cleaned:
        return []
    aliases = [cleaned]
    if cleaned.endswith("@im.bot"):
        aliases.append(f"{cleaned[:-7]}-im-bot")
    elif cleaned.endswith("-im-bot"):
        aliases.append(f"{cleaned[:-7]}@im.bot")
    if cleaned.endswith("@im.wechat"):
        aliases.append(f"{cleaned[:-10]}-im-wechat")
    elif cleaned.endswith("-im-wechat"):
        aliases.append(f"{cleaned[:-10]}@im.wechat")
    return list(dict.fromkeys(aliases))


_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _normalize_phone(phone: str) -> str:
    normalized = str(phone or "")
    for ch in (" ", "-", "(", ")", "."):
        normalized = normalized.replace(ch, "")
    normalized = normalized.strip()
    if not _PHONE_RE.match(normalized):
        raise ValueError("phone must be a valid Chinese mobile number (1[3-9]XXXXXXXXX)")
    return normalized


def _db_path() -> Path:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_from_contacts_schema(conn: sqlite3.Connection) -> None:
    logger.info("db migration: contacts → accounts schema detected, starting migration")

    # Disable FK enforcement so we can rebuild sessions/profiles while messages references them
    conn.execute("PRAGMA foreign_keys = OFF")

    # 1. Add status and notes to accounts
    _ensure_column(conn, "accounts", "status", "TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "accounts", "notes", "TEXT")

    # Copy status/notes from contacts (one contact per account in the old model)
    conn.execute(
        """
        UPDATE accounts
        SET status = COALESCE(
                (SELECT c.status FROM contacts c WHERE c.account_id = accounts.id LIMIT 1),
                'active'
            ),
            notes = (SELECT c.notes FROM contacts c WHERE c.account_id = accounts.id LIMIT 1)
        """
    )

    # 2. Rebuild sessions: remove contact_id FK, add sender_id/chat_id/sender_name
    conn.execute(
        """
        CREATE TABLE sessions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            sender_id TEXT,
            chat_id TEXT,
            sender_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, session_key),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions_new(id, account_id, session_key, sender_id, chat_id, sender_name,
                                  status, created_at, updated_at)
        SELECT s.id, s.account_id, s.session_key,
               c.sender_id, c.chat_id, c.sender_name,
               s.status, s.created_at, s.updated_at
        FROM sessions s
        LEFT JOIN contacts c ON c.id = s.contact_id
        """
    )
    conn.execute("DROP TABLE sessions")
    conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_account_key "
        "ON sessions(account_id, session_key)"
    )

    # 3. Rebuild profiles: remove contact_id, make account_id UNIQUE
    conn.execute(
        """
        CREATE TABLE profiles_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL UNIQUE,
            display_name TEXT,
            style TEXT,
            preferences_json TEXT NOT NULL DEFAULT '{}',
            system_prompt TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    # Take the latest profile per account
    conn.execute(
        """
        INSERT OR IGNORE INTO profiles_new(
            account_id, display_name, style, preferences_json, system_prompt,
            created_at, updated_at
        )
        SELECT p.account_id, p.display_name, p.style, p.preferences_json, p.system_prompt,
               p.created_at, p.updated_at
        FROM profiles p
        JOIN (
            SELECT account_id, MAX(id) AS max_id FROM profiles GROUP BY account_id
        ) latest ON p.id = latest.max_id
        """
    )
    conn.execute("DROP TABLE profiles")
    conn.execute("ALTER TABLE profiles_new RENAME TO profiles")

    # 4. Drop contacts
    conn.execute("DROP TABLE contacts")

    conn.execute("PRAGMA foreign_keys = ON")
    logger.info("db migration: contacts → accounts complete")


def init_db() -> None:
    with connect() as conn:
        # Run one-time migration if old schema detected
        if _table_exists(conn, "contacts"):
            _migrate_from_contacts_schema(conn)

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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS platform_users (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                platform_user_id TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(wallet_id) REFERENCES entitlement_wallets(id),
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
                FOREIGN KEY(entitlement_ledger_id) REFERENCES entitlement_ledger(id)
            );

            CREATE INDEX IF NOT EXISTS ix_cost_events_account_created
            ON cost_events(account_id, created_at);

            CREATE INDEX IF NOT EXISTS ix_cost_events_wallet_created
            ON cost_events(wallet_id, created_at);

            CREATE TABLE IF NOT EXISTS account_owner_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_user_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                binding_method TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                verified_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform_user_id, account_id),
                FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );

            CREATE INDEX IF NOT EXISTS ix_account_owner_bindings_account
            ON account_owner_bindings(account_id);

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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );

            CREATE INDEX IF NOT EXISTS ix_outbound_messages_account_date
            ON outbound_messages(account_id, quota_date, status);

            CREATE INDEX IF NOT EXISTS ix_outbound_messages_status_created
            ON outbound_messages(status, created_at);

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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );

            CREATE INDEX IF NOT EXISTS ix_proactive_account_state_due
            ON proactive_account_state(enabled, next_scan_at, cooldown_until);

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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                FOREIGN KEY(memory_item_id) REFERENCES dreaming_memory_items(id)
            );

            CREATE INDEX IF NOT EXISTS ix_memory_events_account_created
            ON memory_events(account_id, created_at);

            CREATE INDEX IF NOT EXISTS ix_memory_events_item
            ON memory_events(memory_item_id, created_at);

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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                latency_ms INTEGER,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
        _ensure_column(conn, "accounts", "onboarding_state", "TEXT NOT NULL DEFAULT 'pending'")
        _ensure_column(conn, "accounts", "onboarding_updated_at", "TEXT")
        _ensure_column(conn, "outbound_messages", "product_category", "TEXT")
        _ensure_column(conn, "outbound_messages", "policy_version", "TEXT")
        _ensure_column(conn, "outbound_messages", "policy_reason", "TEXT")
        _ensure_column(conn, "outbound_messages", "scheduled_at", "TEXT")
        _ensure_column(conn, "reminders", "recur_rule", "TEXT")
        _ensure_column(conn, "reminders", "sent_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "reminders", "last_sent_at", "TEXT")
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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


# ---------------------------------------------------------------------------
# Runtime health
# ---------------------------------------------------------------------------

def _runtime_timestamp(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).isoformat(timespec="seconds")


def record_scheduler_heartbeat(
    *,
    service: str,
    status: str,
    error: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    seen_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record the latest externally visible heartbeat for a scheduler process."""
    cleaned_service = _clean_text(service)
    if not cleaned_service:
        raise ValueError("service is required")
    cleaned_status = _clean_text(status) or "running"
    now = _runtime_timestamp(seen_at)
    last_success_at = now if cleaned_status in {"ok", "success"} else None
    last_error_at = now if cleaned_status in {"error", "failed"} else None
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_heartbeats(
                service, status, last_seen_at, last_success_at, last_error_at,
                last_error, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service) DO UPDATE SET
                status = excluded.status,
                last_seen_at = excluded.last_seen_at,
                last_success_at = COALESCE(excluded.last_success_at, scheduler_heartbeats.last_success_at),
                last_error_at = COALESCE(excluded.last_error_at, scheduler_heartbeats.last_error_at),
                last_error = excluded.last_error,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                cleaned_service,
                cleaned_status,
                now,
                last_success_at,
                last_error_at,
                error,
                metadata_json,
                now,
            ),
        )
    heartbeat = get_scheduler_heartbeat(cleaned_service)
    if heartbeat is None:
        raise RuntimeError("scheduler heartbeat write failed")
    return heartbeat


def get_scheduler_heartbeat(service: str) -> Optional[Dict[str, Any]]:
    cleaned_service = _clean_text(service)
    if not cleaned_service:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                service, status, last_seen_at, last_success_at, last_error_at,
                last_error, metadata_json, created_at, updated_at
            FROM scheduler_heartbeats
            WHERE service = ?
            """,
            (cleaned_service,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    return result


def list_scheduler_heartbeats() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                service, status, last_seen_at, last_success_at, last_error_at,
                last_error, metadata_json, created_at, updated_at
            FROM scheduler_heartbeats
            ORDER BY service
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# FAQ public messages
# ---------------------------------------------------------------------------

_FAQ_MESSAGE_STATUSES = {"pending", "published", "rejected"}
_FAQ_MODERATION_STATUSES = {"safe", "needs_review", "failed"}


def _decode_faq_message(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["moderation_categories"] = json.loads(
        item.pop("moderation_categories_json") or "[]"
    )
    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    return item


def get_faq_message(*, message_id: str) -> Optional[Dict[str, Any]]:
    """Return a FAQ message by id, including unpublished rows."""
    cleaned_id = _clean_text(message_id)
    if not cleaned_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM faq_messages WHERE id = ?",
            (cleaned_id,),
        ).fetchone()
    return _decode_faq_message(row) if row else None


def create_faq_message(
    *,
    author_name: Optional[str],
    content: str,
    status: str,
    moderation_status: str,
    moderation_reason: Optional[str] = None,
    moderation_categories: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a FAQ main message or one-level reply.

    Replies may only target published top-level messages.  Published replies
    increment the parent reply_count immediately.
    """
    cleaned_content = _clean_text(content)
    if not cleaned_content:
        raise ValueError("content is required")
    cleaned_status = _clean_text(status) or "pending"
    if cleaned_status not in _FAQ_MESSAGE_STATUSES:
        raise ValueError("invalid faq message status")
    cleaned_moderation_status = _clean_text(moderation_status) or "needs_review"
    if cleaned_moderation_status not in _FAQ_MODERATION_STATUSES:
        raise ValueError("invalid faq moderation status")
    cleaned_parent_id = _clean_text(parent_id)
    message_id = _new_id("faq")
    published_at_expr = "CURRENT_TIMESTAMP" if cleaned_status == "published" else "NULL"
    categories_json = json.dumps(moderation_categories or [], ensure_ascii=False)
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with connect() as conn:
        if cleaned_parent_id:
            parent = conn.execute(
                "SELECT id, parent_id, status FROM faq_messages WHERE id = ?",
                (cleaned_parent_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("parent message not found")
            if parent["parent_id"] is not None:
                raise ValueError("replies can only target main messages")
            if parent["status"] != "published":
                raise ValueError("parent message is not published")

        conn.execute(
            f"""
            INSERT INTO faq_messages(
                id, parent_id, author_name, content, status, moderation_status,
                moderation_reason, moderation_categories_json, metadata_json,
                published_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {published_at_expr}, CURRENT_TIMESTAMP)
            """,
            (
                message_id,
                cleaned_parent_id,
                _clean_text(author_name),
                cleaned_content,
                cleaned_status,
                cleaned_moderation_status,
                _clean_text(moderation_reason),
                categories_json,
                metadata_json,
            ),
        )
        if cleaned_parent_id and cleaned_status == "published":
            conn.execute(
                """
                UPDATE faq_messages
                SET reply_count = reply_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cleaned_parent_id,),
            )
        row = conn.execute(
            "SELECT * FROM faq_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("faq message was not created")
    return _decode_faq_message(row)


def list_published_faq_messages(
    *,
    limit: int = 50,
    replies_per_parent: int = 20,
) -> List[Dict[str, Any]]:
    """Return published top-level FAQ messages with published one-level replies."""
    clean_limit = max(1, min(int(limit), 100))
    clean_reply_limit = max(0, min(int(replies_per_parent), 50))
    with connect() as conn:
        parent_rows = conn.execute(
            """
            SELECT *
            FROM faq_messages
            WHERE parent_id IS NULL
              AND status = 'published'
            ORDER BY published_at DESC, created_at DESC
            LIMIT ?
            """,
            (clean_limit,),
        ).fetchall()
        parents = [_decode_faq_message(row) for row in parent_rows]
        for parent in parents:
            if clean_reply_limit == 0:
                parent["replies"] = []
                continue
            reply_rows = conn.execute(
                """
                SELECT *
                FROM faq_messages
                WHERE parent_id = ?
                  AND status = 'published'
                ORDER BY published_at ASC, created_at ASC
                LIMIT ?
                """,
                (parent["id"], clean_reply_limit),
            ).fetchall()
            parent["replies"] = [_decode_faq_message(row) for row in reply_rows]
    return parents


def like_faq_message(*, message_id: str, voter_key: str) -> Optional[Dict[str, Any]]:
    """Like a published FAQ message once per voter_key and return the updated row."""
    cleaned_id = _clean_text(message_id)
    cleaned_voter_key = _clean_text(voter_key)
    if not cleaned_id or not cleaned_voter_key:
        return None
    with connect() as conn:
        message = conn.execute(
            "SELECT id FROM faq_messages WHERE id = ? AND status = 'published'",
            (cleaned_id,),
        ).fetchone()
        if message is None:
            return None
        liked = False
        try:
            conn.execute(
                """
                INSERT INTO faq_message_likes(id, message_id, voter_key)
                VALUES (?, ?, ?)
                """,
                (_new_id("fqlike"), cleaned_id, cleaned_voter_key),
            )
            liked = True
        except sqlite3.IntegrityError:
            liked = False
        if liked:
            conn.execute(
                """
                UPDATE faq_messages
                SET like_count = like_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cleaned_id,),
            )
        row = conn.execute(
            "SELECT * FROM faq_messages WHERE id = ?",
            (cleaned_id,),
        ).fetchone()
    if row is None:
        return None
    result = _decode_faq_message(row)
    result["liked"] = liked
    return result


def get_ops_metrics(*, window_minutes: int = 60) -> Dict[str, Any]:
    window = max(int(window_minutes), 1)
    modifier = f"-{window} minutes"
    with connect() as conn:
        message_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound_total,
                SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound_total,
                SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS error_total,
                AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms ELSE NULL END) AS avg_latency_ms,
                MAX(latency_ms) AS max_latency_ms
            FROM messages
            WHERE created_at >= datetime('now', ?)
            """,
            (modifier,),
        ).fetchone()
        outbound_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_total,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_total,
                SUM(CASE WHEN status IN ('pending', 'sending') THEN 1 ELSE 0 END) AS pending_total
            FROM outbound_messages
            WHERE created_at >= datetime('now', ?)
            """,
            (modifier,),
        ).fetchone()
        binding_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status IN ('completed', 'already_connected') THEN 1 ELSE 0 END) AS success_total,
                SUM(CASE WHEN status IN ('failed', 'expired', 'cancelled') THEN 1 ELSE 0 END) AS failed_total
            FROM binding_intents
            WHERE created_at >= datetime('now', ?)
            """,
            (modifier,),
        ).fetchone()
        account_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_total,
                SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled_total
            FROM accounts
            """
        ).fetchone()
        recent_message_errors = conn.execute(
            """
            SELECT id, account_id, direction, role, message_type, latency_ms, error, created_at
            FROM messages
            WHERE error IS NOT NULL AND error != ''
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()
        recent_outbound_errors = conn.execute(
            """
            SELECT id, account_id, source, status, attempts, error, created_at, updated_at
            FROM outbound_messages
            WHERE error IS NOT NULL AND error != ''
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

    def _row_count(row: sqlite3.Row, key: str) -> int:
        return int(row[key] or 0) if row else 0

    avg_latency = message_row["avg_latency_ms"] if message_row else None
    return {
        "window_minutes": window,
        "accounts": {
            "total": _row_count(account_row, "total"),
            "active": _row_count(account_row, "active_total"),
            "disabled": _row_count(account_row, "disabled_total"),
        },
        "messages": {
            "total": _row_count(message_row, "total"),
            "inbound_total": _row_count(message_row, "inbound_total"),
            "outbound_total": _row_count(message_row, "outbound_total"),
            "error_total": _row_count(message_row, "error_total"),
            "avg_latency_ms": round(float(avg_latency), 1) if avg_latency is not None else None,
            "max_latency_ms": _row_count(message_row, "max_latency_ms"),
        },
        "outbound_messages": {
            "total": _row_count(outbound_row, "total"),
            "sent_total": _row_count(outbound_row, "sent_total"),
            "failed_total": _row_count(outbound_row, "failed_total"),
            "pending_total": _row_count(outbound_row, "pending_total"),
        },
        "binding_intents": {
            "total": _row_count(binding_row, "total"),
            "success_total": _row_count(binding_row, "success_total"),
            "failed_total": _row_count(binding_row, "failed_total"),
        },
        "recent_errors": {
            "messages": [dict(row) for row in recent_message_errors],
            "outbound_messages": [dict(row) for row in recent_outbound_errors],
        },
    }


# ---------------------------------------------------------------------------
# Debug traces
# ---------------------------------------------------------------------------

def insert_debug_trace(
    *,
    trace_id: str,
    account_id: str,
    session_id: int,
    message_id: Optional[str],
    source: str,
    llm_model: Optional[str],
    system_prompt: Optional[str],
    messages: List[Dict[str, Any]],
    reply: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO debug_traces(
                    trace_id, account_id, session_id, message_id, source,
                    llm_model, system_prompt, messages_json, reply,
                    metadata_json, latency_ms, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    account_id,
                    session_id,
                    message_id,
                    source,
                    llm_model,
                    system_prompt,
                    json.dumps(messages, ensure_ascii=False),
                    reply,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    latency_ms,
                    error,
                ),
            )
            return int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        return None


def list_debug_traces(
    *,
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, trace_id, account_id, session_id, message_id, source,
                llm_model, reply, latency_ms, error, created_at
            FROM debug_traces
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_debug_trace(*, trace_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, trace_id, account_id, session_id, message_id, source,
                llm_model, system_prompt, messages_json, reply,
                metadata_json, latency_ms, error, created_at
            FROM debug_traces
            WHERE trace_id = ?
            """,
            (trace_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["messages"] = json.loads(item.pop("messages_json") or "[]")
    except json.JSONDecodeError:
        item["messages"] = []
        item["messages_decode_error"] = True
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


# ---------------------------------------------------------------------------
# Tool invocations / search provider runs
# ---------------------------------------------------------------------------

def _decode_tool_invocation(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field, default in (
        ("args_json", "args", {}),
        ("result_json", "result", {}),
    ):
        raw_json = item.pop(source_field, None)
        try:
            value = json.loads(raw_json or json.dumps(default))
        except json.JSONDecodeError:
            value = default
            item[f"{target_field}_decode_error"] = True
        item[target_field] = value
    return item


def _decode_search_provider_run(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field, default in (
        ("request_json", "request", {}),
        ("response_json", "response", {}),
    ):
        raw_json = item.pop(source_field, None)
        try:
            value = json.loads(raw_json or json.dumps(default))
        except json.JSONDecodeError:
            value = default
            item[f"{target_field}_decode_error"] = True
        item[target_field] = value
    return item


def create_tool_invocation(
    *,
    account_id: str,
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    session_id: Optional[int] = None,
    message_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    status: str = "running",
    result: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_tool_name = _clean_text(tool_name)
    cleaned_status = _clean_text(status) or "running"
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_tool_name:
        raise ValueError("tool_name is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tool_invocations(
                account_id, session_id, message_id, tool_call_id, tool_name,
                status, args_json, result_json, latency_ms, error, finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                    CURRENT_TIMESTAMP)
            """,
            (
                cleaned_account_id,
                session_id,
                _clean_text(message_id),
                _clean_text(tool_call_id),
                cleaned_tool_name,
                cleaned_status,
                json.dumps(args or {}, ensure_ascii=False),
                json.dumps(result or {}, ensure_ascii=False),
                latency_ms,
                _clean_text(error),
                1 if finished else 0,
            ),
        )
        row = conn.execute(
            "SELECT * FROM tool_invocations WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("tool_invocation was not created")
    return _decode_tool_invocation(row)


def get_tool_invocation(*, tool_invocation_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tool_invocations WHERE id = ?",
            (tool_invocation_id,),
        ).fetchone()
    return _decode_tool_invocation(row) if row else None


def list_tool_invocations(
    *,
    account_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if tool_name:
        clauses.append("tool_name = ?")
        params.append(tool_name)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM tool_invocations
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_tool_invocation(row) for row in rows]


def update_tool_invocation(
    *,
    tool_invocation_id: int,
    status: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> Optional[Dict[str, Any]]:
    current = get_tool_invocation(tool_invocation_id=tool_invocation_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE tool_invocations
            SET status = COALESCE(?, status),
                result_json = COALESCE(?, result_json),
                latency_ms = COALESCE(?, latency_ms),
                error = ?,
                finished_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE finished_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                _clean_text(status),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                latency_ms,
                _clean_text(error),
                1 if finished else 0,
                tool_invocation_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM tool_invocations WHERE id = ?",
            (tool_invocation_id,),
        ).fetchone()
    return _decode_tool_invocation(row) if row else None


def create_search_provider_run(
    *,
    account_id: str,
    provider: str,
    request: Optional[Dict[str, Any]] = None,
    tool_invocation_id: Optional[int] = None,
    task_id: Optional[str] = None,
    attempt: int = 1,
    status: str = "running",
    response: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_provider = _clean_text(provider)
    cleaned_status = _clean_text(status) or "running"
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_provider:
        raise ValueError("provider is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_provider_runs(
                tool_invocation_id, task_id, account_id, provider, attempt,
                status, request_json, response_json, finished_at, latency_ms,
                error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                    ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                tool_invocation_id,
                _clean_text(task_id),
                cleaned_account_id,
                cleaned_provider,
                max(1, int(attempt or 1)),
                cleaned_status,
                json.dumps(request or {}, ensure_ascii=False),
                json.dumps(response or {}, ensure_ascii=False),
                1 if finished else 0,
                latency_ms,
                _clean_text(error),
            ),
        )
        row = conn.execute(
            "SELECT * FROM search_provider_runs WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("search_provider_run was not created")
    return _decode_search_provider_run(row)


def list_search_provider_runs(
    *,
    account_id: Optional[str] = None,
    tool_invocation_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if tool_invocation_id is not None:
        clauses.append("tool_invocation_id = ?")
        params.append(tool_invocation_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM search_provider_runs
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_search_provider_run(row) for row in rows]


# ---------------------------------------------------------------------------
# Dreaming runs / memory items / memory events
# ---------------------------------------------------------------------------

def _decode_json_field(
    item: Dict[str, Any],
    *,
    source_field: str,
    target_field: str,
    default: Any,
) -> Dict[str, Any]:
    raw_json = item.pop(source_field, None)
    try:
        item[target_field] = json.loads(raw_json or json.dumps(default))
    except json.JSONDecodeError:
        item[target_field] = default
        item[f"{target_field}_decode_error"] = True
    return item


# ---------------------------------------------------------------------------
# Admin access events
# ---------------------------------------------------------------------------

def upsert_admin_user(
    *,
    admin_user_id: str,
    role: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    status: str = "active",
) -> Dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_users(id, email, display_name, role, status, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                email = COALESCE(excluded.email, admin_users.email),
                display_name = COALESCE(excluded.display_name, admin_users.display_name),
                role = excluded.role,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (admin_user_id, email, display_name, role, status),
        )
        row = conn.execute(
            "SELECT * FROM admin_users WHERE id = ?",
            (admin_user_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("admin_user was not created")
    return dict(row)


def get_admin_user(*, admin_user_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE id = ?",
            (admin_user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_admin_users(*, limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM admin_users
            ORDER BY role ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _decode_admin_plaintext_grant(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field in (
        ("account_scope_json", "account_scope"),
        ("resource_scope_json", "resource_scope"),
    ):
        raw_json = item.pop(source_field, None)
        try:
            value = json.loads(raw_json or "[]")
        except json.JSONDecodeError:
            value = []
            item[f"{target_field}_decode_error"] = True
        item[target_field] = value if isinstance(value, list) else []
    return item


def create_admin_plaintext_grant(
    *,
    requester_admin_user_id: str,
    reason: str,
    account_scope: Optional[List[str]] = None,
    resource_scope: Optional[List[str]] = None,
    time_scope_start: Optional[str] = None,
    time_scope_end: Optional[str] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO admin_plaintext_grants(
                requester_admin_user_id, reason, account_scope_json,
                resource_scope_json, time_scope_start, time_scope_end
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                requester_admin_user_id,
                reason,
                json.dumps(account_scope or [], ensure_ascii=False),
                json.dumps(resource_scope or [], ensure_ascii=False),
                time_scope_start,
                time_scope_end,
            ),
        )
        row = conn.execute(
            "SELECT * FROM admin_plaintext_grants WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("admin_plaintext_grant was not created")
    return _decode_admin_plaintext_grant(row)


def get_admin_plaintext_grant(*, grant_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM admin_plaintext_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
    return _decode_admin_plaintext_grant(row) if row else None


def list_admin_plaintext_grants(
    *,
    requester_admin_user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if requester_admin_user_id:
        clauses.append("requester_admin_user_id = ?")
        params.append(requester_admin_user_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM admin_plaintext_grants
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_admin_plaintext_grant(row) for row in rows]


def update_admin_plaintext_grant_status(
    *,
    grant_id: int,
    status: str,
    approver_admin_user_id: Optional[str] = None,
    approved_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    revoked_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE admin_plaintext_grants
            SET status = ?,
                approver_admin_user_id = COALESCE(?, approver_admin_user_id),
                approved_at = COALESCE(?, approved_at),
                expires_at = COALESCE(?, expires_at),
                revoked_at = COALESCE(?, revoked_at),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                approver_admin_user_id,
                approved_at,
                expires_at,
                revoked_at,
                grant_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM admin_plaintext_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
    return _decode_admin_plaintext_grant(row) if row else None


def find_active_admin_plaintext_grant(
    *,
    requester_admin_user_id: str,
    account_id: str,
    resource_type: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM admin_plaintext_grants
            WHERE requester_admin_user_id = ?
              AND status = 'approved'
              AND expires_at IS NOT NULL
              AND expires_at > ?
            ORDER BY expires_at DESC, id DESC
            """,
            (requester_admin_user_id, now),
        ).fetchall()
    for row in rows:
        grant = _decode_admin_plaintext_grant(row)
        account_scope = [str(item) for item in grant.get("account_scope") or []]
        resource_scope = [str(item) for item in grant.get("resource_scope") or []]
        if account_scope and account_id not in account_scope:
            continue
        if resource_scope and resource_type not in resource_scope:
            continue
        return grant
    return None


def insert_admin_access_event(
    *,
    admin_user_id: Optional[str],
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    account_id: Optional[str] = None,
    plaintext: bool = False,
    grant_id: Optional[int] = None,
    reason: Optional[str] = None,
    request_path: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO admin_access_events(
                admin_user_id, action, resource_type, resource_id, account_id,
                plaintext, grant_id, reason, request_path, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admin_user_id,
                action,
                resource_type,
                resource_id,
                account_id,
                1 if plaintext else 0,
                grant_id,
                reason,
                request_path,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)


def list_admin_access_events(
    *,
    account_id: Optional[str] = None,
    plaintext: Optional[bool] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if plaintext is not None:
        clauses.append("plaintext = ?")
        params.append(1 if plaintext else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, admin_user_id, action, resource_type, resource_id,
                account_id, plaintext, grant_id, reason, request_path,
                metadata_json, created_at
            FROM admin_access_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["plaintext"] = bool(item.get("plaintext"))
        events.append(
            _decode_json_field(
                item,
                source_field="metadata_json",
                target_field="metadata",
                default={},
            )
        )
    return events


def create_dreaming_run(
    *,
    account_id: str,
    source_type: str,
    source_session_id: Optional[int] = None,
    source_business_day: Optional[str] = None,
    status: str = "running",
    prompt_version: str,
    llm_model: Optional[str] = None,
    input_hash: Optional[str] = None,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts(id, updated_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
            """,
            (account_id,),
        )
        cursor = conn.execute(
            """
            INSERT INTO dreaming_runs(
                account_id, source_type, source_session_id, source_business_day,
                status, prompt_version, llm_model, input_hash, actor_type, actor_id,
                started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                account_id,
                source_type,
                source_session_id,
                source_business_day,
                status,
                prompt_version,
                llm_model,
                input_hash,
                actor_type,
                actor_id,
            ),
        )
        run_id = int(cursor.lastrowid)
    run = get_dreaming_run(run_id=run_id)
    if run is None:
        raise RuntimeError("dreaming_run was not created")
    return run


def update_dreaming_run(
    *,
    run_id: int,
    status: Optional[str] = None,
    output: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    token_input: Optional[int] = None,
    token_output: Optional[int] = None,
    completed: bool = False,
) -> Optional[Dict[str, Any]]:
    current = get_dreaming_run(run_id=run_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE dreaming_runs
            SET status = COALESCE(?, status),
                output_json = COALESCE(?, output_json),
                error = ?,
                token_input = COALESCE(?, token_input),
                token_output = COALESCE(?, token_output),
                completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                json.dumps(output, ensure_ascii=False) if output is not None else None,
                error,
                token_input,
                token_output,
                1 if completed else 0,
                run_id,
            ),
        )
    return get_dreaming_run(run_id=run_id)


def get_dreaming_run(*, run_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, account_id, source_type, source_session_id, source_business_day,
                status, prompt_version, llm_model, input_hash, output_json,
                error, token_input, token_output, actor_type, actor_id,
                started_at, completed_at, created_at, updated_at
            FROM dreaming_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return _decode_json_field(
        dict(row),
        source_field="output_json",
        target_field="output",
        default={},
    )


def list_dreaming_runs(
    *,
    account_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 200)))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, account_id, source_type, source_session_id, source_business_day,
                status, prompt_version, llm_model, input_hash, output_json,
                error, token_input, token_output, actor_type, actor_id,
                started_at, completed_at, created_at, updated_at
            FROM dreaming_runs
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        _decode_json_field(
            dict(row),
            source_field="output_json",
            target_field="output",
            default={},
        )
        for row in rows
    ]


def insert_dreaming_memory_item(
    *,
    account_id: str,
    dreaming_run_id: int,
    source_type: str,
    source_session_id: Optional[int],
    source_daily_note_date: Optional[str],
    operation: str,
    target_file: str,
    category: str,
    memory_text: str,
    base_text_hash: Optional[str] = None,
    diff: Optional[Dict[str, Any]] = None,
    importance: str = "medium",
    confidence: float = 0.0,
    sensitivity: str = "normal",
    apply_status: str = "generated",
    skip_reason: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO dreaming_memory_items(
                account_id, dreaming_run_id, source_type, source_session_id,
                source_daily_note_date, operation, target_file, category, memory_text,
                base_text_hash, diff_json, importance, confidence, sensitivity,
                apply_status, skip_reason, reason, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                dreaming_run_id,
                source_type,
                source_session_id,
                source_daily_note_date,
                operation,
                target_file,
                category,
                memory_text,
                base_text_hash,
                json.dumps(diff or {}, ensure_ascii=False),
                importance,
                float(confidence),
                sensitivity,
                apply_status,
                skip_reason,
                reason,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        item_id = int(cursor.lastrowid)
    item = get_dreaming_memory_item(item_id=item_id)
    if item is None:
        raise RuntimeError("dreaming_memory_item was not created")
    return item


def update_dreaming_memory_item_status(
    *,
    item_id: int,
    apply_status: str,
    skip_reason: Optional[str] = None,
    diff: Optional[Dict[str, Any]] = None,
    base_text_hash: Optional[str] = None,
    applied: bool = False,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE dreaming_memory_items
            SET apply_status = ?,
                skip_reason = ?,
                diff_json = COALESCE(?, diff_json),
                base_text_hash = COALESCE(?, base_text_hash),
                applied_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE applied_at END
            WHERE id = ?
            """,
            (
                apply_status,
                skip_reason,
                json.dumps(diff, ensure_ascii=False) if diff is not None else None,
                base_text_hash,
                1 if applied else 0,
                item_id,
            ),
        )
    return get_dreaming_memory_item(item_id=item_id)


def get_dreaming_memory_item(*, item_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, account_id, dreaming_run_id, source_type, source_session_id,
                source_daily_note_date, operation, target_file, category, memory_text,
                base_text_hash, diff_json, importance, confidence, sensitivity,
                apply_status, skip_reason, reason, metadata_json, created_at, applied_at
            FROM dreaming_memory_items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    item = _decode_json_field(
        dict(row),
        source_field="diff_json",
        target_field="diff",
        default={},
    )
    return _decode_json_field(
        item,
        source_field="metadata_json",
        target_field="metadata",
        default={},
    )


def list_dreaming_memory_items(
    *,
    account_id: Optional[str] = None,
    dreaming_run_id: Optional[int] = None,
    apply_status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if dreaming_run_id is not None:
        clauses.append("dreaming_run_id = ?")
        params.append(dreaming_run_id)
    if apply_status:
        clauses.append("apply_status = ?")
        params.append(apply_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, account_id, dreaming_run_id, source_type, source_session_id,
                source_daily_note_date, operation, target_file, category, memory_text,
                base_text_hash, diff_json, importance, confidence, sensitivity,
                apply_status, skip_reason, reason, metadata_json, created_at, applied_at
            FROM dreaming_memory_items
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    items = []
    for row in rows:
        item = _decode_json_field(
            dict(row),
            source_field="diff_json",
            target_field="diff",
            default={},
        )
        items.append(
            _decode_json_field(
                item,
                source_field="metadata_json",
                target_field="metadata",
                default={},
            )
        )
    return items


def insert_memory_event(
    *,
    account_id: str,
    memory_item_id: Optional[int],
    event_type: str,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
    before_text: Optional[str] = None,
    after_text: Optional[str] = None,
    diff_text: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_events(
                account_id, memory_item_id, event_type, actor_type, actor_id,
                before_text, after_text, diff_text, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                memory_item_id,
                event_type,
                actor_type,
                actor_id,
                before_text,
                after_text,
                diff_text,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        event_id = int(cursor.lastrowid)
        row = conn.execute(
            """
            SELECT
                id, account_id, memory_item_id, event_type, actor_type, actor_id,
                before_text, after_text, diff_text, metadata_json, created_at
            FROM memory_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
    event = _decode_json_field(
        dict(row),
        source_field="metadata_json",
        target_field="metadata",
        default={},
    )
    return event


def list_memory_events(
    *,
    account_id: Optional[str] = None,
    memory_item_id: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if memory_item_id is not None:
        clauses.append("memory_item_id = ?")
        params.append(memory_item_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, account_id, memory_item_id, event_type, actor_type, actor_id,
                before_text, after_text, diff_text, metadata_json, created_at
            FROM memory_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        _decode_json_field(
            dict(row),
            source_field="metadata_json",
            target_field="metadata",
            default={},
        )
        for row in rows
    ]


def update_session_summary(
    *,
    session_id: int,
    session_summary: Optional[str],
    carryover_summary: Optional[str],
    summary_model: Optional[str],
    summary_prompt_version: Optional[str],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET session_summary = COALESCE(?, session_summary),
                carryover_summary = COALESCE(?, carryover_summary),
                summary_model = COALESCE(?, summary_model),
                summary_prompt_version = COALESCE(?, summary_prompt_version),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                session_summary,
                carryover_summary,
                summary_model,
                summary_prompt_version,
                session_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def close_session(
    *,
    session_id: int,
    close_reason: str,
    archived_session_key: Optional[str] = None,
    session_summary: Optional[str] = None,
    carryover_summary: Optional[str] = None,
    summary_model: Optional[str] = None,
    summary_prompt_version: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET session_key = COALESCE(?, session_key),
                status = 'closed',
                ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP),
                close_reason = COALESCE(close_reason, ?),
                session_summary = COALESCE(?, session_summary),
                carryover_summary = COALESCE(?, carryover_summary),
                summary_model = COALESCE(?, summary_model),
                summary_prompt_version = COALESCE(?, summary_prompt_version),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                archived_session_key,
                close_reason,
                session_summary,
                carryover_summary,
                summary_model,
                summary_prompt_version,
                session_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_active_sessions_for_business_day_before(
    *,
    business_day: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sessions
            WHERE session_key = ?
              AND status = 'active'
              AND business_day IS NOT NULL
              AND business_day != ''
              AND business_day < ?
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (
                ACCOUNT_ACTIVE_SESSION_KEY,
                business_day,
                max(1, min(int(limit), 500)),
            ),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Session / account creation
# ---------------------------------------------------------------------------

def upsert_channel_binding(
    *,
    account_id: str,
    channel: str,
    session_key: str,
    channel_account_id: Optional[str],
    sender_id: Optional[str],
    chat_id: Optional[str],
    raw_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_bindings(
                account_id, channel, session_key, channel_account_id,
                sender_id, chat_id, raw_identity_json, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, channel, session_key) DO UPDATE SET
                channel_account_id = COALESCE(excluded.channel_account_id, channel_bindings.channel_account_id),
                sender_id = COALESCE(excluded.sender_id, channel_bindings.sender_id),
                chat_id = COALESCE(excluded.chat_id, channel_bindings.chat_id),
                raw_identity_json = excluded.raw_identity_json,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (
                account_id,
                channel,
                session_key,
                channel_account_id,
                sender_id,
                chat_id,
                json.dumps(raw_identity or {}, ensure_ascii=False),
            ),
        )
        # Deduplicate: remove stale rows for the same channel_account_id (different session_key).
        # This prevents two rows accumulating when QR completion and first inbound message
        # use different session_keys but refer to the same channel_account_id.
        if channel_account_id:
            aliases = _channel_account_id_aliases(channel_account_id)
            placeholders = ", ".join("?" for _ in aliases)
            conn.execute(
                f"""
                DELETE FROM channel_bindings
                WHERE account_id = ? AND channel = ?
                  AND channel_account_id IN ({placeholders})
                  AND session_key != ?
                """,
                (account_id, channel, *aliases, session_key),
            )
        row = conn.execute(
            """
            SELECT
                id, account_id, channel, session_key, channel_account_id,
                sender_id, chat_id, raw_identity_json, first_seen_at, last_seen_at
            FROM channel_bindings
            WHERE account_id = ? AND channel = ? AND session_key = ?
            """,
            (account_id, channel, session_key),
        ).fetchone()
    item = dict(row)
    try:
        item["raw_identity"] = json.loads(item.pop("raw_identity_json") or "{}")
    except json.JSONDecodeError:
        item["raw_identity"] = {}
        item["raw_identity_decode_error"] = True
    return item


def list_channel_bindings_for_account(*, account_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, account_id, channel, session_key, channel_account_id,
                sender_id, chat_id, raw_identity_json, first_seen_at, last_seen_at
            FROM channel_bindings
            WHERE account_id = ?
            ORDER BY last_seen_at DESC, id DESC
            """,
            (account_id,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["raw_identity"] = json.loads(item.pop("raw_identity_json") or "{}")
        except json.JSONDecodeError:
            item["raw_identity"] = {}
            item["raw_identity_decode_error"] = True
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Web onboarding
# ---------------------------------------------------------------------------

def create_or_get_platform_user_by_phone(
    *,
    phone: str,
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_phone = _normalize_phone(phone)
    cleaned_display_name = _clean_text(display_name)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, platform_users.display_name),
                updated_at = CURRENT_TIMESTAMP
            """,
            (_new_id("user"), normalized_phone, cleaned_display_name),
        )
        row = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE phone = ?
            """,
            (normalized_phone,),
        ).fetchone()
    return dict(row)


def get_platform_user(*, platform_user_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE id = ?
            """,
            (platform_user_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_subscription_for_user(
    *,
    platform_user_id: str,
    plan: str = "free",
    status: str = "active",
) -> Dict[str, Any]:
    cleaned_plan = _clean_text(plan) or "free"
    cleaned_status = _clean_text(status) or "active"
    with connect() as conn:
        user = conn.execute(
            "SELECT id FROM platform_users WHERE id = ?",
            (platform_user_id,),
        ).fetchone()
        if user is None:
            raise ValueError("platform_user not found")
        existing = conn.execute(
            """
            SELECT id FROM subscriptions
            WHERE platform_user_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
        if existing:
            subscription_id = existing["id"]
            conn.execute(
                """
                UPDATE subscriptions
                SET plan = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cleaned_plan, cleaned_status, subscription_id),
            )
        else:
            subscription_id = _new_id("sub")
            conn.execute(
                """
                INSERT INTO subscriptions(id, platform_user_id, plan, status, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (subscription_id, platform_user_id, cleaned_plan, cleaned_status),
            )
        row = conn.execute(
            """
            SELECT id, platform_user_id, plan, status, created_at, updated_at
            FROM subscriptions
            WHERE id = ?
            """,
            (subscription_id,),
        ).fetchone()
    return dict(row)


def get_latest_subscription_for_user(
    *,
    platform_user_id: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, platform_user_id, plan, status, created_at, updated_at
            FROM subscriptions
            WHERE platform_user_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
    return dict(row) if row else None


def _decode_wallet_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    balance_shell_micros = int(item["balance_shell_micros"])
    item["balance_shell_micros"] = balance_shell_micros
    item["balance_shells"] = _format_shell_amount(balance_shell_micros)
    return item


def _decode_ledger_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    amount_shell_micros = int(item["amount_shell_micros"])
    balance_after_shell_micros = int(item["balance_after_shell_micros"])
    item["amount_shell_micros"] = amount_shell_micros
    item["amount_shells"] = _format_shell_amount(amount_shell_micros)
    item["balance_after_shell_micros"] = balance_after_shell_micros
    item["balance_after_shells"] = _format_shell_amount(balance_after_shell_micros)
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _decode_cost_event_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["billable_to_user"] = bool(item["billable_to_user"])
    item["computed_shell_micros"] = int(item["computed_shell_micros"] or 0)
    item["computed_shells"] = _format_shell_amount(item["computed_shell_micros"])
    item["model_price_multiplier"] = (
        int(item["model_price_multiplier_micros"] or 0) / 1_000_000
    )
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _ensure_wallet_in_conn(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    platform_user_id: str,
) -> sqlite3.Row:
    account = conn.execute(
        "SELECT id FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if account is None:
        raise ValueError("account not found")
    user = conn.execute(
        "SELECT id FROM platform_users WHERE id = ?",
        (platform_user_id,),
    ).fetchone()
    if user is None:
        raise ValueError("platform_user not found")
    conn.execute(
        """
        INSERT INTO entitlement_wallets(
            id, account_id, platform_user_id, balance_shell_micros, status, updated_at
        )
        VALUES (?, ?, ?, 0, 'active', CURRENT_TIMESTAMP)
        ON CONFLICT(account_id) DO NOTHING
        """,
        (_new_id("wallet"), account_id, platform_user_id),
    )
    row = conn.execute(
        """
        SELECT id, account_id, platform_user_id, balance_shell_micros, status,
               created_at, updated_at
        FROM entitlement_wallets
        WHERE account_id = ?
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("entitlement_wallet was not created")
    if row["platform_user_id"] != platform_user_id:
        logger.warning(
            "wallet platform_user_id mismatch account=%s wallet_owner=%s caller=%s — using existing wallet",
            account_id,
            row["platform_user_id"],
            platform_user_id,
        )
    return row


def ensure_wallet(
    *,
    account_id: str,
    platform_user_id: str,
) -> Dict[str, Any]:
    with connect() as conn:
        row = _ensure_wallet_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
    return _decode_wallet_row(row)


def _apply_wallet_ledger_in_conn(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    platform_user_id: str,
    amount_shell_micros: int,
    entry_type: str,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> sqlite3.Row:
    if amount_shell_micros == 0:
        raise ValueError("amount_shell_micros must not be zero")
    cleaned_entry_type = _clean_text(entry_type)
    cleaned_source_type = _clean_text(source_type)
    cleaned_idempotency_key = _clean_text(idempotency_key)
    if cleaned_entry_type not in {"credit", "debit"}:
        raise ValueError("entry_type must be credit or debit")
    if not cleaned_source_type:
        raise ValueError("source_type is required")
    if not cleaned_idempotency_key:
        raise ValueError("idempotency_key is required")

    existing = conn.execute(
        """
        SELECT *
        FROM entitlement_ledger
        WHERE idempotency_key = ?
        """,
        (cleaned_idempotency_key,),
    ).fetchone()
    if existing is not None:
        return existing

    wallet = _ensure_wallet_in_conn(
        conn,
        account_id=account_id,
        platform_user_id=platform_user_id,
    )
    ledger_id = _new_id("ledger")
    # Atomic increment: avoids TOCTOU race where two concurrent transactions
    # both read the same balance and overwrite each other's update.
    conn.execute(
        """
        UPDATE entitlement_wallets
        SET balance_shell_micros = balance_shell_micros + ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (int(amount_shell_micros), wallet["id"]),
    )
    balance_row = conn.execute(
        "SELECT balance_shell_micros FROM entitlement_wallets WHERE id = ?",
        (wallet["id"],),
    ).fetchone()
    balance_after = int(balance_row["balance_shell_micros"])
    conn.execute(
        """
        INSERT INTO entitlement_ledger(
            id, wallet_id, account_id, platform_user_id, entry_type,
            source_type, source_id, amount_shell_micros,
            balance_after_shell_micros, idempotency_key, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ledger_id,
            wallet["id"],
            account_id,
            platform_user_id,
            cleaned_entry_type,
            cleaned_source_type,
            _clean_text(source_id),
            int(amount_shell_micros),
            balance_after,
            cleaned_idempotency_key,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    row = conn.execute(
        "SELECT * FROM entitlement_ledger WHERE id = ?",
        (ledger_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("entitlement_ledger was not created")
    return row


def grant_shells(
    *,
    account_id: str,
    platform_user_id: str,
    amount_shell_micros: int,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if amount_shell_micros <= 0:
        raise ValueError("amount_shell_micros must be positive")
    with connect() as conn:
        row = _apply_wallet_ledger_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=int(amount_shell_micros),
            entry_type="credit",
            source_type=source_type,
            source_id=source_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
    return _decode_ledger_row(row)


def grant_new_user_shells(
    *,
    account_id: str,
    platform_user_id: str,
) -> Dict[str, Any]:
    return grant_shells(
        account_id=account_id,
        platform_user_id=platform_user_id,
        amount_shell_micros=NEW_USER_GRANT_SHELL_MICROS,
        source_type="new_user_grant",
        source_id=account_id,
        idempotency_key=f"new-user-grant-{account_id}",
        metadata={"grant_shells": NEW_USER_GRANT_SHELLS},
    )


def _estimate_tokens_from_text(text: Optional[str]) -> int:
    cleaned = text or ""
    if not cleaned:
        return 0
    # Conservative mixed Chinese/English approximation until provider usage is wired.
    return max(1, math.ceil(len(cleaned) / 2))


def _estimate_tokens_from_messages(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += _estimate_tokens_from_text(content)
        elif isinstance(content, list):
            total += _estimate_tokens_from_text(json.dumps(content, ensure_ascii=False))
    return total


def _shell_micros_for_tokens(
    *,
    billable_tokens: int,
    model_price_multiplier_micros: int = 1_000_000,
) -> int:
    if billable_tokens <= 0:
        return 0
    return math.ceil(
        int(billable_tokens)
        * int(model_price_multiplier_micros)
        * SHELL_MICROS_PER_SHELL
        / (SHELL_BILLABLE_TOKENS_PER_SHELL * 1_000_000)
    )


def get_platform_user_id_for_account(*, account_id: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT platform_user_id
            FROM account_owner_bindings
            WHERE account_id = ?
              AND status = 'active'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
    return str(row["platform_user_id"]) if row else None


def record_chat_usage_charge(
    *,
    account_id: str,
    model: str,
    messages: List[Dict[str, Any]],
    reply: str,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    model_price_multiplier_micros: int = 1_000_000,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    cleaned_idempotency_key = _clean_text(idempotency_key)
    if not cleaned_idempotency_key:
        raise ValueError("idempotency_key is required")

    estimated = input_tokens is None or output_tokens is None
    resolved_input_tokens = (
        int(input_tokens)
        if input_tokens is not None
        else _estimate_tokens_from_messages(messages)
    )
    resolved_output_tokens = (
        int(output_tokens)
        if output_tokens is not None
        else _estimate_tokens_from_text(reply)
    )
    billable_tokens = max(0, resolved_input_tokens) + max(0, resolved_output_tokens)
    computed_shell_micros = _shell_micros_for_tokens(
        billable_tokens=billable_tokens,
        model_price_multiplier_micros=model_price_multiplier_micros,
    )
    if computed_shell_micros <= 0:
        return None

    platform_user_id = get_platform_user_id_for_account(account_id=account_id)
    if platform_user_id is None:
        return None

    with connect() as conn:
        existing = conn.execute(
            """
            SELECT ce.*, l.id AS ledger_exists
            FROM cost_events ce
            LEFT JOIN entitlement_ledger l ON l.id = ce.entitlement_ledger_id
            WHERE ce.idempotency_key = ?
            """,
            (cleaned_idempotency_key,),
        ).fetchone()
        if existing is not None:
            event = _decode_cost_event_row(existing)
            ledger = None
            if event.get("entitlement_ledger_id"):
                ledger_row = conn.execute(
                    "SELECT * FROM entitlement_ledger WHERE id = ?",
                    (event["entitlement_ledger_id"],),
                ).fetchone()
                ledger = _decode_ledger_row(ledger_row) if ledger_row else None
            wallet_row = conn.execute(
                """
                SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                       created_at, updated_at
                FROM entitlement_wallets
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            return {
                "cost_event": event,
                "ledger": ledger,
                "wallet": _decode_wallet_row(wallet_row) if wallet_row else None,
            }

        wallet = _ensure_wallet_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
        event_id = _new_id("cost")
        ledger_idempotency_key = f"usage-charge-{cleaned_idempotency_key}"
        charge_metadata = {
            "estimated": estimated,
            "input_tokens": resolved_input_tokens,
            "output_tokens": resolved_output_tokens,
            "billable_tokens": billable_tokens,
            "model": model,
            **(metadata or {}),
        }
        ledger_row = _apply_wallet_ledger_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=-computed_shell_micros,
            entry_type="debit",
            source_type="usage_charge",
            source_id=source_id,
            idempotency_key=ledger_idempotency_key,
            metadata=charge_metadata,
        )
        conn.execute(
            """
            INSERT INTO cost_events(
                id, wallet_id, account_id, platform_user_id, cost_type,
                cost_owner, billable_to_user, model, input_tokens, output_tokens,
                billable_tokens, model_price_multiplier_micros, computed_shell_micros,
                entitlement_ledger_id, source_type, source_id, idempotency_key,
                metadata_json
            )
            VALUES (?, ?, ?, ?, 'llm_tokens', 'user', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                wallet["id"],
                account_id,
                platform_user_id,
                _clean_text(model),
                resolved_input_tokens,
                resolved_output_tokens,
                billable_tokens,
                int(model_price_multiplier_micros),
                computed_shell_micros,
                ledger_row["id"],
                _clean_text(source_type),
                _clean_text(source_id),
                cleaned_idempotency_key,
                json.dumps(charge_metadata, ensure_ascii=False),
            ),
        )
        event_row = conn.execute(
            "SELECT * FROM cost_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        wallet_row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                   created_at, updated_at
            FROM entitlement_wallets
            WHERE id = ?
            """,
            (wallet["id"],),
        ).fetchone()

    if event_row is None:
        raise RuntimeError("cost_event was not created")
    return {
        "cost_event": _decode_cost_event_row(event_row),
        "ledger": _decode_ledger_row(ledger_row),
        "wallet": _decode_wallet_row(wallet_row),
    }


def get_wallet_summary(
    *,
    account_id: str,
    ensure_grant: bool = False,
    create_if_missing: bool = True,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        owner = conn.execute(
            """
            SELECT b.platform_user_id
            FROM account_owner_bindings b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.account_id = ?
              AND b.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if owner is None:
            return None
        platform_user_id = owner["platform_user_id"]

    if ensure_grant:
        grant_new_user_shells(
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
    elif create_if_missing:
        ensure_wallet(account_id=account_id, platform_user_id=platform_user_id)

    with connect() as conn:
        wallet_row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                   created_at, updated_at
            FROM entitlement_wallets
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        if wallet_row is None:
            return None
        latest_ledger_row = conn.execute(
            """
            SELECT *
            FROM entitlement_ledger
            WHERE wallet_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (wallet_row["id"],),
        ).fetchone()
    wallet = _decode_wallet_row(wallet_row)
    latest_ledger = _decode_ledger_row(latest_ledger_row) if latest_ledger_row else None
    return {
        "wallet": wallet,
        "latest_ledger": latest_ledger,
        "display": {
            "balance": wallet["balance_shells"],
            "unit": "贝壳",
        },
    }


def list_wallet_ledger(
    *,
    account_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clean_limit = max(1, min(int(limit), 200))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM entitlement_ledger
            WHERE account_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (account_id, clean_limit),
        ).fetchall()
    return [_decode_ledger_row(row) for row in rows]


def create_ai4all_account_for_user(
    *,
    platform_user_id: str,
    display_name: Optional[str],
    system_prompt: Optional[str] = None,
    plan: str = "free",
    require_display_name: bool = True,
) -> Dict[str, Any]:
    cleaned_display_name = _clean_text(display_name)
    if require_display_name and not cleaned_display_name:
        raise ValueError("display_name is required")

    cleaned_prompt = _clean_text(system_prompt)
    with connect() as conn:
        user = conn.execute(
            "SELECT id FROM platform_users WHERE id = ?",
            (platform_user_id,),
        ).fetchone()
        if user is None:
            raise ValueError("platform_user not found")
        existing_count = conn.execute(
            """
            SELECT COUNT(*) FROM account_owner_bindings
            WHERE platform_user_id = ? AND status = 'active'
            """,
            (platform_user_id,),
        ).fetchone()[0]
        if existing_count >= 10:
            raise ValueError("platform_user has reached the maximum number of agents (10)")
        last_integrity_error = None
        for _ in range(_ACCOUNT_ID_GENERATION_RETRIES):
            account_id = _new_account_id()
            try:
                conn.execute(
                    """
                    INSERT INTO accounts(id, channel, display_name, updated_at)
                    VALUES (?, 'openclaw-weixin', ?, CURRENT_TIMESTAMP)
                    """,
                    (account_id, cleaned_display_name),
                )
                break
            except sqlite3.IntegrityError as err:
                if "accounts.id" not in str(err):
                    raise
                last_integrity_error = err
        else:
            raise RuntimeError("failed to generate a unique account_id") from last_integrity_error
        conn.execute(
            """
            INSERT INTO profiles(account_id, display_name, system_prompt, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (account_id, cleaned_display_name, cleaned_prompt),
        )
        cursor = conn.execute(
            """
            INSERT INTO account_owner_bindings(
                platform_user_id, account_id, binding_method, status, updated_at
            )
            VALUES (?, ?, 'web_onboarding', 'active', CURRENT_TIMESTAMP)
            """,
            (platform_user_id, account_id),
        )
        owner_binding_id = int(cursor.lastrowid)

    subscription = upsert_subscription_for_user(
        platform_user_id=platform_user_id,
        plan=plan,
    )
    grant_new_user_shells(
        account_id=account_id,
        platform_user_id=platform_user_id,
    )
    return {
        "account": get_account(account_id=account_id),
        "profile": get_profile_for_account(account_id=account_id),
        "owner_binding": get_account_owner_binding(owner_binding_id=owner_binding_id),
        "subscription": subscription,
    }


def get_first_active_account_for_user(
    *,
    platform_user_id: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT b.id, b.account_id
            FROM account_owner_bindings b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.platform_user_id = ?
              AND b.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
    if row is None:
        return None
    account = get_account(account_id=row["account_id"])
    if account is None:
        return None
    grant_new_user_shells(
        account_id=row["account_id"],
        platform_user_id=platform_user_id,
    )
    return {
        "account": account,
        "profile": get_profile_for_account(account_id=row["account_id"]),
        "owner_binding": get_account_owner_binding(owner_binding_id=int(row["id"])),
        "subscription": get_latest_subscription_for_user(
            platform_user_id=platform_user_id,
        ),
    }


def get_or_create_default_ai4all_account_for_user(
    *,
    platform_user_id: str,
    display_name: Optional[str] = None,
    plan: str = "free",
) -> Dict[str, Any]:
    existing = get_first_active_account_for_user(platform_user_id=platform_user_id)
    if existing is not None:
        return existing
    return create_ai4all_account_for_user(
        platform_user_id=platform_user_id,
        display_name=_clean_default_account_display_name(display_name),
        system_prompt=None,
        plan=plan,
        require_display_name=False,
    )


def get_account_owner_binding(
    *,
    owner_binding_id: int,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, binding_method, status,
                verified_at, created_at, updated_at
            FROM account_owner_bindings
            WHERE id = ?
            """,
            (owner_binding_id,),
        ).fetchone()
    return dict(row) if row else None


def list_account_owner_bindings_for_account(
    *,
    account_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, binding_method, status,
                verified_at, created_at, updated_at
            FROM account_owner_bindings
            WHERE account_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_binding_intent(
    *,
    platform_user_id: str,
    account_id: str,
    channel: str = "openclaw-weixin",
) -> Dict[str, Any]:
    cleaned_channel = _clean_text(channel) or "openclaw-weixin"
    binding_intent_id = _new_id("bind")
    openclaw_login_session_key = binding_intent_id
    manual_login_command = (
        "openclaw channels login "
        f"--channel {cleaned_channel} "
        f"--account {openclaw_login_session_key} "
        "--verbose"
    )

    with connect() as conn:
        owner = conn.execute(
            """
            SELECT id FROM account_owner_bindings
            WHERE platform_user_id = ?
              AND account_id = ?
              AND status = 'active'
            """,
            (platform_user_id, account_id),
        ).fetchone()
        if owner is None:
            raise ValueError("account is not owned by platform_user")
        conn.execute(
            """
            INSERT INTO binding_intents(
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, manual_login_command, expires_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'created', ?, datetime('now', '+30 minutes'), CURRENT_TIMESTAMP)
            """,
            (
                binding_intent_id,
                platform_user_id,
                account_id,
                openclaw_login_session_key,
                cleaned_channel,
                manual_login_command,
            ),
        )
    item = get_binding_intent(binding_intent_id=binding_intent_id)
    if item is None:
        raise RuntimeError("binding_intent was not created")
    return item


def update_binding_intent(
    *,
    binding_intent_id: str,
    status: Optional[str] = None,
    openclaw_login_session_key: Optional[str] = None,
    channel_account_id: Optional[str] = None,
    qr_data_url: Optional[str] = None,
    raw_result: Optional[Dict[str, Any]] = None,
    completed: bool = False,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_binding_intent(binding_intent_id=binding_intent_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE binding_intents
            SET status = ?,
                openclaw_login_session_key = ?,
                channel_account_id = COALESCE(?, channel_account_id),
                qr_data_url = ?,
                raw_result_json = ?,
                error = ?,
                completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status if status is not None else current["status"],
                openclaw_login_session_key
                if openclaw_login_session_key is not None
                else current["openclaw_login_session_key"],
                channel_account_id,
                qr_data_url if qr_data_url is not None else current.get("qr_data_url"),
                json.dumps(raw_result, ensure_ascii=False)
                if raw_result is not None
                else json.dumps(current.get("raw_result") or {}, ensure_ascii=False),
                error,
                1 if completed else 0,
                binding_intent_id,
            ),
        )
    return get_binding_intent(binding_intent_id=binding_intent_id)


def set_binding_intent_error(
    *,
    binding_intent_id: str,
    status: str,
    error: str,
    raw_result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_binding_intent(binding_intent_id=binding_intent_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE binding_intents
            SET status = ?,
                raw_result_json = ?,
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                json.dumps(
                    raw_result if raw_result is not None else current.get("raw_result") or {},
                    ensure_ascii=False,
                ),
                error,
                binding_intent_id,
            ),
        )
    return get_binding_intent(binding_intent_id=binding_intent_id)


def get_binding_intent(
    *,
    binding_intent_id: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE id = ?
            """,
            (binding_intent_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    # Auto-expire stale qr_created intents
    if item.get("status") == "qr_created" and item.get("expires_at"):
        with connect() as conn:
            conn.execute(
                """
                UPDATE binding_intents
                SET status = 'expired', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'qr_created'
                  AND expires_at < datetime('now')
                """,
                (binding_intent_id,),
            )
            refreshed = conn.execute(
                """
                SELECT
                    id, platform_user_id, account_id, openclaw_login_session_key,
                    channel, status, channel_account_id, qr_data_url, manual_login_command,
                    raw_result_json, expires_at, completed_at, error, created_at, updated_at
                FROM binding_intents WHERE id = ?
                """,
                (binding_intent_id,),
            ).fetchone()
        if refreshed is not None:
            item = dict(refreshed)
            try:
                item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
            except json.JSONDecodeError:
                item["raw_result"] = {}
    return item


def list_binding_intents_for_account(
    *,
    account_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE account_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
        except json.JSONDecodeError:
            item["raw_result"] = {}
            item["raw_result_decode_error"] = True
        result.append(item)
    return result


def get_active_binding_intent_for_channel_account(
    *,
    channel: str,
    channel_account_id: str,
) -> Optional[Dict[str, Any]]:
    account_aliases = _channel_account_id_aliases(channel_account_id)
    if not account_aliases:
        return None
    placeholders = ", ".join("?" for _ in account_aliases)
    with connect() as conn:
        # Primary lookup: dedicated column (reliable, indexed)
        row = conn.execute(
            f"""
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE channel = ?
              AND status = 'completed'
              AND channel_account_id IN ({placeholders})
            ORDER BY completed_at DESC, updated_at DESC
            LIMIT 1
            """,
            (channel, *account_aliases),
        ).fetchone()
        if row is None:
            # Fallback: json_extract for rows written before the column was added
            row = conn.execute(
                f"""
                SELECT
                    id, platform_user_id, account_id, openclaw_login_session_key,
                    channel, status, channel_account_id, qr_data_url, manual_login_command,
                    raw_result_json, expires_at, completed_at, error, created_at, updated_at
                FROM binding_intents
                WHERE channel = ?
                  AND status = 'completed'
                  AND json_extract(raw_result_json, '$.channel_account_id') IN ({placeholders})
                ORDER BY completed_at DESC, updated_at DESC
                LIMIT 1
                """,
                (channel, *account_aliases),
            ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    return item


def get_completed_binding_intent_for_openclaw_login_session_key(
    *,
    channel: str,
    openclaw_login_session_key: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE channel = ?
              AND status = 'completed'
              AND openclaw_login_session_key = ?
            ORDER BY completed_at DESC, updated_at DESC
            LIMIT 1
            """,
            (channel, openclaw_login_session_key),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    return item


def resolve_account_id_for_inbound_channel_identity(
    *,
    channel: str,
    session_key: str,
    channel_account_id: Optional[str],
) -> str:
    if channel_account_id:
        intent = get_active_binding_intent_for_channel_account(
            channel=channel,
            channel_account_id=channel_account_id,
        )
        if intent:
            return str(intent["account_id"])
        intent = get_completed_binding_intent_for_openclaw_login_session_key(
            channel=channel,
            openclaw_login_session_key=channel_account_id,
        )
        if intent:
            return str(intent["account_id"])
    intent = get_completed_binding_intent_for_openclaw_login_session_key(
        channel=channel,
        openclaw_login_session_key=session_key,
    )
    if intent:
        return str(intent["account_id"])
    return session_key


def get_or_create_session(
    *,
    account_id: str,
    channel: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    session_key: str,
    business_day: Optional[str] = None,
    carryover_summary: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts(id, channel, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                channel = excluded.channel,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, channel),
        )
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()

        conn.execute(
            """
            INSERT INTO sessions(
                account_id, session_key, sender_id, chat_id, sender_name,
                business_day, carryover_summary, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, session_key) DO UPDATE SET
                sender_id = COALESCE(excluded.sender_id, sessions.sender_id),
                chat_id = COALESCE(excluded.chat_id, sessions.chat_id),
                sender_name = COALESCE(excluded.sender_name, sessions.sender_name),
                business_day = COALESCE(sessions.business_day, excluded.business_day),
                carryover_summary = COALESCE(sessions.carryover_summary, excluded.carryover_summary),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                account_id,
                session_key,
                sender_id,
                chat_id,
                sender_name,
                business_day,
                carryover_summary,
                metadata_json,
            ),
        )
        session = conn.execute(
            "SELECT * FROM sessions WHERE account_id = ? AND session_key = ?",
            (account_id, session_key),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO profiles(account_id, updated_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id,),
        )
        profile = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()

        return {
            "account": dict(account),
            "session": dict(session),
            "profile": dict(profile),
        }


def get_or_create_account_active_session(
    *,
    account_id: str,
    channel: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    business_day: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the account-level active session used for main conversation context.

    OpenClaw's session_key is a channel routing/debug field. P0 keeps the
    existing sessions schema and rotates the stable compatibility key when
    the account's active session crosses a lifecycle boundary.
    """
    max_turns = int(max_turns or 0)
    if max_turns <= 0:
        max_turns = 0

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts(id, channel, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                channel = excluded.channel,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, channel),
        )
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()

        conn.execute(
            """
            INSERT INTO profiles(account_id, updated_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id,),
        )

        session = conn.execute(
            """
            SELECT * FROM sessions
            WHERE account_id = ? AND session_key = ?
            """,
            (account_id, ACCOUNT_ACTIVE_SESSION_KEY),
        ).fetchone()

        carryover_summary = None
        created_reason = "account_created"
        if session is not None:
            rotation_reason = _active_session_rotation_reason(
                dict(session),
                business_day=business_day,
                max_turns=max_turns,
            )
            if rotation_reason:
                carryover_summary = _build_session_carryover_summary(
                    conn,
                    session_id=int(session["id"]),
                )
                archived_session_key = f"{ACCOUNT_ACTIVE_SESSION_KEY}:{session['id']}"
                conn.execute(
                    """
                    UPDATE sessions
                    SET session_key = ?,
                        status = 'closed',
                        ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP),
                        close_reason = COALESCE(close_reason, ?),
                        carryover_summary = COALESCE(NULLIF(carryover_summary, ''), ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        archived_session_key,
                        rotation_reason,
                        carryover_summary,
                        int(session["id"]),
                    ),
                )
                session = None
                created_reason = rotation_reason
            else:
                conn.execute(
                    """
                    UPDATE sessions
                    SET sender_id = COALESCE(?, sender_id),
                        chat_id = COALESCE(?, chat_id),
                        sender_name = COALESCE(?, sender_name),
                        business_day = COALESCE(business_day, ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        sender_id,
                        chat_id,
                        sender_name,
                        business_day,
                        int(session["id"]),
                    ),
                )

        if session is None:
            conn.execute(
                """
                INSERT INTO sessions(
                    account_id, session_key, sender_id, chat_id, sender_name,
                    business_day, carryover_summary, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    account_id,
                    ACCOUNT_ACTIVE_SESSION_KEY,
                    sender_id,
                    chat_id,
                    sender_name,
                    business_day,
                    carryover_summary,
                    json.dumps(
                        {"created_reason": created_reason},
                        ensure_ascii=False,
                    ),
                ),
            )

        session = conn.execute(
            """
            SELECT * FROM sessions
            WHERE account_id = ? AND session_key = ?
            """,
            (account_id, ACCOUNT_ACTIVE_SESSION_KEY),
        ).fetchone()
        profile = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()

        return {
            "account": dict(account),
            "session": dict(session),
            "profile": dict(profile),
        }


def _active_session_rotation_reason(
    session: Dict[str, Any],
    *,
    business_day: Optional[str],
    max_turns: int,
) -> Optional[str]:
    if session.get("status") != "active":
        return "replaced"
    session_business_day = _clean_text(session.get("business_day"))
    if business_day and session_business_day and session_business_day != business_day:
        return "daily_dreaming"
    turn_count = int(session.get("turn_count") or 0)
    if max_turns > 0 and turn_count >= max_turns:
        return "max_turns"
    return None


def _build_session_carryover_summary(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    limit: int = 8,
) -> Optional[str]:
    rows = conn.execute(
        """
        SELECT role, content FROM messages
        WHERE session_id = ?
          AND content IS NOT NULL
          AND content != ''
          AND NOT (
            role = 'assistant'
            AND error IS NOT NULL
            AND error != ''
          )
          AND NOT (
            role = 'assistant'
            AND content = ?
          )
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, _NON_CONTEXT_ASSISTANT_REPLY, limit),
    ).fetchall()
    if not rows:
        return None

    lines = ["Recent carryover from previous session:"]
    for row in reversed(rows):
        role = "User" if row["role"] == "user" else "AI"
        content = _clean_text(row["content"]) or ""
        if len(content) > 400:
            content = content[:400] + "...[truncated]"
        lines.append(f"{role}: {content}")
    summary = "\n".join(lines)
    if len(summary) > 2400:
        summary = summary[:2400] + "...[truncated]"
    return summary


def increment_session_turn_count(
    *,
    session_id: int,
    count: int = 1,
) -> Optional[Dict[str, Any]]:
    count = max(1, int(count))
    with connect() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET turn_count = COALESCE(turn_count, 0) + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (count, session_id),
        )
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def insert_message(
    *,
    account_id: str,
    session_id: int,
    message_id: Optional[str],
    reply_to_message_id: Optional[str],
    direction: str,
    role: str,
    message_type: str,
    content: str,
    raw: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages(
                    account_id, session_id, message_id, reply_to_message_id,
                    direction, role, message_type, content, raw_json, latency_ms, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    session_id,
                    message_id,
                    reply_to_message_id,
                    direction,
                    role,
                    message_type,
                    content,
                    json.dumps(raw or {}, ensure_ascii=False),
                    latency_ms,
                    error,
                ),
            )
            return int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        return None


def insert_outbound_delivery_message(
    *,
    outbound_message: Dict[str, Any],
) -> Optional[int]:
    """Persist a delivered proactive outbound text into the account conversation timeline."""
    account_id = _clean_text(outbound_message.get("account_id"))
    channel = _clean_text(outbound_message.get("channel"))
    to_user_id = _clean_text(outbound_message.get("to_user_id"))
    text = _clean_text(outbound_message.get("text"))
    if not account_id or not channel or not to_user_id or not text:
        return None

    session_state = get_or_create_session(
        account_id=account_id,
        channel=channel,
        sender_id=to_user_id,
        sender_name=None,
        chat_id=to_user_id,
        session_key=ACCOUNT_ACTIVE_SESSION_KEY,
        metadata={"created_reason": "proactive_outbound"},
    )
    session = session_state["session"]
    outbound_id = int(outbound_message["id"])
    raw_metadata = {
        "source": "proactive_outbound",
        "outbound_message_id": outbound_id,
        "gateway_message_id": outbound_message.get("gateway_message_id"),
        "idempotency_key": outbound_message.get("idempotency_key"),
        "channel": channel,
        "channel_account_id": outbound_message.get("channel_account_id"),
        "session_key": outbound_message.get("session_key"),
        "product_category": outbound_message.get("product_category"),
        "policy_version": outbound_message.get("policy_version"),
        "policy_reason": outbound_message.get("policy_reason"),
        "outbound_source": outbound_message.get("source"),
        "metadata": outbound_message.get("metadata") or {},
    }
    return insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"outbound-{outbound_id}",
        reply_to_message_id=None,
        direction="outbound",
        role="assistant",
        message_type="text",
        content=text,
        raw=raw_metadata,
        error=outbound_message.get("error"),
    )


def get_duplicate_reply(*, account_id: str, reply_to_message_id: Optional[str]) -> Optional[str]:
    if not reply_to_message_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT content FROM messages
            WHERE account_id = ?
              AND reply_to_message_id = ?
              AND direction = 'outbound'
            ORDER BY id DESC
            LIMIT 1
            """,
            (account_id, reply_to_message_id),
        ).fetchone()
        return str(row["content"]) if row else None


def list_recent_messages(*, session_id: int, limit: int) -> List[Dict[str, str]]:
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE session_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, _NON_CONTEXT_ASSISTANT_REPLY, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def list_recent_messages_for_account(*, account_id: str, limit: int) -> List[Dict[str, Any]]:
    """Return recent context messages across sessions for one isolated account."""
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT session_id, role, content FROM messages
            WHERE account_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, _NON_CONTEXT_ASSISTANT_REPLY, limit),
        ).fetchall()
    return [
        {
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
        }
        for row in reversed(rows)
    ]


def count_context_messages_for_session(*, session_id: int) -> int:
    """Count messages from a session that are eligible for LLM context."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM messages
            WHERE session_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
            """,
            (session_id, _NON_CONTEXT_ASSISTANT_REPLY),
        ).fetchone()
    return int(row["count"] if row else 0)


def clear_session_messages(*, session_id: int) -> int:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        return int(cursor.rowcount)


def clear_all_messages_for_account(*, account_id: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE account_id = ?)",
            (account_id,),
        )
        return int(cursor.rowcount)


def list_session_messages(*, session_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, message_id, reply_to_message_id, direction, role,
                message_type, content, latency_ms, error, created_at,
                (
                    SELECT dt.trace_id
                    FROM debug_traces dt
                    WHERE dt.session_id = messages.session_id
                      AND dt.message_id = messages.message_id
                    ORDER BY dt.id DESC
                    LIMIT 1
                ) AS trace_id
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_recent_message_raw(*, limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.account_id,
                m.session_id,
                m.message_id,
                m.direction,
                m.role,
                m.message_type,
                m.content,
                m.raw_json,
                m.created_at,
                s.session_key
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.direction = 'inbound'
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_decode_raw_message(row) for row in rows]


def get_message_raw(*, message_db_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                m.id,
                m.account_id,
                m.session_id,
                m.message_id,
                m.direction,
                m.role,
                m.message_type,
                m.content,
                m.raw_json,
                m.created_at,
                s.session_key
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.id = ?
            """,
            (message_db_id,),
        ).fetchone()
    return _decode_raw_message(row) if row else None


def _decode_raw_message(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    raw_json = item.pop("raw_json", None)
    try:
        item["raw"] = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        item["raw"] = {"_decode_error": True, "raw_json": raw_json}
    return item


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def list_sessions(*, limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.account_id,
                s.session_key,
                s.sender_id,
                s.chat_id,
                s.sender_name,
                s.status,
                s.ended_at,
                s.close_reason,
                s.turn_count,
                s.business_day,
                s.session_summary,
                s.carryover_summary,
                s.summary_model,
                s.summary_prompt_version,
                s.metadata_json,
                s.created_at,
                s.updated_at,
                p.style,
                p.display_name AS profile_display_name,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_message_at
            FROM sessions s
            LEFT JOIN profiles p ON p.account_id = s.account_id
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY COALESCE(last_message_at, s.updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_session(*, session_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions_for_account(*, account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.account_id,
                s.session_key,
                s.sender_id,
                s.chat_id,
                s.sender_name,
                s.status,
                s.ended_at,
                s.close_reason,
                s.turn_count,
                s.business_day,
                s.session_summary,
                s.carryover_summary,
                s.summary_model,
                s.summary_prompt_version,
                s.metadata_json,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_message_at
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.account_id = ?
            GROUP BY s.id
            ORDER BY COALESCE(last_message_at, s.updated_at) DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def list_accounts() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.channel,
                a.display_name,
                a.status,
                a.notes,
                a.daily_limit,
                a.rpm_limit,
                a.created_at,
                a.updated_at,
                COUNT(DISTINCT s.id) AS session_count,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_active_at
            FROM accounts a
            LEFT JOIN sessions s ON s.account_id = a.id
            LEFT JOIN messages m ON m.account_id = a.id
            GROUP BY a.id
            ORDER BY a.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_account(*, account_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                a.id,
                a.channel,
                a.display_name,
                a.status,
                a.notes,
                a.daily_limit,
                a.rpm_limit,
                a.created_at,
                a.updated_at,
                COUNT(DISTINCT s.id) AS session_count,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_active_at
            FROM accounts a
            LEFT JOIN sessions s ON s.account_id = a.id
            LEFT JOIN messages m ON m.account_id = a.id
            WHERE a.id = ?
            GROUP BY a.id
            """,
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def update_account(
    *,
    account_id: str,
    display_name=_UNSET,
    notes=_UNSET,
    daily_limit=_UNSET,
    rpm_limit=_UNSET,
) -> Optional[Dict[str, Any]]:
    current = get_account(account_id=account_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE accounts
            SET display_name = ?,
                notes = ?,
                daily_limit = ?,
                rpm_limit = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                current.get("display_name") if display_name is _UNSET else display_name,
                current.get("notes") if notes is _UNSET else notes,
                current.get("daily_limit") if daily_limit is _UNSET else daily_limit,
                current.get("rpm_limit") if rpm_limit is _UNSET else rpm_limit,
                account_id,
            ),
        )
    return get_account(account_id=account_id)


def set_account_status(*, account_id: str, status: str) -> Optional[Dict[str, Any]]:
    current = get_account(account_id=account_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, account_id),
        )
    return get_account(account_id=account_id)


def get_account_onboarding_state(*, account_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT onboarding_state FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    if row is None:
        return "pending"
    return row["onboarding_state"] or "pending"


def set_account_onboarding_state(*, account_id: str, state: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET onboarding_state = ?, onboarding_updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (state, account_id),
        )


def get_daily_usage(*, account_id: str, date: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT message_count FROM daily_usage WHERE account_id = ? AND date = ?",
            (account_id, date),
        ).fetchone()
    return int(row["message_count"]) if row else 0


def increment_daily_usage(*, account_id: str, date: str) -> int:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_usage(account_id, date, message_count, updated_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, date) DO UPDATE SET
                message_count = message_count + 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, date),
        )
        row = conn.execute(
            "SELECT message_count FROM daily_usage WHERE account_id = ? AND date = ?",
            (account_id, date),
        ).fetchone()
    return int(row["message_count"]) if row else 1


def get_usage_last_7_days(*, account_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT date, message_count FROM daily_usage
            WHERE account_id = ?
            ORDER BY date DESC
            LIMIT 7
            """,
            (account_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Proactive outbound messages
# ---------------------------------------------------------------------------

OUTBOUND_QUOTA_STATUSES = ("pending", "sending", "sent", "failed")


def _decode_outbound_message(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_outbound_message(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    quota_date: str,
    status: str = "pending",
    error: Optional[str] = None,
    product_category: Optional[str] = None,
    policy_version: Optional[str] = None,
    policy_reason: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_channel = _clean_text(channel)
    cleaned_to_user_id = _clean_text(to_user_id)
    cleaned_source = _clean_text(source)
    cleaned_text = _clean_text(text)
    cleaned_quota_date = _clean_text(quota_date)
    cleaned_status = _clean_text(status) or "pending"
    cleaned_idempotency_key = _clean_text(idempotency_key) or _new_id("out")
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_channel:
        raise ValueError("channel is required")
    if not cleaned_to_user_id:
        raise ValueError("to_user_id is required")
    if not cleaned_source:
        raise ValueError("source is required")
    if not cleaned_text:
        raise ValueError("text is required")
    if not cleaned_quota_date:
        raise ValueError("quota_date is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_messages(
                account_id, channel, channel_account_id, to_user_id, session_key,
                source, text, idempotency_key, status, error, quota_date,
                product_category, policy_version, policy_reason, scheduled_at,
                metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                cleaned_account_id,
                cleaned_channel,
                _clean_text(channel_account_id),
                cleaned_to_user_id,
                _clean_text(session_key),
                cleaned_source,
                cleaned_text,
                cleaned_idempotency_key,
                cleaned_status,
                error,
                cleaned_quota_date,
                _clean_text(product_category),
                _clean_text(policy_version),
                _clean_text(policy_reason),
                _clean_text(scheduled_at),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM outbound_messages
            WHERE idempotency_key = ?
            """,
            (cleaned_idempotency_key,),
        ).fetchone()
    if row is None:
        raise RuntimeError("outbound_message was not created")
    return _decode_outbound_message(row)


def get_outbound_message(*, outbound_message_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def list_outbound_messages(
    *,
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM outbound_messages
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_outbound_message(row) for row in rows]


def get_outbound_daily_usage(
    *,
    account_id: str,
    quota_date: str,
    product_category: Optional[str] = None,
) -> int:
    placeholders = ", ".join("?" for _ in OUTBOUND_QUOTA_STATUSES)
    category_clause = ""
    params: List[Any] = [account_id, quota_date, *OUTBOUND_QUOTA_STATUSES]
    if product_category:
        category_clause = " AND product_category = ?"
        params.append(product_category)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM outbound_messages
            WHERE account_id = ?
              AND quota_date = ?
              AND status IN ({placeholders})
              {category_clause}
            """,
            params,
        ).fetchone()
    return int(row["count"]) if row else 0


def get_pending_reminder_count_in_window(
    *,
    account_id: str,
    start_at: str,
    end_at: str,
) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM reminders
            WHERE account_id = ?
              AND status IN ('pending', 'sending')
              AND due_at >= ?
              AND due_at < ?
            """,
            (account_id, start_at, end_at),
        ).fetchone()
    return int(row["count"]) if row else 0


def get_pending_companion_followup_count_in_window(
    *,
    account_id: str,
    start_at: str,
    end_at: str,
) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM proactive_commitments
                    WHERE account_id = ?
                      AND status IN ('pending', 'sending')
                      AND due_at >= ?
                      AND due_at < ?
                )
                +
                (
                    SELECT COUNT(*)
                    FROM outbound_messages
                    WHERE account_id = ?
                      AND product_category = 'companion_followup'
                      AND status IN ('pending', 'sending', 'sent')
                      AND created_at >= ?
                      AND created_at < ?
                ) AS count
            """,
            (account_id, start_at, end_at, account_id, start_at, end_at),
        ).fetchone()
    return int(row["count"]) if row else 0


def claim_pending_outbound_message(
    *,
    outbound_message_id: int,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'sending',
                attempts = attempts + 1,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
            """,
            (outbound_message_id,),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def mark_outbound_message_sent(
    *,
    outbound_message_id: int,
    gateway_message_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'sent',
                gateway_message_id = ?,
                error = NULL,
                sent_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (gateway_message_id, outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def mark_outbound_message_failed(
    *,
    outbound_message_id: int,
    error: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'failed',
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def cancel_outbound_message(
    *,
    outbound_message_id: int,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'cancelled',
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def _decode_reminder(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_reminder(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    text: str,
    due_at: str,
    reminder_id: Optional[str] = None,
    recur_rule: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_channel = _clean_text(channel)
    cleaned_to_user_id = _clean_text(to_user_id)
    cleaned_text = _clean_text(text)
    cleaned_due_at = _clean_text(due_at)
    cleaned_reminder_id = _clean_text(reminder_id) or _new_id("rem")
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_channel:
        raise ValueError("channel is required")
    if not cleaned_to_user_id:
        raise ValueError("to_user_id is required")
    if not cleaned_text:
        raise ValueError("text is required")
    if not cleaned_due_at:
        raise ValueError("due_at is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reminders(
                id, account_id, channel, channel_account_id, to_user_id,
                session_key, text, due_at, recur_rule, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                cleaned_reminder_id,
                cleaned_account_id,
                cleaned_channel,
                _clean_text(channel_account_id),
                cleaned_to_user_id,
                _clean_text(session_key),
                cleaned_text,
                cleaned_due_at,
                _clean_text(recur_rule) if recur_rule else None,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (cleaned_reminder_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("reminder was not created")
    return _decode_reminder(row)


def get_reminder(*, reminder_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def list_due_reminders(*, now: str, limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE status = 'pending'
              AND due_at <= ?
            ORDER BY due_at ASC, created_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    return [_decode_reminder(row) for row in rows]


def list_reminders_for_account(
    *,
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE account_id = ? AND status = ?
                ORDER BY due_at ASC, created_at ASC
                LIMIT ?
                """,
                (account_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE account_id = ?
                ORDER BY due_at DESC, created_at DESC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
    return [_decode_reminder(row) for row in rows]


def claim_due_reminder(*, reminder_id: str, now: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE reminders
            SET status = 'sending',
                attempts = attempts + 1,
                claimed_at = CURRENT_TIMESTAMP,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
              AND due_at <= ?
            """,
            (reminder_id, now),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def mark_reminder_sent(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    next_due_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        if next_due_at:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'pending',
                    due_at = ?,
                    sent_count = sent_count + 1,
                    last_sent_at = CURRENT_TIMESTAMP,
                    claimed_at = NULL,
                    outbound_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_due_at, outbound_message_id, reminder_id),
            )
        else:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'sent',
                    sent_count = sent_count + 1,
                    last_sent_at = CURRENT_TIMESTAMP,
                    outbound_message_id = ?,
                    error = NULL,
                    sent_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (outbound_message_id, reminder_id),
            )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def mark_reminder_failed(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    error: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'failed',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outbound_message_id, error, reminder_id),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def cancel_reminder(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outbound_message_id, error, reminder_id),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def update_reminder(
    *,
    reminder_id: str,
    text: Optional[str] = None,
    due_at: Optional[str] = None,
    recur_rule: Optional[str] = None,
    clear_recur_rule: bool = False,
) -> Optional[Dict[str, Any]]:
    fields: List[str] = []
    values: List = []
    if text is not None:
        fields.append("text = ?")
        values.append(_clean_text(text))
    if due_at is not None:
        fields.append("due_at = ?")
        values.append(_clean_text(due_at))
    if recur_rule is not None:
        fields.append("recur_rule = ?")
        values.append(_clean_text(recur_rule))
    elif clear_recur_rule:
        fields.append("recur_rule = NULL")
    if not fields:
        return get_reminder(reminder_id=reminder_id)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(reminder_id)
    with connect() as conn:
        conn.execute(
            f"UPDATE reminders SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    return _decode_reminder(row) if row else None


# ---------------------------------------------------------------------------
# Proactive commitments
# ---------------------------------------------------------------------------

def _decode_proactive_commitment(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_proactive_commitment(
    *,
    account_id: str,
    text: str,
    due_at: str,
    session_id: Optional[int] = None,
    source_message_id: Optional[str] = None,
    source_reply_message_id: Optional[str] = None,
    confidence: Optional[float] = None,
    reason: Optional[str] = None,
    commitment_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_text = _clean_text(text)
    cleaned_due_at = _clean_text(due_at)
    cleaned_commitment_id = _clean_text(commitment_id) or _new_id("com")
    cleaned_dedupe_key = _clean_text(dedupe_key) or cleaned_commitment_id
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_text:
        raise ValueError("text is required")
    if not cleaned_due_at:
        raise ValueError("due_at is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO proactive_commitments(
                id, account_id, session_id, source_message_id, source_reply_message_id,
                dedupe_key, text, due_at, confidence, reason, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                cleaned_commitment_id,
                cleaned_account_id,
                session_id,
                _clean_text(source_message_id),
                _clean_text(source_reply_message_id),
                cleaned_dedupe_key,
                cleaned_text,
                cleaned_due_at,
                confidence,
                _clean_text(reason),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE dedupe_key = ?",
            (cleaned_dedupe_key,),
        ).fetchone()
    if row is None:
        raise RuntimeError("proactive_commitment was not created")
    return _decode_proactive_commitment(row)


def get_proactive_commitment(*, commitment_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def list_proactive_commitments_for_account(
    *,
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = ["account_id = ?"]
    params: List[Any] = [account_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM proactive_commitments
            WHERE {' AND '.join(clauses)}
            ORDER BY due_at DESC, created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_proactive_commitment(row) for row in rows]


def list_due_proactive_commitments(
    *,
    now: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*
            FROM proactive_commitments c
            JOIN accounts a ON a.id = c.account_id
            JOIN proactive_account_state s ON s.account_id = c.account_id
            WHERE c.status = 'pending'
              AND c.due_at <= ?
              AND a.status = 'active'
              AND s.enabled = 1
              AND (s.cooldown_until IS NULL OR s.cooldown_until <= ?)
            ORDER BY c.due_at ASC, c.created_at ASC
            LIMIT ?
            """,
            (now, now, limit),
        ).fetchall()
    return [_decode_proactive_commitment(row) for row in rows]


def claim_due_proactive_commitment(
    *,
    commitment_id: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    cleaned_commitment_id = _clean_text(commitment_id)
    cleaned_now = _clean_text(now)
    if not cleaned_commitment_id:
        raise ValueError("commitment_id is required")
    if not cleaned_now:
        raise ValueError("now is required")

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'sending',
                attempts = attempts + 1,
                claimed_at = CURRENT_TIMESTAMP,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
              AND due_at <= ?
              AND EXISTS (
                  SELECT 1 FROM accounts
                  WHERE accounts.id = proactive_commitments.account_id
                    AND accounts.status = 'active'
              )
              AND EXISTS (
                  SELECT 1 FROM proactive_account_state
                  WHERE proactive_account_state.account_id = proactive_commitments.account_id
                    AND proactive_account_state.enabled = 1
                    AND (
                        proactive_account_state.cooldown_until IS NULL
                        OR proactive_account_state.cooldown_until <= ?
                    )
              )
            """,
            (cleaned_commitment_id, cleaned_now, cleaned_now),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (cleaned_commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def mark_proactive_commitment_sent(
    *,
    commitment_id: str,
    outbound_message_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'sent',
                outbound_message_id = ?,
                error = NULL,
                sent_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outbound_message_id, commitment_id),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def mark_proactive_commitment_failed(
    *,
    commitment_id: str,
    outbound_message_id: Optional[int] = None,
    error: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'failed',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outbound_message_id, error, commitment_id),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def cancel_proactive_commitment(
    *,
    commitment_id: str,
    outbound_message_id: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'cancelled',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outbound_message_id, error, commitment_id),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


# ---------------------------------------------------------------------------
# Proactive account state
# ---------------------------------------------------------------------------

def _decode_proactive_account_state(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    item["enabled"] = bool(item.get("enabled"))
    return item


def get_proactive_account_state(*, account_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_account_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return _decode_proactive_account_state(row) if row else None


def upsert_proactive_account_state(
    *,
    account_id: str,
    enabled=_UNSET,
    next_scan_at=_UNSET,
    last_scan_at=_UNSET,
    last_proactive_sent_at=_UNSET,
    cooldown_until=_UNSET,
    metadata=_UNSET,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")

    current = get_proactive_account_state(account_id=cleaned_account_id)
    if current is None:
        enabled_value = 1 if enabled is _UNSET else int(bool(enabled))
        next_scan_at_value = None if next_scan_at is _UNSET else _clean_text(next_scan_at)
        last_scan_at_value = None if last_scan_at is _UNSET else _clean_text(last_scan_at)
        last_proactive_sent_at_value = (
            None
            if last_proactive_sent_at is _UNSET
            else _clean_text(last_proactive_sent_at)
        )
        cooldown_until_value = (
            None if cooldown_until is _UNSET else _clean_text(cooldown_until)
        )
        metadata_value = {} if metadata is _UNSET else (metadata or {})
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO proactive_account_state(
                    account_id, enabled, next_scan_at, last_scan_at,
                    last_proactive_sent_at, cooldown_until, metadata_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    cleaned_account_id,
                    enabled_value,
                    next_scan_at_value,
                    last_scan_at_value,
                    last_proactive_sent_at_value,
                    cooldown_until_value,
                    json.dumps(metadata_value, ensure_ascii=False),
                ),
            )
    else:
        next_metadata = current.get("metadata") or {}
        if metadata is not _UNSET:
            next_metadata = metadata or {}
        with connect() as conn:
            conn.execute(
                """
                UPDATE proactive_account_state
                SET enabled = ?,
                    next_scan_at = ?,
                    last_scan_at = ?,
                    last_proactive_sent_at = ?,
                    cooldown_until = ?,
                    metadata_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                (
                    int(current["enabled"] if enabled is _UNSET else bool(enabled)),
                    current.get("next_scan_at")
                    if next_scan_at is _UNSET
                    else _clean_text(next_scan_at),
                    current.get("last_scan_at")
                    if last_scan_at is _UNSET
                    else _clean_text(last_scan_at),
                    current.get("last_proactive_sent_at")
                    if last_proactive_sent_at is _UNSET
                    else _clean_text(last_proactive_sent_at),
                    current.get("cooldown_until")
                    if cooldown_until is _UNSET
                    else _clean_text(cooldown_until),
                    json.dumps(next_metadata, ensure_ascii=False),
                    cleaned_account_id,
                ),
            )

    item = get_proactive_account_state(account_id=cleaned_account_id)
    if item is None:
        raise RuntimeError("proactive_account_state was not created")
    return item


def list_due_proactive_account_states(
    *,
    now: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, a.status AS account_status
            FROM proactive_account_state s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.enabled = 1
              AND a.status = 'active'
              AND (s.next_scan_at IS NULL OR s.next_scan_at <= ?)
              AND (s.cooldown_until IS NULL OR s.cooldown_until <= ?)
            ORDER BY COALESCE(s.next_scan_at, '0000-01-01 00:00:00') ASC,
                     s.updated_at ASC
            LIMIT ?
            """,
            (now, now, limit),
        ).fetchall()
    return [_decode_proactive_account_state(row) for row in rows]


def claim_due_proactive_account_state(
    *,
    account_id: str,
    now: str,
    next_scan_at: str,
) -> Optional[Dict[str, Any]]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_now = _clean_text(now)
    cleaned_next_scan_at = _clean_text(next_scan_at)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_now:
        raise ValueError("now is required")
    if not cleaned_next_scan_at:
        raise ValueError("next_scan_at is required")

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE proactive_account_state
            SET last_scan_at = ?,
                next_scan_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
              AND enabled = 1
              AND (next_scan_at IS NULL OR next_scan_at <= ?)
              AND (cooldown_until IS NULL OR cooldown_until <= ?)
              AND EXISTS (
                  SELECT 1
                  FROM accounts
                  WHERE accounts.id = proactive_account_state.account_id
                    AND accounts.status = 'active'
              )
            """,
            (
                cleaned_now,
                cleaned_next_scan_at,
                cleaned_account_id,
                cleaned_now,
                cleaned_now,
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            """
            SELECT s.*, a.status AS account_status
            FROM proactive_account_state s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.account_id = ?
            """,
            (cleaned_account_id,),
        ).fetchone()
    return _decode_proactive_account_state(row) if row else None


# ---------------------------------------------------------------------------
# Content invitations
# ---------------------------------------------------------------------------

CONTENT_INVITATION_ACTIVE_STATUSES = ("candidate", "sending", "invited", "accepted")


def _normalize_title_items(title_items: Any) -> List[Dict[str, Any]]:
    if not isinstance(title_items, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in title_items:
        if isinstance(item, dict):
            title = _clean_text(item.get("title"))
            if not title:
                continue
            normalized.append(
                {
                    "title": title[:200],
                    "source_name": _clean_text(item.get("source_name")),
                    "url": _clean_text(item.get("url")),
                    "published_at": _clean_text(item.get("published_at")),
                    "retrieved_at": _clean_text(item.get("retrieved_at")),
                }
            )
        else:
            title = _clean_text(item)
            if title:
                normalized.append({"title": title[:200]})
    return normalized


def _decode_content_invitation(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    title_items_json = item.pop("title_items_json", None)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["title_items"] = json.loads(title_items_json or "[]")
    except json.JSONDecodeError:
        item["title_items"] = []
        item["title_items_decode_error"] = True
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _decode_content_invitation_preference(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_content_invitation(
    *,
    account_id: str,
    topic: str,
    invitation_text: str,
    title_items: List[Dict[str, Any]],
    invitation_id: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    source_task_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_topic = _clean_text(topic)
    cleaned_invitation_text = _clean_text(invitation_text)
    cleaned_invitation_id = _clean_text(invitation_id) or _new_id("cinv")
    normalized_titles = _normalize_title_items(title_items)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_topic:
        raise ValueError("topic is required")
    if not cleaned_invitation_text:
        raise ValueError("invitation_text is required")
    if len(normalized_titles) < 1:
        raise ValueError("title_items is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO content_invitations(
                id, account_id, topic, invitation_text, title_items_json,
                scheduled_at, expires_at, source_task_id, metadata_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                cleaned_invitation_id,
                cleaned_account_id,
                cleaned_topic,
                cleaned_invitation_text,
                json.dumps(normalized_titles, ensure_ascii=False),
                _clean_text(scheduled_at),
                _clean_text(expires_at),
                _clean_text(source_task_id),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (cleaned_invitation_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_invitation was not created")
    return _decode_content_invitation(row)


def get_content_invitation(*, invitation_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (_clean_text(invitation_id),),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def list_content_invitations_for_account(
    *,
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = ["account_id = ?"]
    params: List[Any] = [account_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM content_invitations
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_content_invitation(row) for row in rows]


def get_active_content_invitation(
    *,
    account_id: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM content_invitations
            WHERE account_id = ?
              AND status = 'invited'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY invited_at DESC, updated_at DESC
            LIMIT 1
            """,
            (account_id, now),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def list_due_content_invitations(*, now: str, limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ci.*
            FROM content_invitations ci
            JOIN accounts a ON a.id = ci.account_id
            WHERE ci.status = 'candidate'
              AND (ci.scheduled_at IS NULL OR ci.scheduled_at <= ?)
              AND a.status = 'active'
            ORDER BY COALESCE(ci.scheduled_at, ci.created_at) ASC, ci.created_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    return [_decode_content_invitation(row) for row in rows]


def claim_due_content_invitation(
    *,
    invitation_id: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    cleaned_invitation_id = _clean_text(invitation_id)
    cleaned_now = _clean_text(now)
    if not cleaned_invitation_id:
        raise ValueError("invitation_id is required")
    if not cleaned_now:
        raise ValueError("now is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'sending',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'candidate'
              AND (scheduled_at IS NULL OR scheduled_at <= ?)
              AND EXISTS (
                  SELECT 1 FROM accounts
                  WHERE accounts.id = content_invitations.account_id
                    AND accounts.status = 'active'
              )
            """,
            (cleaned_invitation_id, cleaned_now),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (cleaned_invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def release_content_invitation_claim(
    *,
    invitation_id: str,
) -> bool:
    """Reset a 'sending' invitation back to 'candidate' so it can be retried."""
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'candidate',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'sending'
            """,
            (invitation_id,),
        )
    return cursor.rowcount == 1


def mark_content_invitation_invited(
    *,
    invitation_id: str,
    outbound_message_id: Optional[int],
    invited_at: str,
    expires_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'invited',
                invited_at = ?,
                expires_at = COALESCE(?, expires_at),
                outbound_message_id = ?,
                policy_reason = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'sending'
            """,
            (invited_at, _clean_text(expires_at), outbound_message_id, invitation_id),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def mark_content_invitation_rejected_by_policy(
    *,
    invitation_id: str,
    outbound_message_id: Optional[int],
    policy_reason: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_invitations
            SET status = 'rejected_by_policy',
                outbound_message_id = COALESCE(?, outbound_message_id),
                policy_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (outbound_message_id, _clean_text(policy_reason), invitation_id),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def mark_content_invitation_titles_sent(
    *,
    invitation_id: str,
    trigger_message_id: Optional[str],
    tool_invocation_id: Optional[int],
    responded_at: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_invitations
            SET status = 'titles_sent',
                responded_at = ?,
                trigger_message_id = ?,
                tool_invocation_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'invited'
            """,
            (responded_at, _clean_text(trigger_message_id), tool_invocation_id, invitation_id),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def mark_content_invitation_feedback(
    *,
    invitation_id: str,
    status: str,
    trigger_message_id: Optional[str],
    tool_invocation_id: Optional[int],
    responded_at: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_content_invitation(invitation_id=invitation_id)
    if current is None:
        return None
    next_metadata = dict(current.get("metadata") or {})
    if metadata:
        next_metadata.update(metadata)
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_invitations
            SET status = ?,
                responded_at = ?,
                trigger_message_id = ?,
                tool_invocation_id = ?,
                metadata_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                _clean_text(status) or "declined",
                responded_at,
                _clean_text(trigger_message_id),
                tool_invocation_id,
                json.dumps(next_metadata, ensure_ascii=False),
                invitation_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def expire_content_invitations(*, now: str, limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM content_invitations
            WHERE status = 'invited'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY expires_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE content_invitations
                SET status = 'expired',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            rows = conn.execute(
                f"SELECT * FROM content_invitations WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        else:
            rows = []
    return [_decode_content_invitation(row) for row in rows]


def get_content_invitation_preference(
    *,
    account_id: str,
    topic: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM content_invitation_preferences
            WHERE account_id = ? AND topic = ?
            """,
            (account_id, topic),
        ).fetchone()
    return _decode_content_invitation_preference(row) if row else None


def upsert_content_invitation_preference(
    *,
    account_id: str,
    topic: str,
    status: str,
    cooldown_until: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_topic = _clean_text(topic)
    cleaned_status = _clean_text(status) or "allowed"
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_topic:
        raise ValueError("topic is required")
    current = get_content_invitation_preference(
        account_id=cleaned_account_id,
        topic=cleaned_topic,
    )
    next_metadata = dict((current or {}).get("metadata") or {})
    if metadata:
        next_metadata.update(metadata)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO content_invitation_preferences(
                account_id, topic, status, cooldown_until, last_feedback_at,
                feedback_count, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, topic) DO UPDATE SET
                status = excluded.status,
                cooldown_until = excluded.cooldown_until,
                last_feedback_at = CURRENT_TIMESTAMP,
                feedback_count = content_invitation_preferences.feedback_count + 1,
                metadata_json = excluded.metadata_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                cleaned_account_id,
                cleaned_topic,
                cleaned_status,
                _clean_text(cooldown_until),
                json.dumps(next_metadata, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM content_invitation_preferences
            WHERE account_id = ? AND topic = ?
            """,
            (cleaned_account_id, cleaned_topic),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_invitation_preference was not created")
    return _decode_content_invitation_preference(row)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def get_profile_for_session(*, session_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT p.*
            FROM profiles p
            JOIN sessions s ON s.account_id = p.account_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def get_profile_for_account(*, account_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def update_profile_for_account(
    *,
    account_id: str,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_profile_for_account(account_id=account_id)
    if current is None:
        return None

    next_preferences = preferences
    if next_preferences is None:
        try:
            next_preferences = json.loads(current.get("preferences_json") or "{}")
        except json.JSONDecodeError:
            next_preferences = {}

    with connect() as conn:
        conn.execute(
            """
            UPDATE profiles
            SET display_name = ?,
                style = ?,
                system_prompt = ?,
                preferences_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            (
                display_name if display_name is not None else current.get("display_name"),
                style if style is not None else current.get("style"),
                system_prompt if system_prompt is not None else current.get("system_prompt"),
                json.dumps(next_preferences, ensure_ascii=False),
                account_id,
            ),
        )
    return get_profile_for_account(account_id=account_id)


def update_profile_for_session(
    *,
    session_id: int,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    session = get_session(session_id=session_id)
    if session is None:
        return None
    return update_profile_for_account(
        account_id=session["account_id"],
        display_name=display_name,
        style=style,
        system_prompt=system_prompt,
        preferences=preferences,
    )


# ---------------------------------------------------------------------------
# Phone verification
# ---------------------------------------------------------------------------

def normalize_phone(phone: str) -> str:
    return _normalize_phone(phone)


def create_phone_verification(
    *,
    phone: str,
    code: str,
    expires_minutes: int,
) -> Dict[str, Any]:
    normalized = _normalize_phone(phone)
    verification_id = _new_id("phv")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO phone_verifications(id, phone, code, expires_at)
            VALUES (?, ?, ?, datetime('now', ? || ' minutes'))
            """,
            (verification_id, normalized, code, f"+{expires_minutes}"),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row)


def get_latest_active_verification(phone: str) -> Optional[Dict[str, Any]]:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM phone_verifications
            WHERE phone = ?
              AND verified_at IS NULL
              AND expires_at > datetime('now')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    return dict(row) if row else None


def count_verifications_last_hour(phone: str) -> int:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM phone_verifications
            WHERE phone = ?
              AND created_at > datetime('now', '-1 hour')
            """,
            (normalized,),
        ).fetchone()
    return int(row[0]) if row else 0


def invalidate_verifications_for_phone(phone: str) -> None:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET expires_at = datetime('now', '-1 second')
            WHERE phone = ?
              AND expires_at > datetime('now')
            """,
            (normalized,),
        )


def increment_verify_attempts(verification_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET verify_attempts = verify_attempts + 1
            WHERE id = ?
            """,
            (verification_id,),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row) if row else None


def set_verification_verified(
    verification_id: str,
    *,
    token_expires_minutes: int,
) -> Optional[Dict[str, Any]]:
    token = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET verified_at = datetime('now'),
                verified_token = ?,
                token_expires_at = datetime('now', ? || ' minutes')
            WHERE id = ?
            """,
            (token, f"+{token_expires_minutes}", verification_id),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row) if row else None


def get_verification_by_token(verified_token: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE verified_token = ?",
            (verified_token,),
        ).fetchone()
    return dict(row) if row else None


def consume_verification_token(verification_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET token_consumed_at = datetime('now')
            WHERE id = ?
            """,
            (verification_id,),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row) if row else None


def get_valid_verification_by_token(
    verified_token: str,
    phone: str,
) -> Optional[Dict[str, Any]]:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM phone_verifications
            WHERE verified_token = ?
              AND phone = ?
              AND token_consumed_at IS NULL
              AND token_expires_at > datetime('now')
            """,
            (verified_token, normalized),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Platform user sessions
# ---------------------------------------------------------------------------

def create_platform_user_session(
    *,
    platform_user_id: str,
    days: int = 7,
) -> Dict[str, Any]:
    import secrets
    token = secrets.token_urlsafe(32)
    session_id = f"sess_{uuid.uuid4().hex}"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_user_sessions(id, platform_user_id, token, expires_at)
            VALUES (?, ?, ?, datetime('now', ? || ' days'))
            """,
            (session_id, platform_user_id, token, f"+{days}"),
        )
        row = conn.execute(
            "SELECT * FROM platform_user_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row)


def get_platform_user_by_session_token(*, token: str) -> Optional[Dict[str, Any]]:
    """Return platform_user row if session token is valid and not expired."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT pu.*
            FROM platform_users pu
            JOIN platform_user_sessions s ON s.platform_user_id = pu.id
            WHERE s.token = ?
              AND s.expires_at > datetime('now')
            """,
            (token,),
        ).fetchone()
    return dict(row) if row else None


def consume_valid_verification_token(
    verified_token: str,
    phone: str,
) -> Optional[Dict[str, Any]]:
    """Atomically consume a verified token. Returns the row if it was valid and not yet consumed, None otherwise."""
    normalized = _normalize_phone(phone)
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET token_consumed_at = datetime('now')
            WHERE verified_token = ?
              AND phone = ?
              AND token_consumed_at IS NULL
              AND token_expires_at > datetime('now')
            """,
            (verified_token, normalized),
        )
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            return None
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE verified_token = ? AND phone = ?",
            (verified_token, normalized),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Account unbind / wipe
# ---------------------------------------------------------------------------

def unbind_account_channel(*, account_id: str) -> Dict[str, Any]:
    """Path A: disconnect WeChat channel, cancel reminders & proactive.

    Removes channel routing rows so incoming messages can no longer reach this
    account.  All conversation history and context files are preserved.
    Returns counts of affected rows for audit logging.
    """
    with connect() as conn:
        cb = conn.execute(
            "DELETE FROM channel_bindings WHERE account_id = ?",
            (account_id,),
        ).rowcount
        bi = conn.execute(
            """
            UPDATE binding_intents
            SET status = 'revoked', updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ? AND status = 'completed'
            """,
            (account_id,),
        ).rowcount
        rem = conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled',
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ? AND status = 'pending'
            """,
            (account_id,),
        ).rowcount
        pc = conn.execute(
            """
            DELETE FROM proactive_commitments
            WHERE account_id = ? AND status IN ('pending', 'scheduled')
            """,
            (account_id,),
        ).rowcount
        ci = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'cancelled',
                policy_reason = 'account_unbound',
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
              AND status IN ('candidate', 'sending', 'invited', 'accepted')
            """,
            (account_id,),
        ).rowcount
        conn.execute(
            """
            UPDATE proactive_account_state
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            (account_id,),
        )
    return {
        "channel_bindings_deleted": cb,
        "binding_intents_revoked": bi,
        "reminders_cancelled": rem,
        "proactive_commitments_deleted": pc,
        "content_invitations_cancelled": ci,
    }


def wipe_account_data(*, account_id: str) -> Dict[str, Any]:
    """Path B: hard-delete all account data after unbind_account_channel().

    Removes sessions, messages, dreaming data, profile row, and the
    account_owner_binding.  Sets account status to 'deactivated'.
    Does NOT touch the filesystem — caller must remove user_profiles dir.
    """
    with connect() as conn:
        memory_events = conn.execute(
            "DELETE FROM memory_events WHERE account_id = ?",
            (account_id,),
        ).rowcount
        dmi = conn.execute(
            """
            DELETE FROM dreaming_memory_items
            WHERE dreaming_run_id IN (SELECT id FROM dreaming_runs WHERE account_id = ?)
               OR account_id = ?
            """,
            (account_id, account_id),
        ).rowcount
        dr = conn.execute(
            "DELETE FROM dreaming_runs WHERE account_id = ?",
            (account_id,),
        ).rowcount
        reminders = conn.execute(
            "DELETE FROM reminders WHERE account_id = ?",
            (account_id,),
        ).rowcount
        proactive_commitments = conn.execute(
            "DELETE FROM proactive_commitments WHERE account_id = ?",
            (account_id,),
        ).rowcount
        content_invitations = conn.execute(
            "DELETE FROM content_invitations WHERE account_id = ?",
            (account_id,),
        ).rowcount
        content_invitation_preferences = conn.execute(
            "DELETE FROM content_invitation_preferences WHERE account_id = ?",
            (account_id,),
        ).rowcount
        outbound = conn.execute(
            "DELETE FROM outbound_messages WHERE account_id = ?",
            (account_id,),
        ).rowcount
        cost_events = conn.execute(
            "DELETE FROM cost_events WHERE account_id = ?",
            (account_id,),
        ).rowcount
        ledger = conn.execute(
            "DELETE FROM entitlement_ledger WHERE account_id = ?",
            (account_id,),
        ).rowcount
        wallets = conn.execute(
            "DELETE FROM entitlement_wallets WHERE account_id = ?",
            (account_id,),
        ).rowcount
        daily_usage = conn.execute(
            "DELETE FROM daily_usage WHERE account_id = ?",
            (account_id,),
        ).rowcount
        debug_traces = conn.execute(
            "DELETE FROM debug_traces WHERE account_id = ?",
            (account_id,),
        ).rowcount
        conn.execute(
            "DELETE FROM proactive_account_state WHERE account_id = ?",
            (account_id,),
        )
        msgs = conn.execute(
            "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE account_id = ?)",
            (account_id,),
        ).rowcount
        sess = conn.execute(
            "DELETE FROM sessions WHERE account_id = ?",
            (account_id,),
        ).rowcount
        binding_intents = conn.execute(
            "DELETE FROM binding_intents WHERE account_id = ?",
            (account_id,),
        ).rowcount
        conn.execute(
            "DELETE FROM profiles WHERE account_id = ?",
            (account_id,),
        )
        conn.execute(
            "DELETE FROM account_owner_bindings WHERE account_id = ?",
            (account_id,),
        )
        conn.execute(
            """
            UPDATE accounts
            SET status = 'deactivated', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (account_id,),
        )
    return {
        "messages_deleted": msgs,
        "sessions_deleted": sess,
        "dreaming_memory_items_deleted": dmi,
        "dreaming_runs_deleted": dr,
        "memory_events_deleted": memory_events,
        "reminders_deleted": reminders,
        "proactive_commitments_deleted": proactive_commitments,
        "content_invitations_deleted": content_invitations,
        "content_invitation_preferences_deleted": content_invitation_preferences,
        "cost_events_deleted": cost_events,
        "entitlement_ledger_deleted": ledger,
        "entitlement_wallets_deleted": wallets,
        "outbound_messages_deleted": outbound,
        "daily_usage_deleted": daily_usage,
        "debug_traces_deleted": debug_traces,
        "binding_intents_deleted": binding_intents,
    }


def reenable_proactive_after_rebind(*, account_id: str) -> None:
    """Re-enable proactive state when user successfully re-binds WeChat."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_account_state
            SET enabled = 1, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            (account_id,),
        )
