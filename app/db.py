import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings

logger = logging.getLogger("ai4all.db")

_UNSET = object()
_NON_CONTEXT_ASSISTANT_REPLY = "我这边刚刚有点卡住了，你可以稍后再发我一次。"


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


def _normalize_phone(phone: str) -> str:
    normalized = str(phone or "")
    for ch in (" ", "-", "(", ")", "."):
        normalized = normalized.replace(ch, "")
    normalized = normalized.strip()
    if len(normalized) < 6 or len(normalized) > 32:
        raise ValueError("phone must be 6-32 characters after stripping spaces and hyphens")
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
            """
        )
        # Ensure new columns exist on accounts (for DBs created before this change)
        _ensure_column(conn, "accounts", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(conn, "accounts", "notes", "TEXT")
        _ensure_column(conn, "accounts", "daily_limit", "INTEGER")
        _ensure_column(conn, "accounts", "rpm_limit", "INTEGER")
        _ensure_column(conn, "messages", "latency_ms", "INTEGER")
        _ensure_column(conn, "messages", "error", "TEXT")
        _ensure_column(conn, "binding_intents", "qr_data_url", "TEXT")
        _ensure_column(conn, "binding_intents", "manual_login_command", "TEXT")
        _ensure_column(conn, "binding_intents", "error", "TEXT")
        _ensure_column(conn, "binding_intents", "channel_account_id", "TEXT")


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
) -> Dict[str, Any]:
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
            INSERT INTO sessions(account_id, session_key, sender_id, chat_id, sender_name, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, session_key) DO UPDATE SET
                sender_id = COALESCE(excluded.sender_id, sessions.sender_id),
                chat_id = COALESCE(excluded.chat_id, sessions.chat_id),
                sender_name = COALESCE(excluded.sender_name, sessions.sender_name),
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, session_key, sender_id, chat_id, sender_name),
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
