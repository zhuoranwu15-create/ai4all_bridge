"""v1.5 S5 许愿创建（WISH-001/005）：安全护栏、受控收敛、幂等重放与日额度。"""
import json

import pytest

import app.db as db
from app.platform.moderation.text_sanitizer import TextSanitizerUnavailable


def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    return db.set_verification_verified(row["id"], token_expires_minutes=10)[
        "verified_token"
    ]


def _login(client, phone: str) -> dict:
    response = client.post(
        "/v1/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_wish_{rank}",
            source_type="operations",
            name=f"角色{rank}",
            avatar_ref=f"asset://avatar-{rank}",
            summary=f"简介{rank}",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _bootstrapped(client, phone: str) -> dict:
    with db.connect() as conn:
        seeded = conn.execute(
            "SELECT COUNT(*) c FROM character_templates WHERE source_type='operations'"
        ).fetchone()["c"]
    if not seeded:
        _seed_catalog()
    headers = _login(client, phone)
    assert client.post("/v1/worlds/home/bootstrap", headers=headers).status_code == 200
    return headers


def _enabled(fresh_db) -> None:
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_resident_wish_enabled = True


class _Generator:
    """许愿生成 LLM 的桩：记录调用次数，返回可定制的原始输出。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def __call__(self, messages, **_kwargs):
        self.calls += 1
        self.wish = json.loads(messages[-1]["content"])["wish"]
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)


def _stub_generator(monkeypatch, payload) -> _Generator:
    generator = _Generator(payload)
    monkeypatch.setattr(
        "app.products.zhaoxi.application.companion_world_wish.generate_completion",
        generator,
    )
    return generator


_GOOD = {
    "name": "阿岚",
    "relationship_type": "sibling",
    "personality_traits": ["gentle", "humorous"],
    "avatar_key": "atang",
    "style_note": "说话慢一点，喜欢先听我说完",
}


def _wish(client, headers, text: str, request_id: str):
    return client.post(
        "/v1/worlds/home/resident-drafts/preview",
        headers=headers,
        json={"wish_text": text, "client_request_id": request_id},
    )


def test_wish_is_hidden_when_flag_off(client, fresh_db, monkeypatch):
    """能力位关闭时许愿路径 404，且一次模型都不调（表单路径不受影响）。"""
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_resident_wish_enabled = False
    headers = _bootstrapped(client, "19930005001")
    generator = _stub_generator(monkeypatch, _GOOD)

    response = _wish(client, headers, "我想要一个爱讲冷笑话的姐姐", "wish-off-0001")

    assert response.status_code == 404
    assert response.json()["code"] == "feature_disabled"
    assert generator.calls == 0


def test_wish_renders_controlled_persona_and_draft_is_consumable(
    client, fresh_db, monkeypatch
):
    """一句话 → 受控取值 → 与表单路径同一套渲染、同一个一次性 draft_token。"""
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005002")
    generator = _stub_generator(monkeypatch, _GOOD)

    data = _wish(
        client, headers, "我想要一个爱讲冷笑话的姐姐", "wish-happy-0001"
    ).json()["data"]

    assert data["name"] == "阿岚"
    assert data["relationship_display"] == "兄弟姐妹"
    assert data["tags"] == ["温柔", "幽默"]
    assert data["avatar_ref"].endswith("atang.png")
    assert data["ai_identity_notice"]
    assert generator.calls == 1
    # 许愿原文只作为生成输入，不进任何回显字段，也不落库。
    assert "冷笑话" not in json.dumps(data, ensure_ascii=False)

    created = client.post(
        "/v1/worlds/home/residents",
        headers=headers,
        json={"draft_token": data["draft_token"], "client_request_id": "req-wish-0001"},
    )
    assert created.status_code == 200, created.json()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM resident_drafts").fetchone()
    assert row["source"] == "wish"
    assert row["status"] == "consumed"
    assert "冷笑话" not in json.dumps(dict(row), ensure_ascii=False, default=str)


def test_model_output_outside_vocabulary_is_coerced_not_rejected(
    client, fresh_db, monkeypatch
):
    """模型给出白名单外的取值时收敛到合法值——不能让模型抖动变成用户可见的 4xx。"""
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005003")
    _stub_generator(
        monkeypatch,
        {
            "name": "小野",
            "relationship_type": "mentor",          # 不在白名单
            "personality_traits": ["gentle", "gentle", "wizard"],  # 重复 + 非法
            "avatar_key": "not-an-avatar",
            "style_note": "  说话干脆  ",
        },
    )

    data = _wish(client, headers, "给我一个引路人", "wish-coerce-0001").json()["data"]

    assert data["relationship_display"] == "朋友"     # 回落默认关系
    assert data["tags"] == ["温柔"]                    # 去重后只剩合法项
    assert data["avatar_ref"].endswith(".png")        # 确定性挑一张，不报错
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM resident_drafts").fetchone()
    assert row["relationship_type"] == "friend"
    assert row["avatar_key"] in {
        "linxiaoman", "luxingye", "shenchuan", "atang", "sichen",
    }


def test_unusable_generation_is_retryable_503(client, fresh_db, monkeypatch):
    """生成不可用（报错或输出取不到名字）→ 503，可重试，且不落半成品草稿。"""
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005004")
    _stub_generator(monkeypatch, RuntimeError("upstream timeout"))

    failed = _wish(client, headers, "随便来一个", "wish-fail-0001")
    assert failed.status_code == 503
    assert failed.json()["code"] == "wish_generation_failed"

    _stub_generator(monkeypatch, {**_GOOD, "name": "   "})
    nameless = _wish(client, headers, "随便来一个", "wish-fail-0002")
    assert nameless.status_code == 503
    assert nameless.json()["code"] == "wish_generation_failed"

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM resident_drafts").fetchone()["c"] == 0


def test_hard_reject_and_sanitizer_outage_use_distinct_codes(
    client, fresh_db, monkeypatch
):
    """红线拒绝是用户输入问题（422），清洗器不可用是服务端问题（503）——码必须分开。"""
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005005")
    generator = _stub_generator(monkeypatch, _GOOD)

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion",
        lambda messages, **_kwargs: json.dumps(
            {"verdict": "reject", "categories": ["deceased_memorial"]}
        ),
    )
    rejected = _wish(client, headers, "我想再见到我去世的外婆", "wish-reject-001")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "wish_text_rejected"

    def _boom(messages, **_kwargs):
        raise TextSanitizerUnavailable("provider down")

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion", _boom
    )
    outage = _wish(client, headers, "我想要一个朋友", "wish-outage-0001")
    assert outage.status_code == 503
    assert outage.json()["code"] == "content_review_unavailable"

    # 两条失败路径都发生在生成之前：模型一次都没被调用，用户也没被计费。
    assert generator.calls == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM resident_drafts").fetchone()["c"] == 0


def test_same_client_request_id_replays_without_second_generation(
    client, fresh_db, monkeypatch
):
    """重试同一个 client_request_id：逐字段等值、只有一份草稿、只调一次模型。"""
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005006")
    generator = _stub_generator(monkeypatch, _GOOD)

    first = _wish(client, headers, "我想要一个姐姐", "wish-replay-0001").json()["data"]
    second = _wish(client, headers, "我想要一个姐姐", "wish-replay-0001").json()["data"]

    assert first == second
    assert generator.calls == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM resident_drafts").fetchone()["c"] == 1


def test_wish_request_id_is_owner_scoped(client, fresh_db, monkeypatch):
    """幂等键按主人隔离：别人用同一个 id 拿到的是自己的新草稿，不是我的。"""
    _enabled(fresh_db)
    mine = _bootstrapped(client, "19930005007")
    theirs = _bootstrapped(client, "19930005008")
    _stub_generator(monkeypatch, _GOOD)

    first = _wish(client, mine, "我想要一个姐姐", "wish-shared-0001").json()["data"]
    other = _wish(client, theirs, "我想要一个哥哥", "wish-shared-0001").json()["data"]

    assert first["draft_id"] != other["draft_id"]
    assert first["draft_token"] != other["draft_token"]


def test_daily_quota_is_enforced_before_generation(client, fresh_db, monkeypatch):
    """超过日额度直接 429，且发生在生成之前；重放不占额度。"""
    _enabled(fresh_db)
    fresh_db.companion_world_wish_daily_max = 2
    headers = _bootstrapped(client, "19930005009")
    generator = _stub_generator(monkeypatch, _GOOD)

    assert _wish(client, headers, "第一个愿望", "wish-quota-0001").status_code == 200
    assert _wish(client, headers, "第二个愿望", "wish-quota-0002").status_code == 200
    # 重放不新增计数。
    assert _wish(client, headers, "第一个愿望", "wish-quota-0001").status_code == 200

    blocked = _wish(client, headers, "第三个愿望", "wish-quota-0003")
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "wish_rate_limited"
    assert generator.calls == 2


@pytest.mark.parametrize(
    "payload",
    [
        # 许愿与结构化字段互斥。
        {"wish_text": "我想要一个朋友", "client_request_id": "wish-bad-0001",
         "name": "小满"},
        # 许愿必须带幂等键。
        {"wish_text": "我想要一个朋友"},
        # 幂等键只对许愿路径有意义。
        {"name": "小满", "avatar_key": "linxiaoman", "relationship_type": "friend",
         "personality_traits": ["gentle"], "client_request_id": "wish-bad-0002"},
        # 表单路径缺字段仍然是 422，与 v1.5 之前一致。
        {"avatar_key": "linxiaoman", "relationship_type": "friend",
         "personality_traits": ["gentle"]},
        {"name": "小满", "avatar_key": "linxiaoman", "relationship_type": "friend",
         "personality_traits": []},
    ],
)
def test_invalid_payload_shapes_are_rejected(client, fresh_db, payload):
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005010")

    response = client.post(
        "/v1/worlds/home/resident-drafts/preview", headers=headers, json=payload
    )

    assert response.status_code == 422, response.json()
    assert response.json()["code"] == "invalid_request"


def test_form_path_is_unchanged_by_wish_support(client, fresh_db, monkeypatch):
    """表单路径的出参形状与落库 source 不受许愿改造影响。"""
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005011")
    generator = _stub_generator(monkeypatch, _GOOD)

    data = client.post(
        "/v1/worlds/home/resident-drafts/preview",
        headers=headers,
        json={
            "name": "小满",
            "avatar_key": "linxiaoman",
            "relationship_type": "friend",
            "personality_traits": ["gentle", "humorous"],
        },
    ).json()["data"]

    assert data["name"] == "小满"
    assert data["tags"] == ["温柔", "幽默"]
    assert generator.calls == 0
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM resident_drafts").fetchone()
    assert row["source"] == "form"
    assert row["wish_request_id"] is None


def test_app_config_publishes_wish_limits(client, fresh_db):
    """客户端从 /app/config 拿字数与日额度，不硬编码。"""
    fresh_db.companion_world_wish_daily_max = 7
    limits = client.get("/v1/app/config").json()["limits"]

    assert limits["wish_text_chars"] == 500
    assert limits["wish_daily_max"] == 7
