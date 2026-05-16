import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings


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


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                channel TEXT,
                display_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT,
                chat_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, sender_id),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                contact_id INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, session_key),
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                FOREIGN KEY(contact_id) REFERENCES contacts(id)
            );

            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                contact_id INTEGER NOT NULL,
                display_name TEXT,
                style TEXT,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                system_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, contact_id),
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                FOREIGN KEY(contact_id) REFERENCES contacts(id)
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
            """
        )
        _ensure_column(conn, "contacts", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(conn, "contacts", "notes", "TEXT")
        _ensure_column(conn, "messages", "latency_ms", "INTEGER")
        _ensure_column(conn, "messages", "error", "TEXT")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
        conn.execute(
            """
            INSERT INTO contacts(account_id, sender_id, sender_name, chat_id, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, sender_id) DO UPDATE SET
                sender_name = COALESCE(excluded.sender_name, contacts.sender_name),
                chat_id = COALESCE(excluded.chat_id, contacts.chat_id),
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, sender_id, sender_name, chat_id),
        )
        contact = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ? AND sender_id = ?",
            (account_id, sender_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO sessions(account_id, contact_id, session_key, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, session_key) DO UPDATE SET
                contact_id = excluded.contact_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, contact["id"], session_key),
        )
        session = conn.execute(
            "SELECT * FROM sessions WHERE account_id = ? AND session_key = ?",
            (account_id, session_key),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO profiles(account_id, contact_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, contact_id) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, contact["id"]),
        )
        profile = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ? AND contact_id = ?",
            (account_id, contact["id"]),
        ).fetchone()
        return {
            "account": {"id": account_id, "channel": channel},
            "contact": dict(contact),
            "session": dict(session),
            "profile": dict(profile),
        }


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


def list_sessions(*, limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.account_id,
                s.session_key,
                s.status,
                s.created_at,
                s.updated_at,
                c.sender_id,
                c.sender_name,
                c.chat_id,
                p.style,
                p.display_name AS profile_display_name,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_message_at
            FROM sessions s
            JOIN contacts c ON c.id = s.contact_id
            LEFT JOIN profiles p ON p.contact_id = c.id AND p.account_id = s.account_id
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY COALESCE(last_message_at, s.updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_accounts() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.channel,
                a.display_name,
                a.created_at,
                a.updated_at,
                COUNT(DISTINCT c.id) AS contact_count,
                COUNT(DISTINCT s.id) AS session_count
            FROM accounts a
            LEFT JOIN contacts c ON c.account_id = a.id
            LEFT JOIN sessions s ON s.account_id = a.id
            GROUP BY a.id
            ORDER BY a.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_contacts(*, account_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    query = """
        SELECT
            c.id,
            c.account_id,
            c.sender_id,
            c.sender_name,
            c.chat_id,
            c.status,
            c.notes,
            c.created_at,
            c.updated_at,
            p.display_name AS profile_display_name,
            p.style,
            COUNT(DISTINCT s.id) AS session_count,
            COUNT(m.id) AS message_count,
            MAX(m.created_at) AS last_message_at
        FROM contacts c
        LEFT JOIN profiles p ON p.contact_id = c.id AND p.account_id = c.account_id
        LEFT JOIN sessions s ON s.contact_id = c.id
        LEFT JOIN messages m ON m.session_id = s.id
    """
    params: List[Any] = []
    if account_id:
        query += " WHERE c.account_id = ?"
        params.append(account_id)
    query += """
        GROUP BY c.id
        ORDER BY COALESCE(last_message_at, c.updated_at) DESC
        LIMIT ?
    """
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_contact(*, contact_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                c.*,
                p.id AS profile_id,
                p.display_name AS profile_display_name,
                p.style,
                p.preferences_json,
                p.system_prompt
            FROM contacts c
            LEFT JOIN profiles p ON p.contact_id = c.id AND p.account_id = c.account_id
            WHERE c.id = ?
            """,
            (contact_id,),
        ).fetchone()
    return dict(row) if row else None


def update_contact(
    *,
    contact_id: int,
    status: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_contact(contact_id=contact_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE contacts
            SET status = ?,
                notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status if status is not None else current.get("status"),
                notes if notes is not None else current.get("notes"),
                contact_id,
            ),
        )
    return get_contact(contact_id=contact_id)


def set_contact_status(*, contact_id: int, status: str) -> Optional[Dict[str, Any]]:
    return update_contact(contact_id=contact_id, status=status)


def update_profile_for_contact(
    *,
    contact_id: int,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    contact = get_contact(contact_id=contact_id)
    if contact is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO profiles(account_id, contact_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, contact_id) DO NOTHING
            """,
            (contact["account_id"], contact_id),
        )
    session = get_latest_session_for_contact(contact_id=contact_id)
    if session is None:
        return None
    return update_profile_for_session(
        session_id=session["id"],
        display_name=display_name,
        style=style,
        system_prompt=system_prompt,
        preferences=preferences,
    )


def get_latest_session_for_contact(*, contact_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM sessions
            WHERE contact_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (contact_id,),
        ).fetchone()
    return dict(row) if row else None


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


def get_session(*, session_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.*, c.sender_id, c.sender_name, c.chat_id
            FROM sessions s
            JOIN contacts c ON c.id = s.contact_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def get_profile_for_session(*, session_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT p.*
            FROM profiles p
            JOIN sessions s ON s.contact_id = p.contact_id AND s.account_id = p.account_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def update_profile_for_session(
    *,
    session_id: int,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_profile_for_session(session_id=session_id)
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
            WHERE id = ?
            """,
            (
                display_name if display_name is not None else current.get("display_name"),
                style if style is not None else current.get("style"),
                system_prompt if system_prompt is not None else current.get("system_prompt"),
                json.dumps(next_preferences, ensure_ascii=False),
                current["id"],
            ),
        )
    return get_profile_for_session(session_id=session_id)
