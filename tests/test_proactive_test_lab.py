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
        "user_context": "用户最近提到明天面试，有明显时间点。",
        "memory_evidence": "记忆：用户希望提醒轻一点。",
        "open_loop": "面试结束后可以自然问一句感受。",
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


def test_proactive_test_create_sample_from_session_uses_long_account_scoped_context(client):
    from app.db import get_or_create_session, insert_message

    session = get_or_create_session(
        account_id="acct_long_context",
        channel="openclaw-weixin",
        sender_id="wxid_a",
        sender_name="A",
        chat_id=None,
        session_key="openclaw-weixin:acct_long_context:wxid_a",
    )["session"]
    other = get_or_create_session(
        account_id="acct_other",
        channel="openclaw-weixin",
        sender_id="wxid_b",
        sender_name="B",
        chat_id=None,
        session_key="openclaw-weixin:acct_other:wxid_b",
    )["session"]
    for idx in range(12):
        insert_message(
            account_id="acct_long_context",
            session_id=session["id"],
            message_id=f"m_{idx}",
            reply_to_message_id=None,
            direction="inbound" if idx % 2 == 0 else "outbound",
            role="user" if idx % 2 == 0 else "assistant",
            message_type="text",
            content=f"长上下文消息 {idx}",
        )
    insert_message(
        account_id="acct_other",
        session_id=other["id"],
        message_id="other_1",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="别的账号消息",
    )

    res = client.post(
        "/internal/proactive-test/samples/from-session",
        headers=ADMIN_HEADERS,
        json={
            "account_id": "acct_long_context",
            "session_id": session["id"],
            "scenario_type": "reactivation_topic",
            "context_limit": 10,
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["message_count"] == 10
    sample = data["sample"]
    assert sample["account_id"] == "acct_long_context"
    assert sample["session_id"] == session["id"]
    assert sample["context_source"] == f"session:{session['id']}"
    assert len(sample["chat_history"]) == 10
    assert sample["chat_history"][0]["text"] == "长上下文消息 2"
    assert all("别的账号消息" not in item["text"] for item in sample["chat_history"])

    mismatch = client.post(
        "/internal/proactive-test/samples/from-session",
        headers=ADMIN_HEADERS,
        json={
            "account_id": "acct_long_context",
            "session_id": other["id"],
            "scenario_type": "reactivation_topic",
        },
    )
    assert mismatch.status_code == 404


def test_proactive_test_generate_review_stats_and_no_outbound(client):
    from app.db import list_outbound_messages

    client.post(
        "/internal/proactive-test/samples/import",
        headers=ADMIN_HEADERS,
        json={"samples": [_sample()]},
    )
    with patch(
        "app.proactive.test_lab.generate_completion",
        return_value='{"l0_context":{"recent_session":"用户说明天面试","long_term_memory":"喜欢轻提醒","relationship":"朋友感","open_loops":"面试后反馈","user_energy_state":"紧张","user_proactivity_preference":"低打扰"},"l1_trigger":{"trigger_type":"explicit_open_loop","evidence":"明确未来事件"},"l2_when":{"should_intervene":2,"timing_risk":"低","false_alarm_risk":"低","user_autonomy_risk":"低"},"l3_how":{"target_planning":"轻量关心面试结果","dialogue_guidance":"短句邀请分享","tone":{"friend":"高","system":"低","marketing":"无","pushy":"低"},"grounding":"引用面试上下文","candidate_message":"面试后来和我说说感觉？"},"l4_safety":{"sensitive_topic":false,"wrong_memory":false,"over_triggering":false,"too_intimate":false,"too_pushy":false,"manipulation_risk":false,"risk_tags":["无"],"notes":"安全"},"trigger_type":"task_followup","candidate_message":"面试后来和我说说感觉？","should_send_score":2,"when_reason":"用户提到明确未来事件","content_quality":"短、轻、朋友式","risk_tag":"无","should_send":true,"generated_type":"account_check","text":"面试后来和我说说感觉？","reason":"用户提到明确未来事件","confidence":0.91}',
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
    candidate = data["candidates"][0]
    candidate_id = candidate["id"]
    assert candidate["trigger_type"] == "task_followup"
    assert candidate["candidate_message"] == "面试后来和我说说感觉？"
    assert candidate["should_send_score"] == 2
    assert candidate["risk_tag"] == "无"
    assert candidate["l0_context"]["recent_session"] == "用户说明天面试"
    assert candidate["l1_trigger"]["trigger_type"] == "explicit_open_loop"
    assert candidate["l2_when"]["should_intervene"] == 2
    assert candidate["l3_how"]["target_planning"] == "轻量关心面试结果"
    assert candidate["l4_safety"]["wrong_memory"] is False
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
            "revised_message": "面试结束后想听你讲讲感觉。",
            "final_label": "CD",
            "promote_level": "fewshot",
            "user_response": "accepted",
            "l5_outcome": {
                "user_response": "accepted",
                "future_usefulness": "可沉淀 fewshot",
                "final_label": "CD",
                "promote_level": "fewshot",
            },
            "review_notes": "像在催复盘",
        },
    )
    assert review.status_code == 200
    review_data = review.json()["review"]
    assert review_data["revised_message"] == "面试结束后想听你讲讲感觉。"
    assert review_data["final_label"] == "CD"
    assert review_data["promote_level"] == "fewshot"
    assert review_data["user_response"] == "accepted"

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
