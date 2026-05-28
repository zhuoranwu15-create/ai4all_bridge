import json
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch


TODAY = "2026-05-18"
YESTERDAY = "2026-05-17"
ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _write_daily(base: Path, account_id: str, date_str: str, content: str) -> Path:
    from app.user_profiles import _safe_account_dir_name

    path = (
        base
        / "profiles"
        / _safe_account_dir_name(account_id)
        / "memory"
        / f"{date_str}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _llm_payload(*, items: Optional[List[dict]] = None) -> str:
    return json.dumps(
        {
            "session_summary": {
                "rough_summary": "用户最近在推进 AI 陪伴产品，偏好简洁回复。",
                "carryover_summary": "继续承接 AI 陪伴产品和简洁回复偏好。",
                "open_threads": [{"text": "AI 陪伴产品", "priority": "high"}],
                "tone_notes": "简洁、具体",
            },
            "long_term_memory_items": items or [],
            "excluded_sensitive_items": [],
        },
        ensure_ascii=False,
    )


def test_list_recent_daily_memory_oldest_to_newest(fresh_db, tmp_path):
    from app.dreaming import list_recent_daily_memory

    _write_daily(tmp_path, "acc", TODAY, "# 2026-05-18\n\n- 今天")
    _write_daily(tmp_path, "acc", YESTERDAY, "# 2026-05-17\n\n- 昨天")

    records = list_recent_daily_memory(account_id="acc", today=TODAY, days=2)

    assert [record["date"] for record in records] == [YESTERDAY, TODAY]
    assert "昨天" in records[0]["content"]
    assert "今天" in records[1]["content"]


def test_run_dreaming_writes_run_items_events_and_applies_memory(fresh_db, tmp_path):
    from app.dreaming import read_long_term_memory, run_dreaming
    from app.db import list_dreaming_memory_items, list_dreaming_runs, list_memory_events

    fresh_db.llm_api_key = "fake-key"
    account_id = "acc-dream-apply"
    _write_daily(tmp_path, account_id, TODAY, "# 2026-05-18\n\n- 用户是算法工程师")
    payload = _llm_payload(
        items=[
            {
                "operation": "add",
                "target_file": "MEMORY.md",
                "category": "profile",
                "memory_text": "用户是算法工程师。",
                "importance": "high",
                "confidence": 0.91,
                "sensitivity": "normal",
                "reason": "稳定身份信息",
                "source_daily_note_dates": [TODAY],
            }
        ]
    )

    with patch("app.llm.generate_completion", return_value=payload):
        result = run_dreaming(account_id=account_id, today=TODAY, days=1)

    assert result["status"] == "updated"
    assert result["applied_count"] == 1
    assert "算法工程师" in read_long_term_memory(account_id)

    runs = list_dreaming_runs(account_id=account_id)
    assert len(runs) == 1
    assert runs[0]["prompt_version"] == "dreaming_v1"
    assert runs[0]["status"] == "succeeded"

    items = list_dreaming_memory_items(account_id=account_id)
    assert len(items) == 1
    assert items[0]["apply_status"] == "applied"
    assert items[0]["source_daily_note_date"] == TODAY

    events = list_memory_events(memory_item_id=items[0]["id"])
    assert {event["event_type"] for event in events} == {"generated", "applied"}


def test_run_dreaming_skips_sensitive_and_low_confidence_items(fresh_db, tmp_path):
    from app.dreaming import read_long_term_memory, run_dreaming
    from app.db import list_dreaming_memory_items

    fresh_db.llm_api_key = "fake-key"
    account_id = "acc-dream-skip"
    _write_daily(tmp_path, account_id, TODAY, "# 2026-05-18\n\n- 用户提到银行卡")
    payload = _llm_payload(
        items=[
            {
                "operation": "add",
                "target_file": "MEMORY.md",
                "category": "finance",
                "memory_text": "用户的银行卡信息需要留意。",
                "importance": "high",
                "confidence": 0.95,
                "sensitivity": "sensitive",
                "reason": "金融敏感信息",
            },
            {
                "operation": "add",
                "target_file": "MEMORY.md",
                "category": "other",
                "memory_text": "用户可能喜欢深夜聊天。",
                "importance": "medium",
                "confidence": 0.4,
                "sensitivity": "normal",
                "reason": "证据不足",
            },
        ]
    )

    with patch("app.llm.generate_completion", return_value=payload):
        result = run_dreaming(account_id=account_id, today=TODAY, days=1)

    assert result["applied_count"] == 0
    assert result["skipped_count"] == 2
    assert "银行卡" not in read_long_term_memory(account_id)
    items = list_dreaming_memory_items(account_id=account_id)
    assert {item["skip_reason"] for item in items} == {
        "sensitive_item",
        "low_confidence",
    }


def test_rollback_applied_memory_item_restores_previous_file(fresh_db, tmp_path):
    from app.dreaming import read_long_term_memory, rollback_memory_item, run_dreaming
    from app.db import list_dreaming_memory_items, list_memory_events

    fresh_db.llm_api_key = "fake-key"
    account_id = "acc-dream-rollback"
    _write_daily(tmp_path, account_id, TODAY, "# 2026-05-18\n\n- 用户喜欢简洁")
    payload = _llm_payload(
        items=[
            {
                "operation": "add",
                "target_file": "MEMORY.md",
                "category": "preference",
                "memory_text": "用户喜欢简洁回复。",
                "importance": "high",
                "confidence": 0.9,
                "sensitivity": "normal",
                "reason": "明确偏好",
            }
        ]
    )

    with patch("app.llm.generate_completion", return_value=payload):
        run_dreaming(account_id=account_id, today=TODAY, days=1)

    item = list_dreaming_memory_items(account_id=account_id)[0]
    assert "简洁回复" in read_long_term_memory(account_id)

    result = rollback_memory_item(item_id=item["id"], actor_id="test")

    assert result["status"] == "rolled_back"
    assert "简洁回复" not in read_long_term_memory(account_id)
    events = list_memory_events(memory_item_id=item["id"])
    assert "rollback" in {event["event_type"] for event in events}


def test_session_lifecycle_uses_llm_carryover_when_available(fresh_db, tmp_path):
    from app.db import insert_message, list_sessions_for_account
    from app.session_lifecycle import get_or_create_account_active_session_with_dreaming

    fresh_db.llm_api_key = "fake-key"
    account_id = "acc-life-llm"
    first = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        business_day="2026-05-17",
        max_turns=500,
    )["session"]
    insert_message(
        account_id=account_id,
        session_id=int(first["id"]),
        message_id="life-msg-1",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="昨天的上下文",
    )

    with patch("app.llm.generate_completion", return_value=_llm_payload()):
        second = get_or_create_account_active_session_with_dreaming(
            account_id=account_id,
            channel="openclaw-weixin",
            sender_id="sender",
            sender_name=None,
            chat_id="chat",
            business_day="2026-05-18",
            max_turns=500,
        )["session"]

    assert second["id"] != first["id"]
    assert "继续承接" in second["carryover_summary"]
    closed = next(item for item in list_sessions_for_account(account_id=account_id) if item["id"] == first["id"])
    assert closed["summary_model"] == "test-model"
    assert closed["summary_prompt_version"] == "dreaming_v1"


