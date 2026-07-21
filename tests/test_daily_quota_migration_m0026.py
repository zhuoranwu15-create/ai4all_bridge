"""m0026 daily 配额上迁的直接单测（app/db/_core._migration_0026_daily_usage_platform_user）。

fresh_db 建好时 m0023 已在空库上跑过（回填/合并 = no-op），故合并逻辑本身无覆盖。此处手工
构造「迁移前态」（DROP 唯一索引 + raw 插入同一真人两号的当日 NULL-pu 行），再**直接调用**
迁移函数，验证：

  - 回填：从最早 active binding 把 platform_user_id 补齐；
  - 合并：同 (真人,date) 多行 message_count 求和入主行（MIN(id)）、删其余；
  - 唯一索引重建后，两号经公共 API 读到同一份共享计数、继续 increment 命中同一行；
  - 可重复执行（幂等）。

走内存/临时 SQLite（fresh_db）。
"""
import app.db as db
from app.db._core import _migration_0026_daily_usage_platform_user
from tests.factories import make_resident_account

_DATE = "2026-07-19"
_C1 = 3  # a1 当日计数
_C2 = 5  # a2 当日计数


def _two_accounts_one_user(phone: str):
    # 决策 B：a1 = 用户账号（form-A）；a2 = 居民（form-B，无 binding），走居民内部路径避开 #42 收敛。
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="配额迁移用户")
    a1 = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="甲"
    )["account"]["id"]
    a2 = make_resident_account(user["id"], "乙")
    return user["id"], a1, a2


def _seed_pre_migration_daily(conn, *, user_id, a1, a2):
    """构造迁移前态：DROP 唯一索引后 raw 插两行当日行，迁移后应合并到 (真人,date) 一行。

    决策 B 下 a1=用户账号(form-A)、a2=居民(form-B)：
    - a1 行 pu=NULL，模拟 pre-M1（pu 列尚未回填），测迁移经 owner_binding 回填到真人；
    - a2 行 pu=user_id，居民 daily 天然按真人（世界解析）计数、从不为 NULL（迁移期居民本不存在，
      此行仅为让「同真人多行合并」路径可测）。
    """
    conn.execute("DROP INDEX IF EXISTS ux_daily_usage_user_date")
    for acct, pu, cnt in ((a1, None, _C1), (a2, user_id, _C2)):
        conn.execute(
            "INSERT INTO daily_usage(account_id, platform_user_id, date, message_count, updated_at) "
            "VALUES (?, ?, ?, ?, '2026-07-19 00:00:00')",
            (acct, pu, _DATE, cnt),
        )


def test_m0023_backfills_and_merges_multi_account_daily(fresh_db):
    """一人两号当日各有行：迁移后回填 pu + 合并求和为一行，两号共享读到合计。"""
    user_id, a1, a2 = _two_accounts_one_user("13800030001")
    with db.connect() as conn:
        _seed_pre_migration_daily(conn, user_id=user_id, a1=a1, a2=a2)

    with db.connect() as conn:
        _migration_0026_daily_usage_platform_user(conn)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, platform_user_id, message_count FROM daily_usage "
            "WHERE platform_user_id=? AND date=?",
            (user_id, _DATE),
        ).fetchall()

    # 合并为一行、pu 已回填、message_count 求和。
    assert len(rows) == 1
    assert rows[0]["platform_user_id"] == user_id
    assert int(rows[0]["message_count"]) == _C1 + _C2

    # 公共 API：两号读到同一份共享计数。
    assert db.get_daily_usage(account_id=a1, date=_DATE) == _C1 + _C2
    assert db.get_daily_usage(account_id=a2, date=_DATE) == _C1 + _C2

    # 唯一索引已重建：第二个号 increment 命中同一 (真人,date) 行、不新建。
    db.increment_daily_usage(account_id=a2, date=_DATE)
    assert db.get_daily_usage(account_id=a1, date=_DATE) == _C1 + _C2 + 1


def test_m0023_orphan_no_binding_backfills_account_id(fresh_db):
    """无 active binding 的孤儿号：迁移回退 platform_user_id=account_id（非 NULL），运行时 increment 不崩。

    codex finding ②：旧迁移让孤儿行 pu 恒为 NULL，运行时以 subject=account_id 写入时
    ON CONFLICT(pu,date) 命不中 NULL 行、转撞保留的 UNIQUE(account_id,date) 无 arbiter → IntegrityError。
    """
    user = db.create_or_get_platform_user_by_phone(phone="13800030003", display_name="孤儿迁移")
    acc = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="孤儿"
    )["account"]["id"]
    with db.connect() as conn:
        # 制造孤儿态：删本号 owner binding；DROP 唯一索引后置一行 NULL-pu 当日行（模拟迁移前）。
        conn.execute("DELETE FROM account_owner_bindings WHERE account_id = ?", (acc,))
        conn.execute("DROP INDEX IF EXISTS ux_daily_usage_user_date")
        conn.execute(
            "INSERT INTO daily_usage(account_id, platform_user_id, date, message_count, updated_at) "
            "VALUES (?, NULL, ?, 4, '2026-07-19 00:00:00')",
            (acc, _DATE),
        )

    with db.connect() as conn:
        _migration_0026_daily_usage_platform_user(conn)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT platform_user_id, message_count FROM daily_usage WHERE account_id = ? AND date = ?",
            (acc, _DATE),
        ).fetchone()
    # 孤儿回退：pu = account_id（非 NULL），进入唯一索引。
    assert row["platform_user_id"] == acc
    assert int(row["message_count"]) == 4

    # 运行时：subject=account_id（无绑定 fallback），ON CONFLICT(pu,date) 命中同一行 → 不崩、+1、不双写。
    assert db.increment_daily_usage(account_id=acc, date=_DATE) == 5
    assert db.get_daily_usage(account_id=acc, date=_DATE) == 5


def test_m0023_is_idempotent(fresh_db):
    """迁移可重复执行：第二次跑合并态已收敛，计数/主行不变。"""
    user_id, a1, a2 = _two_accounts_one_user("13800030002")
    with db.connect() as conn:
        _seed_pre_migration_daily(conn, user_id=user_id, a1=a1, a2=a2)
    with db.connect() as conn:
        _migration_0026_daily_usage_platform_user(conn)
    # 再跑一次（模拟重复应用）。
    with db.connect() as conn:
        _migration_0026_daily_usage_platform_user(conn)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT message_count FROM daily_usage WHERE platform_user_id=? AND date=?",
            (user_id, _DATE),
        ).fetchall()
    assert len(rows) == 1
    assert int(rows[0]["message_count"]) == _C1 + _C2
