"""Fibre 产品私有数据访问；消息正文继续使用共享 sessions/messages。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from app.bootstrap.product_registry import FIBRE_APP_ID, PRODUCTION_PRODUCT_REGISTRY
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

_ACTIVE_SESSION_KEY = "__app_active__"

_CHARACTERS = (
    {
        "id": "char_luna",
        "display_name": "露娜",
        "tagline": "在月色和旧唱片里，听你慢慢说",
        "intro": "温柔但不敷衍的深夜电台主播。她擅长接住情绪，也会认真记住你话里的小细节。",
        "greeting": "晚上好。这里的灯刚刚亮起来，你今天想从哪一段说起？",
        "tags": ["温柔陪伴", "深夜电台", "治愈"],
        "heat_count": 12840,
        "accent_color": "#8b5cf6",
        "persona_prompt": (
            "你是露娜，一位深夜电台主播。你温柔、敏锐、有边界感，不说空泛鸡汤。"
            "先理解用户真实感受，再自然回应；一次通常只追问一个问题。"
        ),
        "scenario_prompt": "你和用户在安静的深夜电台直播间里一对一聊天。",
        "speaking_style": "自然、克制、略带诗意；避免长篇说教。",
        "sort_order": 10,
    },
    {
        "id": "char_kai",
        "display_name": "凯",
        "tagline": "嘴上不饶人，行动永远站在你这边",
        "intro": "看起来有点酷的城市摄影师。会开玩笑、会直接指出问题，但从不轻视你的感受。",
        "greeting": "你终于来了。我刚拍完一卷照片——不过先说说你吧，今天过得怎么样？",
        "tags": ["轻松日常", "直球", "摄影师"],
        "heat_count": 9360,
        "accent_color": "#f97316",
        "persona_prompt": (
            "你是凯，一位城市摄影师。你幽默、直接、可靠，偶尔轻微吐槽但绝不刻薄。"
            "像熟悉的朋友一样回应，具体、鲜活，不使用客服腔。"
        ),
        "scenario_prompt": "你刚结束一天的街头拍摄，正和用户在咖啡店聊天。",
        "speaking_style": "短句、口语化、有一点机灵；必要时给出明确建议。",
        "sort_order": 20,
    },
)


def _seed_catalog_in_conn(conn) -> None:
    for item in _CHARACTERS:
        conn.execute(
            """
            INSERT INTO fibre_characters(
                id, display_name, tagline, intro, greeting, tags_json,
                heat_count, accent_color, persona_prompt, scenario_prompt,
                speaking_style, prompt_version, status, sort_order, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                tagline=excluded.tagline,
                intro=excluded.intro,
                greeting=excluded.greeting,
                tags_json=excluded.tags_json,
                heat_count=excluded.heat_count,
                accent_color=excluded.accent_color,
                persona_prompt=excluded.persona_prompt,
                scenario_prompt=excluded.scenario_prompt,
                speaking_style=excluded.speaking_style,
                sort_order=excluded.sort_order,
                updated_at=excluded.updated_at
            """,
            (
                item["id"], item["display_name"], item["tagline"], item["intro"],
                item["greeting"], json.dumps(item["tags"], ensure_ascii=False),
                item["heat_count"], item["accent_color"], item["persona_prompt"],
                item["scenario_prompt"], item["speaking_style"], item["sort_order"],
            ),
        )
    profiles = (
        ("fast", settings.fibre_fast_provider_id, "快速", "响应更快，适合轻松日常", 1_000_000, 0),
        ("balanced", settings.fibre_balanced_provider_id, "均衡", "质量与速度兼顾", 3_000_000, 1),
        ("immersive", settings.fibre_immersive_provider_id, "沉浸", "更细腻、更有角色感", 5_000_000, 0),
    )
    for profile in profiles:
        conn.execute(
            """
            INSERT INTO fibre_model_profiles(
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
                config_version=fibre_model_profiles.config_version+1,
                updated_at=excluded.updated_at
            """,
            profile,
        )


def seed_fibre_dev() -> Dict[str, Any]:
    """幂等准备固定测试用户、membership、入口账号、目录与 1000 金币。"""

    user_id = str(settings.fibre_test_user_id).strip()
    phone = str(settings.fibre_test_phone).strip()
    if not user_id or not phone:
        raise ValueError("FIBRE_TEST_USER_ID and FIBRE_TEST_PHONE are required")
    entry_account_id = "aid_fibre_test"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, status, updated_at)
            VALUES (?, ?, 'Fibre 测试用户', 'active',
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
            app_id=FIBRE_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        conn.execute(
            """
            INSERT INTO accounts(id, channel, display_name, status, onboarding_state, app_id, updated_at)
            VALUES (?, 'native', 'Fibre Test Entry', 'active', 'complete', ?,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO NOTHING
            """,
            (entry_account_id, FIBRE_APP_ID),
        )
        account = conn.execute(
            "SELECT app_id FROM accounts WHERE id=?", (entry_account_id,)
        ).fetchone()
        if account is None or str(account["app_id"]) != FIBRE_APP_ID:
            raise ValueError("fixed Fibre entry account conflicts with existing account")
        conn.execute(
            """
            INSERT INTO profiles(account_id, display_name, updated_at)
            VALUES (?, 'Fibre Test Entry',
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id) DO NOTHING
            """,
            (entry_account_id,),
        )
        ensure_runtime_ownership(
            runtime_account_id=entry_account_id,
            platform_user_id=user_id,
            app_id=FIBRE_APP_ID,
            source_type="product_entry",
            source_id=user_id,
            conn=conn,
        )
        _seed_catalog_in_conn(conn)
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
            (platform_user_id, FIBRE_APP_ID, platform_user_id),
        ).fetchone()
    if row is None:
        raise ValueError("fibre dev seed required")
    return str(row["runtime_account_id"])


def list_characters() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, display_name, tagline, intro, greeting, tags_json,
                   heat_count, avatar_ref, cover_ref, accent_color, prompt_version
            FROM fibre_characters
            WHERE status='active'
            ORDER BY sort_order, id
            """
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        items.append(item)
    return items


def list_model_profiles() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT profile, display_name, description, coin_cost_micros,
                   is_default, config_version
            FROM fibre_model_profiles WHERE enabled=1
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
            "SELECT * FROM fibre_model_profiles WHERE profile=? AND enabled=1",
            (profile,),
        ).fetchone()
    return dict(row) if row else None


def _conversation_row(conn, conversation_id: str, platform_user_id: str):
    return conn.execute(
        """
        SELECT c.*, ch.display_name, ch.tagline, ch.intro, ch.greeting,
               ch.tags_json, ch.heat_count, ch.avatar_ref, ch.cover_ref,
               ch.accent_color
        FROM fibre_conversations c
        JOIN fibre_characters ch ON ch.id=c.character_id
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
            "avatar_ref", "cover_ref", "accent_color",
        )
    }
    item["character"]["id"] = item["character_id"]
    item["character"]["tags"] = json.loads(item.pop("tags_json") or "[]")
    return item


def create_or_get_conversation(
    *, platform_user_id: str, character_id: str
) -> Dict[str, Any]:
    with connect() as conn:
        existing = conn.execute(
            """
            SELECT id FROM fibre_conversations
            WHERE platform_user_id=? AND character_id=? AND status='active'
            """,
            (platform_user_id, character_id),
        ).fetchone()
        if existing is not None:
            return _decode_conversation(
                _conversation_row(conn, str(existing["id"]), platform_user_id)
            )
        character = conn.execute(
            "SELECT * FROM fibre_characters WHERE id=? AND status='active'",
            (character_id,),
        ).fetchone()
        if character is None:
            raise ValueError("character not found")
        # SQLite 的只读 SELECT 不会开启事务；persona account + binding + session +
        # conversation 必须处于同一个显式事务。PG 首条查询已自动开启事务。
        if not is_postgres() and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        binding = conn.execute(
            """
            SELECT * FROM fibre_character_bindings
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
                app_id=FIBRE_APP_ID,
                soul_seed=soul,
                identity_seed=f"# 身份\n\n名字：{character['display_name']}\n",
                registry=PRODUCTION_PRODUCT_REGISTRY,
                conn=conn,
            )
            runtime_account_id = str(runtime["account"]["id"])
            ensure_runtime_ownership(
                runtime_account_id=runtime_account_id,
                platform_user_id=platform_user_id,
                app_id=FIBRE_APP_ID,
                source_type="character_binding",
                source_id=f"{platform_user_id}:{character_id}",
                conn=conn,
            )
            conn.execute(
                """
                INSERT INTO fibre_character_bindings(
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
            VALUES (?, ?, ?, 'Fibre Test User', 'active',
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
            SELECT profile FROM fibre_model_profiles
            WHERE enabled=1 ORDER BY is_default DESC, coin_cost_micros LIMIT 1
            """
        ).fetchone()
        conversation_id = f"fconv_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO fibre_conversations(
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
            UPDATE fibre_conversations SET model_profile=?,
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
            UPDATE fibre_conversations SET
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
            UPDATE fibre_conversations SET status='archived',
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
                close_reason='fibre_restart', session_key=session_key || ':' || id,
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
            VALUES (?, ?, ?, 'Fibre Test User', 'active', date('now', '+8 hours'), '{}',
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
            INSERT INTO fibre_conversations(
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
    "create_or_get_conversation", "get_conversation", "get_entry_account_id",
    "get_model_profile", "list_characters", "list_conversation_messages",
    "list_model_profiles", "restart_conversation", "seed_fibre_dev",
    "touch_conversation", "update_conversation_model",
]
