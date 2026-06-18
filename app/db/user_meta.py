"""app.db.user_meta — account-scoped derived user metadata."""
import json
import math
from typing import Any, Dict, List, Optional

from app.db._core import MODERATION_BLOCKED_ERROR, _clean_text, connect

__all__ = [
    "compute_message_intensity",
    "compute_safety_risk_count_30d",
    "fetch_recent_inbound_messages",
    "get_account_user_meta",
    "insert_account_user_meta_daily",
    "list_accounts_for_meta_refresh",
    "set_companion_type_manual",
    "upsert_account_user_meta",
]


def _encode_string_list(values: Optional[List[str]]) -> str:
    cleaned = []
    for value in values or []:
        text = _clean_text(value)
        if text:
            cleaned.append(text)
    return json.dumps(cleaned, ensure_ascii=False)


def _decode_string_list(raw: Any) -> List[str]:
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        text = _clean_text(value)
        if text:
            result.append(text)
    return result


def _decode_meta_row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    item = dict(row)
    item["companion_secondary_types"] = _decode_string_list(
        item.get("companion_secondary_types")
    )
    return item


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_message_intensity(*, account_id: str, today_start: str) -> int:
    """统计截止 today_start 之前的账号入站消息数，返回 floor(ln(1+x))。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_today_start = _clean_text(today_start)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_today_start:
        raise ValueError("today_start is required")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM messages
            WHERE account_id = ?
              AND direction = 'inbound'
              AND created_at < ?
            """,
            (cleaned_account_id, cleaned_today_start),
        ).fetchone()
    count = int(row["cnt"] or 0) if row else 0
    return math.floor(math.log1p(count))


def compute_safety_risk_count_30d(*, account_id: str, thirty_days_ago: str) -> int:
    """统计近 30 天入站非 pass/safe 的账号级去重风险事件数。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_thirty_days_ago = _clean_text(thirty_days_ago)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_thirty_days_ago:
        raise ValueError("thirty_days_ago is required")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT source_id) AS cnt
            FROM content_moderation_tasks
            WHERE account_id = ?
              AND direction = 'inbound'
              AND source_type = 'message'
              AND risk_level NOT IN ('pass', 'safe')
              AND created_at >= ?
            """,
            (cleaned_account_id, cleaned_thirty_days_ago),
        ).fetchone()
    return int(row["cnt"] or 0) if row else 0


def fetch_recent_inbound_messages(
    *, account_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """取最近 limit 条入站文本/语音消息，排除审核拦截内容，按时间升序返回。"""
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    safe_limit = max(1, min(int(limit or 50), 200))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, message_type, content, created_at
            FROM messages
            WHERE account_id = ?
              AND direction = 'inbound'
              AND message_type IN ('text', 'voice')
              AND content IS NOT NULL
              AND content != ''
              AND (error IS NULL OR error != ?)
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (cleaned_account_id, MODERATION_BLOCKED_ERROR, safe_limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def upsert_account_user_meta(
    *,
    account_id: str,
    registered_at: str,
    message_intensity_level: int,
    companion_primary_type: Optional[str],
    companion_secondary_types: List[str],
    companion_type_confidence: Optional[float],
    companion_type_last_evaluated_at: Optional[str],
    companion_type_source: str,
    companion_type_expires_at: Optional[str],
    companion_type_reasoning: Optional[str],
    safety_risk_trigger_count_30d: int,
    last_evaluated_at: str,
) -> None:
    """写入或覆盖账号当前元属性快照。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_registered_at = _clean_text(registered_at)
    cleaned_last_evaluated_at = _clean_text(last_evaluated_at)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_registered_at:
        raise ValueError("registered_at is required")
    if not cleaned_last_evaluated_at:
        raise ValueError("last_evaluated_at is required")
    source = _clean_text(companion_type_source) or "auto"
    if source not in {"auto", "manual"}:
        raise ValueError("companion_type_source must be auto or manual")
    secondary_json = _encode_string_list(companion_secondary_types)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO account_user_meta(
                account_id, registered_at, message_intensity_level,
                companion_primary_type, companion_secondary_types,
                companion_type_confidence, companion_type_last_evaluated_at,
                companion_type_source, companion_type_expires_at,
                companion_type_reasoning, safety_risk_trigger_count_30d,
                last_evaluated_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                registered_at = excluded.registered_at,
                message_intensity_level = excluded.message_intensity_level,
                companion_primary_type = excluded.companion_primary_type,
                companion_secondary_types = excluded.companion_secondary_types,
                companion_type_confidence = excluded.companion_type_confidence,
                companion_type_last_evaluated_at = excluded.companion_type_last_evaluated_at,
                companion_type_source = excluded.companion_type_source,
                companion_type_expires_at = excluded.companion_type_expires_at,
                companion_type_reasoning = excluded.companion_type_reasoning,
                safety_risk_trigger_count_30d = excluded.safety_risk_trigger_count_30d,
                last_evaluated_at = excluded.last_evaluated_at,
                updated_at = excluded.updated_at
            """,
            (
                cleaned_account_id,
                cleaned_registered_at,
                max(0, _coerce_int(message_intensity_level)),
                _clean_text(companion_primary_type),
                secondary_json,
                companion_type_confidence,
                _clean_text(companion_type_last_evaluated_at),
                source,
                _clean_text(companion_type_expires_at),
                _clean_text(companion_type_reasoning),
                max(0, _coerce_int(safety_risk_trigger_count_30d)),
                cleaned_last_evaluated_at,
                cleaned_last_evaluated_at,
            ),
        )


def insert_account_user_meta_daily(
    *,
    account_id: str,
    snapshot_date: str,
    registered_at: str,
    message_intensity_level: int,
    companion_primary_type: Optional[str],
    companion_secondary_types: List[str],
    companion_type_confidence: Optional[float],
    companion_type_last_evaluated_at: Optional[str],
    companion_type_source: str,
    companion_type_expires_at: Optional[str],
    companion_type_reasoning: Optional[str],
    safety_risk_trigger_count_30d: int,
    last_evaluated_at: str,
) -> None:
    """插入账号每日元属性快照；同账号同日幂等忽略。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_snapshot_date = _clean_text(snapshot_date)
    cleaned_registered_at = _clean_text(registered_at)
    cleaned_last_evaluated_at = _clean_text(last_evaluated_at)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_snapshot_date:
        raise ValueError("snapshot_date is required")
    if not cleaned_registered_at:
        raise ValueError("registered_at is required")
    if not cleaned_last_evaluated_at:
        raise ValueError("last_evaluated_at is required")
    source = _clean_text(companion_type_source) or "auto"
    if source not in {"auto", "manual"}:
        raise ValueError("companion_type_source must be auto or manual")
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO account_user_meta_daily(
                account_id, snapshot_date, registered_at, message_intensity_level,
                companion_primary_type, companion_secondary_types,
                companion_type_confidence, companion_type_last_evaluated_at,
                companion_type_source, companion_type_expires_at,
                companion_type_reasoning, safety_risk_trigger_count_30d,
                last_evaluated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_account_id,
                cleaned_snapshot_date,
                cleaned_registered_at,
                max(0, _coerce_int(message_intensity_level)),
                _clean_text(companion_primary_type),
                _encode_string_list(companion_secondary_types),
                companion_type_confidence,
                _clean_text(companion_type_last_evaluated_at),
                source,
                _clean_text(companion_type_expires_at),
                _clean_text(companion_type_reasoning),
                max(0, _coerce_int(safety_risk_trigger_count_30d)),
                cleaned_last_evaluated_at,
            ),
        )


def get_account_user_meta(*, account_id: str) -> Optional[Dict[str, Any]]:
    """读取账号当前元属性快照，不存在返回 None。"""
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                account_id, registered_at, message_intensity_level,
                companion_primary_type, companion_secondary_types,
                companion_type_confidence, companion_type_last_evaluated_at,
                companion_type_source, companion_type_expires_at,
                companion_type_reasoning, safety_risk_trigger_count_30d,
                last_evaluated_at, created_at, updated_at
            FROM account_user_meta
            WHERE account_id = ?
            """,
            (cleaned_account_id,),
        ).fetchone()
    return _decode_meta_row(row)


