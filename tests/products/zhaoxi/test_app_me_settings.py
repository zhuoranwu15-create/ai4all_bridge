"""S6「我的」Tab 收尾：Profile（ME-01）、注销申请（ME-06/07）、通知偏好（ME-10）。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.bootstrap.product_registry import MINGCHAN_APP_ID, build_test_product_registry
from app.platform.channels import CHANNEL_APP
from app.products.mingchan.application import AppInboxAdapter
from app.products.mingchan.application.proactive import (
    dispatch_resident_notification,
)
from app.products.mingchan.domain.user_profile import USER_AVATAR_KEYS
from app.products.mingchan.infrastructure import (
    account_deletion_records,
    notification_preferences,
)
from app.products.mingchan.manifest import install_public_routes
from app.time_utils import beijing_naive_now, beijing_now
from tests.factories import make_resident_account


MINGCHAN_API = "/api/v1/products/mingchan"


@pytest.fixture
def client(fresh_db):
    """只安装启用态鸣蝉路由，测试不依赖已废弃的 App legacy namespace。"""

    app = FastAPI()
    install_public_routes(
        app,
        registry=build_test_product_registry(),
        config=fresh_db,
    )
    return TestClient(app)


def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    return db.set_verification_verified(row["id"], token_expires_minutes=10)[
        "verified_token"
    ]


def _login(client, phone: str) -> tuple[dict, dict]:
    response = client.post(
        f"{MINGCHAN_API}/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data


# --- ME-01 Profile --------------------------------------------------------


def test_profile_options_expose_controlled_avatars_and_limits(client):
    headers, _ = _login(client, "13800139000")

    response = client.get(f"{MINGCHAN_API}/me/profile-options", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert [item["key"] for item in data["avatars"]] == list(USER_AVATAR_KEYS)
    assert all(item["avatar_ref"] for item in data["avatars"])
    assert data["limits"]["nickname_chars"] >= data["limits"]["nickname_min_chars"] >= 1
    assert response.headers["Cache-Control"] == "no-store"


def test_profile_update_persists_and_me_reflects_it(client):
    headers, login = _login(client, "13800139001")
    avatar_key = next(iter(USER_AVATAR_KEYS))

    response = client.patch(
        f"{MINGCHAN_API}/me/profile",
        headers=headers,
        json={"display_name": "小满", "avatar_key": avatar_key},
    )

    assert response.status_code == 200, response.text
    updated = response.json()["platform_user"]
    assert updated["display_name"] == "小满"
    assert updated["avatar_key"] == avatar_key
    assert updated["avatar_ref"].endswith(".png")
    # 手机号在任何 Profile 响应里都只出脱敏值。
    assert "13800139001" not in response.text
    assert updated["phone_masked"] == login["platform_user"]["phone_masked"]

    me = client.get(f"{MINGCHAN_API}/me", headers=headers).json()
    assert me["platform_user"]["display_name"] == "小满"
    assert me["platform_user"]["avatar_key"] == avatar_key


def test_profile_update_accepts_single_field_and_rejects_empty_payload(client):
    headers, _ = _login(client, "13800139002")
    avatar_key = next(iter(USER_AVATAR_KEYS))
    client.patch(
        f"{MINGCHAN_API}/me/profile",
        headers=headers,
        json={"display_name": "阿吉", "avatar_key": avatar_key},
    )

    # 只传头像不应清空昵称：None 表示「本次不改」，不是「清空」。
    only_avatar = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"avatar_key": avatar_key}
    )
    assert only_avatar.status_code == 200
    assert only_avatar.json()["platform_user"]["display_name"] == "阿吉"

    empty = client.patch(f"{MINGCHAN_API}/me/profile", headers=headers, json={})
    assert empty.status_code == 422
    assert empty.json()["detail"] == "profile_update_empty"


def test_profile_update_rejects_bad_nickname_and_unknown_avatar_key(client):
    headers, _ = _login(client, "13800139003")

    too_long = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"display_name": "满" * 40}
    )
    assert too_long.status_code == 422

    blank = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"display_name": "   "}
    )
    assert blank.status_code == 422
    assert blank.json()["detail"] == "nickname_length_invalid"

    charset = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"display_name": "小​满"}
    )
    assert charset.status_code == 422
    assert charset.json()["detail"] == "nickname_charset_invalid"

    unknown_avatar = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"avatar_key": "resident_01"}
    )
    assert unknown_avatar.status_code == 422
    assert unknown_avatar.json()["detail"] == "avatar_key_invalid"

    # 未知 key 不得落库。
    assert client.get(f"{MINGCHAN_API}/me", headers=headers).json()["platform_user"][
        "avatar_key"
    ] is None


def test_nickname_hard_reject_and_sanitizer_outage_fail_closed(
    client, monkeypatch
):
    """昵称对外可见，走与自建角色名同一条 D-B 红线；清洗器不可用时 fail closed。"""
    headers, _ = _login(client, "13800139004")

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion",
        lambda *_a, **_k: json.dumps(
            {"verdict": "reject", "categories": ["real_person_replication"]}
        ),
    )
    rejected = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"display_name": "某真人明星"}
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "content_rejected"

    def _boom(*_args, **_kwargs):
        raise TimeoutError("moderation upstream timeout")

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion", _boom
    )
    unavailable = client.patch(
        f"{MINGCHAN_API}/me/profile", headers=headers, json={"display_name": "随便"}
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "content_review_unavailable"

    assert client.get(f"{MINGCHAN_API}/me", headers=headers).json()["platform_user"][
        "display_name"
    ] != "某真人明星"


def test_profile_update_is_scoped_to_the_calling_user(client):
    """账号隔离：改自己的 Profile 不得影响另一个真人。"""
    headers_a, _ = _login(client, "13800139005")
    headers_b, _ = _login(client, "13800139006")
    client.patch(f"{MINGCHAN_API}/me/profile", headers=headers_a, json={"display_name": "甲"})

    client.patch(f"{MINGCHAN_API}/me/profile", headers=headers_b, json={"display_name": "乙"})

    assert (
        client.get(f"{MINGCHAN_API}/me", headers=headers_a).json()["platform_user"]["display_name"]
        == "甲"
    )


# --- ME-06/07 注销：立即清除 -----------------------------------------------


def test_deletion_executes_immediately_and_revokes_session(client):
    headers, login = _login(client, "13800139007")
    user_id = login["platform_user"]["id"]

    response = client.post(
        f"{MINGCHAN_API}/me/account/deletion",
        headers=headers,
        json={"confirm": True, "reason_code": "not_useful"},
    )

    assert response.status_code == 200, response.text
    request = response.json()["request"]
    assert request["status"] == "executed"
    assert request["reason_code"] == "not_useful"
    assert request["executed_at"].endswith("+08:00")
    # 运营字段不进客户端契约。
    assert "purge_stats_json" not in request
    # 注销后旧 token 立即失效，客户端必须回登录页。
    assert client.get(f"{MINGCHAN_API}/me", headers=headers).status_code == 401
    # 手机号不被永久占用：platform_users 行保留，可重新注册成全新用户。
    assert db.get_platform_user(platform_user_id=user_id) is not None


def test_deletion_purges_chat_records_and_memories(client, fresh_db):
    """核心口径（Q14）：注销后聊天原文、账号级记忆与世界级共享记忆都必须消失。"""
    fresh_db.mingchan_p1_enabled = True
    headers, account_id, user_id = _ready_user(client, "13800139008", "小满")
    scope = db.resolve_resident_memory_scope(
        runtime_account_id=account_id,
        expected_app_id=MINGCHAN_APP_ID,
    )
    session = db.get_or_create_session(
        account_id=account_id,
        channel=CHANNEL_APP,
        sender_id=user_id,
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
    )["session"]
    db.insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id="msg-delete-me",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="我怕黑",
    )
    db.append_universe_fact(
        universe_id=scope["universe_id"],
        fact_type="user_preference",
        payload_json='{"memory_text":"用户怕黑","operation":"add"}',
        source_account_id=account_id,
        source_resident_id=scope["resident_id"],
        source_message_id="msg-delete-me",
        occurred_at="2026-07-26T09:00:00+08:00",
    )
    assert db.list_recent_messages_for_account(account_id=account_id, limit=10)
    assert db.read_universe_facts(universe_id=scope["universe_id"])

    client.post(f"{MINGCHAN_API}/me/account/deletion", headers=headers, json={"confirm": True})

    assert db.list_recent_messages_for_account(account_id=account_id, limit=10) == []
    assert db.read_universe_facts(universe_id=scope["universe_id"], status=None) == []
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM ai_conversations WHERE universe_id = ?",
                (scope["universe_id"],),
            ).fetchone()["c"]
            == 0
        )
        # 世界行本身受 UNIQUE(owner_platform_user_id) 约束保留，但回到引导前状态。
        assert (
            conn.execute(
                "SELECT onboarding_state FROM universes WHERE id = ?",
                (scope["universe_id"],),
            ).fetchone()["onboarding_state"]
            == "preparing"
        )
    # 居民全部遣散：重新登录拿到的是干净的新世界。
    assert db.list_residents(
        universe_id=scope["universe_id"], statuses=("active", "offline", "candidate")
    ) == []


def test_deletion_requires_explicit_confirmation(client):
    """不可撤销的破坏性操作不接受空 body：漏传 confirm 必须失败且不动数据。"""
    headers, login = _login(client, "13800139009")

    assert client.post(f"{MINGCHAN_API}/me/account/deletion", headers=headers, json={}).status_code == 422
    not_confirmed = client.post(
        f"{MINGCHAN_API}/me/account/deletion", headers=headers, json={"confirm": False}
    )
    assert not_confirmed.status_code == 422
    assert not_confirmed.json()["detail"] == "deletion_not_confirmed"
    # 未确认时会话仍然有效。
    assert client.get(f"{MINGCHAN_API}/me", headers=headers).status_code == 200
    assert (
        account_deletion_records.get_last_deletion_record(
            platform_user_id=login["platform_user"]["id"]
        )
        is None
    )


def test_deletion_rejects_free_text_reason(client):
    headers, _ = _login(client, "13800139010")

    response = client.post(
        f"{MINGCHAN_API}/me/account/deletion",
        headers=headers,
        json={"confirm": True, "reason_code": "我就是不想用了"},
    )

    assert response.status_code == 422
    # 校验失败不得留下任何清除痕迹。
    assert client.get(f"{MINGCHAN_API}/me", headers=headers).status_code == 200


def test_deletion_is_isolated_across_users(client, fresh_db):
    """账号隔离：注销自己不得动到另一个真人的数据与登录态。"""
    fresh_db.mingchan_p1_enabled = True
    headers_a, account_a, _ = _ready_user(client, "13800139011", "甲居民")
    headers_b, account_b, _ = _ready_user(client, "13800139012", "乙居民")
    scope_b = db.resolve_resident_memory_scope(
        runtime_account_id=account_b,
        expected_app_id=MINGCHAN_APP_ID,
    )
    session_b = db.get_or_create_session(
        account_id=account_b,
        channel=CHANNEL_APP,
        sender_id="sender-b",
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
    )["session"]
    db.insert_message(
        account_id=account_b,
        session_id=int(session_b["id"]),
        message_id="msg-keep-me",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="乙的消息",
    )
    db.append_universe_fact(
        universe_id=scope_b["universe_id"],
        fact_type="user_preference",
        payload_json='{"memory_text":"乙的记忆","operation":"add"}',
        source_account_id=account_b,
        source_resident_id=scope_b["resident_id"],
        source_message_id="msg-keep-me",
        occurred_at="2026-07-26T09:00:00+08:00",
    )

    client.post(f"{MINGCHAN_API}/me/account/deletion", headers=headers_a, json={"confirm": True})

    assert client.get(f"{MINGCHAN_API}/me", headers=headers_b).status_code == 200
    assert [
        row["content"]
        for row in db.list_recent_messages_for_account(account_id=account_b, limit=10)
    ] == ["乙的消息"]
    assert db.read_universe_facts(universe_id=scope_b["universe_id"])
    residents_b = db.list_residents(universe_id=scope_b["universe_id"], statuses=("active",))
    assert [row["runtime_account_id"] for row in residents_b] == [account_b]
    assert account_a != account_b


def _upload_image(client, headers) -> str:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (12, 8), color="red").save(buffer, format="JPEG")
    response = client.post(
        f"{MINGCHAN_API}/media/uploads",
        headers=headers,
        files={"file": ("photo.jpg", buffer.getvalue(), "image/jpeg")},
        data={"kind": "image"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["media_id"]


def _media_file_exists(media_id: str) -> bool:
    from app.platform.media.assets import resolve_media_file
    from app.platform.media.persistence import get_media_asset_unscoped

    asset = get_media_asset_unscoped(media_id=media_id)
    assert asset is not None, f"asset row missing: {media_id}"
    return resolve_media_file(str(asset["storage_path"])).exists()


def test_deletion_purges_owned_media_and_keeps_human_chat_media(
    client, fresh_db, monkeypatch
):
    """媒体清除（口径 B）：AI 会话与未发出的资产连行带文件删；真人会话里自己发的保留。

    已被引用的资产 ``expires_at`` 是 NULL，孤儿回收永远抓不到它们——这条路径不删，
    「注销成功」之后图和语音就永久留在库与磁盘上。
    """
    from app.db._core import _tx
    from app.platform.media.persistence import (
        get_media_asset_unscoped,
        mark_media_assets_referenced,
    )
    from app.products.mingchan.application.visits import (
        CompanionWorldVisitService,
    )

    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_chat_image_enabled = True
    fresh_db.mingchan_human_chat_enabled = True
    for target in (
        "app.products.mingchan.api.media.settings.mingchan_chat_image_enabled",
        "app.products.mingchan.api.human_chat.settings"
        ".mingchan_human_chat_enabled",
    ):
        monkeypatch.setattr(target, True)
    now = beijing_naive_now()

    headers, _account_id, user_id = _ready_user(client, "13800139020", "小满")
    other_headers, _other_account, other_id = _ready_user(client, "13800139021", "访客")

    chat_media = _upload_image(client, headers)      # 已发进 AI 会话
    pending_media = _upload_image(client, headers)   # 传了但没发出去
    other_media = _upload_image(client, other_headers)
    with _tx(None) as conn:
        mark_media_assets_referenced(
            media_ids=[chat_media], owner_platform_user_id=user_id, conn=conn
        )
        mark_media_assets_referenced(
            media_ids=[other_media], owner_platform_user_id=other_id, conn=conn
        )

    # 真人会话：主人给访客发一张图（同一条资产既属于我，也出现在对方的聊天记录里）。
    service = CompanionWorldVisitService()
    invitation = service.create_invite(user_id, now=now)
    visit = service.redeem(other_id, code=invitation["code"], now=now)
    conversation = service.accept(user_id, visit_id=visit["id"], now=now)["conversation"]
    human_media = _upload_image(client, headers)
    sent = client.post(
        f"{MINGCHAN_API}/human-conversations/{conversation['id']}/messages",
        headers=headers,
        json={"client_message_id": "human_media_del_001", "media_ref": human_media},
    )
    assert sent.status_code == 200, sent.text
    assert all(
        _media_file_exists(media_id)
        for media_id in (chat_media, pending_media, other_media, human_media)
    )

    client.post(
        f"{MINGCHAN_API}/me/account/deletion",
        headers=headers,
        json={"confirm": True},
    )

    # AI 会话图与未发出的资产：行与文件都不复存在。
    for media_id in (chat_media, pending_media):
        assert get_media_asset_unscoped(media_id=media_id) is None
    # 真人会话图保留：删了等于在对方的聊天记录里留一张永远加载不出来的图。
    assert get_media_asset_unscoped(media_id=human_media) is not None
    assert _media_file_exists(human_media)
    # 账号隔离：别人的资产一根汗毛都不动。
    assert get_media_asset_unscoped(media_id=other_media) is not None
    assert _media_file_exists(other_media)

    record = account_deletion_records.get_last_deletion_record(
        platform_user_id=user_id
    )
    stats = json.loads(record["purge_stats_json"])
    assert stats["media_assets_deleted"] == 2
    assert stats["media_files_deleted"] == 2
    assert stats["media_assets_kept"] == 1
    assert stats["media_file_errors"] == 0


def test_deletion_purges_feed_posts_and_keeps_other_worlds(client, fresh_db, monkeypatch):
    """Feed 归零：动态、图片挂载与 outbox 一起删，别人世界的动态不受影响。

    与媒体清除必须同一口径——只删图不删动态会留下一批永远加载不出图的空壳动态。
    """
    from app.platform.media.persistence import get_media_asset_unscoped

    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_feed_enabled = True
    fresh_db.mingchan_feed_image_enabled = True
    for module in ("app.products.mingchan.api.media", "app.products.mingchan.api.world"):
        monkeypatch.setattr(f"{module}.settings.mingchan_feed_image_enabled", True)

    headers, account_a, user_a = _ready_user(client, "13800139022", "甲居民")
    other_headers, account_b, _user_b = _ready_user(client, "13800139023", "乙居民")
    universe_a = db.resolve_resident_memory_scope(runtime_account_id=account_a)["universe_id"]
    universe_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)["universe_id"]

    feed_media = _upload_image(client, headers)
    published = client.post(
        f"{MINGCHAN_API}/worlds/home/feed/posts",
        headers=headers,
        json={
            "client_request_id": "deletion-feed-001",
            "text": "带图动态",
            "media_refs": [feed_media],
        },
    )
    assert published.status_code == 201, published.text
    kept = client.post(
        f"{MINGCHAN_API}/worlds/home/feed/posts",
        headers=other_headers,
        json={"client_request_id": "deletion-feed-002", "text": "乙的动态"},
    )
    assert kept.status_code == 201, kept.text
    assert _media_file_exists(feed_media)

    def _counts(universe_id: str) -> dict:
        with db.connect() as conn:
            return {
                "posts": conn.execute(
                    "SELECT COUNT(*) c FROM universe_posts WHERE universe_id = ?",
                    (universe_id,),
                ).fetchone()["c"],
                "post_media": conn.execute(
                    "SELECT COUNT(*) c FROM universe_post_media WHERE post_id IN "
                    "(SELECT id FROM universe_posts WHERE universe_id = ?)",
                    (universe_id,),
                ).fetchone()["c"],
                "outbox": conn.execute(
                    "SELECT COUNT(*) c FROM companion_world_outbox WHERE universe_id = ?",
                    (universe_id,),
                ).fetchone()["c"],
            }

    before = _counts(universe_a)
    assert before["posts"] == 1 and before["post_media"] == 1 and before["outbox"] >= 1

    client.post(
        f"{MINGCHAN_API}/me/account/deletion",
        headers=headers,
        json={"confirm": True},
    )

    assert _counts(universe_a) == {"posts": 0, "post_media": 0, "outbox": 0}
    # 账号隔离：乙的动态与推送派生物完好。
    after_b = _counts(universe_b)
    assert after_b["posts"] == 1 and after_b["outbox"] >= 1
    # 图片资产同批删掉，动态与媒体不会一边留一边删。
    assert get_media_asset_unscoped(media_id=feed_media) is None

    record = account_deletion_records.get_last_deletion_record(
        platform_user_id=user_a
    )
    stats = json.loads(record["purge_stats_json"])
    assert stats["universe_posts_deleted"] == 1
    assert stats["companion_world_outbox_deleted"] == before["outbox"]
    assert stats["media_assets_deleted"] == 1


def test_repeated_deletion_after_reregistration_keeps_full_history(client):
    """注销后可用同一手机号重新注册；第二次注销另起一条流水，不覆盖第一条。"""
    phone = "13800139013"
    headers, _ = _login(client, phone)
    first = client.post(
        f"{MINGCHAN_API}/me/account/deletion", headers=headers, json={"confirm": True}
    ).json()["request"]["request_id"]

    headers_again, _ = _login(client, phone)
    second = client.post(
        f"{MINGCHAN_API}/me/account/deletion", headers=headers_again, json={"confirm": True}
    ).json()["request"]["request_id"]

    assert second != first
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM account_deletion_requests"
            ).fetchone()["c"]
            == 2
        )


# --- ME-10 通知偏好 --------------------------------------------------------


def _ready_user(client, phone: str, name: str):
    headers, login = _login(client, phone)
    user_id = login["platform_user"]["id"]
    account_id = make_resident_account(
        user_id,
        name,
        app_id=MINGCHAN_APP_ID,
    )
    scope = db.resolve_resident_memory_scope(
        runtime_account_id=account_id,
        expected_app_id=MINGCHAN_APP_ID,
    )
    db.set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    return headers, account_id, user_id


def test_notification_preferences_default_and_upsert(client, fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    headers, _ = _login(client, "13800139014")

    default = client.get(f"{MINGCHAN_API}/notifications/preferences", headers=headers)
    assert default.status_code == 200, default.text
    assert default.json()["data"]["quiet_level"] == "standard"
    assert default.json()["data"]["available_levels"] == list(
        notification_preferences.QUIET_LEVELS
    )

    updated = client.patch(
        f"{MINGCHAN_API}/notifications/preferences", headers=headers, json={"quiet_level": "quiet"}
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["quiet_level"] == "quiet"
    # 幂等：重复设置同一值不报错。
    assert (
        client.patch(
            f"{MINGCHAN_API}/notifications/preferences",
            headers=headers,
            json={"quiet_level": "quiet"},
        ).json()["data"]["quiet_level"]
        == "quiet"
    )
    assert client.get(f"{MINGCHAN_API}/notifications/preferences", headers=headers).json()["data"][
        "quiet_level"
    ] == "quiet"


def test_notification_preferences_reject_unknown_level(client, fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    headers, _ = _login(client, "13800139015")

    response = client.patch(
        f"{MINGCHAN_API}/notifications/preferences", headers=headers, json={"quiet_level": "off"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "quiet_level_invalid"


def test_notification_preferences_are_isolated_across_users(client, fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    headers_a, _ = _login(client, "13800139016")
    headers_b, _ = _login(client, "13800139017")

    client.patch(
        f"{MINGCHAN_API}/notifications/preferences", headers=headers_a, json={"quiet_level": "quiet"}
    )

    assert client.get(f"{MINGCHAN_API}/notifications/preferences", headers=headers_b).json()["data"][
        "quiet_level"
    ] == "standard"


def test_quiet_mode_cancels_new_dispatches_but_keeps_existing_inbox(client, fresh_db):
    """安静模式只压制**将来**的投递；已在箱内的通知不回收。

    走鸣蝉产品级主动投递入口，而不是直接调用底层 ``deliver()``；偏好门控必须在
    创建通知行之前返回 ``cancelled``。
    """
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    # 本用例断言的是安静模式，不是频次策略；放开分类日上限以免撞上 daily_limit。
    fresh_db.companion_followup_daily_limit = 5
    headers, account_id, user_id = _ready_user(client, "13800139018", "小满")
    # 固定到当天白天，避免 CI 在北京时间 22:00–08:00 运行时先被全局 quiet hours 拦截。
    dispatch_now = beijing_naive_now().replace(hour=12, minute=0, second=0, microsecond=0)

    def _dispatch(key: str):
        return dispatch_resident_notification(
            runtime_account_id=account_id,
            source="commitment",
            text=f"通知 {key}",
            idempotency_key=f"resident-obligation:v1:commitment:{key}",
            now=dispatch_now,
            product_category="companion_followup",
            source_id=key,
            target_id=f"conv-{key}",
        )

    assert _dispatch("before")["status"] == "sent"
    assert client.get(f"{MINGCHAN_API}/notifications", headers=headers).json()["data"][
        "unread_count"
    ] == 1

    client.patch(
        f"{MINGCHAN_API}/notifications/preferences", headers=headers, json={"quiet_level": "quiet"}
    )
    assert AppInboxAdapter().can_deliver(account_id) is False
    suppressed = _dispatch("after")
    assert suppressed["status"] == "cancelled"
    assert suppressed["error"] == "app_inbox_quiet_hours_preference"

    listed = client.get(f"{MINGCHAN_API}/notifications", headers=headers).json()["data"]
    assert listed["unread_count"] == 1
    assert [item["body"]["text"] for item in listed["items"]] == ["通知 before"]

    # 关掉安静模式后恢复投递。
    client.patch(
        f"{MINGCHAN_API}/notifications/preferences",
        headers=headers,
        json={"quiet_level": "standard"},
    )
    assert _dispatch("after")["status"] == "sent"
    assert client.get(f"{MINGCHAN_API}/notifications", headers=headers).json()["data"][
        "unread_count"
    ] == 2
