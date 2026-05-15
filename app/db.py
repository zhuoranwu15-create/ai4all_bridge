import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

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
        return {"account": {"id": account_id, "channel": channel}, "contact": dict(contact), "session": dict(session)}


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
) -> Optional[int]:
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages(
                    account_id, session_id, message_id, reply_to_message_id,
                    direction, role, message_type, content, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
