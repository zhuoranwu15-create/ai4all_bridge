"""Admin campaign codes 路由测试（/admin/campaign-codes，campaign_codes_technical_design.md §5.1/§6）。"""
from pathlib import Path

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
STAFF_HEADERS = {"Authorization": "Bearer test-staff"}


def test_admin_ui_does_not_inline_code_into_onclick():
    """回归测试：activity code 一度被拼进单引号 onclick 属性，admin.js 的 esc() 不转义单引号/
    反斜杠会造成 JS 注入（codex P1 发现）。改为 data-code + 事件委托后不应再出现该拼接模式。"""
    html = Path("app/static/campaign_codes_admin.html").read_text(encoding="utf-8")

    assert "onclick=\"toggleStatus(" not in html
    assert "data-code=" in html
    assert "e.target.closest('button[data-code]')" in html


def test_admin_ui_has_edit_form_fields():
    html = Path("app/static/campaign_codes_admin.html").read_text(encoding="utf-8")

    for field_id in ("e-campaign-key", "e-status", "e-mission-id", "e-soul-preset", "e-valid-from", "e-expires-at", "e-script"):
        assert f'id="{field_id}"' in html


def test_admin_can_create_campaign_code(client):
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "618A", "campaign_key": "618种草视频A"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["campaign_code"]["code"] == "618A"
    assert body["campaign_code"]["created_by_admin_user_id"] == "admin"


def test_staff_can_create_campaign_code(client):
    """运营 staff 应能自主建活码，不必每次找 admin（确认决定 5）。"""
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "STAFF1", "campaign_key": "a"},
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["campaign_code"]["created_by_admin_user_id"] == "staff"


def test_create_without_token_is_rejected(client):
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "NOAUTH", "campaign_key": "a"},
    )
    assert res.status_code == 401


def test_create_duplicate_code_returns_409(client):
    client.post(
        "/admin/campaign-codes",
        json={"code": "DUP1", "campaign_key": "a"},
        headers=ADMIN_HEADERS,
    )
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "DUP1", "campaign_key": "b"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 409


def test_create_with_unknown_mission_id_returns_400(client):
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "BADM", "campaign_key": "a", "mission_id": "mission_999"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 400


def test_list_campaign_codes(client):
    client.post(
        "/admin/campaign-codes",
        json={"code": "LIST1", "campaign_key": "a"},
        headers=ADMIN_HEADERS,
    )
    res = client.get("/admin/campaign-codes", headers=STAFF_HEADERS)
    assert res.status_code == 200
    codes = {c["code"] for c in res.json()["campaign_codes"]}
    assert "LIST1" in codes


def test_list_without_token_is_rejected(client):
    res = client.get("/admin/campaign-codes")
    assert res.status_code == 401


def test_update_campaign_code_status(client):
    client.post(
        "/admin/campaign-codes",
        json={"code": "UPD1", "campaign_key": "a"},
        headers=ADMIN_HEADERS,
    )
    res = client.patch(
        "/admin/campaign-codes/UPD1",
        json={"status": "disabled"},
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["campaign_code"]["status"] == "disabled"


def test_update_unknown_code_returns_404(client):
    res = client.patch(
        "/admin/campaign-codes/NOPE",
        json={"status": "disabled"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 404


def test_reviewer_cannot_create_campaign_code(client):
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "REV1", "campaign_key": "a"},
        headers={"Authorization": "Bearer test-reviewer"},
    )
    assert res.status_code == 403


def test_create_with_invalid_status_returns_422(client):
    """status 收窄为 Literal["active","disabled"]，非法值在 FastAPI 校验层即被拒绝（codex P2 修复）。"""
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "BADSTAT1", "campaign_key": "a", "status": "not_a_status"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 422


def test_update_with_invalid_status_returns_422(client):
    client.post(
        "/admin/campaign-codes",
        json={"code": "UPDSTAT1", "campaign_key": "a"},
        headers=ADMIN_HEADERS,
    )
    res = client.patch(
        "/admin/campaign-codes/UPDSTAT1",
        json={"status": "not_a_status"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 422


def test_create_with_invalid_code_charset_returns_400(client):
    """code 限定 URL-safe 短码字符集，堵住管理 UI 把 code 拼进 HTML 属性时的注入面（codex P1 修复）。"""
    res = client.post(
        "/admin/campaign-codes",
        json={"code": "bad code'", "campaign_key": "a"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 400


def test_update_edits_multiple_fields(client):
    """管理 UI 编辑弹窗需要能改 campaign_key/mission/soul/script/有效期，不只是启停（codex P2 修复）。"""
    client.post(
        "/admin/campaign-codes",
        json={"code": "EDITALL", "campaign_key": "old"},
        headers=ADMIN_HEADERS,
    )
    res = client.patch(
        "/admin/campaign-codes/EDITALL",
        json={
            "campaign_key": "new",
            "mission_id": "mission_002",
            "soul_preset_key": "ju",
            "onboarding_script_variant": "hi there",
            "expires_at": "2099-01-01 00:00:00",
        },
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    campaign = res.json()["campaign_code"]
    assert campaign["campaign_key"] == "new"
    assert campaign["mission_id"] == "mission_002"
    assert campaign["soul_preset_key"] == "ju"
    assert campaign["onboarding_script_variant"] == "hi there"
    assert campaign["expires_at"] == "2099-01-01 00:00:00"
