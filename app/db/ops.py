"""app.db.ops — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
from app.db._backend import IntegrityError, Row, is_postgres
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from app.config import settings
from app.time_utils import beijing_naive_now
from app.db._core import (
    _clean_text,
    _db_path,
    _new_id,
    connect,
)
__all__ = [
    'checkpoint_wal',
    'create_faq_message',
    'get_account_water_level',
    'get_database_storage_stats',
    'get_faq_message',
    'get_inbound_message_rate',
    'get_recent_reply_latencies',
    'get_today_inbound_message_rate',
    'get_ops_metrics',
    'get_scheduler_heartbeat',
    'like_faq_message',
    'list_published_faq_messages',
    'list_scheduler_heartbeats',
    'record_scheduler_heartbeat',
]
# ---------------------------------------------------------------------------
# Runtime health
# ---------------------------------------------------------------------------

def _runtime_timestamp(value: Optional[datetime] = None) -> str:
    # scheduler 心跳落库 last_seen_at/last_success_at/last_error_at,与全库其余时间列
    # 统一为北京墙钟 naive;消费端(monitor_health 陈旧度比较、serializers 展示)同口径。
    return (value or beijing_naive_now()).isoformat(timespec="seconds")


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


def _decode_faq_message(row: Row) -> Dict[str, Any]:
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
    published_at_expr = "strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))" if cleaned_status == "published" else "NULL"
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {published_at_expr}, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
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
        # INSERT OR IGNORE + rowcount 判断是否新点赞：避免「捕获 IntegrityError 后继续用连接」，
        # 该模式在 PG 下会因唯一冲突中止整个事务（SQLite 可继续，PG 不行）。
        like_cursor = conn.execute(
            """
            INSERT OR IGNORE INTO faq_message_likes(id, message_id, voter_key)
            VALUES (?, ?, ?)
            """,
            (_new_id("fqlike"), cleaned_id, cleaned_voter_key),
        )
        liked = like_cursor.rowcount == 1
        if liked:
            conn.execute(
                """
                UPDATE faq_messages
                SET like_count = like_count + 1,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
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


