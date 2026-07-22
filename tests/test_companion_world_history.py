"""C1 App conversation 历史 helper：跨日读取且严格隔离 scope/runtime account。"""
import json

import app.db as db
from app.db import APP_ACTIVE_SESSION_KEY, WEB_ACTIVE_SESSION_KEY
from app.domains.companion_world import (
    CompanionWorldService,
    ResidentSelection,
)
from app.platform import SqlCompanionWorldRepository


def _resident_accounts() -> tuple[str, str]:
    user_id = db.create_or_get_platform_user_by_phone(
        phone="19920002001", display_name="用户"
    )["id"]
    for rank in range(1, 5):
        db.create_character_template(
            source_type="operations",
            name=f"角色{rank}",
            avatar_ref=f"asset://{rank}",
            summary=f"简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),
            persona_seed_json=json.dumps(
                {"SOUL.md": f"soul-{rank}", "IDENTITY.md": f"identity-{rank}"}
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )
    service = CompanionWorldService(SqlCompanionWorldRepository())
    boot = service.bootstrap_home(user_id)
    residents = service.confirm_residents(
        user_id,
        [
            ResidentSelection(boot.candidates[0].template.id),
            ResidentSelection(boot.candidates[1].template.id),
        ],
    )
    return residents[0].runtime_account_id, residents[1].runtime_account_id


def _message(account_id: str, session_id: int, key: str, text: str) -> None:
    db.insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=key,
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=text,
    )


def test_app_history_crosses_archived_days_without_scope_or_resident_leakage(fresh_db):
    account_a, account_b = _resident_accounts()
    old_app = db.get_or_create_session(
        account_id=account_a,
        channel="native",
        sender_id="u",
        sender_name=None,
        chat_id=None,
        session_key=APP_ACTIVE_SESSION_KEY,
        business_day="2026-07-20",
    )["session"]
    _message(account_a, int(old_app["id"]), "old-app", "A 的 App 旧日消息")
    db.close_session(
        session_id=int(old_app["id"]),
        close_reason="daily_dreaming",
        archived_session_key=f"{APP_ACTIVE_SESSION_KEY}:{old_app['id']}",
    )
    new_app = db.get_or_create_session(
        account_id=account_a,
        channel="native",
        sender_id="u",
        sender_name=None,
        chat_id=None,
        session_key=APP_ACTIVE_SESSION_KEY,
        business_day="2026-07-21",
    )["session"]
    _message(account_a, int(new_app["id"]), "new-app", "A 的 App 今日消息")

    web = db.get_or_create_session(
        account_id=account_a,
        channel="web",
        sender_id="u",
        sender_name=None,
        chat_id=None,
        session_key=WEB_ACTIVE_SESSION_KEY,
        business_day="2026-07-21",
    )["session"]
    _message(account_a, int(web["id"]), "web", "A 的 Web 消息")
    other_app = db.get_or_create_session(
        account_id=account_b,
        channel="native",
        sender_id="u",
        sender_name=None,
        chat_id=None,
        session_key=APP_ACTIVE_SESSION_KEY,
        business_day="2026-07-21",
    )["session"]
    _message(account_b, int(other_app["id"]), "other", "B 的 App 消息")

    history = db.list_app_conversation_messages_before(runtime_account_id=account_a)
    assert [item["content"] for item in history] == [
        "A 的 App 旧日消息",
        "A 的 App 今日消息",
    ]
    assert all("Web" not in item["content"] and "B 的" not in item["content"] for item in history)

    page = db.list_app_conversation_messages_before(
        runtime_account_id=account_a,
        before_id=int(history[-1]["id"]),
        limit=1,
    )
    assert [item["content"] for item in page] == ["A 的 App 旧日消息"]
