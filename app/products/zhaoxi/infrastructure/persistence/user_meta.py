"""app.products.zhaoxi.infrastructure.persistence.user_meta — account-scoped derived user metadata."""
import json
import math
from typing import Any, Dict, List, Optional

from app.db._core import MODERATION_BLOCKED_ERROR, _clean_text, connect
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.time_utils import beijing_naive_now

__all__ = [
    "GROWTH_STATUS_VALUES",
    "RELATIONSHIP_STAGE_VALUES",
    "SURVIVAL_STATUS_VALUES",
    "TRUST_STATUS_VALUES",
    "compute_message_intensity",
    "compute_safety_risk_count_30d",
    "count_inbound_messages",
    "fetch_recent_inbound_messages",
    "list_recent_inbound_message_dates",
    "get_account_user_meta",
    "insert_account_user_meta_daily",
    "list_account_user_meta_current",
    "list_accounts_for_meta_refresh",
    "set_companion_type_manual",
    "update_account_user_meta_relationship",
    "upsert_account_user_meta",
]

# 关系状态枚举与默认值（见 docs/architecture/products/zhaoxi/relationship_state_design.md §3）。
# 非法值在写入入口回退默认，避免脏值入库。
RELATIONSHIP_STAGE_VALUES = {"icebreaking", "acquainted", "deep_bond"}
SURVIVAL_STATUS_VALUES = {"healthy", "cooling", "inactive", "resource_risk"}
TRUST_STATUS_VALUES = {"building", "stable", "damaged"}
GROWTH_STATUS_VALUES = {"not_started", "emerging", "stable"}

_RELATIONSHIP_DEFAULTS = {
    "relationship_stage": ("icebreaking", RELATIONSHIP_STAGE_VALUES),
    "agent_need_survival_status": ("cooling", SURVIVAL_STATUS_VALUES),
    "agent_need_trust_status": ("building", TRUST_STATUS_VALUES),
    "agent_need_growth_status": ("not_started", GROWTH_STATUS_VALUES),
}


def _normalize_relationship_value(column: str, value: Any) -> str:
    """把关系状态值规整到合法枚举；非法值回退该列默认。"""
    default, allowed = _RELATIONSHIP_DEFAULTS[column]
    text = _clean_text(value)
    return text if text in allowed else default


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