def test_session_lifecycle_falls_back_when_llm_fails(fresh_db, tmp_path):
    from app.db import insert_message
    from app.session_lifecycle import get_or_create_account_active_session_with_dreaming

    account_id = "acc-life-fallback"
    first = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        business_day="2026-05-17",
        max_turns=500,
    )["session"]
    insert_message(
        account_id=account_id,
        session_id=int(first["id"]),
        message_id="life-msg-fallback",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="fallback 上下文",
    )

    second = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        business_day="2026-05-18",
        max_turns=500,
    )["session"]

    assert second["id"] != first["id"]
    assert "fallback 上下文" in second["carryover_summary"]


def test_admin_dreaming_endpoint_and_debug_redaction(client, fresh_db, tmp_path):
    from app.user_profiles import _safe_account_dir_name

    fresh_db.llm_api_key = "fake-key"
    account_id = "sk-admin-dream"
    memory_dir = (
        tmp_path
        / "profiles"
        / _safe_account_dir_name(account_id)
        / "memory"
    )
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{TODAY}.md").write_text("# 2026-05-18\n\n- 用户手机号 13800138000", encoding="utf-8")

    payload = _llm_payload(
        items=[
            {
                "operation": "add",
                "target_file": "MEMORY.md",
                "category": "preference",
                "memory_text": "用户手机号 13800138000，不应明文展示。",
                "importance": "high",
                "confidence": 0.9,
                "sensitivity": "sensitive",
                "reason": "测试脱敏",
            }
        ]
    )

    res = client.post(
        "/openclaw/turn",
        json={
            "account_id": "acc-admin-dream",
            "session_key": account_id,
            "sender_id": "sender-test",
            "chat_type": "private",
            "message_type": "text",
            "message_id": "m-admin-dream",
            "text": "hello",
        },
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    with patch("app.main.date_cls") as mock_date, patch("app.llm.generate_completion", return_value=payload):
        mock_date.today.return_value.isoformat.return_value = TODAY
        res = client.post(
            f"/admin/accounts/{account_id}/dreaming?days=1",
            headers=ADMIN_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["skipped_count"] == 1

    res = client.get(f"/admin/accounts/{account_id}/dreaming", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    debug = res.json()
    assert debug["redacted"] is True
    assert "13800138000" not in json.dumps(debug, ensure_ascii=False)
    assert "[redacted-phone]" in json.dumps(debug, ensure_ascii=False)
