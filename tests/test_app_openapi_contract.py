"""S5 契约门禁（CONTRACT-001）：OpenAPI snapshot 一致性 + 主链路响应形状不漂移。

三条门禁，各自守一种漂移：

1. **snapshot 过期** —— 提交的 `app_v1.json` 与实时导出必须逐字节一致，改契约必须显式
   重新导出，客户端 CI 才有可比对的基线。
2. **退回裸 dict** —— 主链路端点必须有非空的 200 响应 schema。少了 `response_model`
   的端点导出来只有路径没有响应，会给客户端「有契约门禁」的错觉。
3. **静默丢字段** —— FastAPI 按 `response_model` 过滤未声明字段，模型漏写一个字段就会
   悄悄少返回。这里拿**真实响应体**逐层比对声明 schema 的键集，多一个少一个都失败。
"""
import json

import pytest

import app.db as db
from scripts.export_openapi import SNAPSHOT_PATH, build_snapshot, render

# 主链路端点（M1 服务端计划 §2.15 的 8 项 + S4 冻结的 /read）。这些必须有真实响应 schema。
MAIN_CHAIN_OPERATIONS = [
    ("/v1/app/config", "get"),
    ("/v1/me", "get"),
    ("/v1/worlds/home/bootstrap", "post"),
    ("/v1/worlds/home/resident-candidates", "get"),
    ("/v1/worlds/home/residents", "get"),
    ("/v1/worlds/home/residents/confirm", "post"),
    ("/v1/conversations", "get"),
    ("/v1/ai-conversations/{conversation_id}/messages", "get"),
    ("/v1/ai-conversations/{conversation_id}/turn", "post"),
    ("/v1/ai-conversations/{conversation_id}/read", "post"),
    # S6「我的」Tab：Profile / 注销 / 通知偏好同样是客户端要按 schema 渲染的主链路。
    ("/v1/me/profile-options", "get"),
    ("/v1/me/profile", "patch"),
    ("/v1/me/account/deletion", "post"),
    ("/v1/notifications/preferences", "get"),
    ("/v1/notifications/preferences", "patch"),
]


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
            template_id=f"tmpl_s5_{rank}",
            source_type="operations",
            name=f"S5角色{rank}",
            avatar_ref=f"asset://s5-{rank}",
            summary=f"S5简介{rank}",
            long_summary=f"S5长简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),  # 初始目录硬性要求恰好 3 个标签
            persona_seed_json=json.dumps(
                {"SOUL.md": f"# SOUL\n\nS5人格{rank}", "IDENTITY.md": "# IDENTITY\n\nS5"},
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
            name_pool_json=json.dumps(["甲", "乙", "丙"], ensure_ascii=False),
            name_pool_version="np_s5",
        )


# --- schema 遍历 -----------------------------------------------------------


def _resolve(schema: dict, spec: dict) -> dict:
    """展开 ``$ref``；snapshot 里只有 ``#/components/schemas/X`` 一种引用形式。"""
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def _assert_shape(value, schema: dict, spec: dict, where: str) -> None:
    """断言真实响应值与声明 schema 的键集逐层一致。

    只比对**结构**（对象有哪些键、数组元素形状），不比对类型取值——类型由 pydantic 在
    序列化时已经保证，这里要抓的是「模型漏声明导致字段被过滤掉」和「实现多返回了未进
    契约的字段」。
    """
    schema = _resolve(schema, spec)
    if "anyOf" in schema:
        # Optional[X] 生成 anyOf[X, null]：值为 null 直接通过，否则按非 null 分支继续。
        branches = [item for item in schema["anyOf"] if item.get("type") != "null"]
        if value is None or not branches:
            return
        _assert_shape(value, branches[0], spec, where)
        return
    if value is None:
        return
    if schema.get("type") == "array":
        for index, item in enumerate(value):
            _assert_shape(item, schema.get("items") or {}, spec, f"{where}[{index}]")
        return
    properties = schema.get("properties")
    if not properties:
        return
    assert isinstance(value, dict), f"{where} 期望对象，实际 {type(value).__name__}"
    assert set(value) == set(properties), (
        f"{where} 字段集与契约不一致：\n"
        f"  响应多出：{sorted(set(value) - set(properties))}\n"
        f"  契约多出：{sorted(set(properties) - set(value))}\n"
        f"（改了响应字段就要同步 api/contracts.py 并重新导出 snapshot）"
    )
    for key, sub_schema in properties.items():
        _assert_shape(value[key], sub_schema, spec, f"{where}.{key}")