def _file_size_bytes(path: Path) -> int:
    """文件大小（字节）；不存在或不可读时返回 0。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def get_database_storage_stats() -> Dict[str, Any]:
    """SQLite 存储与 WAL 运行态指标，用于 ops 监控。

    重点观测 ``-wal`` 文件大小：WAL 模式下若有长生命周期读连接（如独立调度器进程）
    压住 checkpoint，``-wal`` 会持续增长。这里只读取、不主动 checkpoint。
    """
    if is_postgres():
        # PG 后端无 WAL 文件/PRAGMA 概念：返回库大小，其余 SQLite 专有字段置空。
        with connect() as conn:
            size_row = conn.execute(
                "SELECT pg_database_size(current_database()) AS db_bytes"
            ).fetchone()
        return {
            "db_bytes": int(size_row["db_bytes"]) if size_row and size_row["db_bytes"] is not None else None,
            "wal_bytes": None,
            "shm_bytes": None,
            "journal_mode": None,
            "synchronous": None,
            "wal_autocheckpoint_pages": None,
        }
    db_path = _db_path()
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")
    with connect() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        synchronous = conn.execute("PRAGMA synchronous").fetchone()
        wal_autocheckpoint = conn.execute("PRAGMA wal_autocheckpoint").fetchone()
    return {
        "db_bytes": _file_size_bytes(db_path),
        "wal_bytes": _file_size_bytes(wal_path),
        "shm_bytes": _file_size_bytes(shm_path),
        "journal_mode": journal_mode[0] if journal_mode else None,
        # synchronous: 0=OFF 1=NORMAL 2=FULL 3=EXTRA
        "synchronous": int(synchronous[0]) if synchronous else None,
        "wal_autocheckpoint_pages": int(wal_autocheckpoint[0]) if wal_autocheckpoint else None,
    }


def checkpoint_wal(*, mode: str = "TRUNCATE") -> Dict[str, Any]:
    """主动对 WAL 做一次 checkpoint，回收 ``-wal`` 文件，用于 ops 自愈。

    默认 TRUNCATE：checkpoint 后把 ``-wal`` 截断回 0 字节。若存在长生命周期读连接
    压住 WAL 帧，SQLite 只能做部分 checkpoint 并返回 ``busy=1``，``-wal`` 不会缩小——
    调用方可据此判断「是否真有读连接卡住」。跨进程操作共享 WAL，从任一连接发起均可。

    仅 WAL 模式有意义；非 WAL 模式下该 PRAGMA 是无害的 no-op。
    """
    mode = (mode or "TRUNCATE").upper()
    if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
        raise ValueError(f"unsupported wal_checkpoint mode: {mode}")
    if is_postgres():
        # PG 无应用级 WAL checkpoint（属服务端职责）：返回 no-op 结果，结构对齐。
        return {
            "mode": mode,
            "busy": None,
            "log_frames": None,
            "checkpointed_frames": None,
            "wal_bytes_after": None,
        }
    with connect() as conn:
        # 返回单行 (busy, log_frames, checkpointed_frames)
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    busy, log_frames, checkpointed_frames = (row[0], row[1], row[2]) if row else (None, None, None)
    wal_path = _db_path().with_name(_db_path().name + "-wal")
    return {
        "mode": mode,
        # busy=1 表示有读/写连接挡住，未能完整 checkpoint
        "busy": int(busy) if busy is not None else None,
        "log_frames": int(log_frames) if log_frames is not None else None,
        "checkpointed_frames": int(checkpointed_frames) if checkpointed_frames is not None else None,
        "wal_bytes_after": _file_size_bytes(wal_path),
    }


def get_account_water_level(*, active_windows_minutes=(15, 60, 1440)) -> Dict[str, Any]:
    """账号挂载水位快照：已绑定账号总数 + 各时间窗内仍活跃（有入站）的去重账号数。

    用途：规模化前建立「当前实测水位」基线（见
    docs/architecture/shared/access/single-host-multi-openclaw-scale.md §7）。`channel_bindings.last_seen_at`
    在每条入站消息 upsert 时刷新，因此「窗口内 last_seen_at 命中的去重 account_id」
    是账号在线/活跃的可靠 proxy——真正的长轮询在线态在 OpenClaw 侧，此处刻意只用
    bridge 自有数据，避免盲解析不可控的 openclaw CLI 文本。重复采样即得在线率/掉线率趋势。

    active_windows_minutes：要统计的时间窗口（分钟），<=0 的值按 1 处理。
    """
    windows = [max(int(w), 1) for w in active_windows_minutes]
    with connect() as conn:
        total_row = conn.execute(
            "SELECT COUNT(DISTINCT account_id) AS total FROM channel_bindings"
        ).fetchone()
        by_channel_rows = conn.execute(
            """
            SELECT channel, COUNT(DISTINCT account_id) AS total
            FROM channel_bindings
            GROUP BY channel
            """
        ).fetchall()
        active: Dict[str, int] = {}
        for window in windows:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT account_id) AS active
                FROM channel_bindings
                WHERE last_seen_at >= datetime('now', '+8 hours', ?)
                """,
                (f"-{window} minutes",),
            ).fetchone()
            active[str(window)] = int(row["active"] or 0) if row else 0
    return {
        "total_bound_accounts": int(total_row["total"] or 0) if total_row else 0,
        "bound_accounts_by_channel": {
            r["channel"]: int(r["total"] or 0) for r in by_channel_rows
        },
        "active_accounts": active,
    }