def list_accounts_for_meta_refresh(
    *, offset: int = 0, limit: int = 100
) -> List[Dict[str, Any]]:
    """分页返回需要刷新元属性的非 debug、非 deactivated 账号。"""
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 100), 1000))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at
            FROM accounts
            WHERE COALESCE(is_debug, 0) = 0
              AND COALESCE(status, 'active') != 'deactivated'
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (safe_limit, safe_offset),
        ).fetchall()
    return [dict(row) for row in rows]


def set_companion_type_manual(
    *,
    account_id: str,
    primary_type: str,
    secondary_types: List[str],
    confidence: float,
    expires_at: Optional[str],
    reasoning: Optional[str],
    now: str,
) -> None:
    """Admin 人工覆盖陪伴类型，写入 source='manual' 并保留当前计算字段。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_primary_type = _clean_text(primary_type)
    cleaned_now = _clean_text(now)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_primary_type:
        raise ValueError("primary_type is required")
    if not cleaned_now:
        raise ValueError("now is required")
    with connect() as conn:
        current = conn.execute(
            "SELECT * FROM account_user_meta WHERE account_id = ?",
            (cleaned_account_id,),
        ).fetchone()
        account = conn.execute(
            "SELECT created_at FROM accounts WHERE id = ?",
            (cleaned_account_id,),
        ).fetchone()
        if account is None:
            raise ValueError("account not found")
        registered_at = current["registered_at"] if current else account["created_at"]
        message_intensity_level = current["message_intensity_level"] if current else 0
        safety_count = current["safety_risk_trigger_count_30d"] if current else 0
        conn.execute(
            """
            INSERT INTO account_user_meta(
                account_id, registered_at, message_intensity_level,
                companion_primary_type, companion_secondary_types,
                companion_type_confidence, companion_type_last_evaluated_at,
                companion_type_source, companion_type_expires_at,
                companion_type_reasoning, safety_risk_trigger_count_30d,
                last_evaluated_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                companion_primary_type = excluded.companion_primary_type,
                companion_secondary_types = excluded.companion_secondary_types,
                companion_type_confidence = excluded.companion_type_confidence,
                companion_type_last_evaluated_at = excluded.companion_type_last_evaluated_at,
                companion_type_source = excluded.companion_type_source,
                companion_type_expires_at = excluded.companion_type_expires_at,
                companion_type_reasoning = excluded.companion_type_reasoning,
                last_evaluated_at = excluded.last_evaluated_at,
                updated_at = excluded.updated_at
            """,
            (
                cleaned_account_id,
                registered_at,
                max(0, _coerce_int(message_intensity_level)),
                cleaned_primary_type,
                _encode_string_list(secondary_types),
                max(0.0, min(1.0, float(confidence))),
                cleaned_now,
                _clean_text(expires_at),
                _clean_text(reasoning),
                max(0, _coerce_int(safety_count)),
                cleaned_now,
                cleaned_now,
            ),
        )
