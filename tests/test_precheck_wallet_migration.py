"""M1-0 钱包迁移预检脚本的判定逻辑单测（scripts/precheck_wallet_migration.py）。

预检是 D-14 钱包上迁的 money 路径发布闸，自身必须可信：干净多钱包用户应 PASS，
四条阻断条件（ambiguous_owner / orphan_wallet / owner_drift / primary_undefined）
各自注入后应 BLOCK。生产真值在 PG（本地 SQLite 仅 2 user）；此处只验查询与判定逻辑
的正确性，走内存 SQLite（fresh_db）。
"""
import app.db as db
from app.db._core import NEW_USER_GRANT_SHELL_MICROS
from scripts.precheck_wallet_migration import run_precheck


def _make_user_with_accounts(phone: str, n: int) -> tuple:
    """建一个 platform_user + n 个 active account（各自 binding，钱包由测试另补）。

    决策 B / main #42：一手机号 × 一 App = 一个用户账号，故仅**首个** account 走真实注册入口
    create_ai4all_account_for_user（form-A：binding + 共享钱包 + 一次赠权）。预检面向的「一人多号
    多钱包」是 **pre-#42 / pre-M1 legacy** 存量（旧模型每号各有 binding+wallet），对第 2..n 个号
    raw 建 account + active owner_binding——先 DROP #42 的 ux_owner_binding_active_user_app，
    忠实复现迁移前「无一人一 active binding 约束」的存量形态（与各测试 DROP 钱包唯一索引同理）。
    钱包由各测试用 _seed_extra_active_wallet 另补。

    返回 (platform_user_id, [account_id, ...])，account 顺序即创建顺序（最早在前）。
    """
    from app.db._core import _new_account_id

    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="预检用户")
    uid = user["id"]
    account_ids = [
        db.create_ai4all_account_for_user(
            platform_user_id=uid, display_name="居民0"
        )["account"]["id"]
    ]
    if n > 1:
        with db.connect() as conn:
            conn.execute("DROP INDEX IF EXISTS ux_owner_binding_active_user_app")
            for i in range(1, n):
                acc = _new_account_id()
                conn.execute(
                    "INSERT INTO accounts(id, channel, display_name, app_id, updated_at) "
                    "VALUES (?, 'openclaw-weixin', ?, 'zhaoxi', "
                    "strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))",
                    (acc, f"居民{i}"),
                )
                conn.execute(
                    "INSERT INTO account_owner_bindings("
                    "platform_user_id, account_id, binding_method, status, app_id, updated_at) "
                    "VALUES (?, ?, 'legacy', 'active', 'zhaoxi', "
                    "strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))",
                    (uid, acc),
                )
                account_ids.append(acc)
    return uid, account_ids


def _blocking_names(report) -> set:
    return {c.name for c in report.blocking()}


def _seed_extra_active_wallet(conn, *, account_id, platform_user_id, with_grant=True):
    """模拟「迁移前」多钱包存量：绕过 m0022 局部唯一索引，为 account 直插一个 active 钱包。

    m0022 加了 ux_entitlement_wallets_user_active 后，正常路径不再能为同一真人建第二个
    active 钱包；但预检面向的正是**迁移前** DB（migration 尚未跑、无此索引），故此处先
    DROP 该索引再 raw 插入，忠实复现预检要检测的存量形态。with_grant=True 时附一条
    pre-D-14 的按 account 赠权流水（幂等键含 account_id），供 multi_grant_users 盘点。
    """
    conn.execute("DROP INDEX IF EXISTS ux_entitlement_wallets_user_active")
    conn.execute("DROP INDEX IF EXISTS ux_entitlement_wallets_user_app_active")
    wallet_id = f"wallet_pre_{account_id}"
    conn.execute(
        """
        INSERT INTO entitlement_wallets(
            id, account_id, platform_user_id, balance_shell_micros, status, updated_at
        )
        VALUES (?, ?, ?, 0, 'active',
                strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        """,
        (wallet_id, account_id, platform_user_id),
    )
    if with_grant:
        conn.execute(
            """
            INSERT INTO entitlement_ledger(
                id, wallet_id, account_id, platform_user_id, entry_type,
                source_type, source_id, amount_shell_micros,
                balance_after_shell_micros, idempotency_key, metadata_json
            )
            VALUES (?, ?, ?, ?, 'credit', 'new_user_grant', ?, ?, ?, ?, '{}')
            """,
            (
                f"ledger_pre_{account_id}",
                wallet_id,
                account_id,
                platform_user_id,
                account_id,
                NEW_USER_GRANT_SHELL_MICROS,
                NEW_USER_GRANT_SHELL_MICROS,
                f"new-user-grant-{account_id}",
            ),
        )
    return wallet_id


