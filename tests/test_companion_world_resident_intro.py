"""CONTENT-001 / CONTENT-002 / CONTENT-004：预设居民入场文案与使命展示形态。

覆盖三件事：确认候选时欢迎语与自我介绍动态同事务落库、重放不重复、未配文案的人设
（含全部自建角色）一条都不写。
"""
import json

import pytest

import app.db as db
from app.products.zhaoxi.domain.companion_world import (
    CompanionWorldService,
    ResidentSelection,
    TemplateDraft,
)
from app.products.zhaoxi.domain.companion_world.onboarding_content import (
    RESIDENT_INTRO_CONTENT,
    intro_content_for_persona,
)
from app.products.zhaoxi.domain.missions.registry import mission_display_for_persona
from app.products.zhaoxi.application import SqlCompanionWorldRepository

# 与预设 manifest 一致的五个人设 key，按 rank 升序。
PERSONA_KEYS = ("linxiaoman", "luxingye", "shenchuan", "atang", "sichen")


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone, display_name="用户")["id"]


def _seed_catalog(persona_keys=PERSONA_KEYS) -> list[dict]:
    """按给定人设 key 建连续 rank 1..N 的初始候选目录。"""
    return [
        db.create_character_template(
            source_type="operations",
            name=f"角色{rank}",
            avatar_ref=f"asset://avatar-{rank}",
            summary=f"角色 {rank} 简介",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {"SOUL.md": f"# SOUL\n\n人格 {rank}", "IDENTITY.md": "# IDENTITY\n\n- 设定"},
                ensure_ascii=False,
            ),
            persona_version="v1",
            initial_candidate_rank=rank,
            persona_key=persona_key,
        )
        for rank, persona_key in enumerate(persona_keys, start=1)
    ]


def _service() -> CompanionWorldService:
    return CompanionWorldService(SqlCompanionWorldRepository())


def _messages(runtime_account_id: str) -> list[dict]:
    return db.list_app_conversation_messages_before(runtime_account_id=runtime_account_id)


def _intro_posts(universe_id: str) -> list[dict]:
    with db.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM universe_posts WHERE universe_id = ? "
                "AND source_type = 'resident_intro' ORDER BY id",
                (universe_id,),
            ).fetchall()
        ]


def test_confirm_seeds_welcome_message_and_intro_post_per_resident(fresh_db):
    user_id = _user("19930001001")
    _seed_catalog()
    service = _service()
    boot = service.bootstrap_home(user_id)
    chosen = [
        ResidentSelection(template_id=boot.candidates[0].template.id),
        ResidentSelection(template_id=boot.candidates[4].template.id, display_name="老辰"),
    ]

    residents = service.confirm_residents(user_id, chosen)
    assert len(residents) == 2
    by_name = {item.name: item for item in residents}

    # 欢迎语落在居民自己的 App 会话里，且是第一条可见 assistant 消息。
    for name, persona_key in (("角色1", "linxiaoman"), ("老辰", "sichen")):
        resident = by_name[name]
        messages = _messages(resident.runtime_account_id)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["message_type"] == "text"
        assert messages[0]["content"] == RESIDENT_INTRO_CONTENT[persona_key].welcome_message
        assert messages[0]["message_id"] == f"welcome-{resident.resident_id}"

    # 自我介绍动态：每位一条 published，作者是居民本人。
    posts = _intro_posts(boot.world.id)
    assert len(posts) == 2
    assert {row["status"] for row in posts} == {"published"}
    assert {row["author_type"] for row in posts} == {"resident"}
    assert {row["text"] for row in posts} == {
        RESIDENT_INTRO_CONTENT["linxiaoman"].intro_post,
        RESIDENT_INTRO_CONTENT["sichen"].intro_post,
    }
    assert {row["author_resident_id"] for row in posts} == {
        item.resident_id for item in residents
    }

    # 每条 published post 都挂一条 outbox 事件，与 user_post / ai_feed 口径一致。
    with db.connect() as conn:
        for row in posts:
            assert conn.execute(
                "SELECT COUNT(*) c FROM companion_world_outbox "
                "WHERE post_id = ? AND event_type = 'universe_post.published.v1'",
                (row["id"],),
            ).fetchone()["c"] == 1

    # 主人的 Feed 读路径能直接看到这两条（不额外过滤 source_type）。
    feed = db.list_published_feed_posts_for_owner(owner_platform_user_id=user_id)
    assert {row["id"] for row in feed} >= {row["id"] for row in posts}


def test_confirm_replay_does_not_duplicate_intro_content(fresh_db):
    user_id = _user("19930001002")
    _seed_catalog()
    service = _service()
    boot = service.bootstrap_home(user_id)
    chosen = [ResidentSelection(template_id=boot.candidates[0].template.id)]

    first = service.confirm_residents(user_id, chosen)
    second = service.confirm_residents(user_id, chosen)
    assert [item.resident_id for item in second] == [item.resident_id for item in first]

    # 重放：消息靠 UNIQUE(account_id, message_id)、动态靠 ux_universe_posts_resident_intro 收敛。
    assert len(_messages(first[0].runtime_account_id)) == 1
    assert len(_intro_posts(boot.world.id)) == 1


