import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings

logger = logging.getLogger("ai4all.db")

_UNSET = object()
_NON_CONTEXT_ASSISTANT_REPLY = "我这边刚刚有点卡住了，你可以稍后再发我一次。"
ACCOUNT_ACTIVE_SESSION_KEY = "__account_active__"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def create_ai4all_account_for_user(
    *,
    platform_user_id: str,
    display_name: str,
    system_prompt: Optional[str] = None,
    plan: str = "free",
) -> Dict[str, Any]:
    cleaned_display_name = _clean_text(display_name)
    if not cleaned_display_name:
        raise ValueError("display_name is required")

    account_id = _new_id("acct")
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
        conn.execute(
            """
            INSERT INTO accounts(id, channel, display_name, updated_at)
            VALUES (?, 'openclaw-weixin', ?, CURRENT_TIMESTAMP)
            """,
            (account_id, cleaned_display_name),
        )
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
    cleaned_display_name = _clean_text(display_name) or "AI4ALL 助手"
    return create_ai4all_account_for_user(
        platform_user_id=platform_user_id,
        display_name=cleaned_display_name,
        system_prompt=None,
        plan=plan,
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
                message_type, content, latency_ms, error, created_at
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
                metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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


def get_outbound_daily_usage(*, account_id: str, quota_date: str) -> int:
    placeholders = ", ".join("?" for _ in OUTBOUND_QUOTA_STATUSES)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM outbound_messages
            WHERE account_id = ?
              AND quota_date = ?
              AND status IN ({placeholders})
            """,
            (account_id, quota_date, *OUTBOUND_QUOTA_STATUSES),
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
                session_key, text, due_at, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
    limit: int = 50,
) -> List[Dict[str, Any]]:
    with connect() as conn:
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
    outbound_message_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'sent',
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
