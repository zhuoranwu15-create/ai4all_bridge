"""m0025 钱包合并迁移的直接单测（app/db/_core._migration_0025_wallet_unique_platform_user）。

fresh_db 建好时 m0022 已在空库上跑过（合并分支 = no-op），故合并逻辑本身无覆盖。此处手工
构造「迁移前多钱包老用户」态（DROP 局部唯一索引 + raw 插入第二个 active 钱包及其
ledger/cost_events），再**直接调用**迁移函数，验证 money 路径的合并正确性：

  - 选主 = 最早 active binding 对应 account 的 active 钱包；
  - 余额求和入主钱包；
  - 非主钱包的 ledger/cost_events.wallet_id 归并到主；
  - 非主钱包置 status='merged'（保留、不删，避免 FK 冲突）；
  - 局部唯一索引重建后，再对第二个 account get-or-create 返回同一主钱包；
  - 可重复执行（幂等）。

走内存/临时 SQLite（fresh_db）。
"""
import app.db as db
from app.db._core import _migration_0025_wallet_unique_platform_user

_B1 = 1_000  # 主钱包（a1）余额
_B2 = 2_500  # 被并钱包（a2）余额


def _seed_pre_migration_two_wallets(conn, *, user_id, a1, a2):
    """把 M1-1 后的「1 共享钱包」态改回「迁移前 2 钱包」态，并在 a2 钱包上挂流水/成本行。

    返回 (w1_id, w2_id)。w1 为 a1（最早 binding）的既有共享钱包，w2 为补出的 a2 钱包。
    """
    w1 = conn.execute(
        "SELECT id, account_id FROM entitlement_wallets "
        "WHERE platform_user_id=? AND status='active'",
        (user_id,),
    ).fetchone()
    assert w1["account_id"] == a1  # 共享钱包创建来源 = 最早的 account
    w1_id = w1["id"]
    conn.execute(
        "UPDATE entitlement_wallets SET balance_shell_micros=? WHERE id=?",
        (_B1, w1_id),
    )
    # 迁移前 DB 无此索引；DROP 后 raw 插第二个 active 钱包（a2）。
    conn.execute("DROP INDEX IF EXISTS ux_entitlement_wallets_user_active")
    w2_id = "wallet_pre_a2"
    conn.execute(
        """
        INSERT INTO entitlement_wallets(
            id, account_id, platform_user_id, balance_shell_micros, status
        ) VALUES (?, ?, ?, ?, 'active')
        """,
        (w2_id, a2, user_id, _B2),
    )
    # 在被并钱包上挂一条 ledger + 一条 cost_events，用于验证 wallet_id 归并。
    conn.execute(
        """
        INSERT INTO entitlement_ledger(
            id, wallet_id, account_id, platform_user_id, entry_type,
            source_type, amount_shell_micros, balance_after_shell_micros, idempotency_key
        ) VALUES ('ledger_pre_a2', ?, ?, ?, 'debit', 'chat_usage', -100, ?, 'idem-pre-a2')
        """,
        (w2_id, a2, user_id, _B2 - 100),
    )
    conn.execute(
        """
        INSERT INTO cost_events(
            id, wallet_id, account_id, platform_user_id, cost_type, idempotency_key
        ) VALUES ('cost_pre_a2', ?, ?, ?, 'llm_tokens', 'cost-idem-pre-a2')
        """,
        (w2_id, a2, user_id),
    )
    return w1_id, w2_id


def _two_accounts_one_user(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="迁移用户")
    a1 = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="甲"
    )["account"]["id"]
    a2 = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="乙"
    )["account"]["id"]
    return user["id"], a1, a2


def test_m0022_merges_multi_wallet_user(fresh_db):
    """两 active 钱包老用户：合并后主钱包=最早 binding 钱包、余额求和、流水归并、他钱包 merged。"""
    user_id, a1, a2 = _two_accounts_one_user("13800020001")
    with db.connect() as conn:
        w1_id, w2_id = _seed_pre_migration_two_wallets(
            conn, user_id=user_id, a1=a1, a2=a2
        )

    with db.connect() as conn:
        _migration_0025_wallet_unique_platform_user(conn)

    with db.connect() as conn:
        active = conn.execute(
            "SELECT id, balance_shell_micros FROM entitlement_wallets "
            "WHERE platform_user_id=? AND status='active'",
            (user_id,),
        ).fetchall()
        w2 = conn.execute(
            "SELECT status FROM entitlement_wallets WHERE id=?", (w2_id,)
        ).fetchone()
        ledger_wallet = conn.execute(
            "SELECT wallet_id FROM entitlement_ledger WHERE id='ledger_pre_a2'"
        ).fetchone()["wallet_id"]
        cost_wallet = conn.execute(
            "SELECT wallet_id FROM cost_events WHERE id='cost_pre_a2'"
        ).fetchone()["wallet_id"]

    # 选主 = a1（最早 binding）的钱包；只剩这一个 active。
    assert len(active) == 1
    assert active[0]["id"] == w1_id
    # 余额求和入主钱包。
    assert int(active[0]["balance_shell_micros"]) == _B1 + _B2
    # 被并钱包置 merged（保留、不删）。
    assert w2["status"] == "merged"
    # 被并钱包的 ledger/cost_events 归并到主钱包。
    assert ledger_wallet == w1_id
    assert cost_wallet == w1_id
    # 索引已重建：对第二个 account get-or-create 返回同一主钱包，不再新建。
    assert db.ensure_wallet(account_id=a2, platform_user_id=user_id)["id"] == w1_id


def test_m0022_is_idempotent(fresh_db):
    """迁移可重复执行：第二次跑合并态已收敛，余额/主钱包不变。"""
    user_id, a1, a2 = _two_accounts_one_user("13800020002")
    with db.connect() as conn:
        w1_id, _w2_id = _seed_pre_migration_two_wallets(
            conn, user_id=user_id, a1=a1, a2=a2
        )
    with db.connect() as conn:
        _migration_0025_wallet_unique_platform_user(conn)
    # 再跑一次（模拟重复应用）。
    with db.connect() as conn:
        _migration_0025_wallet_unique_platform_user(conn)

    with db.connect() as conn:
        active = conn.execute(
            "SELECT id, balance_shell_micros FROM entitlement_wallets "
            "WHERE platform_user_id=? AND status='active'",
            (user_id,),
        ).fetchall()
    assert len(active) == 1
    assert active[0]["id"] == w1_id
    assert int(active[0]["balance_shell_micros"]) == _B1 + _B2
