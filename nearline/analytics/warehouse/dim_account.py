"""dim_account 装载：账号维度当前态（SCD-1 主键 UPSERT）。

注册信息来自 accounts，首聊/活跃日来自 messages 的 role=user 入站聚合。
"""

import sqlite3
from datetime import date
from typing import Optional


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """判断源快照是否包含可选归属表，兼容旧 SQLite 开发快照。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """判断源表是否包含加性列。"""
    return column in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _owner_maps(source_conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """返回 account→真人 owner、真人注册日与产品加入日映射。

    微信形态优先使用 active account_owner_binding；App resident 则回落
    universe_residents→universes.owner_platform_user_id。
    """
    owners = {}
    if _table_exists(source_conn, "account_owner_bindings"):
        for row in source_conn.execute(
            "SELECT account_id, platform_user_id FROM account_owner_bindings "
            "WHERE status = 'active'"
        ):
            owners[row["account_id"]] = row["platform_user_id"]
    if _table_exists(source_conn, "universe_residents") and _table_exists(source_conn, "universes"):
        for row in source_conn.execute(
            "SELECT r.runtime_account_id AS account_id, u.owner_platform_user_id "
            "FROM universe_residents r JOIN universes u ON r.universe_id = u.id "
            "WHERE r.runtime_account_id IS NOT NULL"
        ):
            owners.setdefault(row["account_id"], row["owner_platform_user_id"])

    registered_dates = {}
    if _table_exists(source_conn, "platform_users"):
        for row in source_conn.execute("SELECT id, created_at FROM platform_users"):
            value = row["created_at"]
            registered_dates[row["id"]] = value[:10] if value else None
    membership_dates = {}
    if _table_exists(source_conn, "product_memberships"):
        for row in source_conn.execute(
            "SELECT platform_user_id, app_id, created_at FROM product_memberships"
        ):
            value = row["created_at"]
            membership_dates[(row["platform_user_id"], row["app_id"])] = (
                value[:10] if value else None
            )
    return owners, registered_dates, membership_dates


def _iso_week_from_date_str(date_str: Optional[str]) -> Optional[str]:
    """把 YYYY-MM-DD 转 ISO 周标签；空值安全。"""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(date_str[:10])
    except ValueError:
        return None
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def load(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection, now_iso: str) -> int:
    """全量刷新 dim_account 当前态，返回处理账号数。"""
    app_id_expr = "app_id" if _column_exists(source_conn, "accounts", "app_id") else "'zhaoxi' AS app_id"
    accounts = source_conn.execute(
        f"SELECT id, {app_id_expr}, channel, is_debug, created_at, onboarding_state FROM accounts"
    ).fetchall()
    owners, platform_user_registered_dates, membership_dates = _owner_maps(source_conn)
    first_product_account_dates = {}
    for account in accounts:
        owner_id = owners.get(account["id"])
        registered_at = account["created_at"]
        if not owner_id or not registered_at:
            continue
        owner_app = (owner_id, account["app_id"] or "zhaoxi")
        registered_date = registered_at[:10]
        previous = first_product_account_dates.get(owner_app)
        if previous is None or registered_date < previous:
            first_product_account_dates[owner_app] = registered_date

    # 每账号 role=user 入站活跃聚合（首条时间、首聊日、最近活跃日）。
    activity = {}
    for r in source_conn.execute(
        "SELECT account_id, "
        "       MIN(created_at) AS first_inbound_at, "
        "       MIN(DATE(created_at)) AS first_active_date, "
        "       MAX(DATE(created_at)) AS last_active_date "
        "FROM messages "
        "WHERE direction = 'inbound' AND role = 'user' "
        "GROUP BY account_id"
    ).fetchall():
        activity[r["account_id"]] = r

    rows = []
    for a in accounts:
        act = activity.get(a["id"])
        registered_at = a["created_at"]
        registered_date = registered_at[:10] if registered_at else None
        platform_user_id = owners.get(a["id"])
        owner_app = (platform_user_id, a["app_id"] or "zhaoxi")
        product_dates = [
            value for value in (
                membership_dates.get(owner_app),
                first_product_account_dates.get(owner_app),
            ) if value
        ]
        product_registered_date = min(product_dates) if product_dates else None
        rows.append(
            (
                a["id"],
                a["app_id"] or "zhaoxi",
                a["channel"],
                platform_user_id,
                platform_user_registered_dates.get(platform_user_id),
                product_registered_date,
                int(a["is_debug"] or 0),
                registered_at,
                registered_date,
                _iso_week_from_date_str(registered_date),
                act["first_inbound_at"] if act else None,
                act["first_active_date"] if act else None,
                act["last_active_date"] if act else None,
                a["onboarding_state"],
                now_iso,
            )
        )

    facts_conn.executemany(
        "INSERT INTO dim_account"
        "(account_id, app_id, channel, platform_user_id, platform_user_registered_date, "
        " product_member_registered_date, "
        " is_debug, registered_at, registered_date, registered_week, "
        " first_inbound_at, first_active_date, last_active_date, current_onboarding_state, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        " app_id = excluded.app_id, "
        " channel = excluded.channel, "
        " platform_user_id = excluded.platform_user_id, "
        " platform_user_registered_date = excluded.platform_user_registered_date, "
        " product_member_registered_date = excluded.product_member_registered_date, "
        " is_debug = excluded.is_debug, "
        " registered_at = excluded.registered_at, "
        " registered_date = excluded.registered_date, "
        " registered_week = excluded.registered_week, "
        " first_inbound_at = excluded.first_inbound_at, "
        " first_active_date = excluded.first_active_date, "
        " last_active_date = excluded.last_active_date, "
        " current_onboarding_state = excluded.current_onboarding_state, "
        " updated_at = excluded.updated_at",
        rows,
    )
    return len(rows)