def count_inbound_messages(*, account_id: str) -> int:
    """统计账号入站消息总数，供 relationship_stage 的 30 条阈值判断（含当天）。"""
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM messages
            WHERE account_id = ?
              AND direction = 'inbound'
            """,
            (cleaned_account_id,),
        ).fetchone()
    return int(row["cnt"] or 0) if row else 0


def list_recent_inbound_message_dates(
    *, account_id: str, since_date: str
) -> List[str]:
    """返回自 since_date（含）起有用户入站消息的去重北京自然日，升序。

    messages.created_at 以北京时间字符串存储，自然日取其前 10 位（YYYY-MM-DD）。
    since_date 可传日期（'2026-06-01'）或日期时间字符串，按字符串下界比较。
    供 agent_need_survival_status 的连续天数判断。
    """
    cleaned_account_id = _clean_text(account_id)
    cleaned_since_date = _clean_text(since_date)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_since_date:
        raise ValueError("since_date is required")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(created_at, 1, 10) AS d
            FROM messages
            WHERE account_id = ?
              AND direction = 'inbound'
              AND created_at >= ?
            ORDER BY d
            """,
            (cleaned_account_id, cleaned_since_date),
        ).fetchall()
    return [row["d"] for row in rows if row["d"]]


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
    relationship_stage: Optional[str] = None,
    agent_need_survival_status: Optional[str] = None,
    agent_need_trust_status: Optional[str] = None,
    agent_need_growth_status: Optional[str] = None,
) -> None:
    """插入账号每日元属性快照；同账号同日幂等忽略。

    四个关系状态入参用于把当前关系快照进每日历史；None 或非法值回退该列默认。
    """
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
            INSERT INTO account_user_meta_daily(
                account_id, snapshot_date, registered_at, message_intensity_level,
                companion_primary_type, companion_secondary_types,
                companion_type_confidence, companion_type_last_evaluated_at,
                companion_type_source, companion_type_expires_at,
                companion_type_reasoning, safety_risk_trigger_count_30d,
                last_evaluated_at,
                relationship_stage, agent_need_survival_status,
                agent_need_trust_status, agent_need_growth_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
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
                _normalize_relationship_value("relationship_stage", relationship_stage),
                _normalize_relationship_value(
                    "agent_need_survival_status", agent_need_survival_status
                ),
                _normalize_relationship_value(
                    "agent_need_trust_status", agent_need_trust_status
                ),
                _normalize_relationship_value(
                    "agent_need_growth_status", agent_need_growth_status
                ),
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
                relationship_stage, agent_need_survival_status,
                agent_need_trust_status, agent_need_growth_status,
                last_evaluated_at, created_at, updated_at
            FROM account_user_meta
            WHERE account_id = ?
            """,
            (cleaned_account_id,),
        ).fetchone()
    return _decode_meta_row(row)


def list_account_user_meta_current(
    *,
    q: Optional[str] = None,
    primary_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """返回账号当前元属性列表，包含未生成 meta 的账号，供 Admin 表格展示。"""
    safe_limit = max(1, min(int(limit or 500), 1000))
    filters = []
    params: List[Any] = []
    search = _clean_text(q)
    if search:
        filters.append("(a.id LIKE ? OR COALESCE(a.display_name, '') LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    cleaned_primary = _clean_text(primary_type)
    if cleaned_primary:
        filters.append("m.companion_primary_type = ?")
        params.append(cleaned_primary)
    cleaned_source = _clean_text(source)
    if cleaned_source:
        filters.append("m.companion_type_source = ?")
        params.append(cleaned_source)
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.id AS account_id,
                a.display_name,
                a.status,
                a.channel,
                a.created_at AS account_created_at,
                MAX(msg.created_at) AS last_active_at,
                m.registered_at,
                m.message_intensity_level,
                m.companion_primary_type,
                m.companion_secondary_types,
                m.companion_type_confidence,
                m.companion_type_last_evaluated_at,
                m.companion_type_source,
                m.companion_type_expires_at,
                m.companion_type_reasoning,
                m.safety_risk_trigger_count_30d,
                m.relationship_stage,
                m.agent_need_survival_status,
                m.agent_need_trust_status,
                m.agent_need_growth_status,
                m.last_evaluated_at,
                m.created_at AS meta_created_at,
                m.updated_at AS meta_updated_at
            FROM accounts a
            LEFT JOIN account_user_meta m ON m.account_id = a.id
            LEFT JOIN messages msg ON msg.account_id = a.id
            {where_sql}
            -- 含 m.account_id（account_user_meta 主键）：PG 据此放行所选 m.* 列的函数依赖
            GROUP BY a.id, m.account_id
            ORDER BY
                CASE WHEN m.last_evaluated_at IS NULL THEN 1 ELSE 0 END,
                m.last_evaluated_at DESC,
                last_active_at DESC,
                a.created_at DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()
    return [_decode_meta_row(row) or {} for row in rows]


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
              AND app_id = ?
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (ZHAOXI_APP_ID, safe_limit, safe_offset),
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


def update_account_user_meta_relationship(
    *,
    account_id: str,
    relationship_stage: Optional[str] = None,
    agent_need_survival_status: Optional[str] = None,
    agent_need_trust_status: Optional[str] = None,
    agent_need_growth_status: Optional[str] = None,
) -> None:
    """更新账号关系状态列，仅写入非 None 入参；其余列保持不变。

    关系状态（relationship_stage / 三个 agent_need_*）的唯一写入口，turn 级与天级
    确定性 / LLM 更新共用；companion 字段不在此处改动。非法 enum 值回退该列默认。
    account_user_meta 行不存在时按默认值建行（registered_at 取 accounts.created_at），
    以保证新账号在天级任务首跑前也能写入关系状态。
    """
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")

    # 收集本次要写的列：仅非 None 入参，并规整到合法枚举。
    updates: List[tuple] = []
    for column, value in (
        ("relationship_stage", relationship_stage),
        ("agent_need_survival_status", agent_need_survival_status),
        ("agent_need_trust_status", agent_need_trust_status),
        ("agent_need_growth_status", agent_need_growth_status),
    ):
        if value is not None:
            updates.append((column, _normalize_relationship_value(column, value)))
    if not updates:
        return

    now = beijing_naive_now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        account = conn.execute(
            "SELECT created_at FROM accounts WHERE id = ?",
            (cleaned_account_id,),
        ).fetchone()
        if account is None:
            raise ValueError("account not found")
        # 缺行建行：registered_at / last_evaluated_at 为 NOT NULL 无默认，需补值。
        # 已存在则走 ON CONFLICT，仅更新关系列与 updated_at，不动这两列。
        insert_columns = ["account_id", "registered_at", "last_evaluated_at", "updated_at"]
        insert_values: List[Any] = [
            cleaned_account_id,
            str(account["created_at"]),
            now,
            now,
        ]
        for column, value in updates:
            insert_columns.append(column)
            insert_values.append(value)
        set_clause = ", ".join(f"{column} = excluded.{column}" for column, _ in updates)
        placeholders = ", ".join("?" for _ in insert_columns)
        conn.execute(
            f"""
            INSERT INTO account_user_meta({", ".join(insert_columns)})
            VALUES ({placeholders})
            ON CONFLICT(account_id) DO UPDATE SET
                {set_clause},
                updated_at = excluded.updated_at
            """,
            tuple(insert_values),
        )
