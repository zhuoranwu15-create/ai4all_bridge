"""M0-1 characterization：钉住 M1 重构会翻转的确定性现状（安全网）。

普查发现 prompt 组装 / session 轮转 / moderation / reminder / reactivation / proactive
budget 等接缝已被现有测试充分钉住；本文件只补三处此前无直接 characterization、且正是
M1（D-14 钱包上迁 / D-09 配额上迁）会改动的接缝：

  1) 消息幂等：insert_message 对重复 (account_id, message_id) 返回 None、不重复入库，
     且按 account 隔离（不同 account 同名 message_id 不算重复）。
  2) 一人多号钱包/赠权：同一 platform_user 的两个 account 共用一个 active 钱包、只赠一次
     new_user_grant（幂等键按 platform_user）——**M1-1+M1-7（D-14）已落地，此为翻转后终态**。
  3) 一人多号 daily 配额：daily 按 platform_user 聚合、全部居民共享一套额度——**M1-3+M1-4
     （D-09）已落地，此为翻转后终态**。

重要：2) 原钉 pre-D-14 现状、已随 M1-1+M1-7 翻转为新预期；3) 原钉 pre-D-09 现状、已随
M1-3+M1-4 翻转为新预期。RPM 的 account 隔离已由 test_rate_limiter::test_accounts_are_isolated
覆盖，此处不重复。全部走内存 SQLite（fresh_db）。
"""
import app.db as db
from tests import factories

from app.db._core import NEW_USER_GRANT_SHELL_MICROS


# ---------------------------------------------------------------------------
# 接缝 3：消息幂等（持久化去重的确定性契约）
# ---------------------------------------------------------------------------
def test_insert_message_is_idempotent_per_account(fresh_db):
    """重复 (account_id, message_id) 二次插入返回 None，且库内只留一行。"""
    account_id = "char-acct-msg"
    session_id = factories.create_account(account_id)

    def _insert(content: str):
        return db.insert_message(
            account_id=account_id,
            session_id=session_id,
            message_id="dup-1",
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type="text",
            content=content,
        )

    first = _insert("hi")
    assert first is not None
    # 同 (account_id, message_id) 重放：唯一索引冲突被吞，返回 None、不重复入库。
    second = _insert("hi again")
    assert second is None

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE account_id=? AND message_id=?",
            (account_id, "dup-1"),
        ).fetchone()["c"]
    assert count == 1


def test_insert_message_dedup_is_account_scoped(fresh_db):
    """相同 message_id 落在不同 account 不算重复（按 account 隔离的核心不变量）。"""
    a1, a2 = "char-acct-a", "char-acct-b"
    s1 = factories.create_account(a1)
    s2 = factories.create_account(a2)

    common_kwargs = dict(
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="x",
    )
    first = db.insert_message(
        account_id=a1, session_id=s1, message_id="shared-mid", **common_kwargs
    )
    other = db.insert_message(
        account_id=a2, session_id=s2, message_id="shared-mid", **common_kwargs
    )
    assert first is not None
    assert other is not None  # 不同 account 的同名 message_id 各自入库


# ---------------------------------------------------------------------------
# 接缝 5：一人多号钱包 + 赠权终态（D-14 / M1-1 + M1-7 已落地）
# ---------------------------------------------------------------------------
def _two_accounts_of_one_user() -> tuple:
    """同一 platform_user 的两个 account，返回 (platform_user_id, a1, a2)。

    决策 B：a1 = 用户账号（form-A，发 owner_binding）；a2 = 世界里的居民（form-B runtime account，
    无 binding，经世界归属解析共享真人钱包/配额）。第二号走居民内部路径，不再用注册入口撞 #42 收敛。
    """
    user = db.create_or_get_platform_user_by_phone(
        phone="13800009001", display_name="多号用户"
    )
    a1 = factories.make_user_account(
        user["id"], "居民甲", app_id="mingchan"
    )
    a2 = factories.make_resident_account(user["id"], "居民乙")
    assert a1 != a2
    assert (
        db.get_platform_user_id_for_account(account_id=a1)
        == db.get_platform_user_id_for_account(account_id=a2)
        == user["id"]
    )
    return user["id"], a1, a2


def test_multi_account_shared_wallet_and_single_grant_per_person(fresh_db):
    """D-14 后终态：一人两号 = 一个共享 active 钱包 + 一次 new_user_grant（键按 platform_user）。

    M0-1 曾钉 pre-D-14 现状（两独立钱包 + 两次赠权，键含 account_id）；M1-1+M1-7 落地后
    翻转为「一真人一钱包一赠权、全部居民共用一份余额」，此断言即新预期。
    """
    user_id, a1, a2 = _two_accounts_of_one_user()

    with db.connect() as conn:
        active_wallets = conn.execute(
            """
            SELECT id, account_id, platform_user_id
            FROM entitlement_wallets
            WHERE platform_user_id = ? AND status = 'active'
            """,
            (user_id,),
        ).fetchall()
        grant_rows = conn.execute(
            """
            SELECT idempotency_key, amount_shell_micros
            FROM entitlement_ledger
            WHERE source_type='new_user_grant' AND platform_user_id = ?
            """,
            (user_id,),
        ).fetchall()

    # 同一真人的两个 account 共用一个 active 钱包（局部唯一索引保证唯一）。
    assert len(active_wallets) == 1
    # 全部居民共用一份余额 → 该真人只赠一次，幂等键按 platform_user。
    assert len(grant_rows) == 1
    assert grant_rows[0]["idempotency_key"] == f"new-user-grant-mingchan-{user_id}"
    assert int(grant_rows[0]["amount_shell_micros"]) == NEW_USER_GRANT_SHELL_MICROS


# ---------------------------------------------------------------------------
# 接缝 6：一人多号 daily 配额终态（D-09 / M1-3 + M1-4 已落地，按 platform_user 共享）
# ---------------------------------------------------------------------------
def test_multi_account_daily_usage_is_shared_per_person(fresh_db):
    """D-09 后终态：同一真人的两个号 daily 计数共享一套额度（按 platform_user 聚合）。

    M0-1 曾钉 pre-D-09 现状（两号各自独立计数）；M1-3+M1-4 落地后翻转为「一真人一套配额、
    全部居民共享」，此断言即新预期。
    """
    _user_id, a1, a2 = _two_accounts_of_one_user()
    date = "2026-07-19"

    db.increment_daily_usage(account_id=a1, date=date)
    db.increment_daily_usage(account_id=a1, date=date)
    # 另一个号继续消耗同一套额度（不再各自独立）。
    db.increment_daily_usage(account_id=a2, date=date)

    # 两个号读到同一份共享计数（3），而非各自独立。
    assert db.get_daily_usage(account_id=a1, date=date) == 3
    assert db.get_daily_usage(account_id=a2, date=date) == 3
