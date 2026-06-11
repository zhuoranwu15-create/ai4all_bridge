def _session(account_id: str = "acc-mod"):
    from app.db import get_or_create_session

    state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )
    return state["session"]


def _message(*, account_id: str = "acc-mod", content: str, message_type: str = "text") -> tuple[dict, int]:
    from app.db import insert_message

    session = _session(account_id)
    message_db_id = insert_message(
        account_id=account_id,
        session_id=session["id"],
        message_id=f"msg-{account_id}-{message_type}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type=message_type,
        content=content,
        raw={},
    )
    assert message_db_id is not None
    return session, int(message_db_id)


def test_enqueue_message_records_passed_rule_result(fresh_db):
    from app.db import list_content_moderation_results
    from app.moderation.service import enqueue_message_for_moderation

    session, message_db_id = _message(content="hello, this is a normal long enough message")
    task = enqueue_message_for_moderation(
        message_db_id=message_db_id,
        account_id="acc-mod",
        session_id=session["id"],
        direction="inbound",
        content_kind="text",
        text="hello, this is a normal long enough message",
        source_message_id="msg-normal",
    )
    duplicate = enqueue_message_for_moderation(
        message_db_id=message_db_id,
        account_id="acc-mod",
        session_id=session["id"],
        direction="inbound",
        content_kind="text",
        text="hello, this is a normal long enough message",
        source_message_id="msg-normal",
    )
    results = list_content_moderation_results(task_id=task["id"])

    assert duplicate["id"] == task["id"]
    assert task["account_id"] == "acc-mod"
    assert task["status"] == "machine_passed"
    assert task["risk_level"] == "pass"
    assert task["sample_rate_percent"] == 15
    assert len(results) == 1
    assert results[0]["result_level"] == "pass"


def test_enqueue_message_rule_hit_enters_review_queue(fresh_db):
    from app.db import list_content_moderation_results
    from app.moderation.service import enqueue_message_for_moderation

    session, message_db_id = _message(content="please check MODERATION_TEST_BLOCK")
    task = enqueue_message_for_moderation(
        message_db_id=message_db_id,
        account_id="acc-mod",
        session_id=session["id"],
        direction="inbound",
        content_kind="text",
        text="please check MODERATION_TEST_BLOCK",
        source_message_id="msg-block",
    )
    results = list_content_moderation_results(task_id=task["id"])

    assert task["status"] == "needs_review"
    assert task["risk_level"] == "block"
    assert task["risk_categories"] == ["test_block"]
    assert results[0]["matched_terms"][0]["id"] == "test_block"


def test_enqueue_image_message_keeps_media_reference_without_file_read(fresh_db):
    from app.moderation.service import enqueue_message_for_moderation

    session, message_db_id = _message(
        account_id="acc-image",
        content="[用户发来一张图片：normal image]",
        message_type="image",
    )
    task = enqueue_message_for_moderation(
        message_db_id=message_db_id,
        account_id="acc-image",
        session_id=session["id"],
        direction="inbound",
        content_kind="image",
        text="[用户发来一张图片：normal image]",
        media={"url": "https://example.com/a.jpg", "path": "../../outside.jpg", "format": "image"},
        source_message_id="msg-image",
    )

    assert task["account_id"] == "acc-image"
    assert task["content_kind"] == "image"
    assert task["media"]["url"] == "https://example.com/a.jpg"
    assert task["media"]["path"] == "../../outside.jpg"
