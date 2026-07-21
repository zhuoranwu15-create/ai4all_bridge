"""B-② 居民内部建号原语（M1-5）契约测试：create_resident_runtime_account。

冻结账号模型决策 B（docs/tech_design/companion_world_account_model_reconciliation.md）：
居民 runtime account 走「世界归属」解析到真人，**不发 owner_binding、不赠新客贝壳、
不建独立钱包/subscription、不占用户账号容量**。owner_binding 是微信接入独有的产物。
"""
import app.db as db


def test_resident_resolves_via_world_and_has_no_binding(fresh_db):
    u = db.create_or_get_platform_user_by_phone(phone="13800099001", display_name="P")
    uid = u["id"]
    # 形态 A：用户账号（微信 / Web onboarding 入口），发 owner_binding。
    a1 = db.create_ai4all_account_for_user(platform_user_id=uid, display_name="用户账号")["account"]["id"]
    # 形态 B：真人世界里的居民 runtime account（内部路径）。
    uni = db.get_or_create_home_universe(platform_user_id=uid)
    tmpl = db.create_character_template(source_type="official", name="居民甲")
    res = db.create_resident_runtime_account(
        universe_id=uni["id"], character_template_id=tmpl["id"], display_name="居民甲"
    )
    a2 = res["account"]["id"]

    assert a1 != a2
    with db.connect() as conn:
        # 两号都解析到同一真人：a1 经 owner_binding、a2 经世界归属。
        assert db.resolve_owner_platform_user_id(conn, a1) == uid
        assert db.resolve_owner_platform_user_id(conn, a2) == uid
        # 居民不发 owner_binding（否则会撞 #42 的 ux_owner_binding_active_user_app）。
        assert conn.execute(
            "SELECT COUNT(*) c FROM account_owner_bindings WHERE account_id=?", (a2,)
        ).fetchone()["c"] == 0
        # 居民不建独立钱包（钱包锚 platform_user、全世界居民共享一份）。
        assert conn.execute(
            "SELECT COUNT(*) c FROM entitlement_wallets WHERE account_id=?", (a2,)
        ).fetchone()["c"] == 0
    # 容量真相（D-07）：居民计一个 active 位。
    assert db.count_active_residents(universe_id=uni["id"]) == 1


def test_resident_primitive_requires_existing_universe(fresh_db):
    tmpl = db.create_character_template(source_type="official", name="孤儿模板")
    try:
        db.create_resident_runtime_account(
            universe_id="uni_does_not_exist", character_template_id=tmpl["id"], display_name="X"
        )
        assert False, "should raise for missing universe"
    except ValueError as err:
        assert "universe not found" in str(err)