# --- 门禁 1：snapshot 与实现一致 -------------------------------------------


def test_committed_snapshot_matches_current_app():
    """提交的 snapshot 必须是最新导出结果，否则客户端 CI 比对的是过期基线。"""
    assert SNAPSHOT_PATH.exists(), f"缺少 snapshot：{SNAPSHOT_PATH}"
    assert SNAPSHOT_PATH.read_text(encoding="utf-8") == render(build_snapshot()), (
        "OpenAPI snapshot 已过期。"
        "请运行 .venv/bin/python scripts/export_openapi.py 重新导出并提交。"
    )


def test_snapshot_only_contains_client_facing_paths():
    """snapshot 只收客户端直连的 /v1；产品 namespace 是同批路由的重复挂载点，不入契约。"""
    paths = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["paths"]
    assert paths
    assert all(path.startswith("/v1/") for path in paths)
    assert not any(path.startswith("/v1/products/") for path in paths)
    # admin / bridge / web 路由不属于客户端契约。
    assert not any(
        path.startswith(("/v1/admin", "/v1/debug", "/v1/bridge")) for path in paths
    )


# --- 门禁 2：主链路必须有响应 schema ---------------------------------------


@pytest.mark.parametrize("path,method", MAIN_CHAIN_OPERATIONS)
def test_main_chain_operations_declare_response_schema(path, method):
    """没有 response_model 的端点导出来响应是空的，等于没有契约门禁。"""
    spec = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    operation = spec["paths"][path][method]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    resolved = _resolve(schema, spec)
    assert resolved.get("properties"), f"{method.upper()} {path} 的 200 响应 schema 为空"


def test_world_endpoints_declare_error_envelope():
    """世界类端点的失败响应也要进契约：客户端按 code 分支，不能靠猜。"""
    spec = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    operation = spec["paths"]["/v1/conversations"]["get"]
    for status in ("401", "404", "409", "422"):
        ref = operation["responses"][status]["content"]["application/json"]["schema"]
        assert _resolve(ref, spec)["properties"].keys() >= {
            "code",
            "request_id",
            "server_time",
            "message",
        }


# --- 门禁 3：真实响应与契约不漂移 -------------------------------------------


