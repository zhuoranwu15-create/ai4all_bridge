"""M3-1 三表、repository 原语、owner 隔离与幂等功能测试。"""
import pytest

import app.db as db
from app.db._backend import is_postgres
from app.db._core import _migration_0033_companion_world_m3_content
from app.products.zhaoxi.domain.companion_world import user_post_fingerprint


_M3_TABLES = (
    "universe_posts",
    "companion_world_outbox",
    "app_notifications",
)


def _world_with_resident(phone: str, name: str):
    user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name=name
    )["id"]
    world = db.get_or_create_home_universe(platform_user_id=user_id)
    template = db.create_character_template(
        source_type="official", name=f"模板-{name}"
    )
    runtime = db.create_resident_runtime_account(
        universe_id=world["id"],
        character_template_id=template["id"],
        display_name=name,
    )
    db.set_universe_onboarding_state(
        universe_id=world["id"], onboarding_state="confirmed"
    )
    return user_id, world, runtime["resident"]


def _claim_slot(world: dict, resident: dict, *, date: str = "2026-07-22", slot: str = "morning"):
    return db.claim_ai_feed_slot(
        universe_id=world["id"],
        author_resident_id=resident["id"],
        ai_local_date=date,
        ai_slot=slot,
        slot_window_end_at=f"{date} 11:00:00" if slot == "morning" else f"{date} 21:00:00",
        claim_token=f"claim-{date}-{slot}",
        claimed_at=f"{date} 09:00:00" if slot == "morning" else f"{date} 19:00:00",
    )


def test_m3_tables_exist_and_exclude_account_moderation_anchor(fresh_db):
    for table in _M3_TABLES:
        with db.connect() as conn:
            conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0").fetchall()
    with db.connect() as conn:
        if is_postgres():
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'universe_posts'"
            ).fetchall()
            columns = {str(row["column_name"]) for row in rows}
        else:
            rows = conn.execute("PRAGMA table_info(universe_posts)").fetchall()
            columns = {str(row["name"]) for row in rows}
    assert {"universe_id", "author_platform_user_id", "author_resident_id"} <= columns
    assert "moderation_account_id" not in columns


def test_m0033_is_idempotent(fresh_db):
    with db.connect() as conn:
        _migration_0033_companion_world_m3_content(conn)
        _migration_0033_companion_world_m3_content(conn)
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        # 当前库已继续追加到 m0047（Nooki 核心表）；重跑历史 m0033 不得回退或推进版本。
    assert int(version) == 50


def test_feed_slot_owner_isolation_and_atomic_outbox(fresh_db):
    user_a, world_a, resident_a = _world_with_resident("19961001001", "甲")
    user_b, world_b, resident_b = _world_with_resident("19961001002", "乙")

    post, created = _claim_slot(world_a, resident_a)
    assert created is True
    assert post is not None and post["status"] == "generating"

    replay, replay_created = _claim_slot(world_a, resident_a)
    assert replay_created is False
    assert replay["id"] == post["id"]

    occupied_cross, occupied_cross_created = db.claim_ai_feed_slot(
        universe_id=world_a["id"],
        author_resident_id=resident_b["id"],
        ai_local_date="2026-07-22",
        ai_slot="morning",
        slot_window_end_at="2026-07-22 11:00:00",
        claim_token="cross-owner-occupied",
        claimed_at="2026-07-22 09:00:00",
    )
    assert occupied_cross is None and occupied_cross_created is False

    # B 的 resident 不能作为 A 世界作者，即使 slot 尚未占用也不得落行。
    cross, cross_created = db.claim_ai_feed_slot(
        universe_id=world_a["id"],
        author_resident_id=resident_b["id"],
        ai_local_date="2026-07-22",
        ai_slot="evening",
        slot_window_end_at="2026-07-22 21:00:00",
        claim_token="cross-owner",
        claimed_at="2026-07-22 19:00:00",
    )
    assert cross is None and cross_created is False

    published, outbox = db.publish_ai_feed_post_with_outbox(
        post_id=post["id"],
        claim_token=post["claim_token"],
        text="今天世界里有一阵很轻的风。",
        published_at="2026-07-22 09:30:00",
        outbox_idempotency_key=f"world-post-published:v1:{post['id']}",
        payload={"post_id": post["id"], "universe_id": world_a["id"]},
    )
    assert published["status"] == "published"
    assert outbox["post_id"] == post["id"] and outbox["status"] == "pending"
    assert db.get_universe_post_for_owner(
        post_id=post["id"], owner_platform_user_id=user_a
    )["id"] == post["id"]
    assert db.get_universe_post_for_owner(
        post_id=post["id"], owner_platform_user_id=user_b
    ) is None

    first_claim = db.claim_companion_world_outbox(
        batch_size=1,
        now="2026-07-22 09:31:00",
        claim_token="outbox-first",
    )
    assert [row["id"] for row in first_claim] == [outbox["id"]]
    stale_reclaim = db.claim_companion_world_outbox(
        batch_size=1,
        now="2026-07-22 09:40:00",
        claim_token="outbox-reclaim",
        stale_before="2026-07-22 09:31:00",
    )
    assert [row["id"] for row in stale_reclaim] == [outbox["id"]]
    assert stale_reclaim[0]["attempts"] == 2
    stale_complete = db.complete_companion_world_outbox(
        outbox_id=outbox["id"],
        claim_token="outbox-first",
        delivered_at="2026-07-22 09:40:01",
    )
    assert stale_complete["status"] == "processing"
    assert stale_complete["claim_token"] == "outbox-reclaim"
    stale_fail = db.fail_companion_world_outbox(
        outbox_id=outbox["id"],
        claim_token="outbox-first",
        error="old worker",
        next_attempt_at="2026-07-22 09:41:00",
        max_attempts=3,
    )
    assert stale_fail["status"] == "processing"
    assert stale_fail["claim_token"] == "outbox-reclaim"


