"""app.db.mission — 账号级使命分配与记录的瞬间。

见 docs/tech_design/agent_mission_and_orchestration_design.md §3/§5。

不可变性保证：本模块**不提供**任何改写 `mission_id` 的函数——`assign_mission`
只在账号尚无分配时插入一行（`ON CONFLICT DO NOTHING` 兜底），没有 update 路径，
即"没有写路径"本身就是不可更改性的强制手段，比运行时权限检查更硬。
"""
from typing import Any, Dict, List, Optional

from app.db._core import _clean_text, connect
from app.time_utils import beijing_now_str

__all__ = [
    "assign_mission",
    "count_mission_moments",
    "get_account_mission",
    "list_mission_moments",
    "record_mission_moment",
]


def assign_mission(*, account_id: str, mission_id: str) -> None:
    """仅当账号尚无使命分配时插入一行；已存在则不做任何写入。

    调用方（app.products.zhaoxi.application.missions.assignment.assign_mission_if_absent）应先落 MISSION.md
    prose 再调用本函数——两步都可安全重复调用，顺序保证进程中途崩溃后重试
    不会留下"DB 已分配但 MISSION.md 仍是占位符"的不一致状态。
    """
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    cleaned_mission_id = _clean_text(mission_id)
    if not cleaned_mission_id:
        raise ValueError("mission_id is required")

    now = beijing_now_str()
    with connect() as conn:
        account = conn.execute(
            "SELECT id FROM accounts WHERE id = ?", (cleaned_account_id,)
        ).fetchone()
        if account is None:
            raise ValueError("account not found")
        conn.execute(
            """
            INSERT INTO account_mission (account_id, mission_id, assigned_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (cleaned_account_id, cleaned_mission_id, now, now, now),
        )


def get_account_mission(*, account_id: str) -> Optional[Dict[str, Any]]:
    """返回账号当前使命分配 {account_id, mission_id, assigned_at}，未分配返回 None。"""
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT account_id, mission_id, assigned_at FROM account_mission WHERE account_id = ?",
            (cleaned_account_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "account_id": row["account_id"],
        "mission_id": row["mission_id"],
        "assigned_at": row["assigned_at"],
    }


def record_mission_moment(
    *,
    account_id: str,
    mission_id: str,
    content: str,
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> int:
    """插入一条瞬间记录（追加型、不可撤销），返回新行 id。"""
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    cleaned_mission_id = _clean_text(mission_id)
    if not cleaned_mission_id:
        raise ValueError("mission_id is required")
    cleaned_content = _clean_text(content)
    if not cleaned_content:
        raise ValueError("content is required")

    now = beijing_now_str()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO mission_moments
                (account_id, mission_id, content, session_id, message_id, recorded_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cleaned_account_id, cleaned_mission_id, cleaned_content, session_id, message_id, now, now),
        )
        return int(cursor.lastrowid)


def count_mission_moments(*, account_id: str, mission_id: str) -> int:
    """已记录瞬间数——使命进度的唯一派生来源，不另建计数字段。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_mission_id = _clean_text(mission_id)
    if not cleaned_account_id or not cleaned_mission_id:
        return 0
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM mission_moments WHERE account_id = ? AND mission_id = ?",
            (cleaned_account_id, cleaned_mission_id),
        ).fetchone()
    return int(row["c"]) if row else 0


def list_mission_moments(*, account_id: str, mission_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """最近记录的瞬间（按记录时间倒序），供渲染器/工具展示最近内容。"""
    cleaned_account_id = _clean_text(account_id)
    cleaned_mission_id = _clean_text(mission_id)
    if not cleaned_account_id or not cleaned_mission_id:
        return []
    safe_limit = max(1, min(int(limit or 20), 100))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content, recorded_at
            FROM mission_moments
            WHERE account_id = ? AND mission_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (cleaned_account_id, cleaned_mission_id, safe_limit),
        ).fetchall()
    return [{"id": r["id"], "content": r["content"], "recorded_at": r["recorded_at"]} for r in rows]
