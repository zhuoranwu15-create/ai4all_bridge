"""Plum 产品私有数据访问；消息正文继续使用共享 sessions/messages。"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.bootstrap.product_registry import PLUM_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.config import settings
from app.db import (
    connect,
    ensure_runtime_ownership,
    grant_new_user_shells,
    insert_resident_runtime_account,
    list_session_messages_before,
)
from app.db._backend import is_postgres
from app.db.product_memberships import _ensure_product_membership_in_conn
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


def _seed_catalog_in_conn(conn, *, platform_user_id: Optional[str] = None) -> None:
    conn.execute(
        """
        UPDATE plum_characters SET status='archived',
            updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
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
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        ON CONFLICT(id) DO UPDATE SET
            platform_user_id=COALESCE(excluded.platform_user_id, plum_public_profiles.platform_user_id),
            display_name=excluded.display_name,
            status='active',
            updated_at=excluded.updated_at
        """,
        (platform_user_id,),
    )
    for index, item in enumerate(REFERENCE_CHARACTERS, start=1):
        conn.execute(
            """
            INSERT INTO plum_characters(
                id, display_name, tagline, intro, greeting, tags_json,
                heat_count, avatar_ref, cover_ref, accent_color, persona_prompt,
                scenario_prompt, speaking_style, prompt_version, status,
                sort_order, creator_profile_id, content_rating,
                capabilities_json, fixture_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?,
                    ?, 'mature', ?, ?,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                tagline=excluded.tagline,
                intro=excluded.intro,
                greeting=excluded.greeting,
                tags_json=excluded.tags_json,
                heat_count=excluded.heat_count,
                avatar_ref=excluded.avatar_ref,
                cover_ref=excluded.cover_ref,
                accent_color=excluded.accent_color,
                persona_prompt=excluded.persona_prompt,
                scenario_prompt=excluded.scenario_prompt,
                speaking_style=excluded.speaking_style,
                sort_order=excluded.sort_order,
                creator_profile_id=excluded.creator_profile_id,
                content_rating=excluded.content_rating,
                capabilities_json=excluded.capabilities_json,
                fixture_version=excluded.fixture_version,
                status='active',
                updated_at=excluded.updated_at
            """,
            (
                item["id"], item["display_name"], item["tagline"], item["intro"],
                item["greeting"], json.dumps(item["tags"], ensure_ascii=False),
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

    for badge in BADGES:
        conn.execute(
            """
            INSERT INTO plum_character_badges(
                id, code, display_name, style_token, status, updated_at
            )
            VALUES (?, ?, ?, ?, 'active',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        ON CONFLICT(id) DO UPDATE SET
            display_name=excluded.display_name,
            description=excluded.description,
            prompt_text=excluded.prompt_text,
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
    *, platform_user_id: str, display_name: str
) -> Dict[str, Any]:
    """Ensure one invited user has membership, runtime ownership and 1000 coins."""

    cleaned_name = str(display_name or "").strip()[:40] or "Plum 测试用户"
    account_id = _entry_account_id(platform_user_id)
    with connect() as conn:
        user = conn.execute(
            "SELECT id, status FROM platform_users WHERE id=?", (platform_user_id,)
        ).fetchone()
        if user is None or str(user["status"]) != "active":
            raise ValueError("platform_user_not_active")
        _ensure_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        conn.execute(
            """
            INSERT INTO accounts(
                id, channel, display_name, status, onboarding_state, app_id, updated_at
            )
            VALUES (?, 'native', ?, 'active', 'complete', ?,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
            VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
        _seed_catalog_in_conn(conn)
    grant = grant_new_user_shells(
        account_id=account_id,
        platform_user_id=platform_user_id,
        registry=PRODUCTION_PRODUCT_REGISTRY,
    )
    return {
        "platform_user_id": platform_user_id,
        "entry_account_id": account_id,
        "display_name": cleaned_name,
        "grant_balance_shells": grant["balance_after_shells"],
    }


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
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
    lock = " FOR UPDATE" if is_postgres() else ""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, platform_user_id FROM plum_access_invites
            WHERE code_hash=? AND status='active'
              AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                        strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                """,
                (platform_user_id, phone, cleaned_name),
            )
            claimed = conn.execute(
                """
                UPDATE plum_access_invites
                SET platform_user_id=?, claimed_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    last_used_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
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
                SET display_name=?, updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id=? AND status='active'
                """,
                (cleaned_name, platform_user_id),
            )
            conn.execute(
                """
                UPDATE plum_access_invites
                SET last_used_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id=?
                """,
                (row["id"],),
            )
        _ensure_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
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
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
            FROM plum_characters WHERE status='active'
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
                  AND (a.starts_at IS NULL OR a.starts_at <= strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                  AND (a.ends_at IS NULL OR a.ends_at > strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
        JOIN plum_characters ch ON ch.id=c.character_id
        WHERE c.id=? AND c.platform_user_id=? AND c.status='active'
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


def create_or_get_conversation(
    *, platform_user_id: str, character_id: str
) -> Dict[str, Any]:
    with connect() as conn:
        existing = conn.execute(
            """
            SELECT id FROM plum_conversations
            WHERE platform_user_id=? AND character_id=? AND status='active'
            """,
            (platform_user_id, character_id),
        ).fetchone()
        if existing is not None:
            return _decode_conversation(
                _conversation_row(conn, str(existing["id"]), platform_user_id)
            )
        character = conn.execute(
            "SELECT * FROM plum_characters WHERE id=? AND status='active'",
            (character_id,),
        ).fetchone()
        if character is None:
            raise ValueError("character not found")
        # SQLite 的只读 SELECT 不会开启事务；persona account + binding + session +
        # conversation 必须处于同一个显式事务。PG 首条查询已自动开启事务。
        if not is_postgres() and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO plum_user_character_relationships(
                platform_user_id, character_id, state, updated_at
            )
            VALUES (?, ?, 'connected',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(platform_user_id, character_id) DO UPDATE SET
                state='connected', updated_at=excluded.updated_at
            """,
            (platform_user_id, character_id),
        )
        binding = conn.execute(
            """
            SELECT * FROM plum_character_bindings
            WHERE platform_user_id=? AND character_id=? AND status='active'
            """,
            (platform_user_id, character_id),
        ).fetchone()
        if binding is None:
            soul = "\n\n".join(
                part for part in (
                    str(character["persona_prompt"] or "").strip(),
                    str(character["scenario_prompt"] or "").strip(),
                    str(character["speaking_style"] or "").strip(),
                ) if part
            )
            runtime = insert_resident_runtime_account(
                platform_user_id=platform_user_id,
                display_name=str(character["display_name"]),
                initial_channel="native",
                app_id=PLUM_APP_ID,
                soul_seed=soul,
                identity_seed=f"# 身份\n\n名字：{character['display_name']}\n",
                registry=PRODUCTION_PRODUCT_REGISTRY,
                conn=conn,
            )
            runtime_account_id = str(runtime["account"]["id"])
            ensure_runtime_ownership(
                runtime_account_id=runtime_account_id,
                platform_user_id=platform_user_id,
                app_id=PLUM_APP_ID,
                source_type="character_binding",
                source_id=f"{platform_user_id}:{character_id}",
                conn=conn,
            )
            conn.execute(
                """
                INSERT INTO plum_character_bindings(
                    platform_user_id, character_id, runtime_account_id,
                    character_prompt_version, status, updated_at
                )
                VALUES (?, ?, ?, ?, 'active',
                        strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                """,
                (
                    platform_user_id, character_id, runtime_account_id,
                    int(character["prompt_version"]),
                ),
            )
        else:
            runtime_account_id = str(binding["runtime_account_id"])
        conn.execute(
            """
            INSERT INTO sessions(
                account_id, session_key, sender_id, sender_name, status,
                business_day, metadata_json, updated_at
            )
            VALUES (?, ?, ?, 'Plum Test User', 'active',
                    date('now', '+8 hours'), '{}',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id, session_key) DO UPDATE SET
                status='active', ended_at=NULL, close_reason=NULL,
                updated_at=excluded.updated_at
            """,
            (runtime_account_id, _ACTIVE_SESSION_KEY, platform_user_id),
        )
        session = conn.execute(
            "SELECT id FROM sessions WHERE account_id=? AND session_key=?",
            (runtime_account_id, _ACTIVE_SESSION_KEY),
        ).fetchone()
        default_model = conn.execute(
            """
            SELECT profile FROM plum_model_profiles
            WHERE enabled=1 ORDER BY is_default DESC, coin_cost_micros LIMIT 1
            """
        ).fetchone()
        conversation_id = f"fconv_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO plum_conversations(
                id, platform_user_id, character_id, runtime_account_id,
                runtime_session_id, model_profile, status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                conversation_id, platform_user_id, character_id, runtime_account_id,
                int(session["id"]), str(default_model["profile"]),
            ),
        )
        return _decode_conversation(
            _conversation_row(conn, conversation_id, platform_user_id)
        )


def get_conversation(
    *, conversation_id: str, platform_user_id: str
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = _conversation_row(conn, conversation_id, platform_user_id)
    return _decode_conversation(row) if row else None


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
            SELECT character_id, current_chapter_no
            FROM plum_conversations
            WHERE id=? AND platform_user_id=? AND status='active'
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
            FROM plum_user_character_relationships
            WHERE platform_user_id=? AND character_id=? AND state='connected'
            """,
            (platform_user_id, character_id),
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
            WHERE platform_user_id=? AND status='active' AND is_default=1
            """,
            (platform_user_id,),
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
            "SELECT 1 FROM plum_characters WHERE id=? AND status='active'",
            (character_id,),
        ).fetchone()
        if character is None:
            raise ValueError("character not found")
        if not is_postgres() and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
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
                        updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
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
                        updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
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
    return list_session_messages_before(
        account_id=str(conversation["runtime_account_id"]),
        session_id=int(conversation["runtime_session_id"]),
        limit=limit,
    )


def update_conversation_model(
    *, conversation_id: str, platform_user_id: str, model_profile: str
) -> Dict[str, Any]:
    if get_model_profile(model_profile) is None:
        raise ValueError("model profile not found")
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE plum_conversations SET model_profile=?,
                updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id=? AND platform_user_id=? AND status='active'
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
            UPDATE plum_conversations SET
                updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id=? AND platform_user_id=? AND status='active'
            """,
            (conversation_id, platform_user_id),
        )


def restart_conversation(
    *, conversation_id: str, platform_user_id: str
) -> Dict[str, Any]:
    with connect() as conn:
        current = _conversation_row(conn, conversation_id, platform_user_id)
        if current is None:
            raise ValueError("conversation not found")
        conn.execute(
            """
            UPDATE plum_conversations SET status='archived',
                archived_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id=? AND platform_user_id=? AND status='active'
            """,
            (conversation_id, platform_user_id),
        )
        session_id = int(current["runtime_session_id"])
        conn.execute(
            """
            UPDATE sessions SET status='closed', ended_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                close_reason='plum_restart', session_key=session_key || ':' || id,
                updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id=? AND account_id=? AND status='active'
            """,
            (session_id, current["runtime_account_id"]),
        )
        conn.execute(
            """
            INSERT INTO sessions(
                account_id, session_key, sender_id, sender_name, status,
                business_day, metadata_json, updated_at
            )
            VALUES (?, ?, ?, 'Plum Test User', 'active', date('now', '+8 hours'), '{}',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (current["runtime_account_id"], _ACTIVE_SESSION_KEY, platform_user_id),
        )
        session = conn.execute(
            "SELECT id FROM sessions WHERE account_id=? AND session_key=?",
            (current["runtime_account_id"], _ACTIVE_SESSION_KEY),
        ).fetchone()
        new_id = f"fconv_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO plum_conversations(
                id, platform_user_id, character_id, runtime_account_id,
                runtime_session_id, model_profile, status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                new_id, platform_user_id, current["character_id"],
                current["runtime_account_id"], int(session["id"]),
                current["model_profile"],
            ),
        )
        return _decode_conversation(_conversation_row(conn, new_id, platform_user_id))


__all__ = [
    "create_or_get_conversation", "get_character_experience",
    "get_conversation", "get_entry_account_id", "get_model_profile",
    "list_characters", "list_conversation_messages", "list_model_profiles",
    "create_plum_access_invite", "redeem_plum_access_invite",
    "restart_conversation", "seed_plum_catalog", "seed_plum_dev",
    "set_character_favorite",
    "set_character_like", "touch_conversation", "update_conversation_model",
]