def test_human_reservation_is_platform_user_scoped_and_idempotent(fresh_db):
    user_a, world_a, _ = _world_with_resident("19961001003", "甲")
    user_b, world_b, _ = _world_with_resident("19961001004", "乙")

    reserved, created = db.reserve_human_app_notification(
        platform_user_id=user_a,
        universe_id=world_a["id"],
        category="content_invitation",
        source_type="account_check",
        source_id="due-a",
        idempotency_key="human-proactive:v1:content_invitation:due-a",
        request_fingerprint="fingerprint-a",
        claim_token="reservation-a",
        claim_expires_at="2026-07-22 10:05:00",
        now="2026-07-22 10:00:00",
        visible_since="2026-07-21 10:00:00",
    )
    assert created is True and reserved["delivery_status"] == "reserved"
    assert db.get_app_notification_for_owner(
        notification_id=reserved["id"], platform_user_id=user_a
    )["id"] == reserved["id"]
    assert db.get_app_notification_for_owner(
        notification_id=reserved["id"], platform_user_id=user_b
    ) is None

    replay, replay_created = db.reserve_human_app_notification(
        platform_user_id=user_a,
        universe_id=world_a["id"],
        category="content_invitation",
        source_type="account_check",
        source_id="due-a",
        idempotency_key="human-proactive:v1:content_invitation:due-a",
        request_fingerprint="fingerprint-a",
        claim_token="ignored-on-replay",
        claim_expires_at="2026-07-22 10:05:00",
        now="2026-07-22 10:00:00",
        visible_since="2026-07-21 10:00:00",
    )
    assert replay_created is False and replay["id"] == reserved["id"]

    reacquired, reacquired_ok = db.reserve_human_app_notification(
        platform_user_id=user_a,
        universe_id=world_a["id"],
        category="content_invitation",
        source_type="account_check",
        source_id="due-a",
        idempotency_key="human-proactive:v1:content_invitation:due-a",
        request_fingerprint="fingerprint-a",
        claim_token="reservation-a-retry",
        claim_expires_at="2026-07-22 10:11:00",
        now="2026-07-22 10:06:00",
        visible_since="2026-07-21 10:06:00",
    )
    assert reacquired_ok is True and reacquired["id"] == reserved["id"]
    assert reacquired["claim_token"] == "reservation-a-retry"

    with pytest.raises(ValueError, match="ownership"):
        db.reserve_human_app_notification(
            platform_user_id=user_a,
            universe_id=world_b["id"],
            category="content_invitation",
            source_type="account_check",
            source_id="due-cross",
            idempotency_key="human-proactive:v1:content_invitation:due-cross",
            request_fingerprint="fingerprint-cross",
            claim_token="reservation-cross",
            claim_expires_at="2026-07-22 10:05:00",
            now="2026-07-22 10:00:00",
            visible_since="2026-07-21 10:00:00",
        )