def test_main_chain_responses_match_declared_contract(client, fresh_db):
    """跑通主链路，逐个端点把真实响应体和声明 schema 对齐（防 response_model 静默丢字段）。"""
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    spec = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    def _check(path: str, method: str, response, *, expect: int = 200) -> dict:
        assert response.status_code == expect, response.text
        schema = spec["paths"][path][method]["responses"][str(expect)]["content"][
            "application/json"
        ]["schema"]
        body = response.json()
        _assert_shape(body, schema, spec, f"{method.upper()} {path}")
        return body

    _check("/v1/app/config", "get", client.get("/v1/app/config"))

    headers = _login(client, "19970003001")
    bootstrap = _check(
        "/v1/worlds/home/bootstrap",
        "post",
        client.post("/v1/worlds/home/bootstrap", headers=headers),
    )
    assert bootstrap["data"]["candidates"], "候选为空则 CandidateData 没被实际校验到"

    # /me 放在 bootstrap 之后，让 world 摘要非 null（P1 新用户此时 account 仍为 null）。
    me = _check("/v1/me", "get", client.get("/v1/me", headers=headers))
    assert me["world"] is not None

    _check(
        "/v1/worlds/home/resident-candidates",
        "get",
        client.get("/v1/worlds/home/resident-candidates", headers=headers),
    )

    confirmed = _check(
        "/v1/worlds/home/residents/confirm",
        "post",
        client.post(
            "/v1/worlds/home/residents/confirm",
            headers=headers,
            json={
                "selections": [
                    {"template_id": item["template_id"]}
                    for item in bootstrap["data"]["candidates"][:2]
                ]
            },
        ),
    )
    conversation_id = confirmed["data"]["residents"][0]["conversation_id"]

    _check(
        "/v1/worlds/home/residents",
        "get",
        client.get("/v1/worlds/home/residents", headers=headers),
    )
    conversations = _check(
        "/v1/conversations", "get", client.get("/v1/conversations", headers=headers)
    )
    assert conversations["data"]["items"]

    turn = _check(
        "/v1/ai-conversations/{conversation_id}/turn",
        "post",
        client.post(
            f"/v1/ai-conversations/{conversation_id}/turn",
            headers=headers,
            json={"client_message_id": "s5_shape_0001", "text": "你好"},
        ),
    )
    assert turn["data"]["reply"] is not None, "reply 为 null 则 TurnReply 没被校验到"

    messages = _check(
        "/v1/ai-conversations/{conversation_id}/messages",
        "get",
        client.get(f"/v1/ai-conversations/{conversation_id}/messages", headers=headers),
    )
    assert messages["data"]["messages"]

    _check(
        "/v1/ai-conversations/{conversation_id}/read",
        "post",
        client.post(
            f"/v1/ai-conversations/{conversation_id}/read",
            headers=headers,
            json={"last_message_id": messages["data"]["messages"][-1]["id"]},
        ),
    )


def test_me_tab_responses_match_declared_contract(client, fresh_db):
    """S6「我的」Tab 的真实响应体逐层对齐契约（Profile / 注销 / 通知偏好）。"""
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    spec = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    headers = _login(client, "19970003003")

    def _check(path: str, method: str, response, *, expect: int = 200) -> dict:
        assert response.status_code == expect, response.text
        body = response.json()
        _assert_shape(
            body,
            spec["paths"][path][method]["responses"][str(expect)]["content"][
                "application/json"
            ]["schema"],
            spec,
            f"{method.upper()} {path}",
        )
        return body

    options = _check(
        "/v1/me/profile-options",
        "get",
        client.get("/v1/me/profile-options", headers=headers),
    )
    _check(
        "/v1/me/profile",
        "patch",
        client.patch(
            "/v1/me/profile",
            headers=headers,
            json={
                "display_name": "小满",
                "avatar_key": options["avatars"][0]["key"],
            },
        ),
    )
    _check(
        "/v1/notifications/preferences",
        "get",
        client.get("/v1/notifications/preferences", headers=headers),
    )
    _check(
        "/v1/notifications/preferences",
        "patch",
        client.patch(
            "/v1/notifications/preferences",
            headers=headers,
            json={"quiet_level": "quiet"},
        ),
    )
    # 注销放最后：它会就地吊销本次会话，之后这批 headers 全部 401。
    _check(
        "/v1/me/account/deletion",
        "post",
        client.post(
            "/v1/me/account/deletion",
            headers=headers,
            json={"confirm": True, "reason_code": "other"},
        ),
    )


def test_me_account_shape_matches_contract_for_legacy_user(client, fresh_db):
    """P1 关闭时 `/me` 的 account 非 null，单独覆盖 PublicAccount 分支。"""
    fresh_db.companion_world_p1_enabled = False
    spec = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    headers = _login(client, "19970003002")

    response = client.get("/v1/me", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["account"] is not None
    _assert_shape(
        body,
        spec["paths"]["/v1/me"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"],
        spec,
        "GET /v1/me",
    )
