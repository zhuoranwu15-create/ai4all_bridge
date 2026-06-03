from unittest.mock import patch


class _FakeResponse:
    def raise_for_status(self):
        return None


def test_faq_message_publishes_when_moderation_is_safe(client):
    with patch(
        "app.main.generate_completion",
        return_value='{"safe": true, "categories": [], "reason": "ok"}',
    ):
        res = client.post(
            "/web/faq/messages",
            json={"author_name": "Alice", "content": "我想了解怎么绑定微信。"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["message_status"] == "published"
    assert body["message"]["author_name"] == "Alice"

    list_res = client.get("/web/faq/messages")
    assert list_res.status_code == 200
    messages = list_res.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "我想了解怎么绑定微信。"


def test_faq_message_sends_website_webhook_when_submitted(client, fresh_db):
    fresh_db.feishu_website_webhook_url = "https://example.test/website-hook"
    with patch(
        "app.main.generate_completion",
        return_value='{"safe": true, "categories": [], "reason": "ok"}',
    ), patch("app.main.httpx.post", return_value=_FakeResponse()) as post:
        res = client.post(
            "/web/faq/messages",
            json={"author_name": "Alice", "content": "我想了解怎么绑定微信。"},
        )

    assert res.status_code == 200
    assert res.json()["message_status"] == "published"
    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["msg_type"] == "text"
    text = post.call_args.kwargs["json"]["content"]["text"]
    assert "FAQ 用户发表留言" in text
    assert "我想了解怎么绑定微信。" in text


def test_faq_message_stays_pending_when_moderation_flags_risk(client):
    with patch(
        "app.main.generate_completion",
        return_value='{"safe": false, "categories": ["spam"], "reason": "ad"}',
    ):
        res = client.post(
            "/web/faq/messages",
            json={"author_name": "Bob", "content": "风险内容示例"},
        )

    assert res.status_code == 200
    assert res.json()["message_status"] == "pending"

    list_res = client.get("/web/faq/messages")
    assert list_res.status_code == 200
    assert list_res.json()["messages"] == []


def test_faq_pending_message_sends_review_webhook(client, fresh_db):
    fresh_db.feishu_website_webhook_url = "https://example.test/website-hook"
    with patch(
        "app.main.generate_completion",
        return_value='{"safe": false, "categories": ["spam"], "reason": "ad"}',
    ), patch("app.main.httpx.post", return_value=_FakeResponse()) as post:
        res = client.post(
            "/web/faq/messages",
            json={"author_name": "Bob", "content": "风险内容示例"},
        )

    assert res.status_code == 200
    assert res.json()["message_status"] == "pending"
    assert post.call_count == 2
    texts = [
        call.kwargs["json"]["content"]["text"]
        for call in post.call_args_list
    ]
    assert any("FAQ 用户发表留言" in text for text in texts)
    assert any("FAQ 留言需要审核待处理" in text for text in texts)


def test_faq_replies_are_one_level_and_likes_increment(client):
    with patch(
        "app.main.generate_completion",
        return_value='{"safe": true, "categories": [], "reason": "ok"}',
    ):
        parent_res = client.post(
            "/web/faq/messages",
            json={"author_name": "Alice", "content": "主贴"},
        )
        parent_id = parent_res.json()["message"]["id"]

        reply_res = client.post(
            f"/web/faq/messages/{parent_id}/replies",
            json={"author_name": "Carol", "content": "主贴回复"},
        )
        reply_id = reply_res.json()["message"]["id"]

        nested_res = client.post(
            f"/web/faq/messages/{reply_id}/replies",
            json={"author_name": "Dave", "content": "不允许的二级回复"},
        )

    assert reply_res.status_code == 200
    assert reply_res.json()["message_status"] == "published"
    assert nested_res.status_code == 400

    like_res = client.post(
        f"/web/faq/messages/{parent_id}/like",
        json={"voter_token": "voter-a"},
    )
    assert like_res.status_code == 200
    assert like_res.json()["liked"] is True
    assert like_res.json()["message"]["like_count"] == 1

    duplicate_like_res = client.post(
        f"/web/faq/messages/{parent_id}/like",
        json={"voter_token": "voter-a"},
    )
    assert duplicate_like_res.status_code == 200
    assert duplicate_like_res.json()["liked"] is False
    assert duplicate_like_res.json()["message"]["like_count"] == 1

    second_voter_like_res = client.post(
        f"/web/faq/messages/{parent_id}/like",
        json={"voter_token": "voter-b"},
    )
    assert second_voter_like_res.status_code == 200
    assert second_voter_like_res.json()["liked"] is True
    assert second_voter_like_res.json()["message"]["like_count"] == 2

    list_res = client.get("/web/faq/messages")
    message = list_res.json()["messages"][0]
    assert message["like_count"] == 2
    assert message["reply_count"] == 1
    assert message["replies"][0]["content"] == "主贴回复"
