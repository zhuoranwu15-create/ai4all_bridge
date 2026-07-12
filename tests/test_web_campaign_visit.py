"""落地页曝光 beacon 端点测试（POST /web/campaign-visit，
campaign_funnel_analytics_technical_design.md §7.3）。"""
from unittest.mock import patch

from app.db import get_campaign_visit_stats
from app.time_utils import beijing_now

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _create_code(client, code="BEA1"):
    client.post(
        "/admin/campaign-codes",
        json={"code": code, "campaign_key": "k"},
        headers=ADMIN_HEADERS,
    )


def _today():
    return beijing_now().date().isoformat()


def test_beacon_records_visit_for_known_code(client):
    _create_code(client)
    res = client.post(
        "/web/campaign-visit",
        json={"campaign_code": "BEA1", "visitor_token": "v1", "page": "onboarding"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    stats = get_campaign_visit_stats(campaign_code="BEA1", date_from=_today(), date_to=_today())
    assert stats["pv"] == 1 and stats["uv"] == 1


def test_beacon_ignores_unknown_code(client):
    res = client.post("/web/campaign-visit", json={"campaign_code": "NOPE", "visitor_token": "v1"})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert get_campaign_visit_stats(campaign_code="NOPE", date_from=_today(), date_to=_today())["pv"] == 0


def test_beacon_ignores_empty_code(client):
    res = client.post("/web/campaign-visit", json={"campaign_code": "  "})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"


def test_beacon_requires_no_auth(client):
    """落地页公开访问：无 Authorization 头也应放行。"""
    _create_code(client, "BEA2")
    res = client.post("/web/campaign-visit", json={"campaign_code": "BEA2"})
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_beacon_fail_open_on_record_error(client):
    """后端写入异常也必须返回 ok，不阻塞落地页。"""
    _create_code(client, "BEA3")
    with patch("app.routers.web.record_campaign_visit", side_effect=RuntimeError("boom")):
        res = client.post(
            "/web/campaign-visit",
            json={"campaign_code": "BEA3", "visitor_token": "v1"},
        )
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
