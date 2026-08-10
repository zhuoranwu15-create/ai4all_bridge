ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def test_proactive_debug_static_page_is_local_only(client, fresh_db):
    fresh_db.app_env = "production"
    res = client.get("/ui/proactive_debug.html")
    assert res.status_code == 403

    fresh_db.app_env = "local"
    res = client.get("/ui/proactive_debug.html")
    assert res.status_code == 200
    assert "Proactive Debug Panel" in res.text
    assert "/admin/proactive/scheduler/run-once" in res.text
    assert "/proactive-check/run-once" in res.text
