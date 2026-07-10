"""Onboarding debug 后台 + campaign_code 模拟入口测试（/debug/accounts/create）。

覆盖：不带活码时行为不变；带有效活码时写入 account_campaign_attribution 快照并应用
强制 SOUL 人设，但不应 increment_campaign_code_used（调试流量不应污染活码转化统计）；
带无效/不存在活码时不阻断账号创建（fail-open，与生产 create_ai4all_account_for_user 一致）。
"""
from app.db import get_account_onboarding_state, get_campaign_attribution
from app.db.campaign import create_campaign_code, get_campaign_code
from app.user_profiles import read_context_file

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def test_create_debug_account_without_campaign_code_is_unaffected(client, fresh_db):
    res = client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-no-camp"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["account_id"] == "dbg-no-camp"
    assert body["campaign_attribution"] is None
    assert get_campaign_attribution(account_id="dbg-no-camp") is None
    assert get_account_onboarding_state(account_id="dbg-no-camp") == "pending"


def test_create_debug_account_with_valid_campaign_code_applies_attribution(client, fresh_db):
    create_campaign_code(
        code="DBGCAMP1",
        campaign_key="debug-panel-test",
        mission_id="mission_001",
        soul_preset_key="xiaotaiyang",
    )

    res = client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-with-camp", "campaign_code": "DBGCAMP1"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    attribution = body["campaign_attribution"]
    assert attribution["applied"] is True
    assert attribution["campaign_code"] == "DBGCAMP1"
    assert attribution["soul_preset_key"] == "xiaotaiyang"
    assert attribution["mission_id"] == "mission_001"

    stored = get_campaign_attribution(account_id="dbg-with-camp")
    assert stored is not None
    assert stored["campaign_code"] == "DBGCAMP1"
    assert stored["soul_preset_key"] == "xiaotaiyang"

    # SOUL.md 已按活码强制人设写入
    soul_text = read_context_file("dbg-with-camp", "SOUL.md")
    assert soul_text

    # 调试流量不应 increment 活码 used_count
    assert get_campaign_code(code="DBGCAMP1")["used_count"] == 0


def test_create_debug_account_with_invalid_campaign_code_does_not_block_creation(client, fresh_db):
    res = client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-bad-camp", "campaign_code": "NOSUCHCODE"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "created"
    attribution = body["campaign_attribution"]
    assert attribution["applied"] is False
    assert attribution["reason"] == "not_found"
    assert get_campaign_attribution(account_id="dbg-bad-camp") is None


def test_onboarding_debug_view_exposes_campaign_attribution(client, fresh_db):
    create_campaign_code(
        code="DBGCAMP2",
        campaign_key="debug-panel-test-2",
        onboarding_script_variant="欢迎语变体 A",
        soul_preset_key="xiaoyueya",
    )
    client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-view-camp", "campaign_code": "DBGCAMP2"},
        headers=ADMIN_HEADERS,
    )

    res = client.get("/debug/accounts/dbg-view-camp/onboarding", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["campaign_attribution"]["campaign_code"] == "DBGCAMP2"
    assert body["campaign_attribution"]["soul_preset_key"] == "xiaoyueya"


def test_reset_reapplies_forced_soul_preset(client, fresh_db):
    """Finding B 回归：带活码强制人设的测试账号重置后，SOUL 应按注册快照重新落地，
    而不是退回通用空白模板（否则会与 onboarding "已强制人设" 分支逻辑漂移）。"""
    create_campaign_code(code="DBGCAMP3", campaign_key="a", soul_preset_key="xiaotaiyang")
    client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-reset-camp", "campaign_code": "DBGCAMP3"},
        headers=ADMIN_HEADERS,
    )
    # 建号后即为强制人设 SOUL
    assert "小精灵" in (read_context_file("dbg-reset-camp", "SOUL.md") or "")

    res = client.post(
        "/debug/accounts/dbg-reset-camp/onboarding/reset",
        json={"clear_context_files": True},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["restored_soul_preset"] == "xiaotaiyang"

    # 重置后 SOUL 仍是强制人设，而不是空白模板
    soul_after = read_context_file("dbg-reset-camp", "SOUL.md") or ""
    assert "小精灵" in soul_after
    assert "# SOUL" not in soul_after


def test_reset_without_campaign_code_uses_blank_template(client, fresh_db):
    """无活码账号重置行为不变：SOUL 回退空白模板，restored_soul_preset 为 None。"""
    client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-reset-plain"},
        headers=ADMIN_HEADERS,
    )
    res = client.post(
        "/debug/accounts/dbg-reset-plain/onboarding/reset",
        json={"clear_context_files": True},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["restored_soul_preset"] is None


def test_recreate_with_different_campaign_code_keeps_first_attribution(client, fresh_db):
    """Finding C 回归：同一 account_id 换活码二次建号，归因与 SOUL 都保持首次（写入即定型），
    第二次返回 already_attributed，不覆盖 SOUL。"""
    create_campaign_code(code="DBGFIRST", campaign_key="a", soul_preset_key="xiaotaiyang")
    create_campaign_code(code="DBGSECOND", campaign_key="b", soul_preset_key="ju")

    client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-recreate", "campaign_code": "DBGFIRST"},
        headers=ADMIN_HEADERS,
    )
    res = client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-recreate", "campaign_code": "DBGSECOND"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    attribution = res.json()["campaign_attribution"]
    assert attribution["applied"] is False
    assert attribution["reason"] == "already_attributed"

    # 归因快照仍是第一次的活码，SOUL 未被第二次的 ju 覆盖（仍是 xiaotaiyang 的"小精灵"）
    stored = get_campaign_attribution(account_id="dbg-recreate")
    assert stored["campaign_code"] == "DBGFIRST"
    assert stored["soul_preset_key"] == "xiaotaiyang"
    soul = read_context_file("dbg-recreate", "SOUL.md") or ""
    assert "小精灵" in soul


def test_reset_reapplies_forced_ai_name(client, fresh_db):
    """§4.4 回归：带强制 AI 名字的账号重置后，IDENTITY 应按注册快照重新落地该名字。"""
    create_campaign_code(
        code="DBGAINAME", campaign_key="a", soul_preset_key="xiaotaiyang", ai_name_preset="小满"
    )
    client.post(
        "/debug/accounts/create",
        json={"account_id": "dbg-reset-ainame", "campaign_code": "DBGAINAME"},
        headers=ADMIN_HEADERS,
    )
    assert "小满" in (read_context_file("dbg-reset-ainame", "IDENTITY.md") or "")

    res = client.post(
        "/debug/accounts/dbg-reset-ainame/onboarding/reset",
        json={"clear_context_files": True},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["restored_ai_name"] == "小满"
    assert body["restored_soul_preset"] == "xiaotaiyang"

    # 重置后 IDENTITY 仍是强制名字，SOUL 自称也带上该名字
    assert "小满" in (read_context_file("dbg-reset-ainame", "IDENTITY.md") or "")
    assert "小满" in (read_context_file("dbg-reset-ainame", "SOUL.md") or "")
