"""S4 图片机审：入队、批处理判定、下架接线与 fail-open。

**不触网**：阿里云调用统一用替身桩住 :func:`review_image_url`（`test_moderation_aliyun.py`
已覆盖真实请求的构造与响应解析），这里只验"拿到某个结论之后系统怎么做"。
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Dict, List

from PIL import Image

import app.db as db
from app.platform.media.moderation import (
    MAX_MODERATION_ATTEMPTS,
    media_moderation_ready,
    reset_media_moderation_throttle,
    review_pending_media_batch,
)
from app.platform.media.persistence import (
    get_media_asset_unscoped,
    insert_media_asset,
    mark_media_assets_referenced,
)
from app.platform.moderation.models import MachineReviewResult
from app.products.zhaoxi.infrastructure.persistence import companion_world as world_db
from app.products.zhaoxi.jobs.media_moderation import review_pending_media_job

BASE_URL = "https://media.example.com"


def _result(level: str, *, error: str = "", categories=()) -> MachineReviewResult:
    return MachineReviewResult(
        reviewer_type="image_safety",
        engine="aliyun_image_moderation",
        level=level,
        categories=list(categories),
        error=error or None,
    )


def _stub_review(monkeypatch, results) -> List[Dict[str, Any]]:
    """桩住云调用，返回被记录的调用参数列表；``results`` 可以是单个结论或队列。"""
    calls: List[Dict[str, Any]] = []
    queue = list(results) if isinstance(results, list) else None

    def _fake(*, image_url: str, data_id: str, user_id: str = "") -> MachineReviewResult:
        calls.append({"image_url": image_url, "data_id": data_id, "user_id": user_id})
        if queue is not None:
            return queue.pop(0)
        return results

    monkeypatch.setattr("app.platform.media.moderation.review_image_url", _fake)
    return calls


def _configure_moderation(fresh_db, *, base_url: str = BASE_URL) -> None:
    """把图片机审的三重门控 + 公网基址一次配齐（等价于运维在阿里云侧配好了）。"""
    fresh_db.moderation_image_safety_enabled = True
    fresh_db.moderation_image_safety_model = "baselineCheck"
    fresh_db.aliyun_access_key_id = "test-ak"
    fresh_db.aliyun_access_key_secret = "test-sk"
    fresh_db.moderation_aliyun_endpoint = "green-cip.cn-beijing.aliyuncs.com"
    fresh_db.media_public_base_url = base_url


# --------------------------------------------------------------------------- 门控


def test_batch_disabled_until_credentials_and_base_url_are_both_configured(
    fresh_db, monkeypatch
):
    """开关/凭证齐了但没配公网基址 → 依然 disabled：阿里云取不到图，跑了也是白跑。"""
    assert media_moderation_ready() is False
    _configure_moderation(fresh_db, base_url="")
    assert media_moderation_ready() is False
    reset_media_moderation_throttle()
    assert review_pending_media_batch()["status"] == "disabled"

    fresh_db.media_public_base_url = BASE_URL
    assert media_moderation_ready() is True


def test_batch_throttles_between_ticks_and_force_bypasses(fresh_db, monkeypatch):
    """每 tick 都调用是安全的：函数自己按 interval 节流，force 供 admin/测试立刻跑。"""
    _configure_moderation(fresh_db)
    _stub_review(monkeypatch, _result("pass"))
    reset_media_moderation_throttle()

    assert review_pending_media_batch(_monotonic=1000.0)["status"] == "ok"
    assert review_pending_media_batch(_monotonic=1030.0)["status"] == "skipped"
    assert review_pending_media_batch(_monotonic=1030.0, force=True)["status"] == "ok"
    # 过了 interval（默认 60s）自然放行。
    assert review_pending_media_batch(_monotonic=1100.0)["status"] == "ok"


# ------------------------------------------------------------------- 入队（引用时）


def _user(phone: str) -> str:
    """真建一个 platform_user：``media_assets.owner_platform_user_id`` 带外键。"""
    return db.create_or_get_platform_user_by_phone(phone=phone)["id"]


def _seed_asset(owner: str, *, kind: str = "image", media_id: str = "media_1") -> str:
    insert_media_asset(
        media_id=media_id,
        owner_platform_user_id=owner,
        kind=kind,
        mime="image/jpeg" if kind == "image" else "audio/mp4",
        bytes_len=1024,
        sha256=f"sha-{media_id}",
        storage_path=f"/tmp/{media_id}",
        expires_at="2026-07-30 12:00:00",
    )
    return media_id


def test_reference_queues_only_images_and_only_when_configured(fresh_db):
    """引用即入队，但仅图片、且仅在机审配好时；语音与未配置一律恒 skipped。"""
    from app.db._core import _tx

    owner = _user("19911160001")
    _seed_asset(owner, media_id="media_img_off")
    with _tx(None) as conn:
        mark_media_assets_referenced(
            media_ids=["media_img_off"],
            owner_platform_user_id=owner,
            conn=conn,
            queue_moderation=False,
        )
    assert get_media_asset_unscoped(media_id="media_img_off")["moderation_status"] == "skipped"

    _seed_asset(owner, media_id="media_img_on")
    _seed_asset(owner, kind="voice", media_id="media_voice_on")
    with _tx(None) as conn:
        mark_media_assets_referenced(
            media_ids=["media_img_on", "media_voice_on"],
            owner_platform_user_id=owner,
            conn=conn,
            queue_moderation=True,
        )
    assert get_media_asset_unscoped(media_id="media_img_on")["moderation_status"] == "pending"
    # v1.5 只审图：语音入不了队，否则批处理会拿着音频去调图片接口。
    assert get_media_asset_unscoped(media_id="media_voice_on")["moderation_status"] == "skipped"


def _queue_image(phone: str, media_id: str) -> str:
    """造一份"已发出去、待审"的图片资产，返回它的主人 id。"""
    from app.db._core import _tx

    owner = _user(phone)
    _seed_asset(owner, media_id=media_id)
    with _tx(None) as conn:
        mark_media_assets_referenced(
            media_ids=[media_id],
            owner_platform_user_id=owner,
            conn=conn,
            queue_moderation=True,
        )
    return owner


# --------------------------------------------------------------------- 批处理判定


def test_pass_writes_terminal_status_and_signs_absolute_owner_url(fresh_db, monkeypatch):
    """通过 → passed；送审地址是"公网基址 + 主人 scope 签名"的绝对 URL。"""
    _configure_moderation(fresh_db)
    calls = _stub_review(monkeypatch, _result("pass"))
    owner = _queue_image("19911160002", "media_pass")
    reset_media_moderation_throttle()

    summary = review_pending_media_batch()
    assert (summary["scanned"], summary["passed"], summary["rejected"]) == (1, 1, 0)
    assert get_media_asset_unscoped(media_id="media_pass")["moderation_status"] == "passed"
    assert len(calls) == 1
    url = calls[0]["image_url"]
    assert url.startswith(f"{BASE_URL}/v1/media/media_pass?")
    assert f"scope=pu:{owner}" in url and "sig=" in url
    assert calls[0]["data_id"] == "media_pass"
    # 结案后不再重复送审。
    assert review_pending_media_batch(force=True)["scanned"] == 0


def test_review_level_passes_without_takedown(fresh_db, monkeypatch):
    """``review`` 档放过：D-7 只对红线动手，误删已经被看见的内容代价更大。"""
    _configure_moderation(fresh_db)
    _stub_review(monkeypatch, _result("review", categories=["cloud:sexual"]))
    _queue_image("19911160003", "media_review")
    reset_media_moderation_throttle()
    rejected: List[str] = []

    summary = review_pending_media_batch(
        on_rejected=lambda asset, result: rejected.append(str(asset["id"]))
    )
    assert (summary["passed"], summary["rejected"]) == (1, 0)
    assert rejected == []
    assert get_media_asset_unscoped(media_id="media_review")["moderation_status"] == "passed"


def test_block_and_escalate_invoke_takedown_then_write_rejected(fresh_db, monkeypatch):
    """红线（block/escalate）→ 先下架再结案；回调拿到资产行与结论。"""
    _configure_moderation(fresh_db)
    _stub_review(monkeypatch, [_result("block"), _result("escalate")])
    _queue_image("19911160004", "media_block_1")
    _queue_image("19911160004", "media_block_2")
    reset_media_moderation_throttle()
    seen: List[tuple] = []

    summary = review_pending_media_batch(
        on_rejected=lambda asset, result: seen.append((str(asset["id"]), result.level))
    )
    assert (summary["scanned"], summary["rejected"]) == (2, 2)
    assert seen == [("media_block_1", "block"), ("media_block_2", "escalate")]
    for media_id in ("media_block_1", "media_block_2"):
        assert get_media_asset_unscoped(media_id=media_id)["moderation_status"] == "rejected"


def test_takedown_failure_keeps_asset_pending_for_retry(fresh_db, monkeypatch):
    """下架失败不结案：宁可下轮重试（下架幂等），也不能留下"已判红线却还在线上"。"""
    _configure_moderation(fresh_db)
    _stub_review(monkeypatch, _result("block"))
    _queue_image("19911160005", "media_takedown_fail")
    reset_media_moderation_throttle()

    def _boom(asset, result):
        raise RuntimeError("outbox down")

    summary = review_pending_media_batch(on_rejected=_boom)
    assert (summary["rejected"], summary["takedown_errors"]) == (0, 1)
    row = get_media_asset_unscoped(media_id="media_takedown_fail")
    assert row["moderation_status"] == "pending"
    assert row["moderation_attempts"] == 1


def test_call_error_retries_then_fails_open_as_skipped(fresh_db, monkeypatch):
    """云调用一直失败 → 计次重试，用尽后 fail-open 记 skipped（不当成命中删内容）。"""
    _configure_moderation(fresh_db)
    _stub_review(monkeypatch, _result("error", error="moderation_image_request_failed"))
    _queue_image("19911160006", "media_err")

    for attempt in range(1, MAX_MODERATION_ATTEMPTS + 1):
        reset_media_moderation_throttle()
        summary = review_pending_media_batch()
        assert summary["errors"] == 1, f"attempt {attempt}"
        row = get_media_asset_unscoped(media_id="media_err")
        assert (row["moderation_status"], row["moderation_attempts"]) == ("pending", attempt)

    # 第 MAX+1 轮：不再送审，直接放过并结案，避免这行永远占着待审索引。
    reset_media_moderation_throttle()
    summary = review_pending_media_batch()
    assert (summary["exhausted"], summary["errors"]) == (1, 0)
    assert get_media_asset_unscoped(media_id="media_err")["moderation_status"] == "skipped"


# ------------------------------------------------------------- 朝夕侧下架（端到端）


def _login(client, phone: str) -> tuple[dict, str]:
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    verified = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    response = client.post(
        "/v1/auth/session", json={"phone": phone, "verified_token": verified}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return (
        {"Authorization": f"Bearer {payload['access_token']}"},
        payload["platform_user"]["id"],
    )


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_mod_{rank}",
            source_type="operations",
            name=f"机审角色{rank}",
            avatar_ref=f"asset://mod-{rank}",
            summary=f"机审简介{rank}",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n机审人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是机审角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (12, 8), color="green").save(buffer, format="JPEG")
    return buffer.getvalue()


def test_rejected_feed_image_retires_post_and_hides_it_from_owner_feed(
    client, fresh_db, monkeypatch
):
    """端到端：发带图动态 → 机审判红线 → 动态终态下架、主人 Feed 立刻不可见。"""
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_feed_enabled = True
    fresh_db.companion_world_feed_image_enabled = True
    for module in ("app.products.zhaoxi.api.media", "app.products.zhaoxi.api.companion_world"):
        monkeypatch.setattr(f"{module}.settings.companion_world_feed_image_enabled", True)
    _configure_moderation(fresh_db)

    _seed_catalog()
    headers, platform_user_id = _login(client, "19911150001")
    candidates = client.post("/v1/worlds/home/bootstrap", headers=headers).json()["data"][
        "candidates"
    ]
    client.post(
        "/v1/worlds/home/residents/confirm",
        headers=headers,
        json={"selections": [{"template_id": candidates[0]["template_id"]}]},
    )
    upload = client.post(
        "/v1/media/uploads",
        headers=headers,
        files={"file": ("photo.jpg", _jpeg(), "image/jpeg")},
        data={"kind": "image"},
    )
    assert upload.status_code == 200, upload.text
    media_id = upload.json()["data"]["media_id"]
    published = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={
            "client_request_id": "moderation-001",
            "text": "一张照片",
            "media_refs": [media_id],
        },
    )
    assert published.status_code == 201, published.text
    post_id = published.json()["data"]["post"]["post_id"]
    # 发布路径已把资产入队（机审此刻是配好的）。
    assert get_media_asset_unscoped(media_id=media_id)["moderation_status"] == "pending"

    _stub_review(monkeypatch, _result("block", categories=["cloud:sexual"]))
    reset_media_moderation_throttle()
    summary = review_pending_media_job()
    assert summary["rejected"] == 1

    row = world_db.get_universe_post_for_owner(
        owner_platform_user_id=platform_user_id, post_id=post_id
    )
    assert row["status"] == "deleted"
    assert row["terminal_reason"] == "moderation"
    feed = client.get("/v1/worlds/home/feed", headers=headers)
    assert [item["post_id"] for item in feed.json()["data"]["items"]] == []
    # 下架事件进了 outbox，端上按既有 deleted 事件同步。
    from app.db._core import _tx

    with _tx(None) as conn:
        keys = [
            str(r["idempotency_key"])
            for r in conn.execute(
                "SELECT idempotency_key FROM companion_world_outbox WHERE post_id = ?",
                (post_id,),
            ).fetchall()
        ]
    assert f"world-post-deleted:v1:{post_id}" in keys


def test_scheduler_tick_reports_moderation_and_isolates_its_failure(fresh_db):
    """调度接线：机审结果落在 ``media_moderation`` 键上，且它炸了不拖垮整个 tick。"""
    import asyncio

    from app.products.zhaoxi.proactive.orchestration.scheduler import (
        run_proactive_scheduler_once,
    )

    result = asyncio.run(
        run_proactive_scheduler_once(
            batch_size=1,
            review_pending_media=lambda: {"status": "disabled", "scanned": 0},
        )
    )
    assert result["media_moderation"] == {"status": "disabled", "scanned": 0}

    def _boom():
        raise RuntimeError("aliyun down")

    failed = asyncio.run(
        run_proactive_scheduler_once(batch_size=1, review_pending_media=_boom)
    )
    assert failed["status"] == "partial_error"
    assert "aliyun down" in failed["errors"]["media_moderation"]
    # 未注入时该键为空 dict，不是 None——运维读日志/返回体的形状恒定。
    assert asyncio.run(run_proactive_scheduler_once(batch_size=1))["media_moderation"] == {}


def test_rejected_chat_image_only_records_verdict(fresh_db, monkeypatch):
    """会话图命中红线：只写 rejected、不撤回消息（D-7 已知敞口，v1.6 补撤回）。"""
    _configure_moderation(fresh_db)
    _stub_review(monkeypatch, _result("block"))
    _queue_image("19911160007", "media_chat_block")
    reset_media_moderation_throttle()

    summary = review_pending_media_job()
    assert summary["rejected"] == 1
    assert get_media_asset_unscoped(media_id="media_chat_block")["moderation_status"] == "rejected"
    # 不在 Feed 上 → 反查为空，回调只记日志，不抛错、不影响结案。
    assert world_db.find_post_owner_by_media_id(media_id="media_chat_block") is None
