"""S1 孤儿媒体回收（D-10）：过期 pending 连行带文件清掉，referenced 一律不动。"""
from __future__ import annotations

from datetime import timedelta

import app.db as db
from app.platform.media import assets
from app.platform.media.persistence import (
    get_media_asset_unscoped,
    insert_media_asset,
    mark_media_assets_referenced,
    pending_expires_at,
)
from app.platform.media.reclaim import reclaim_orphan_media_batch, reset_reclaim_throttle
from app.time_utils import beijing_now

_PAYLOAD = b"orphan-bytes" * 8


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone)["id"]


def _asset(owner: str, *, media_id: str, expires_at) -> dict:
    digest = assets.sha256_hex(media_id.encode("utf-8") + _PAYLOAD)
    storage_path = assets.build_storage_path(media_id=media_id, sha256=digest)
    assets.write_media_file(storage_path=storage_path, data=_PAYLOAD)
    return insert_media_asset(
        media_id=media_id,
        owner_platform_user_id=owner,
        kind="image",
        mime="image/png",
        bytes_len=len(_PAYLOAD),
        sha256=digest,
        storage_path=storage_path,
        expires_at=expires_at,
    )


def _past() -> str:
    return (beijing_now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")


def test_reclaim_deletes_expired_pending_row_and_file(client, fresh_db):
    reset_reclaim_throttle()
    owner = _user("19965501001")
    expired = _asset(owner, media_id="mda_orphan", expires_at=_past())
    fresh = _asset(owner, media_id="mda_fresh", expires_at=pending_expires_at(ttl_hours=2))

    result = reclaim_orphan_media_batch()

    assert result["status"] == "ok"
    assert (result["scanned"], result["deleted_rows"], result["deleted_files"]) == (1, 1, 1)
    assert get_media_asset_unscoped(media_id="mda_orphan") is None
    assert not assets.resolve_media_file(str(expired["storage_path"])).exists()
    # 未到期的资产必须完好——它可能正躺在客户端的编辑器里等着发出去。
    assert get_media_asset_unscoped(media_id="mda_fresh") is not None
    assert assets.read_media_file(str(fresh["storage_path"])) == _PAYLOAD


def test_reclaim_never_touches_referenced_assets(client, fresh_db):
    """已被消息/动态引用的资产 expires_at 被置空，永不进回收视野。"""
    reset_reclaim_throttle()
    owner = _user("19965501002")
    asset = _asset(owner, media_id="mda_sent", expires_at=_past())
    with db.connect() as conn:
        mark_media_assets_referenced(
            media_ids=[asset["id"]], owner_platform_user_id=owner, conn=conn
        )

    result = reclaim_orphan_media_batch()

    assert result["deleted_rows"] == 0
    row = get_media_asset_unscoped(media_id="mda_sent")
    assert row is not None and row["status"] == "referenced" and row["expires_at"] is None
    assert assets.read_media_file(str(asset["storage_path"])) == _PAYLOAD


def test_reclaim_skips_asset_referenced_between_scan_and_delete(client, fresh_db, monkeypatch):
    """扫描与删除之间被发送事务抢走：只能跳过，绝不能删文件（否则消息里的图打不开）。"""
    reset_reclaim_throttle()
    owner = _user("19965501003")
    asset = _asset(owner, media_id="mda_race", expires_at=_past())
    monkeypatch.setattr(
        "app.platform.media.reclaim.delete_media_asset_row", lambda **kwargs: False
    )

    result = reclaim_orphan_media_batch()

    assert (result["scanned"], result["deleted_rows"]) == (1, 0)
    assert result["skipped_referenced"] == 1
    assert assets.read_media_file(str(asset["storage_path"])) == _PAYLOAD


def test_reclaim_is_throttled_between_ticks(client, fresh_db):
    """scheduler 每分钟调一次，但回收按小时跑：未到点必须零 DB 开销地返回 skipped。"""
    reset_reclaim_throttle()
    owner = _user("19965501004")
    _asset(owner, media_id="mda_throttled", expires_at=_past())

    first = reclaim_orphan_media_batch(_monotonic=1000.0)
    second = reclaim_orphan_media_batch(_monotonic=1030.0)
    third = reclaim_orphan_media_batch(_monotonic=1000.0 + 4000.0)

    assert first["status"] == "ok"
    assert second["status"] == "skipped" and second["scanned"] == 0
    assert third["status"] == "ok"
    # force 跳过节流，供 admin run-once 立刻核账。
    assert reclaim_orphan_media_batch(_monotonic=1000.0 + 4001.0, force=True)["status"] == "ok"


def test_reclaim_tolerates_missing_file_on_disk(client, fresh_db):
    """文件已被人工删掉：行照删，不让整批回收挂掉。"""
    reset_reclaim_throttle()
    owner = _user("19965501005")
    asset = _asset(owner, media_id="mda_no_file", expires_at=_past())
    assets.delete_media_file(str(asset["storage_path"]))

    result = reclaim_orphan_media_batch()

    assert (result["deleted_rows"], result["deleted_files"], result["file_errors"]) == (1, 0, 0)
    assert get_media_asset_unscoped(media_id="mda_no_file") is None
