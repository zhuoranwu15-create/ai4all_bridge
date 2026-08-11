"""Plum 产品私有数据访问；消息正文继续使用共享 sessions/messages。"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.bootstrap.product_registry import (
    PLUM_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import (
    connect,
    ensure_runtime_ownership,
    grant_new_user_shells,
    insert_resident_runtime_account,
    list_session_messages_before,
)
from app.db.product_memberships import _ensure_product_membership_in_conn
from app.platform.media.persistence import mark_media_assets_referenced
from app.products.plum.infrastructure.fixtures import (
    BADGES,
    FIXTURE_VERSION,
    HOT_COMMENTS,
    INSPIRATION_PROMPTS,
    PUBLIC_MEMORIES,
    PUBLIC_PROFILES,
    REFERENCE_CHARACTERS,
)
from app.time_utils import beijing_now

_ACTIVE_SESSION_KEY = "__app_active__"


class PlumConflictError(ValueError):
    """A Plum mutation reused an idempotency key for another operation."""


def _fixture_work_id(character_id: str) -> str:
    """Return the stable one-character Work id used by the built-in catalog."""

    return f"work_for_{character_id}"


def _seed_catalog_in_conn(conn, *, platform_user_id: Optional[str] = None) -> None:
    conn.execute(
        """
        UPDATE plum_characters SET status='archived',
            updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
        WHERE id IN ('char_luna', 'char_kai') AND status='active'
        """
    )
    for profile in PUBLIC_PROFILES:
        conn.execute(
            """
            INSERT INTO plum_public_profiles(
                id, platform_user_id, handle, display_name, profile_type, status,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'active',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO UPDATE SET
                handle=excluded.handle,
                display_name=excluded.display_name,
                profile_type=excluded.profile_type,
                status='active',
                updated_at=excluded.updated_at
            """,
            profile,
        )
    conn.execute(
        """
        INSERT INTO plum_public_profiles(
            id, platform_user_id, handle, display_name, profile_type, status,
            updated_at
        )
        VALUES ('fprof_user_test', ?, 'plum-community', '社区用户', 'user',
                'active',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        ON CONFLICT(id) DO UPDATE SET
            platform_user_id=COALESCE(excluded.platform_user_id, plum_public_profiles.platform_user_id),
            display_name=excluded.display_name,
            status='active',
            updated_at=excluded.updated_at
        """,
        (platform_user_id,),
    )
    for index, item in enumerate(REFERENCE_CHARACTERS, start=1):
        work_id = _fixture_work_id(item["id"])
        existing = conn.execute(
            "SELECT fixture_version FROM plum_characters WHERE id=?",
            (item["id"],),
        ).fetchone()
        if existing is not None and str(existing["fixture_version"] or "") != str(
            FIXTURE_VERSION
        ):
            raise RuntimeError(
                "built-in Plum Character fixture version drift: "
                f"{item['id']}; publish a new Character version before reseeding"
            )
        conn.execute(
            """
            INSERT INTO plum_works(
                id, owner_kind, owner_platform_user_id, creator_profile_id,
                lifecycle_status, updated_at
            )
            VALUES (?, 'system', NULL, ?, 'active',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO NOTHING
            """,
            (work_id, item["creator_profile_id"]),
        )
        conn.execute(
            """
            INSERT INTO plum_characters(
                id, work_id, display_name, tagline, intro, greeting, tags_json,
                heat_count, avatar_ref, cover_ref, accent_color, persona_prompt,
                scenario_prompt, speaking_style, example_dialogues,
                prompt_version, content_version, status, sort_order,
                creator_profile_id, content_rating, creator_declared_rating,
                platform_effective_rating, access_policy_version, visibility,
                capabilities_json, fixture_version, published_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1, 1,
                    'active', ?, ?, 'mature', 'mature', 'mature',
                    'plum-rating-v1', 'public', ?, ?,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO NOTHING
            """,
            (
                item["id"], work_id, item["display_name"], item["tagline"],
                item["intro"], item["greeting"],
                json.dumps(item["tags"], ensure_ascii=False),
                item["interaction_count"], item["cover_ref"], item["cover_ref"],
                item["accent_color"], item["persona_prompt"],
                item["scenario_prompt"], item["speaking_style"], index * 10,
                item["creator_profile_id"],
                json.dumps(
                    {"text": True, "voice": bool(item["has_voice"])},
                    separators=(",", ":"),
                ),
                FIXTURE_VERSION,
            ),
        )
        conn.execute(
            """
            INSERT INTO plum_character_versions(
                character_id, version_number, prompt_version, display_name,
                gender, portrait_media_id, portrait_position_x,
                portrait_position_y, avatar_position_x, avatar_position_y,
                intro, opening_scene, character_settings, scenario_prompt,
                example_dialogues, response_rules, creator_declared_rating,
                platform_effective_rating, access_policy_version,
                moderation_decision_id, visibility, created_at
            )
            SELECT id, 1, prompt_version, display_name, gender,
                   portrait_media_id, portrait_position_x, portrait_position_y,
                   avatar_position_x, avatar_position_y, intro, greeting,
                   persona_prompt, scenario_prompt, example_dialogues,
                   speaking_style, creator_declared_rating,
                   platform_effective_rating, access_policy_version,
                   moderation_decision_id, visibility, updated_at
            FROM plum_characters WHERE id=?
            ON CONFLICT(character_id, version_number) DO NOTHING
            """,
            (item["id"],),
        )

    for badge in BADGES:
        conn.execute(
            """
            INSERT INTO plum_character_badges(
                id, code, display_name, style_token, status, updated_at
            )
            VALUES (?, ?, ?, ?, 'active',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO UPDATE SET
                code=excluded.code,
                display_name=excluded.display_name,
                style_token=excluded.style_token,
                status='active',
                updated_at=excluded.updated_at
            """,
            badge,
        )

    for item in REFERENCE_CHARACTERS:
        badge_code = item["badge_code"]
        if badge_code:
            conn.execute(
                """
                INSERT INTO plum_character_badge_assignments(
                    character_id, badge_id, sort_order
                )
                SELECT ?, id, 10 FROM plum_character_badges WHERE code=?
                ON CONFLICT(character_id, badge_id) DO UPDATE SET
                    sort_order=excluded.sort_order
                """,
                (item["id"], badge_code),
            )
        conn.execute(
            """
            INSERT INTO plum_character_stats(
                character_id, interaction_count, connector_count,
                comment_count, memory_count, like_count, favorite_count
            )
            VALUES (?, ?, 12400, 2154, 18, 119, 120)
            ON CONFLICT(character_id) DO NOTHING
            """,
            (item["id"], item["interaction_count"]),
        )
        for slug, author_id, content, like_count, rank in HOT_COMMENTS:
            conn.execute(
                """
                INSERT INTO plum_character_comments(
                    id, character_id, author_profile_id, content, source_locale,
                    status, like_count, is_featured, featured_rank, created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'en', 'visible', ?, 1, ?,
                        '2026-02-13 20:00:00', '2026-02-13 20:00:00')
                ON CONFLICT(id) DO UPDATE SET
                    content=excluded.content,
                    like_count=excluded.like_count,
                    status='visible',
                    is_featured=1,
                    featured_rank=excluded.featured_rank,
                    updated_at=excluded.updated_at
                """,
                (
                    f"fcomment_{item['id'].removeprefix('char_ref_')}_{slug}",
                    item["id"], author_id, content, like_count, rank,
                ),
            )
        for slug, title, message_count, engagement_count, published_at in PUBLIC_MEMORIES:
            conn.execute(
                """
                INSERT INTO plum_character_memories(
                    id, character_id, owner_profile_id, origin, title,
                    message_count, engagement_count, visibility,
                    moderation_status, published_at
                )
                VALUES (?, ?, 'fprof_user_test', 'seed', ?, ?, ?, 'public',
                        'approved', ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    message_count=excluded.message_count,
                    engagement_count=excluded.engagement_count,
                    visibility='public',
                    moderation_status='approved',
                    published_at=excluded.published_at
                """,
                (
                    f"fmemory_{item['id'].removeprefix('char_ref_')}_{slug}",
                    item["id"], title, message_count, engagement_count,
                    published_at,
                ),
            )

    if platform_user_id:
        _seed_default_persona_in_conn(
            conn,
            platform_user_id=platform_user_id,
            display_name="测试用户",
        )
    profiles = (
        ("fast", settings.plum_fast_provider_id, "快速", "响应更快，适合轻松日常", 1_000_000, 0),
        ("balanced", settings.plum_balanced_provider_id, "均衡", "质量与速度兼顾", 3_000_000, 1),
        ("immersive", settings.plum_immersive_provider_id, "沉浸", "更细腻、更有角色感", 5_000_000, 0),
    )
    for profile in profiles:
        conn.execute(
            """
            INSERT INTO plum_model_profiles(
                profile, provider_id, display_name, description,
                coin_cost_micros, enabled, is_default, config_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, 1,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(profile) DO UPDATE SET
                provider_id=excluded.provider_id,
                display_name=excluded.display_name,
                description=excluded.description,
                coin_cost_micros=excluded.coin_cost_micros,
                enabled=excluded.enabled,
                is_default=excluded.is_default,
                config_version=plum_model_profiles.config_version+1,
                updated_at=excluded.updated_at
            """,
            profile,
        )


def _seed_default_persona_in_conn(
    conn, *, platform_user_id: str, display_name: str
) -> None:
    persona_id = (
        "fpersona_test_default"
        if platform_user_id == str(settings.plum_test_user_id)
        else f"fpersona_{uuid.uuid5(uuid.NAMESPACE_URL, platform_user_id).hex}"
    )
    conn.execute(
        """
        INSERT INTO plum_user_personas(
            id, platform_user_id, display_name, description, prompt_text,
            status, is_default, version, updated_at
        )
        VALUES (?, ?, ?, '定义“你”在故事中的身份与背景。',
                '用户以自己选择的身份进入故事；不要替用户决定行动或台词。',
                'active', 1, 1,
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        ON CONFLICT(id) DO UPDATE SET
            display_name=CASE
                WHEN plum_user_personas.locked_at IS NULL
                    THEN excluded.display_name
                ELSE plum_user_personas.display_name END,
            description=CASE
                WHEN plum_user_personas.locked_at IS NULL
                    THEN excluded.description
                ELSE plum_user_personas.description END,
            prompt_text=CASE
                WHEN plum_user_personas.locked_at IS NULL
                    THEN excluded.prompt_text
                ELSE plum_user_personas.prompt_text END,
            status='active',
            is_default=1,
            updated_at=excluded.updated_at
        """,
        (persona_id, platform_user_id, display_name),
    )


def seed_plum_catalog() -> Dict[str, Any]:
    """Idempotently load the shared character catalog without creating a user."""

    with connect() as conn:
        _seed_catalog_in_conn(conn)
    return {"character_count": len(REFERENCE_CHARACTERS), "fixture_version": FIXTURE_VERSION}


def _entry_account_id(platform_user_id: str) -> str:
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, f"plum-entry:{platform_user_id}").hex[:24]
    return f"aid_web_{suffix}"


def ensure_plum_user(
    *,
    platform_user_id: str,
    display_name: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    """Ensure one active user has Plum membership, entry assets and 1000 coins."""

    cleaned_name = str(display_name or "").strip()[:40] or "Plum 测试用户"
    account_id = _entry_account_id(platform_user_id)
    with connect() as conn:
        user = conn.execute(
            "SELECT id, status FROM platform_users WHERE id=?", (platform_user_id,)
        ).fetchone()
        if user is None or str(user["status"]) != "active":
            raise ValueError("platform_user_not_active")
        membership_result = _ensure_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            registry=registry,
        )
        if membership_result["membership"]["status"] != "active":
            raise ValueError("product_membership_disabled")
        conn.execute(
            """
            INSERT INTO accounts(
                id, channel, display_name, status, onboarding_state, app_id, updated_at
            )
            VALUES (?, 'native', ?, 'active', 'complete', ?,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                status='active',
                onboarding_state='complete',
                updated_at=excluded.updated_at
            """,
            (account_id, cleaned_name, PLUM_APP_ID),
        )
        conn.execute(
            """
            INSERT INTO profiles(account_id, display_name, updated_at)
            VALUES (?, ?, to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(account_id) DO UPDATE SET
                display_name=excluded.display_name,
                updated_at=excluded.updated_at
            """,
            (account_id, cleaned_name),
        )
        ensure_runtime_ownership(
            runtime_account_id=account_id,
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            source_type="product_entry",
            source_id=platform_user_id,
            conn=conn,
        )
        _seed_default_persona_in_conn(
            conn, platform_user_id=platform_user_id, display_name=cleaned_name
        )
    grant = grant_new_user_shells(
        account_id=account_id,
        platform_user_id=platform_user_id,
        registry=registry,
    )
    return {
        "platform_user_id": platform_user_id,
        "entry_account_id": account_id,
        "display_name": cleaned_name,
        "is_new_membership": bool(membership_result["is_new_membership"]),
        "grant_balance_shells": grant["balance_after_shells"],
    }


def _required_bounded_text(value: str, *, field: str, max_length: int) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field}_required")
    if len(cleaned) > max_length:
        raise ValueError(f"{field}_too_long")
    return cleaned


def _created_character_result_in_conn(
    conn, *, platform_user_id: str, idempotency_key: str
) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT req.work_id, req.character_id, ch.display_name, ch.gender,
               ch.portrait_media_id, ch.content_version, ch.prompt_version,
               ch.status, ch.visibility, ch.creator_profile_id,
               ch.creator_declared_rating, ch.platform_effective_rating,
               ch.access_policy_version, ch.moderation_decision_id,
               ch.published_at
        FROM plum_character_create_requests req
        JOIN plum_works w
          ON w.id=req.work_id
         AND w.owner_platform_user_id=req.platform_user_id
        JOIN plum_characters ch
          ON ch.id=req.character_id AND ch.work_id=w.id
        WHERE req.platform_user_id=? AND req.idempotency_key=?
        """,
        (platform_user_id, idempotency_key),
    ).fetchone()
    if row is None:
        raise RuntimeError("completed Character create result is missing")
    result = dict(row)
    tags = conn.execute(
        """
        SELECT rel.tag_id
        FROM plum_character_version_tags rel
        JOIN plum_tags tag ON tag.id=rel.tag_id
        WHERE rel.character_id=? AND rel.version_number=?
        ORDER BY tag.sort_order, tag.id
        """,
        (result["character_id"], result["content_version"]),
    ).fetchall()
    result["tag_ids"] = [str(tag["tag_id"]) for tag in tags]
    result["idempotency_key"] = idempotency_key
    return result


def publish_created_character(
    *,
    platform_user_id: str,
    idempotency_key: str,
    display_name: str,
    gender: str,
    portrait_media_id: str,
    intro: str,
    opening_scene: str,
    character_settings: str,
    tag_ids: List[str],
    creator_declared_rating: str,
    approved_moderation_decision_id: str,
    platform_effective_rating: str,
    visibility: str = "private",
    portrait_position_x: int = 50,
    portrait_position_y: int = 50,
    avatar_position_x: int = 50,
    avatar_position_y: int = 50,
    example_dialogues: str = "",
    response_rules: str = "",
    access_policy_version: str = "plum-rating-v1",
) -> Dict[str, Any]:
    """Atomically publish one already-approved user-created Character.

    This persistence boundary never performs moderation and must not be exposed
    directly to clients. The application layer supplies a real, verified review
    decision after evaluating the exact normalized payload published here.
    """

    owner_id = _required_bounded_text(
        platform_user_id, field="platform_user_id", max_length=100
    )
    request_key = _required_bounded_text(
        idempotency_key, field="idempotency_key", max_length=128
    )
    if len(request_key) < 8:
        raise ValueError("idempotency_key_too_short")
    name = _required_bounded_text(display_name, field="display_name", max_length=40)
    cleaned_gender = str(gender or "").strip()
    if cleaned_gender not in {"male", "female", "non_binary"}:
        raise ValueError("gender_invalid")
    media_id = _required_bounded_text(
        portrait_media_id, field="portrait_media_id", max_length=100
    )
    cleaned_intro = _required_bounded_text(intro, field="intro", max_length=500)
    cleaned_opening = _required_bounded_text(
        opening_scene, field="opening_scene", max_length=2000
    )
    cleaned_settings = _required_bounded_text(
        character_settings, field="character_settings", max_length=12000
    )
    cleaned_examples = str(example_dialogues or "").strip()
    cleaned_rules = str(response_rules or "").strip()
    if len(cleaned_examples) > 6000:
        raise ValueError("example_dialogues_too_long")
    if len(cleaned_rules) > 3000:
        raise ValueError("response_rules_too_long")
    cleaned_tags = [str(tag_id or "").strip() for tag_id in tag_ids]
    if (
        not 1 <= len(cleaned_tags) <= 5
        or any(not tag_id or len(tag_id) > 80 for tag_id in cleaned_tags)
        or len(set(cleaned_tags)) != len(cleaned_tags)
    ):
        raise ValueError("character_tag_invalid")
    cleaned_declared_rating = str(creator_declared_rating or "").strip()
    cleaned_effective_rating = str(platform_effective_rating or "").strip()
    if cleaned_declared_rating not in {"general", "mature"}:
        raise ValueError("creator_declared_rating_invalid")
    if cleaned_effective_rating not in {"general", "mature"}:
        raise ValueError("platform_effective_rating_invalid")
    cleaned_visibility = str(visibility or "").strip()
    if cleaned_visibility not in {"private", "public"}:
        raise ValueError("visibility_invalid")
    policy_version = _required_bounded_text(
        access_policy_version, field="access_policy_version", max_length=80
    )
    decision_id = _required_bounded_text(
        approved_moderation_decision_id,
        field="approved_moderation_decision_id",
        max_length=160,
    )
    positions = {
        "portrait_position_x": int(portrait_position_x),
        "portrait_position_y": int(portrait_position_y),
        "avatar_position_x": int(avatar_position_x),
        "avatar_position_y": int(avatar_position_y),
    }
    if any(value < 0 or value > 100 for value in positions.values()):
        raise ValueError("portrait_position_invalid")

    # Only client-controlled normalized content participates in replay identity.
    # A retry keeps the original server review result instead of creating a new
    # Character when a provider returns a different decision id.
    client_payload = {
        "display_name": name,
        "gender": cleaned_gender,
        "portrait_media_id": media_id,
        **positions,
        "intro": cleaned_intro,
        "opening_scene": cleaned_opening,
        "character_settings": cleaned_settings,
        "example_dialogues": cleaned_examples,
        "response_rules": cleaned_rules,
        "tag_ids": sorted(cleaned_tags),
        "creator_declared_rating": cleaned_declared_rating,
        "visibility": cleaned_visibility,
    }
    request_hash = hashlib.sha256(
        json.dumps(
            client_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    work_id = f"work_{uuid.uuid4().hex}"
    character_id = f"char_{uuid.uuid4().hex}"

    with connect() as conn:
        access = conn.execute(
            """
            SELECT pu.display_name, pu.status AS user_status,
                   pm.status AS membership_status
            FROM platform_users pu
            LEFT JOIN product_memberships pm
              ON pm.platform_user_id=pu.id AND pm.app_id=?
            WHERE pu.id=?
            """,
            (PLUM_APP_ID, owner_id),
        ).fetchone()
        if access is None or str(access["user_status"]) != "active":
            raise ValueError("platform_user_not_active")
        if str(access["membership_status"] or "") != "active":
            raise ValueError("product_membership_disabled")

        inserted = conn.execute(
            """
            INSERT INTO plum_character_create_requests(
                platform_user_id, idempotency_key, request_hash,
                work_id, character_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform_user_id, idempotency_key) DO NOTHING
            """,
            (owner_id, request_key, request_hash, work_id, character_id),
        )
        request_row = conn.execute(
            """
            SELECT request_hash FROM plum_character_create_requests
            WHERE platform_user_id=? AND idempotency_key=?
            """,
            (owner_id, request_key),
        ).fetchone()
        if request_row is None:
            raise RuntimeError("Character create idempotency row was not created")
        if str(request_row["request_hash"]) != request_hash:
            raise PlumConflictError("character_create_idempotency_conflict")
        if inserted.rowcount == 0:
            return _created_character_result_in_conn(
                conn, platform_user_id=owner_id, idempotency_key=request_key
            )

        asset = conn.execute(
            """
            SELECT kind, status FROM media_assets
            WHERE id=? AND owner_platform_user_id=?
            """,
            (media_id, owner_id),
        ).fetchone()
        if (
            asset is None
            or str(asset["kind"]) != "image"
            or str(asset["status"]) != "pending"
        ):
            raise ValueError("creator_media_not_claimable")

        placeholders = ",".join("?" for _ in cleaned_tags)
        tag_rows = conn.execute(
            f"""
            SELECT id, display_name FROM plum_tags
            WHERE id IN ({placeholders}) AND status='active'
            ORDER BY sort_order, id
            """,
            tuple(cleaned_tags),
        ).fetchall()
        if len(tag_rows) != len(cleaned_tags):
            raise ValueError("character_tag_invalid")

        profile = conn.execute(
            """
            SELECT id, status FROM plum_public_profiles
            WHERE platform_user_id=?
            """,
            (owner_id,),
        ).fetchone()
        if profile is None:
            stable_suffix = uuid.uuid5(
                uuid.NAMESPACE_URL, f"plum-creator-profile:{owner_id}"
            ).hex
            profile_id = f"fprof_user_{stable_suffix[:24]}"
            handle = f"creator-{stable_suffix[:16]}"
            conn.execute(
                """
                INSERT INTO plum_public_profiles(
                    id, platform_user_id, handle, display_name,
                    profile_type, status, updated_at
                ) VALUES (?, ?, ?, ?, 'creator', 'active',
                          to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
                ON CONFLICT(platform_user_id)
                    WHERE platform_user_id IS NOT NULL DO NOTHING
                """,
                (
                    profile_id,
                    owner_id,
                    handle,
                    str(access["display_name"] or "").strip() or "Plum Creator",
                ),
            )
            profile = conn.execute(
                """
                SELECT id, status FROM plum_public_profiles
                WHERE platform_user_id=?
                """,
                (owner_id,),
            ).fetchone()
        if profile is None or str(profile["status"]) != "active":
            raise ValueError("creator_profile_unavailable")
        profile_id = str(profile["id"])
        tag_names = [str(tag["display_name"]) for tag in tag_rows]

        conn.execute(
            """
            INSERT INTO plum_works(
                id, owner_kind, owner_platform_user_id, creator_profile_id,
                lifecycle_status, updated_at
            ) VALUES (?, 'platform_user', ?, ?, 'active',
                      to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            """,
            (work_id, owner_id, profile_id),
        )
        conn.execute(
            """
            INSERT INTO plum_characters(
                id, work_id, display_name, tagline, intro, greeting, tags_json,
                heat_count, avatar_ref, cover_ref, accent_color, persona_prompt,
                scenario_prompt, speaking_style, example_dialogues,
                prompt_version, content_version, status, sort_order,
                creator_profile_id, content_rating, gender, portrait_media_id,
                portrait_position_x, portrait_position_y,
                avatar_position_x, avatar_position_y,
                creator_declared_rating, platform_effective_rating,
                access_policy_version, moderation_decision_id, visibility,
                capabilities_json, fixture_version, published_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, '#7c3aed', ?,
                      '', ?, ?, 1, 1, 'active', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, '{"text":true,"voice":false}', NULL,
                      to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                      to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            """,
            (
                character_id,
                work_id,
                name,
                cleaned_intro,
                cleaned_intro,
                cleaned_opening,
                json.dumps(tag_names, ensure_ascii=False),
                cleaned_settings,
                cleaned_rules,
                cleaned_examples,
                profile_id,
                cleaned_effective_rating,
                cleaned_gender,
                media_id,
                positions["portrait_position_x"],
                positions["portrait_position_y"],
                positions["avatar_position_x"],
                positions["avatar_position_y"],
                cleaned_declared_rating,
                cleaned_effective_rating,
                policy_version,
                decision_id,
                cleaned_visibility,
            ),
        )
        conn.execute(
            """
            INSERT INTO plum_character_versions(
                character_id, version_number, prompt_version, display_name,
                gender, portrait_media_id, portrait_position_x,
                portrait_position_y, avatar_position_x, avatar_position_y,
                intro, opening_scene, character_settings, scenario_prompt,
                example_dialogues, response_rules, creator_declared_rating,
                platform_effective_rating, access_policy_version,
                moderation_decision_id, visibility
            ) VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?,
                      ?, ?, ?)
            """,
            (
                character_id,
                name,
                cleaned_gender,
                media_id,
                positions["portrait_position_x"],
                positions["portrait_position_y"],
                positions["avatar_position_x"],
                positions["avatar_position_y"],
                cleaned_intro,
                cleaned_opening,
                cleaned_settings,
                cleaned_examples,
                cleaned_rules,
                cleaned_declared_rating,
                cleaned_effective_rating,
                policy_version,
                decision_id,
                cleaned_visibility,
            ),
        )
        for tag in tag_rows:
            conn.execute(
                """
                INSERT INTO plum_character_version_tags(
                    character_id, version_number, tag_id
                ) VALUES (?, 1, ?)
                """,
                (character_id, tag["id"]),
            )
        conn.execute(
            "INSERT INTO plum_character_stats(character_id) VALUES (?)",
            (character_id,),
        )
        try:
            mark_media_assets_referenced(
                media_ids=[media_id],
                owner_platform_user_id=owner_id,
                conn=conn,
            )
        except ValueError as err:
            raise ValueError("creator_media_not_claimable") from err
        return _created_character_result_in_conn(
            conn, platform_user_id=owner_id, idempotency_key=request_key
        )


def _access_code_hash(access_code: str) -> str:
    cleaned = str(access_code or "").strip()
    if len(cleaned) < 16:
        raise ValueError("invalid_access_code")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def create_plum_access_invite(
    *, label: str = "", expires_days: Optional[int] = 30
) -> Dict[str, Any]:
    """Create an invite and return its plaintext code exactly once."""

    access_code = f"plum_{secrets.token_urlsafe(24)}"
    invite_id = f"finvite_{uuid.uuid4().hex}"
    expires_at = (
        (beijing_now() + timedelta(days=max(1, int(expires_days)))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if expires_days
        else None
    )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO plum_access_invites(
                id, code_hash, label, status, expires_at, updated_at
            )
            VALUES (?, ?, ?, 'active', ?,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            """,
            (invite_id, _access_code_hash(access_code), str(label or "").strip()[:80], expires_at),
        )
        row = conn.execute(
            "SELECT id, label, status, expires_at, created_at FROM plum_access_invites WHERE id=?",
            (invite_id,),
        ).fetchone()
    return {**dict(row), "access_code": access_code}


def redeem_plum_access_invite(
    *, access_code: str, display_name: str
) -> Dict[str, Any]:
    """Bind one high-entropy invite to one stable platform user."""

    code_hash = _access_code_hash(access_code)
    cleaned_name = str(display_name or "").strip()[:40]
    if not cleaned_name:
        raise ValueError("display_name_required")
    lock = " FOR UPDATE"
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, platform_user_id FROM plum_access_invites
            WHERE code_hash=? AND status='active'
              AND (expires_at IS NULL OR expires_at > to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            """ + lock,
            (code_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("invalid_access_code")
        platform_user_id = str(row["platform_user_id"] or "")
        if not platform_user_id:
            platform_user_id = f"user_{uuid.uuid4().hex}"
            phone = f"{row['id']}@plum-invite.invalid"
            conn.execute(
                """
                INSERT INTO platform_users(
                    id, phone, display_name, status, updated_at
                )
                VALUES (?, ?, ?, 'active',
                        to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
                """,
                (platform_user_id, phone, cleaned_name),
            )
            claimed = conn.execute(
                """
                UPDATE plum_access_invites
                SET platform_user_id=?, claimed_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                    last_used_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=? AND platform_user_id IS NULL
                """,
                (platform_user_id, row["id"]),
            )
            if claimed.rowcount != 1:
                raise ValueError("access_code_already_claimed")
        else:
            conn.execute(
                """
                UPDATE platform_users
                SET display_name=?, updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=? AND status='active'
                """,
                (cleaned_name, platform_user_id),
            )
            conn.execute(
                """
                UPDATE plum_access_invites
                SET last_used_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=?
                """,
                (row["id"],),
            )
        membership_result = _ensure_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        if membership_result["membership"]["status"] != "active":
            raise ValueError("product_membership_disabled")
        # The legacy access-code flow doubles as a self-contained public-test
        # bootstrap. Formal provider login uses create_plum_login_session and
        # deliberately does not mutate the global Character catalog.
        _seed_catalog_in_conn(conn)
    return ensure_plum_user(
        platform_user_id=platform_user_id, display_name=cleaned_name
    )


def seed_plum_dev() -> Dict[str, Any]:
    """幂等准备固定测试用户、membership、入口账号、目录与 1000 金币。"""

    user_id = str(settings.plum_test_user_id).strip()
    phone = str(settings.plum_test_phone).strip()
    if not user_id or not phone:
        raise ValueError("PLUM_TEST_USER_ID and PLUM_TEST_PHONE are required")
    entry_account_id = "aid_plum_test"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, status, updated_at)
            VALUES (?, ?, 'Plum 测试用户', 'active',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                status='active',
                updated_at=excluded.updated_at
            """,
            (user_id, phone),
        )
        _ensure_product_membership_in_conn(
            conn,
            platform_user_id=user_id,
            app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        conn.execute(
            """
            INSERT INTO accounts(id, channel, display_name, status, onboarding_state, app_id, updated_at)
            VALUES (?, 'native', 'Plum Test Entry', 'active', 'complete', ?,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(id) DO NOTHING
            """,
            (entry_account_id, PLUM_APP_ID),
        )
        account = conn.execute(
            "SELECT app_id FROM accounts WHERE id=?", (entry_account_id,)
        ).fetchone()
        if account is None or str(account["app_id"]) != PLUM_APP_ID:
            raise ValueError("fixed Plum entry account conflicts with existing account")
        conn.execute(
            """
            INSERT INTO profiles(account_id, display_name, updated_at)
            VALUES (?, 'Plum Test Entry',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(account_id) DO NOTHING
            """,
            (entry_account_id,),
        )
        ensure_runtime_ownership(
            runtime_account_id=entry_account_id,
            platform_user_id=user_id,
            app_id=PLUM_APP_ID,
            source_type="product_entry",
            source_id=user_id,
            conn=conn,
        )
        _seed_catalog_in_conn(conn, platform_user_id=user_id)
    grant = grant_new_user_shells(
        account_id=entry_account_id,
        platform_user_id=user_id,
        registry=PRODUCTION_PRODUCT_REGISTRY,
    )
    return {
        "platform_user_id": user_id,
        "entry_account_id": entry_account_id,
        "grant_balance_shells": grant["balance_after_shells"],
    }


def get_entry_account_id(platform_user_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT runtime_account_id FROM runtime_ownerships
            WHERE platform_user_id=? AND app_id=? AND source_type='product_entry'
              AND source_id=? AND status='active'
            """,
            (platform_user_id, PLUM_APP_ID, platform_user_id),
        ).fetchone()
    if row is None:
        raise ValueError("plum user setup required")
    return str(row["runtime_account_id"])


def list_characters() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, display_name, tagline, intro, greeting, tags_json,
                   heat_count, avatar_ref, cover_ref, accent_color,
                   prompt_version, content_rating, capabilities_json,
                   creator_profile_id
            FROM plum_characters
            WHERE status='active' AND visibility='public'
            ORDER BY sort_order, id
            """
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
            item["capabilities"] = json.loads(
                item.pop("capabilities_json") or '{"text":true,"voice":false}'
            )
            creator = conn.execute(
                """
                SELECT id, handle, display_name, avatar_ref
                FROM plum_public_profiles
                WHERE id=? AND status='active'
                """,
                (item.pop("creator_profile_id"),),
            ).fetchone()
            item["creator"] = dict(creator) if creator else None
            stats = conn.execute(
                """
                SELECT interaction_count, connector_count, comment_count,
                       memory_count, like_count, favorite_count
                FROM plum_character_stats WHERE character_id=?
                """,
                (item["id"],),
            ).fetchone()
            item["stats"] = dict(stats) if stats else {
                "interaction_count": int(item["heat_count"]),
                "connector_count": 0,
                "comment_count": 0,
                "memory_count": 0,
                "like_count": 0,
                "favorite_count": 0,
            }
            item["interaction_count"] = int(item["stats"]["interaction_count"])
            badge_rows = conn.execute(
                """
                SELECT b.code, b.display_name, b.icon_ref, b.style_token
                FROM plum_character_badge_assignments a
                JOIN plum_character_badges b ON b.id=a.badge_id
                WHERE a.character_id=? AND b.status='active'
                  AND (a.starts_at IS NULL OR a.starts_at <= to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
                  AND (a.ends_at IS NULL OR a.ends_at > to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
                ORDER BY a.sort_order, b.code
                """,
                (item["id"],),
            ).fetchall()
            item["badges"] = [dict(badge) for badge in badge_rows]
            items.append(item)
    return items


def list_model_profiles() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT profile, display_name, description, coin_cost_micros,
                   is_default, config_version
            FROM plum_model_profiles WHERE enabled=1
            ORDER BY coin_cost_micros, profile
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "coin_cost": int(row["coin_cost_micros"]) // 1_000_000,
            "is_default": bool(row["is_default"]),
        }
        for row in rows
    ]


def get_model_profile(profile: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM plum_model_profiles WHERE profile=? AND enabled=1",
            (profile,),
        ).fetchone()
    return dict(row) if row else None


def _conversation_row(conn, conversation_id: str, platform_user_id: str):
    return conn.execute(
        """
        SELECT c.*, ch.display_name, ch.tagline, ch.intro, ch.greeting,
               ch.tags_json, ch.heat_count, ch.avatar_ref, ch.cover_ref,
               ch.accent_color, ch.prompt_version, ch.content_rating,
               ch.capabilities_json
        FROM plum_conversations c
        JOIN plum_connections pc
          ON pc.id=c.connection_id
         AND pc.platform_user_id=c.platform_user_id
         AND pc.character_id=c.character_id
        JOIN plum_storylines ps
          ON ps.id=c.storyline_id
         AND ps.connection_id=c.connection_id
         AND ps.platform_user_id=c.platform_user_id
         AND ps.character_id=c.character_id
        JOIN plum_characters ch ON ch.id=c.character_id
        WHERE c.id=? AND c.platform_user_id=? AND c.status='active'
          AND pc.status='active' AND ps.status='active' AND ch.status='active'
        """,
        (conversation_id, platform_user_id),
    ).fetchone()


def _decode_conversation(row) -> Dict[str, Any]:
    item = dict(row)
    item["character"] = {
        key: item.pop(key)
        for key in (
            "display_name", "tagline", "intro", "greeting", "heat_count",
            "avatar_ref", "cover_ref", "accent_color", "prompt_version",
            "content_rating",
        )
    }
    item["character"]["id"] = item["character_id"]
    item["character"]["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["character"]["capabilities"] = json.loads(
        item.pop("capabilities_json") or '{"text":true,"voice":false}'
    )
    return item


def _select_connection_persona(
    conn, *, platform_user_id: str, persona_id: Optional[str]
):
    """Lock and return one active Persona owned by the requesting User."""

    if persona_id:
        row = conn.execute(
            """
            SELECT * FROM plum_user_personas
            WHERE id=? AND platform_user_id=? AND status='active'
            FOR UPDATE
            """,
            (persona_id, platform_user_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM plum_user_personas
            WHERE platform_user_id=? AND status='active' AND is_default=1
            ORDER BY created_at, id LIMIT 1 FOR UPDATE
            """,
            (platform_user_id,),
        ).fetchone()
    if row is None:
        raise ValueError("active persona not found")
    return row


def _create_connection_in_conn(
    conn,
    *,
    platform_user_id: str,
    persona_id: str,
    character,
    creation_reason: str,
    creation_idempotency_key: Optional[str] = None,
    replaces_connection_id: Optional[str] = None,
):
    """Create one blank Connection aggregate and its compatibility projection."""

    connection_id = f"fconn_{uuid.uuid4().hex}"
    now_sql = (
        "to_char((now() AT TIME ZONE 'Asia/Shanghai'), "
        "'YYYY-MM-DD HH24:MI:SS')"
    )
    conn.execute(
        f"""
        UPDATE plum_user_personas SET locked_at=COALESCE(locked_at, {now_sql})
        WHERE id=? AND platform_user_id=? AND status='active'
        """,
        (persona_id, platform_user_id),
    )
    conn.execute(
        """
        INSERT INTO plum_connections(
            id, platform_user_id, persona_id, character_id,
            current_character_version, status, creation_reason,
            creation_idempotency_key, replaces_connection_id, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?,
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        """,
        (
            connection_id,
            platform_user_id,
            persona_id,
            str(character["id"]),
            int(character["content_version"]),
            creation_reason,
            creation_idempotency_key,
            replaces_connection_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO plum_connection_character_adoptions(
            id, connection_id, character_id,
            from_content_version, to_content_version,
            from_prompt_version, to_prompt_version,
            trigger, status, applied_at
        )
        VALUES (?, ?, ?, NULL, ?, NULL, ?, 'created', 'applied',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        """,
        (
            f"fadopt_{uuid.uuid4().hex}",
            connection_id,
            str(character["id"]),
            int(character["content_version"]),
            int(character["prompt_version"]),
        ),
    )
    conn.execute(
        """
        INSERT INTO plum_connection_relationship_state(connection_id)
        VALUES (?)
        """,
        (connection_id,),
    )
    _create_initial_storyline_in_conn(
        conn,
        connection_id=connection_id,
        platform_user_id=platform_user_id,
        character_id=str(character["id"]),
        opening_character_version=int(character["content_version"]),
    )
    return conn.execute(
        "SELECT * FROM plum_connections WHERE id=? AND platform_user_id=?",
        (connection_id, platform_user_id),
    ).fetchone()


def _create_initial_storyline_in_conn(
    conn,
    *,
    connection_id: str,
    platform_user_id: str,
    character_id: str,
    opening_character_version: int,
):
    """Create the one initial Storyline and empty state for a new Connection."""

    storyline_id = f"fstory_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO plum_storylines(
            id, platform_user_id, connection_id, character_id, ordinal,
            opening_character_version, status, updated_at
        )
        VALUES (?, ?, ?, ?, 1, ?, 'active',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        """,
        (
            storyline_id,
            platform_user_id,
            connection_id,
            character_id,
            opening_character_version,
        ),
    )
    conn.execute(
        """
        INSERT INTO plum_storyline_state(
            storyline_id, platform_user_id, connection_id,
            current_chapter_no, state_schema_version, plot_state_json
        )
        VALUES (?, ?, ?, 1, 1, '{}'::jsonb)
        """,
        (storyline_id, platform_user_id, connection_id),
    )
    return conn.execute(
        """
        SELECT * FROM plum_storylines
        WHERE id=? AND platform_user_id=? AND connection_id=?
        """,
        (storyline_id, platform_user_id, connection_id),
    ).fetchone()


def _ensure_connection_runtime_in_conn(conn, *, connection) -> str:
    """Return the Connection-owned Runtime, creating an isolated one if absent."""

    binding = conn.execute(
        """
        SELECT runtime_account_id FROM plum_connection_runtime_bindings
        WHERE connection_id=? AND platform_user_id=? AND status='active'
        """,
        (connection["id"], connection["platform_user_id"]),
    ).fetchone()
    if binding is not None:
        return str(binding["runtime_account_id"])

    version = conn.execute(
        """
        SELECT * FROM plum_character_versions
        WHERE character_id=? AND version_number=?
        """,
        (connection["character_id"], connection["current_character_version"]),
    ).fetchone()
    if version is None:
        raise RuntimeError("connection character version not found")
    soul = "\n\n".join(
        part
        for part in (
            str(version["character_settings"] or "").strip(),
            str(version["scenario_prompt"] or "").strip(),
            str(version["response_rules"] or "").strip(),
        )
        if part
    )
    runtime = insert_resident_runtime_account(
        platform_user_id=str(connection["platform_user_id"]),
        display_name=str(version["display_name"]),
        initial_channel="native",
        app_id=PLUM_APP_ID,
        soul_seed=soul,
        identity_seed=f"# 身份\n\n名字：{version['display_name']}\n",
        registry=PRODUCTION_PRODUCT_REGISTRY,
        conn=conn,
    )
    runtime_account_id = str(runtime["account"]["id"])
    ensure_runtime_ownership(
        runtime_account_id=runtime_account_id,
        platform_user_id=str(connection["platform_user_id"]),
        app_id=PLUM_APP_ID,
        source_type="plum_connection",
        source_id=str(connection["id"]),
        conn=conn,
    )
    conn.execute(
        """
        INSERT INTO plum_connection_runtime_bindings(
            connection_id, platform_user_id, runtime_account_id,
            applied_content_version, applied_prompt_version, status, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'active',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        ON CONFLICT(connection_id) DO UPDATE SET
            platform_user_id=excluded.platform_user_id,
            runtime_account_id=excluded.runtime_account_id,
            applied_content_version=excluded.applied_content_version,
            applied_prompt_version=excluded.applied_prompt_version,
            status='active', updated_at=excluded.updated_at
        """,
        (
            connection["id"],
            connection["platform_user_id"],
            runtime_account_id,
            int(connection["current_character_version"]),
            int(version["prompt_version"]),
        ),
    )
    return runtime_account_id


def _create_conversation_for_connection_in_conn(
    conn, *, connection, character, model_profile: Optional[str] = None
) -> Dict[str, Any]:
    """Create or restore the sole active Conversation for one Connection."""

    existing = conn.execute(
        """
        SELECT id FROM plum_conversations
        WHERE connection_id=? AND platform_user_id=? AND status='active'
        """,
        (connection["id"], connection["platform_user_id"]),
    ).fetchone()
    if existing is not None:
        return _decode_conversation(
            _conversation_row(
                conn, str(existing["id"]), str(connection["platform_user_id"])
            )
        )

    storyline = conn.execute(
        """
        SELECT * FROM plum_storylines
        WHERE connection_id=? AND platform_user_id=? AND status='active'
        """,
        (connection["id"], connection["platform_user_id"]),
    ).fetchone()
    if storyline is None:
        raise RuntimeError("active Storyline not found for Plum Connection")

    runtime_account_id = _ensure_connection_runtime_in_conn(
        conn, connection=connection
    )
    conn.execute(
        """
        INSERT INTO sessions(
            account_id, session_key, sender_id, sender_name, status,
            business_day, metadata_json, updated_at
        )
        VALUES (?, ?, ?, 'Plum User', 'active',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD'), '{}',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        ON CONFLICT(account_id, session_key) DO UPDATE SET
            status='active', ended_at=NULL, close_reason=NULL,
            updated_at=excluded.updated_at
        """,
        (runtime_account_id, _ACTIVE_SESSION_KEY, connection["platform_user_id"]),
    )
    session = conn.execute(
        "SELECT id FROM sessions WHERE account_id=? AND session_key=?",
        (runtime_account_id, _ACTIVE_SESSION_KEY),
    ).fetchone()
    selected_model = model_profile
    if not selected_model:
        default_model = conn.execute(
            """
            SELECT profile FROM plum_model_profiles
            WHERE enabled=1 ORDER BY is_default DESC, coin_cost_micros LIMIT 1
            """
        ).fetchone()
        if default_model is None:
            raise RuntimeError("default Plum model profile not found")
        selected_model = str(default_model["profile"])
    conversation_id = f"fconv_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO plum_conversations(
            id, platform_user_id, character_id, connection_id, storyline_id,
            runtime_account_id, runtime_session_id, model_profile,
            status, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active',
                to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        """,
        (
            conversation_id,
            connection["platform_user_id"],
            connection["character_id"],
            connection["id"],
            storyline["id"],
            runtime_account_id,
            int(session["id"]),
            selected_model,
        ),
    )
    return _decode_conversation(
        _conversation_row(
            conn, conversation_id, str(connection["platform_user_id"])
        )
    )


def create_or_get_conversation(
    *,
    platform_user_id: str,
    character_id: str,
    persona_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or restore the active Persona–Character Connection conversation."""

    with connect() as conn:
        character = conn.execute(
            """
            SELECT ch.*
            FROM plum_characters ch
            JOIN plum_works w ON w.id=ch.work_id
            WHERE ch.id=? AND ch.status='active'
              AND (ch.visibility='public'
                   OR (w.owner_kind='platform_user'
                       AND w.owner_platform_user_id=?))
            """,
            (character_id, platform_user_id),
        ).fetchone()
        if character is None:
            raise ValueError("character not found")
        persona = _select_connection_persona(
            conn, platform_user_id=platform_user_id, persona_id=persona_id
        )
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-connection:{persona['id']}:{character_id}",),
        )
        connection = conn.execute(
            """
            SELECT * FROM plum_connections
            WHERE platform_user_id=? AND persona_id=? AND character_id=?
              AND status='active'
            """,
            (platform_user_id, persona["id"], character_id),
        ).fetchone()
        if connection is None:
            connection = _create_connection_in_conn(
                conn,
                platform_user_id=platform_user_id,
                persona_id=str(persona["id"]),
                character=character,
                creation_reason="initial",
            )
        return _create_conversation_for_connection_in_conn(
            conn, connection=connection, character=character
        )


def get_conversation(
    *, conversation_id: str, platform_user_id: str
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = _conversation_row(conn, conversation_id, platform_user_id)
    return _decode_conversation(row) if row else None


def list_user_conversations(
    *, platform_user_id: str, limit: int = 30
) -> List[Dict[str, Any]]:
    """Return one user's active Plum conversations, most recently used first."""

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*, ch.display_name, ch.tagline, ch.intro, ch.greeting,
                   ch.tags_json, ch.heat_count, ch.avatar_ref, ch.cover_ref,
                   ch.accent_color, ch.prompt_version, ch.content_rating,
                   ch.capabilities_json
            FROM plum_conversations c
            JOIN plum_connections pc
              ON pc.id=c.connection_id
             AND pc.platform_user_id=c.platform_user_id
            JOIN plum_storylines ps
              ON ps.id=c.storyline_id
             AND ps.connection_id=c.connection_id
             AND ps.platform_user_id=c.platform_user_id
            JOIN plum_characters ch ON ch.id=c.character_id
            WHERE c.platform_user_id=? AND c.status='active'
              AND pc.status='active' AND ps.status='active'
              AND ch.status='active'
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT ?
            """,
            (platform_user_id, limit),
        ).fetchall()
    return [_decode_conversation(row) for row in rows]


def _api_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if "T" not in text:
        text = text.replace(" ", "T", 1)
    return text if text.endswith(("Z", "+08:00")) else f"{text}+08:00"


def _public_profile(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "display_name": str(row["display_name"]),
        "avatar_ref": row["avatar_ref"],
    }


def get_character_experience(
    *, conversation_id: str, platform_user_id: str
) -> Optional[Dict[str, Any]]:
    """聚合当前用户在一个 Plum 会话里需要的 Profile 与 viewer state。"""

    with connect() as conn:
        conversation = conn.execute(
            """
            SELECT c.character_id, c.connection_id, c.current_chapter_no,
                   pc.persona_id
            FROM plum_conversations c
            JOIN plum_connections pc
              ON pc.id=c.connection_id
             AND pc.platform_user_id=c.platform_user_id
             AND pc.character_id=c.character_id
            JOIN plum_storylines ps
              ON ps.id=c.storyline_id
             AND ps.connection_id=c.connection_id
             AND ps.platform_user_id=c.platform_user_id
            WHERE c.id=? AND c.platform_user_id=? AND c.status='active'
              AND pc.status='active' AND ps.status='active'
            """,
            (conversation_id, platform_user_id),
        ).fetchone()
        if conversation is None:
            return None
        character_id = str(conversation["character_id"])
        character = conn.execute(
            """
            SELECT ch.tags_json, p.id, p.display_name, p.avatar_ref
            FROM plum_characters ch
            JOIN plum_public_profiles p ON p.id=ch.creator_profile_id
            WHERE ch.id=? AND ch.status='active' AND p.status='active'
            """,
            (character_id,),
        ).fetchone()
        stats = conn.execute(
            """
            SELECT interaction_count, connector_count, comment_count,
                   memory_count, like_count, favorite_count
            FROM plum_character_stats WHERE character_id=?
            """,
            (character_id,),
        ).fetchone()
        badge_rows = conn.execute(
            """
            SELECT b.code, b.display_name, b.style_token
            FROM plum_character_badge_assignments a
            JOIN plum_character_badges b ON b.id=a.badge_id
            WHERE a.character_id=? AND b.status='active'
            ORDER BY a.sort_order, b.code
            """,
            (character_id,),
        ).fetchall()
        comment_rows = conn.execute(
            """
            SELECT c.id AS comment_id, c.content, c.source_locale,
                   c.like_count, c.created_at, p.id, p.display_name,
                   p.avatar_ref
            FROM plum_character_comments c
            JOIN plum_public_profiles p ON p.id=c.author_profile_id
            WHERE c.character_id=? AND c.status='visible' AND c.is_featured=1
            ORDER BY c.featured_rank, c.created_at DESC LIMIT 2
            """,
            (character_id,),
        ).fetchall()
        memory_rows = conn.execute(
            """
            SELECT m.id AS memory_id, m.title, m.message_count,
                   m.engagement_count, m.published_at, p.id,
                   p.display_name, p.avatar_ref
            FROM plum_character_memories m
            JOIN plum_public_profiles p ON p.id=m.owner_profile_id
            WHERE m.character_id=? AND m.visibility='public'
              AND m.moderation_status='approved'
            ORDER BY m.published_at DESC, m.id LIMIT 3
            """,
            (character_id,),
        ).fetchall()
        relationship = conn.execute(
            """
            SELECT relationship_level, relationship_xp
            FROM plum_connection_relationship_state
            WHERE connection_id=? AND state='connected'
            """,
            (conversation["connection_id"],),
        ).fetchone()
        liked = conn.execute(
            """
            SELECT 1 FROM plum_character_likes
            WHERE platform_user_id=? AND character_id=?
            """,
            (platform_user_id, character_id),
        ).fetchone()
        favorited = conn.execute(
            """
            SELECT 1 FROM plum_character_favorites
            WHERE platform_user_id=? AND character_id=?
            """,
            (platform_user_id, character_id),
        ).fetchone()
        persona = conn.execute(
            """
            SELECT id, display_name, avatar_ref, description
            FROM plum_user_personas
            WHERE id=? AND platform_user_id=?
            """,
            (conversation["persona_id"], platform_user_id),
        ).fetchone()
        pin_rows = conn.execute(
            """
            SELECT id, content_snapshot, sort_order
            FROM plum_conversation_pins
            WHERE conversation_id=? AND status='active'
            ORDER BY sort_order, id
            """,
            (conversation_id,),
        ).fetchall()

    if character is None or stats is None:
        return None
    comments = []
    for row in comment_rows:
        comments.append({
            "id": str(row["comment_id"]),
            "author": _public_profile(row),
            "content": str(row["content"]),
            "source_locale": str(row["source_locale"]),
            "like_count": int(row["like_count"]),
            "viewer_has_liked": False,
            "created_at": _api_timestamp(row["created_at"]),
        })
    memories = []
    for row in memory_rows:
        memories.append({
            "id": str(row["memory_id"]),
            "title": str(row["title"]),
            "owner": _public_profile(row),
            "message_count": int(row["message_count"]),
            "engagement_count": int(row["engagement_count"]),
            "published_at": _api_timestamp(row["published_at"]),
        })
    return {
        "profile": {
            "creator": _public_profile(character),
            "badges": [dict(row) for row in badge_rows],
            "tags": json.loads(character["tags_json"] or "[]"),
            "stats": {
                key: int(stats[key])
                for key in (
                    "interaction_count", "connector_count", "comment_count",
                    "memory_count",
                )
            },
            "hot_comments": comments,
            "memories": memories,
        },
        "viewer_state": {
            "relationship_level": int(relationship["relationship_level"]) if relationship else 0,
            "relationship_xp": int(relationship["relationship_xp"]) if relationship else 0,
            "current_chapter": int(conversation["current_chapter_no"]),
            "has_liked": liked is not None,
            "like_count": int(stats["like_count"]),
            "is_favorite": favorited is not None,
            "favorite_count": int(stats["favorite_count"]),
        },
        "conversation_tools": {
            "role_card": dict(persona) if persona else None,
            "pins": [
                {
                    "id": str(row["id"]),
                    "content": str(row["content_snapshot"]),
                    "sort_order": int(row["sort_order"]),
                }
                for row in pin_rows
            ],
        },
        "inspiration_prompts": list(INSPIRATION_PROMPTS),
    }


def _set_character_reaction(
    *, platform_user_id: str, character_id: str, reaction: str, active: bool
) -> Dict[str, Any]:
    table, count_column = {
        "like": ("plum_character_likes", "like_count"),
        "favorite": ("plum_character_favorites", "favorite_count"),
    }[reaction]
    with connect() as conn:
        character = conn.execute(
            """
            SELECT 1
            FROM plum_characters ch
            JOIN plum_works w ON w.id=ch.work_id
            WHERE ch.id=? AND ch.status='active'
              AND (ch.visibility='public'
                   OR (w.owner_kind='platform_user'
                       AND w.owner_platform_user_id=?))
            """,
            (character_id, platform_user_id),
        ).fetchone()
        if character is None:
            raise ValueError("character not found")
        if active:
            changed = conn.execute(
                f"""
                INSERT INTO {table}(platform_user_id, character_id)
                VALUES (?, ?) ON CONFLICT(platform_user_id, character_id) DO NOTHING
                """,
                (platform_user_id, character_id),
            ).rowcount
            if changed:
                conn.execute(
                    f"""
                    UPDATE plum_character_stats
                    SET {count_column}={count_column}+1, stats_version=stats_version+1,
                        updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                    WHERE character_id=?
                    """,
                    (character_id,),
                )
        else:
            changed = conn.execute(
                f"DELETE FROM {table} WHERE platform_user_id=? AND character_id=?",
                (platform_user_id, character_id),
            ).rowcount
            if changed:
                conn.execute(
                    f"""
                    UPDATE plum_character_stats
                    SET {count_column}=CASE WHEN {count_column}>0 THEN {count_column}-1 ELSE 0 END,
                        stats_version=stats_version+1,
                        updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                    WHERE character_id=?
                    """,
                    (character_id,),
                )
        current = conn.execute(
            f"SELECT 1 FROM {table} WHERE platform_user_id=? AND character_id=?",
            (platform_user_id, character_id),
        ).fetchone()
        stats = conn.execute(
            f"SELECT {count_column} AS count FROM plum_character_stats WHERE character_id=?",
            (character_id,),
        ).fetchone()
    return {"active": current is not None, "count": int(stats["count"])}


def set_character_like(
    *, platform_user_id: str, character_id: str, active: bool
) -> Dict[str, Any]:
    """幂等设置当前用户对角色的点赞状态，并返回权威计数。"""

    return _set_character_reaction(
        platform_user_id=platform_user_id,
        character_id=character_id,
        reaction="like",
        active=active,
    )


def set_character_favorite(
    *, platform_user_id: str, character_id: str, active: bool
) -> Dict[str, Any]:
    """幂等设置当前用户对角色的收藏状态，并返回权威计数。"""

    return _set_character_reaction(
        platform_user_id=platform_user_id,
        character_id=character_id,
        reaction="favorite",
        active=active,
    )


def list_conversation_messages(conversation: Dict[str, Any], *, limit: int = 100):
    messages = list_session_messages_before(
        account_id=str(conversation["runtime_account_id"]),
        session_id=int(conversation["runtime_session_id"]),
        limit=limit,
    )
    assistant_ids = {
        str(message["message_id"])
        for message in messages
        if message.get("role") == "assistant" and message.get("message_id")
    }
    run_statuses: Dict[str, str] = {}
    if assistant_ids:
        placeholders = ",".join("?" for _ in assistant_ids)
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT assistant_message_id, status FROM runtime_turn_runs
                WHERE app_id='plum' AND account_id=? AND session_id=?
                  AND assistant_message_id IN ({placeholders})
                """,
                (
                    str(conversation["runtime_account_id"]),
                    int(conversation["runtime_session_id"]),
                    *sorted(assistant_ids),
                ),
            ).fetchall()
        run_statuses = {
            str(row["assistant_message_id"]): str(row["status"])
            for row in rows
        }
    return [
        {
            **message,
            "status": run_statuses.get(str(message.get("message_id")), "completed"),
        }
        for message in messages
    ]


def update_conversation_model(
    *, conversation_id: str, platform_user_id: str, model_profile: str
) -> Dict[str, Any]:
    if get_model_profile(model_profile) is None:
        raise ValueError("model profile not found")
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE plum_conversations c SET model_profile=?,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            FROM plum_connections pc, plum_storylines ps, plum_characters ch
            WHERE c.id=? AND c.platform_user_id=? AND c.status='active'
              AND pc.id=c.connection_id
              AND pc.platform_user_id=c.platform_user_id
              AND pc.character_id=c.character_id
              AND pc.status='active'
              AND ps.id=c.storyline_id
              AND ps.platform_user_id=c.platform_user_id
              AND ps.connection_id=c.connection_id
              AND ps.character_id=c.character_id
              AND ps.status='active'
              AND ch.id=c.character_id AND ch.status='active'
            """,
            (model_profile, conversation_id, platform_user_id),
        )
        if updated.rowcount != 1:
            raise ValueError("conversation not found")
        return _decode_conversation(
            _conversation_row(conn, conversation_id, platform_user_id)
        )


def touch_conversation(*, conversation_id: str, platform_user_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE plum_conversations c SET
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            FROM plum_connections pc, plum_storylines ps, plum_characters ch
            WHERE c.id=? AND c.platform_user_id=? AND c.status='active'
              AND pc.id=c.connection_id
              AND pc.platform_user_id=c.platform_user_id
              AND pc.character_id=c.character_id
              AND pc.status='active'
              AND ps.id=c.storyline_id
              AND ps.platform_user_id=c.platform_user_id
              AND ps.connection_id=c.connection_id
              AND ps.character_id=c.character_id
              AND ps.status='active'
              AND ch.id=c.character_id AND ch.status='active'
            """,
            (conversation_id, platform_user_id),
        )


def restart_conversation(
    *,
    conversation_id: str,
    platform_user_id: str,
    creation_idempotency_key: str,
) -> Dict[str, Any]:
    """Archive one Connection and create a blank, Runtime-isolated replacement."""

    key = str(creation_idempotency_key or "").strip()
    if not key:
        raise ValueError("creation idempotency key is required")
    with connect() as conn:
        current = conn.execute(
            """
            SELECT c.*, pc.persona_id, pc.status AS connection_status
            FROM plum_conversations c
            JOIN plum_connections pc
              ON pc.id=c.connection_id
             AND pc.platform_user_id=c.platform_user_id
             AND pc.character_id=c.character_id
            WHERE c.id=? AND c.platform_user_id=?
            FOR UPDATE OF c, pc
            """,
            (conversation_id, platform_user_id),
        ).fetchone()
        if current is None:
            raise ValueError("conversation not found")

        replay = conn.execute(
            """
            SELECT * FROM plum_connections
            WHERE platform_user_id=? AND creation_idempotency_key=?
            """,
            (platform_user_id, key),
        ).fetchone()
        if replay is not None:
            if str(replay["replaces_connection_id"] or "") != str(
                current["connection_id"]
            ):
                raise PlumConflictError("creation idempotency key conflict")
            replay_conversation = conn.execute(
                """
                SELECT id FROM plum_conversations
                WHERE connection_id=? AND platform_user_id=? AND status='active'
                """,
                (replay["id"], platform_user_id),
            ).fetchone()
            if replay_conversation is None:
                raise RuntimeError("restarted conversation not found")
            row = _conversation_row(
                conn, str(replay_conversation["id"]), platform_user_id
            )
            if row is None:
                raise ValueError("character not found")
            return _decode_conversation(row)

        if (
            str(current["status"]) != "active"
            or str(current["connection_status"]) != "active"
        ):
            raise ValueError("conversation not active")
        character = conn.execute(
            "SELECT * FROM plum_characters WHERE id=? AND status='active'",
            (current["character_id"],),
        ).fetchone()
        if character is None:
            raise ValueError("character not found")

        conn.execute(
            """
            UPDATE plum_conversations SET status='archived',
                archived_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE connection_id=? AND platform_user_id=? AND status='active'
            """,
            (current["connection_id"], platform_user_id),
        )
        conn.execute(
            """
            UPDATE sessions SET status='closed',
                ended_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                close_reason='plum_restart', session_key=session_key || ':' || id,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND account_id=? AND status='active'
            """,
            (int(current["runtime_session_id"]), current["runtime_account_id"]),
        )
        conn.execute(
            """
            UPDATE plum_connection_relationship_state
            SET state='ended',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE connection_id=?
            """,
            (current["connection_id"],),
        )
        conn.execute(
            """
            UPDATE plum_storylines
            SET status='archived',
                archived_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE connection_id=? AND platform_user_id=? AND status='active'
            """,
            (current["connection_id"], platform_user_id),
        )
        conn.execute(
            """
            UPDATE plum_connection_runtime_bindings
            SET status='inactive',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE connection_id=? AND platform_user_id=? AND status='active'
            """,
            (current["connection_id"], platform_user_id),
        )
        conn.execute(
            """
            UPDATE runtime_ownerships
            SET status='inactive',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE runtime_account_id=? AND platform_user_id=?
              AND app_id='plum' AND source_type='plum_connection'
              AND source_id=? AND status='active'
            """,
            (
                current["runtime_account_id"],
                platform_user_id,
                current["connection_id"],
            ),
        )
        conn.execute(
            """
            UPDATE accounts
            SET status='disabled',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND app_id='plum' AND status='active'
            """,
            (current["runtime_account_id"],),
        )
        conn.execute(
            """
            UPDATE plum_connections
            SET status='archived', blocked_reason=NULL,
                archived_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND platform_user_id=? AND status='active'
            """,
            (current["connection_id"], platform_user_id),
        )
        replacement = _create_connection_in_conn(
            conn,
            platform_user_id=platform_user_id,
            persona_id=str(current["persona_id"]),
            character=character,
            creation_reason="restart",
            creation_idempotency_key=key,
            replaces_connection_id=str(current["connection_id"]),
        )
        return _create_conversation_for_connection_in_conn(
            conn,
            connection=replacement,
            character=character,
            model_profile=str(current["model_profile"]),
        )


__all__ = [
    "PlumConflictError",
    "create_or_get_conversation", "get_character_experience",
    "get_conversation", "get_entry_account_id", "get_model_profile",
    "list_characters", "list_conversation_messages", "list_model_profiles",
    "list_user_conversations",
    "create_plum_access_invite", "redeem_plum_access_invite",
    "publish_created_character",
    "restart_conversation", "seed_plum_catalog", "seed_plum_dev",
    "set_character_favorite",
    "set_character_like", "touch_conversation", "update_conversation_model",
]