def test_personas_without_copy_seed_nothing(fresh_db):
    """未配文案的预设人设与自建角色都不落库，且不写兜底句。"""
    user_id = _user("19930001003")
    _seed_catalog(("persona_1", "persona_2", "persona_3", "persona_4"))
    service = _service()
    boot = service.bootstrap_home(user_id)

    residents = service.confirm_residents(
        user_id, [ResidentSelection(template_id=boot.candidates[0].template.id)]
    )
    assert len(_messages(residents[0].runtime_account_id)) == 0
    assert _intro_posts(boot.world.id) == []

    # confirmed 世界里新增的自建角色（persona_key 恒 None）同样不落文案。
    custom = service.create_resident(
        user_id,
        custom_template=TemplateDraft(
            name="自建角色",
            persona_seed_json=json.dumps(
                {"SOUL.md": "# SOUL\n\n自建", "IDENTITY.md": "# IDENTITY\n\n- 设定"},
                ensure_ascii=False,
            ),
        ),
    )
    assert len(_messages(custom.runtime_account_id)) == 0
    assert _intro_posts(boot.world.id) == []


def test_intro_content_registry_covers_every_shipped_persona():
    """五位预设人设都必须配齐两条文案，且文案里不出现角色自己的名字。"""
    assert set(RESIDENT_INTRO_CONTENT) == set(PERSONA_KEYS)
    for persona_key in PERSONA_KEYS:
        content = intro_content_for_persona(persona_key)
        assert content is not None
        assert content.welcome_message.strip() and content.intro_post.strip()
        # 欢迎语受 50 字上限约束（v1.5 计划 §10）。
        assert len(content.welcome_message) <= 50
    assert intro_content_for_persona(None) is None
    assert intro_content_for_persona("  ") is None
    assert intro_content_for_persona("unknown_persona") is None


def test_client_sees_welcome_message_and_mission_display_end_to_end(client, fresh_db):
    """客户端视角：确认后会话里已有欢迎语，居民 DTO 带 mission_display、不带 persona_key。"""
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    phone = "19930001004"
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    token = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    login = client.post(
        "/v1/auth/session", json={"phone": phone, "verified_token": token}
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    boot = client.post("/v1/worlds/home/bootstrap", headers=headers).json()["data"]
    # 司辰是 rank 5，落在候选目录最后一位。
    sichen = boot["candidates"][4]
    confirmed = client.post(
        "/v1/worlds/home/residents/confirm",
        headers=headers,
        json={"selections": [{"template_id": sichen["template_id"]}]},
    )
    assert confirmed.status_code == 200, confirmed.text
    resident = confirmed.json()["data"]["residents"][0]
    # CONTENT-004：司辰走叙事型使命；persona_key 不下发（分支规则留在服务端）。
    assert resident["mission_display"] == "narrative"
    assert "persona_key" not in resident

    messages = client.get(
        f"/v1/ai-conversations/{resident['conversation_id']}/messages",
        headers=headers,
    ).json()["data"]["messages"]
    assert [item["role"] for item in messages] == ["assistant"]
    assert messages[0]["text"] == RESIDENT_INTRO_CONTENT["sichen"].welcome_message

    # 会话列表拿它当预览与未读，避免出现「有居民但列表空白」的首屏。
    item = client.get("/v1/conversations", headers=headers).json()["data"]["items"][0]
    assert item["last_preview"] == RESIDENT_INTRO_CONTENT["sichen"].welcome_message
    assert item["unread"] == 1
    assert item["last_message_at"] is not None

    # 自我介绍动态出现在主人的世界 Feed 里，作者是居民本人。
    fresh_db.companion_world_feed_enabled = True
    feed = client.get("/v1/worlds/home/feed", headers=headers).json()["data"]["items"]
    intro = [row for row in feed if row["source"] == "resident_intro"]
    assert len(intro) == 1
    assert intro[0]["author"]["type"] == "resident"
    assert intro[0]["author"]["resident_id"] == resident["resident_id"]
    assert intro[0]["content"]["text"] == RESIDENT_INTRO_CONTENT["sichen"].intro_post


@pytest.mark.parametrize(
    "persona_key,expected",
    [
        ("sichen", "narrative"),
        ("linxiaoman", "countable"),
        ("luxingye", "countable"),
        ("shenchuan", "countable"),
        ("atang", "countable"),
        (None, "countable"),
        ("", "countable"),
        ("unknown_persona", "countable"),
    ],
)
def test_mission_display_is_decided_by_persona(persona_key, expected):
    """CONTENT-004：只有司辰走叙事型使命，其余（含自建）一律可数。"""
    assert mission_display_for_persona(persona_key) == expected