def test_user_feed_publish_list_idempotency_and_delete_seam(fresh_db):
    user_a, world_a, _ = _world_with_resident("19961001005", "甲")
    user_b, _world_b, _ = _world_with_resident("19961001006", "乙")
    text = "  今天想在世界里留下一句话。  ".strip()
    fingerprint = user_post_fingerprint(text)

    post, created = db.publish_user_feed_post_with_outbox(
        owner_platform_user_id=user_a,
        client_request_id="feed_req_0001",
        text=text,
        request_fingerprint=fingerprint,
        published_at="2026-07-22 12:00:00",
    )
    replay, replay_created = db.publish_user_feed_post_with_outbox(
        owner_platform_user_id=user_a,
        client_request_id="feed_req_0001",
        text=text,
        request_fingerprint=fingerprint,
        published_at="2026-07-22 12:00:05",
    )
    assert created is True and replay_created is False
    assert replay["id"] == post["id"]
    assert post["author_type"] == "human" and post["universe_id"] == world_a["id"]

    with pytest.raises(ValueError, match="idempotency_conflict"):
        db.publish_user_feed_post_with_outbox(
            owner_platform_user_id=user_a,
            client_request_id="feed_req_0001",
            text="同一个键换了正文",
            request_fingerprint=user_post_fingerprint("同一个键换了正文"),
            published_at="2026-07-22 12:00:06",
        )

    own = db.list_published_feed_posts_for_owner(
        owner_platform_user_id=user_a, limit=20
    )
    other = db.list_published_feed_posts_for_owner(
        owner_platform_user_id=user_b, limit=20
    )
    assert [row["id"] for row in own] == [post["id"]]
    assert other == []
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox WHERE post_id = ?",
            (post["id"],),
        ).fetchone()["c"] == 1

    deleted = db.delete_feed_post_with_outbox(
        owner_platform_user_id=user_a,
        post_id=post["id"],
        reason_code="future_policy_test",
        deleted_at="2026-07-22 12:05:00",
    )
    assert deleted["status"] == "deleted"
    replay_deleted = db.delete_feed_post_with_outbox(
        owner_platform_user_id=user_a,
        post_id=post["id"],
        reason_code="future_policy_test",
        deleted_at="2026-07-22 12:05:00",
    )
    assert replay_deleted["id"] == post["id"]
    with pytest.raises(ValueError, match="outbox idempotency conflict"):
        db.delete_feed_post_with_outbox(
            owner_platform_user_id=user_a,
            post_id=post["id"],
            reason_code="changed_reason",
            deleted_at="2026-07-22 12:05:01",
        )
    assert db.list_published_feed_posts_for_owner(
        owner_platform_user_id=user_a, limit=20
    ) == []
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox WHERE post_id = ?",
            (post["id"],),
        ).fetchone()["c"] == 2


def test_user_feed_rolls_back_post_when_outbox_insert_fails(fresh_db):
    if is_postgres():
        pytest.skip("SQLite trigger 注入测试；PG 原子性由同事务与并发门禁覆盖")
    user_id, _world, _resident = _world_with_resident("19961001007", "回滚用户")
    with db.connect() as conn:
        conn.executescript(
            """
            CREATE TRIGGER fail_user_feed_outbox
            BEFORE INSERT ON companion_world_outbox
            BEGIN
                SELECT RAISE(ABORT, 'forced outbox failure');
            END;
            """
        )

    text = "这条不能只落一半"
    with pytest.raises(Exception, match="forced outbox failure"):
        db.publish_user_feed_post_with_outbox(
            owner_platform_user_id=user_id,
            client_request_id="feed_req_rollback",
            text=text,
            request_fingerprint=user_post_fingerprint(text),
            published_at="2026-07-22 12:10:00",
        )
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_posts WHERE author_platform_user_id = ?",
            (user_id,),
        ).fetchone()["c"] == 0