def get_inbound_message_rate(
    *, windows_minutes: Iterable[int] = (10, 60)
) -> List[Dict[str, Any]]:
    """统计各滚动时间窗口内的入站消息数与去重账号数（实时入站监控用）。

    单条 SQL 用条件 SUM 一次算出所有窗口，避免逐窗口扫表。`created_at` 存北京时间，
    与 `datetime('now','+8 hours')` 比较，与 get_ops_metrics 时区约定一致。
    返回按窗口升序排列的 ``[{"minutes": N, "count": M, "unique_accounts": U}, ...]``。
    """
    # 去重 + 过滤非正数，按窗口升序，保证查询列与返回顺序稳定
    windows = sorted({int(m) for m in windows_minutes if int(m) > 0})
    if not windows:
        return []
    # 为每个窗口生成一个条件 SUM；最大窗口用于 WHERE 预过滤减少扫描
    select_exprs = ", ".join(
        f"SUM(CASE WHEN created_at >= datetime('now', '+8 hours', '-{m} minutes') "
        f"THEN 1 ELSE 0 END) AS w{m}_count, "
        f"COUNT(DISTINCT CASE WHEN created_at >= datetime('now', '+8 hours', '-{m} minutes') "
        f"THEN account_id ELSE NULL END) AS w{m}_unique_accounts, "
        f"datetime('now', '+8 hours', '-{m} minutes') AS w{m}_since"
        for m in windows
    )
    max_window = windows[-1]
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT {select_exprs}
            FROM messages
            WHERE direction = 'inbound'
              AND role = 'user'
              AND created_at >= datetime('now', '+8 hours', '-{max_window} minutes')
              AND created_at <= datetime('now', '+8 hours')
            """
        ).fetchone()
    return [
        {
            "minutes": m,
            "count": int((row[f"w{m}_count"] if row else 0) or 0),
            "unique_accounts": int((row[f"w{m}_unique_accounts"] if row else 0) or 0),
            "since": row[f"w{m}_since"] if row else None,
        }
        for m in windows
    ]


def get_today_inbound_message_rate() -> Dict[str, Any]:
    """统计北京时间今日 0 点以来的入站消息数与去重账号数。"""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS count,
                COUNT(DISTINCT account_id) AS unique_accounts,
                date('now', '+8 hours') || ' 00:00:00' AS since
            FROM messages
            WHERE direction = 'inbound'
              AND role = 'user'
              AND created_at >= date('now', '+8 hours') || ' 00:00:00'
              AND created_at <= datetime('now', '+8 hours')
            """
        ).fetchone()
    return {
        "key": "today",
        "count": int((row["count"] if row else 0) or 0),
        "unique_accounts": int((row["unique_accounts"] if row else 0) or 0),
        "since": row["since"] if row else None,
    }


def get_recent_reply_latencies(*, limit: int = 10) -> List[Dict[str, Any]]:
    """返回最近若干条同步回复相对上一条用户入站消息的延时。"""
    safe_limit = max(1, min(int(limit), 100))
    with connect() as conn:
        rows = conn.execute(
            """
            WITH recent_replies AS (
                SELECT
                    id, account_id, session_id, message_id, reply_to_message_id,
                    latency_ms, created_at
                FROM messages
                WHERE direction = 'outbound'
                  AND role = 'assistant'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            SELECT
                r.id AS reply_id,
                r.account_id AS account_id,
                r.session_id AS session_id,
                r.message_id AS reply_message_id,
                r.reply_to_message_id AS reply_to_message_id,
                r.created_at AS reply_created_at,
                r.latency_ms AS recorded_latency_ms,
                u.id AS inbound_id,
                u.message_id AS inbound_message_id,
                u.created_at AS inbound_created_at,
                CASE
                    WHEN u.id IS NULL THEN NULL
                    WHEN r.latency_ms IS NOT NULL THEN r.latency_ms
                    ELSE CAST(ROUND((julianday(r.created_at) - julianday(u.created_at)) * 86400000) AS INTEGER)
                END AS latency_ms,
                CASE
                    WHEN u.id IS NULL THEN NULL
                    ELSE CAST(ROUND((julianday(r.created_at) - julianday(u.created_at)) * 86400000) AS INTEGER)
                END AS created_at_delta_ms,
                CASE
                    WHEN u.id IS NULL THEN 'missing_inbound'
                    WHEN r.latency_ms IS NOT NULL THEN 'recorded_latency_ms'
                    ELSE 'created_at_delta'
                END AS latency_source
            FROM recent_replies r
            LEFT JOIN messages u ON u.id = COALESCE(
                (
                    SELECT m.id
                    FROM messages m
                    WHERE m.account_id = r.account_id
                      AND m.direction = 'inbound'
                      AND m.role = 'user'
                      AND r.reply_to_message_id IS NOT NULL
                      AND r.reply_to_message_id != ''
                      AND m.message_id = r.reply_to_message_id
                    ORDER BY m.id DESC
                    LIMIT 1
                ),
                (
                    SELECT m.id
                    FROM messages m
                    WHERE m.account_id = r.account_id
                      AND m.session_id = r.session_id
                      AND m.direction = 'inbound'
                      AND m.role = 'user'
                      AND (
                          m.created_at < r.created_at
                          OR (m.created_at = r.created_at AND m.id < r.id)
                      )
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT 1
                )
            )
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


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
            WHERE created_at >= datetime('now', '+8 hours', ?)
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
            WHERE created_at >= datetime('now', '+8 hours', ?)
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
            WHERE created_at >= datetime('now', '+8 hours', ?)
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

    def _row_count(row: Row, key: str) -> int:
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
        "database": get_database_storage_stats(),
    }
