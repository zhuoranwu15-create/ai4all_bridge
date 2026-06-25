from unittest.mock import patch

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _sample(sample_id="sample_001", scenario_type="account_check"):
    return {
        "sample_id": sample_id,
        "source": "synthetic",
        "scenario_type": scenario_type,
        "chat_history": [
            {"role": "user", "text": "我明天要面试，有点紧张。"},
            {"role": "assistant", "text": "可以先准备一个一分钟自我介绍。"},
        ],
        "silence_hours": 24,
        "notes": "面试后跟进场景",
    }


def test_proactive_test_import_jsonl_and_list(client):
    raw = "\n".join([
        '{"sample_id":"sample_001","source":"synthetic","scenario_type":"account_check","chat_history":[{"role":"user","text":"我明天要面试。"}],"silence_hours":24}',
        '{"sample_id":"sample_002","source":"synthetic","scenario_type":"content_invitation","chat_history":[{"role":"user","text":"我想找 AI agent 入门资料。"}],"silence_hours":48}',
    ])

    res = client.post(
        "/internal/proactive-test/samples/import",
        headers=ADMIN_HEADERS,
        json={"raw_text": raw},
    )

    assert res.status_code == 200
    assert res.json()["imported"] == 2
    listed = client.get(
        "/internal/proactive-test/samples?scenario_type=content_invitation",
        headers=ADMIN_HEADERS,
    ).json()
    assert [item["sample_id"] for item in listed["items"]] == ["sample_002"]


def test_proactive_test_bootstrap_imports_synthetic_samples(client):
    res = client.post(
        "/internal/proactive-test/samples/bootstrap",
        headers=ADMIN_HEADERS,
        json={"limit": 12},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["sample_count"] == 12
    assert data["import_db"]["inserted"] == 12

    listed = client.get(
        "/internal/proactive-test/samples?limit=20",
        headers=ADMIN_HEADERS,
    ).json()
    assert len(listed["items"]) == 12
    assert {item["source"] for item in listed["items"]} == {"synthetic"}


def test_proactive_test_generate_review_stats_and_no_outbound(client):
    from app.db import list_outbound_messages

    client.post(
        "/internal/proactive-test/samples/import",
        headers=ADMIN_HEADERS,
        json={"samples": [_sample()]},
    )
    with patch(
        "app.proactive.test_lab.generate_completion",
        return_value='{"should_send": true, "generated_type": "account_check", "text": "面试后来和我说说感觉？", "reason": "用户提到明确未来事件", "confidence": 0.91}',
    ):
        res = client.post(
            "/internal/proactive-test/generate",
            headers=ADMIN_HEADERS,
            json={"sample_ids": ["sample_001"], "dry_run": True},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["success"] == 1
    assert data["skip"] == 0
    candidate_id = data["candidates"][0]["id"]
    assert list_outbound_messages() == []

    review = client.post(
        "/internal/proactive-test/reviews",
        headers=ADMIN_HEADERS,
        json={
            "candidate_id": candidate_id,
            "human_should_promote": False,
            "reject_reason": "too_pushy",
            "tone_score": 2,
            "pressure_score": 4,
            "marketing_score": 1,
            "privacy_risk": False,
            "hallucination_risk": False,
            "review_notes": "像在催复盘",
        },
    )
    assert review.status_code == 200

    stats = client.get("/internal/proactive-test/stats", headers=ADMIN_HEADERS).json()
    assert stats["total_samples"] == 1
    assert stats["total_candidates"] == 1
    assert stats["generation_success_rate"] == 1.0
    assert stats["human_promote_rate"] == 0.0
    assert stats["reject_reasons"]["too_pushy"] == 1
    assert stats["average_scores"]["pressure_score"] == 4.0


def test_proactive_test_generation_records_skip_and_error(client):
    client.post(
        "/internal/proactive-test/samples/import",
        headers=ADMIN_HEADERS,
        json={"samples": [_sample("sample_skip", "reactivation_topic")]},
    )
    with patch(
        "app.proactive.test_lab.generate_completion",
        return_value='{"should_send": false, "generated_type": "reactivation_topic", "text": "", "reason": "没有自然续聊点", "confidence": 0.2}',
    ):
        res = client.post(
            "/internal/proactive-test/generate",
            headers=ADMIN_HEADERS,
            json={"sample_ids": ["sample_skip", "missing_sample"], "dry_run": True},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["skip"] == 1
    assert data["error"] == 1
    listed = client.get("/internal/proactive-test/candidates", headers=ADMIN_HEADERS).json()
    statuses = {item["sample_id"]: item["generation_status"] for item in listed["items"]}
    assert statuses["sample_skip"] == "skip"
    assert statuses["missing_sample"] == "error"


def test_proactive_test_rejects_non_dry_run_and_bad_review(client):
    client.post(
        "/internal/proactive-test/samples/import",
        headers=ADMIN_HEADERS,
        json={"samples": [_sample()]},
    )

    res = client.post(
        "/internal/proactive-test/generate",
        headers=ADMIN_HEADERS,
        json={"sample_ids": ["sample_001"], "dry_run": False},
    )
    assert res.status_code == 400

    review = client.post(
        "/internal/proactive-test/reviews",
        headers=ADMIN_HEADERS,
        json={
            "candidate_id": "missing",
            "human_should_promote": False,
            "reject_reason": "not_a_reason",
        },
    )
    assert review.status_code == 404


def test_proactive_test_lab_is_non_production_only(client, fresh_db):
    fresh_db.app_env = "production"
    res = client.get("/internal/proactive-test/stats", headers=ADMIN_HEADERS)
    assert res.status_code == 403

    static_res = client.get("/ui/proactive_test_lab.html")
    assert static_res.status_code == 403

    bootstrap_res = client.post(
        "/internal/proactive-test/samples/bootstrap",
        headers=ADMIN_HEADERS,
        json={"limit": 1},
    )
    assert bootstrap_res.status_code == 403

    fresh_db.app_env = "local"
    ok = client.get("/ui/proactive_test_lab.html")
    assert ok.status_code == 200
    assert "Proactive Test Lab" in ok.text
