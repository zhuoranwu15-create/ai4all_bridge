import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings

logger = logging.getLogger("ai4all.db")

_UNSET = object()


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
            """
        )
        # Ensure new columns exist on accounts (for DBs created before this change)
        _ensure_column(conn, "accounts", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(conn, "accounts", "notes", "TEXT")
        _ensure_column(conn, "accounts", "daily_limit", "INTEGER")
        _ensure_column(conn, "accounts", "rpm_limit", "INTEGER")
        _ensure_column(conn, "messages", "latency_ms", "INTEGER")
        _ensure_column(conn, "messages", "error", "TEXT")


# ---------------------------------------------------------------------------
# Session / account creation
# ---------------------------------------------------------------------------

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
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
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