def test_precheck_clean_multi_wallet_passes(fresh_db):
    """干净的一人两号（迁移前形态）：2 active 钱包 + 2 赠权、归属一致 → 无阻断，PASS。"""
    user_id, accounts = _make_user_with_accounts("13800010001", 2)
    assert len(accounts) == 2
    # M1-1 后建号只建 1 共享钱包；此处补出迁移前的第二个 active 钱包 + 其按 account 赠权。
    with db.connect() as conn:
        _seed_extra_active_wallet(
            conn, account_id=accounts[1], platform_user_id=user_id, with_grant=True
        )

    with db.connect() as conn:
        report = run_precheck(conn)

    assert report.active_wallets == 2
    assert report.distinct_owners == 1
    assert len(report.multi_wallet_users) == 1
    assert report.multi_wallet_users[0]["wallet_count"] == 2
    # 两个 account 各赠一次 new_user_grant → 该真人是「多次赠权」population。
    assert len(report.multi_grant_users) == 1
    assert report.multi_grant_users[0]["grant_count"] == 2
    # 干净数据无任何阻断条件。
    assert report.blocking() == []


def test_precheck_detects_ambiguous_owner(fresh_db):
    """同一 account 被第二个真人也 active 绑定 → ambiguous_owner，BLOCK。"""
    _user_id, accounts = _make_user_with_accounts("13800010002", 2)
    other = db.create_or_get_platform_user_by_phone(
        phone="13800010099", display_name="第二真人"
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO account_owner_bindings(
                platform_user_id, account_id, binding_method, status, updated_at
            ) VALUES (?, ?, 'test', 'active',
                      strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (other["id"], accounts[0]),
        )

    with db.connect() as conn:
        report = run_precheck(conn)

    assert "ambiguous_owner" in _blocking_names(report)


def test_precheck_detects_orphan_wallet(fresh_db):
    """active 钱包对应 account 无 active binding → orphan_wallet，BLOCK。"""
    _user_id, accounts = _make_user_with_accounts("13800010003", 1)
    with db.connect() as conn:
        # 唯一的 owner binding 置为非 active，钱包变孤儿。
        conn.execute(
            "UPDATE account_owner_bindings SET status='inactive' WHERE account_id=?",
            (accounts[0],),
        )

    with db.connect() as conn:
        report = run_precheck(conn)

    assert "orphan_wallet" in _blocking_names(report)


def test_precheck_detects_owner_drift(fresh_db):
    """钱包 platform_user_id 与唯一 active 归属人不一致 → owner_drift，BLOCK。"""
    _user_id, accounts = _make_user_with_accounts("13800010004", 1)
    other = db.create_or_get_platform_user_by_phone(
        phone="13800010098", display_name="漂移真人"
    )
    with db.connect() as conn:
        # 把唯一 active binding 的归属人改成 other，但钱包 platform_user_id 仍是原真人。
        conn.execute(
            "UPDATE account_owner_bindings SET platform_user_id=? WHERE account_id=?",
            (other["id"], accounts[0]),
        )

    with db.connect() as conn:
        report = run_precheck(conn)

    assert "owner_drift" in _blocking_names(report)


def test_precheck_detects_primary_undefined(fresh_db):
    """多钱包用户最早 active binding 对应 account 无 active 钱包 → primary_undefined，BLOCK。"""
    user_id, accounts = _make_user_with_accounts("13800010005", 3)
    with db.connect() as conn:
        # 迁移前形态：为 a2/a3 各补一个 active 钱包（连同 a1 的共 3 active）。
        _seed_extra_active_wallet(
            conn, account_id=accounts[1], platform_user_id=user_id, with_grant=False
        )
        _seed_extra_active_wallet(
            conn, account_id=accounts[2], platform_user_id=user_id, with_grant=False
        )
        # 最早创建的 account（accounts[0]）的钱包置 merged：仍多钱包(a2/a3 两 active)，
        # 但冻结的「选主=最早 active binding 对应钱包」取不到主。
        conn.execute(
            "UPDATE entitlement_wallets SET status='merged' WHERE account_id=?",
            (accounts[0],),
        )

    with db.connect() as conn:
        report = run_precheck(conn)

    # a1 钱包非 active → 剩 a2/a3 两个 active 钱包，仍是多钱包用户。
    assert len(report.multi_wallet_users) == 1
    assert report.multi_wallet_users[0]["wallet_count"] == 2
    assert "primary_undefined" in _blocking_names(report)
