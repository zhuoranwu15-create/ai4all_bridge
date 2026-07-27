"""聊天中的稍后项捕获必须持久化、幂等且按用户隔离。"""
from types import SimpleNamespace

import app.db as db
from app.bootstrap.product_registry import NOOKI_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.products.nooki.infrastructure.repositories.later_items import NookiLaterItemRepository
from app.products.nooki.tools.registry import NOOKI_TOOL_POLICY
from app.tools.executor import execute_tool_call


def _context(phone: str, message_id: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id=NOOKI_APP_ID, registry=PRODUCTION_PRODUCT_REGISTRY
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="Nooki 测试用户",
        app_id=NOOKI_APP_ID,
        registry=PRODUCTION_PRODUCT_REGISTRY,
    )["account"]
    return user, SimpleNamespace(
        account_id=account["id"],
        app_id=NOOKI_APP_ID,
        message_id=message_id,
        tool_policy=NOOKI_TOOL_POLICY,
    )


def test_capture_later_item_is_idempotent_for_one_message(fresh_db):
    user, ctx = _context("13800039001", "msg-capture-1")

    first = execute_tool_call("nooki_capture_later_item", {"content": "周末整理书桌"}, ctx)
    repeated = execute_tool_call("nooki_capture_later_item", {"content": "周末整理书桌"}, ctx)

    assert first["status"] == "ok"
    assert first["metadata"]["deduplicated"] is False
    assert repeated["item"]["later_item_id"] == first["item"]["later_item_id"]
    assert repeated["metadata"]["deduplicated"] is True
    assert NookiLaterItemRepository().list_items(platform_user_id=user["id"]) == [first["item"]]


def test_capture_later_item_never_leaks_to_another_user(fresh_db):
    _, first_ctx = _context("13800039002", "msg-capture-shared")
    second_user, second_ctx = _context("13800039003", "msg-capture-shared")

    execute_tool_call("nooki_capture_later_item", {"content": "只属于第一个人"}, first_ctx)
    second = execute_tool_call("nooki_capture_later_item", {"content": "只属于第二个人"}, second_ctx)

    assert second["status"] == "ok"
    assert NookiLaterItemRepository().list_items(platform_user_id=second_user["id"]) == [
        second["item"]
    ]


def test_capture_later_item_rejects_blank_content(fresh_db):
    _, ctx = _context("13800039004", "msg-capture-blank")

    result = execute_tool_call("nooki_capture_later_item", {"content": "   "}, ctx)

    assert result == {"status": "failed", "error": "later_item_content_invalid"}
