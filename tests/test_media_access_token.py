"""S1 签名读端点（``GET /media/{media_id}``）：签名校验、过期、scope 越权、visit 终止即失效。

这条链路的凭据**只有 URL 签名**，没有 ``Authorization``（D-3），所以越权用例必须逐个盯：
签名对了不代表还有权看。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import app.db as db
from app.platform.media import assets
from app.platform.media.access import (
    owner_scope,
    sign_media_url,
    visit_scope,
)
from app.platform.media.persistence import insert_media_asset, pending_expires_at
from app.products.zhaoxi.application.companion_world_visits import CompanionWorldVisitService

NOW = datetime(2026, 7, 29, 12, 0, 0)
_PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"pretend-image-bytes" * 4


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone)["id"]


def _store_asset(owner_platform_user_id: str, *, media_id: str = "mda_read_1") -> dict:
    """直接建一份已落盘的资产，绕开上传端点——本文件只验读侧。"""
    digest = assets.sha256_hex(_PAYLOAD)
    storage_path = assets.build_storage_path(media_id=media_id, sha256=digest)
    assets.write_media_file(storage_path=storage_path, data=_PAYLOAD)
    return insert_media_asset(
        media_id=media_id,
        owner_platform_user_id=owner_platform_user_id,
        kind="image",
        mime="image/png",
        bytes_len=len(_PAYLOAD),
        sha256=digest,
        storage_path=storage_path,
        width=8,
        height=6,
        expires_at=pending_expires_at(ttl_hours=2),
    )


def _active_visit(owner_id: str, visitor_id: str) -> dict:
    world = db.get_or_create_home_universe(platform_user_id=owner_id)
    db.set_universe_onboarding_state(universe_id=world["id"], onboarding_state="confirmed")
    service = CompanionWorldVisitService()
    invitation = service.create_invite(owner_id, now=NOW)
    visit = service.redeem(visitor_id, code=invitation["code"], now=NOW)
    return service.accept(owner_id, visit_id=visit["id"], now=NOW)["visit"]


def _get(client, grant) -> "object":
    return client.get(grant.url)


def test_owner_signed_url_serves_bytes_without_authorization(client, fresh_db):
    owner = _user("19965401001")
    asset = _store_asset(owner)
    grant = sign_media_url(
        media_id=asset["id"], scope=owner_scope(owner), ttl_seconds=900
    )

    response = _get(client, grant)

    assert response.status_code == 200, response.text
    assert response.content == _PAYLOAD
    assert response.headers["content-type"].startswith("image/png")
    # 主人可短时本地缓存；nosniff 防止字节被当作别的类型执行。
    assert response.headers["cache-control"] == "private, max-age=600"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_tampered_or_missing_signature_is_denied(client, fresh_db):
    owner = _user("19965401002")
    asset = _store_asset(owner)
    grant = sign_media_url(
        media_id=asset["id"], scope=owner_scope(owner), ttl_seconds=900
    )

    tampered = client.get(grant.url.replace(grant.signature, "f" * len(grant.signature)))
    assert tampered.status_code == 403
    assert tampered.json()["code"] == "media_access_denied"

    # 换 exp 想延长有效期：exp 在签名 payload 里，改了就不匹配。
    stretched = client.get(
        f"/v1/media/{asset['id']}?exp={grant.expires_at + 86400}"
        f"&scope={grant.scope}&sig={grant.signature}"
    )
    assert stretched.status_code == 403
    assert stretched.json()["code"] == "media_access_denied"

    # 缺失或形状错误也属于媒体访问失败，不能被参数层泄漏成 invalid_request。
    bare = client.get(f"/v1/media/{asset['id']}")
    assert bare.status_code == 403
    assert bare.json()["code"] == "media_access_denied"

    malformed = client.get(
        f"/api/v1/media/{asset['id']}?exp=not-an-int&scope=x&sig=short"
    )
    assert malformed.status_code == 403
    assert malformed.json()["code"] == "media_access_denied"


def test_configured_public_base_returns_absolute_canonical_url(client, fresh_db):
    owner = _user("19965401017")
    asset = _store_asset(owner, media_id="mda_absolute_url")
    fresh_db.media_public_base_url = "https://media.example/"

    grant = sign_media_url(
        media_id=asset["id"], scope=owner_scope(owner), ttl_seconds=900
    )

    assert grant.url.startswith(
        "https://media.example/api/v1/media/mda_absolute_url?exp="
    )
    assert _get(client, grant).status_code == 200


def test_expired_signature_is_denied(client, fresh_db):
    owner = _user("19965401003")
    asset = _store_asset(owner)
    # 时钟容忍是 60 秒，这里过期得足够久。
    grant = sign_media_url(
        media_id=asset["id"],
        scope=owner_scope(owner),
        ttl_seconds=60,
        now=int(time.time()) - 3600,
    )

    response = _get(client, grant)

    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"


def test_other_users_scope_cannot_read_asset(client, fresh_db):
    """签名合法但 scope 指向别人：owner 复查这一道必须拦住（账号隔离）。"""
    owner = _user("19965401004")
    stranger = _user("19965401005")
    asset = _store_asset(owner)
    grant = sign_media_url(
        media_id=asset["id"], scope=owner_scope(stranger), ttl_seconds=900
    )

    response = _get(client, grant)

    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"


def test_unknown_media_id_is_denied_not_404(client, fresh_db):
    """不存在与无权限同码，不给资源枚举信号。"""
    grant = sign_media_url(
        media_id="mda_nope", scope=owner_scope(_user("19965401006")), ttl_seconds=900
    )

    response = _get(client, grant)

    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"


def test_visit_scope_reads_either_participant_media_while_active(client, fresh_db):
    owner = _user("19965401007")
    visitor = _user("19965401008")
    visit = _active_visit(owner, visitor)
    owner_asset = _store_asset(owner, media_id="mda_owner_pic")
    visitor_asset = _store_asset(visitor, media_id="mda_visitor_pic")

    for asset in (owner_asset, visitor_asset):
        grant = sign_media_url(
            media_id=asset["id"], scope=visit_scope(visit["id"]), ttl_seconds=600
        )
        response = _get(client, grant)
        assert response.status_code == 200, response.text
        assert response.content == _PAYLOAD
        # 访客侧一律 no-store：visit 结束后连本地缓存都不该留。
        assert response.headers["cache-control"] == "no-store, private"


def test_visit_scope_cannot_read_outsider_media(client, fresh_db):
    owner = _user("19965401009")
    visitor = _user("19965401010")
    outsider = _user("19965401011")
    visit = _active_visit(owner, visitor)
    asset = _store_asset(outsider, media_id="mda_outsider_pic")

    grant = sign_media_url(
        media_id=asset["id"], scope=visit_scope(visit["id"]), ttl_seconds=600
    )

    assert _get(client, grant).status_code == 403


def test_visit_scope_fails_immediately_after_visit_ends(client, fresh_db):
    """未过期的签名 + 已终止的 visit → 立刻失效（§3.3 隐私红线）。"""
    owner = _user("19965401012")
    visitor = _user("19965401013")
    visit = _active_visit(owner, visitor)
    asset = _store_asset(owner, media_id="mda_ending_pic")
    grant = sign_media_url(
        media_id=asset["id"], scope=visit_scope(visit["id"]), ttl_seconds=600
    )
    assert _get(client, grant).status_code == 200

    CompanionWorldVisitService().terminate(
        owner, visit_id=visit["id"], action="revoke", now=NOW
    )

    response = _get(client, grant)
    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"


def test_visit_scope_fails_after_visit_expires(client, fresh_db):
    """visit 仍是 active 但已到期：读侧按时间复查，不等清理 job。"""
    owner = _user("19965401014")
    visitor = _user("19965401015")
    visit = _active_visit(owner, visitor)
    asset = _store_asset(owner, media_id="mda_expired_visit_pic")
    grant = sign_media_url(
        media_id=asset["id"], scope=visit_scope(visit["id"]), ttl_seconds=600
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_visits SET expires_at = ? WHERE id = ?",
            (
                (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                visit["id"],
            ),
        )

    response = _get(client, grant)

    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"


def test_missing_file_on_disk_is_denied(client, fresh_db):
    """库里有行、磁盘无文件（人工干预或回收竞态）：对外仍是拒绝，不 500。"""
    owner = _user("19965401016")
    asset = _store_asset(owner, media_id="mda_ghost_pic")
    assets.delete_media_file(str(asset["storage_path"]))
    grant = sign_media_url(
        media_id=asset["id"], scope=owner_scope(owner), ttl_seconds=900
    )

    response = _get(client, grant)

    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"
