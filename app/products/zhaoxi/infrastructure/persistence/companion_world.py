"""app.products.zhaoxi.infrastructure.persistence.companion_world — 朝夕相伴 P1 多居民数据访问层（M2-A 数据基座）。

承载 universe / character_template / universe_resident / ai_conversation /
universe_memory_facts（L3）五张 P1 表的底层 repo 原语。见
docs/tech_design/companion_world_p1_backend_spec.md §2 与 ADR §6/§7.3/D-05/D-06。

分层归属：本模块是**数据层**（app.db.*），非领域层——领域层
（app.products.zhaoxi.domain.companion_world）受 tests/test_layer_boundaries.py 门禁约束不得直接
import 本模块，须经 app.agent_runtime 端口。二者同名不同包、互不干涉。

M2-A 只落存储 + 纯 DB 原语（无 live 调用方、零行为变更）；L3 读注入/sink 路由（M2-B）、
居民 bootstrap/confirm/backfill（M2-C，待 ADR §10.1/.2/.6 产品冻结）为后续刀。
"""
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.db._backend import Connection, is_postgres
from app.db._core import (
    APP_ACTIVE_SESSION_KEY,
    _new_id,
    _tx,
    advisory_lock_key,
    connect,
)
from app.platform.media.view import stored_content_preview

LEGACY_CHARACTER_TEMPLATE_ID = "tmpl_legacy"

# 微信侧带入居民的默认展示名。仅在该账号 profiles.display_name 为空时生效
# （用户在微信 onboarding 里起过名就沿用那个名字），并只作用于 App 展示，
# 不回写 profiles，因此不会改变微信侧 AI 的自称。
LEGACY_RESIDENT_DEFAULT_NAME = "来自微信的Bot"

__all__ = [
    "get_or_create_home_universe",
    "get_universe",
    "lock_universe",
    "set_universe_onboarding_state",
    "mark_universe_legacy_confirmed",
    "mark_universe_legacy_primary",
    "create_character_template",
    "get_template",
    "list_initial_character_templates",
    "get_available_character_template",
    "get_character_template_for_owner",
    "get_or_create_legacy_template",
    "insert_resident_draft",
    "get_resident_draft_by_token",
    "get_resident_draft_by_client_request",
    "consume_resident_draft",
    "create_resident",
    "get_or_create_candidate_resident",
    "list_candidate_residents",
    "activate_candidate_resident",
    "dismiss_unselected_candidate_residents",
    "get_or_create_legacy_resident",
    "count_active_residents",
    "list_residents",
    "list_resident_details_for_owner",
    "create_ai_conversation",
    "insert_resident_welcome_message",
    "get_conversation",
    "resolve_conversation_for_owner",
    "list_conversations_for_owner",
    "try_conversation_transaction_lock",
    "try_conversation_turn_lock",
    "list_active_account_ids_for_user",
    "list_human_proactive_account_ids_for_user",
    "get_human_proactive_owner_scope",
    "get_owner_last_inbound_at",
    "count_owner_inbound_after",
    "has_legacy_primary_weixin_route",
    "select_human_app_speaker",
    "resolve_resident_memory_scope",
    "resolve_resident_proactive_scope",
    "append_universe_fact",
    "read_universe_facts",
    "list_universe_ids_for_memory_compact",
    "compact_universe_facts",
    "claim_ai_feed_slot",
    "get_universe_post_for_owner",
    "publish_user_feed_post_with_outbox",
    "publish_resident_intro_post_with_outbox",
    "list_published_feed_posts_for_owner",
    "retire_feed_post_with_outbox",
    "publish_ai_feed_post_with_outbox",
    "claim_companion_world_outbox",
    "get_companion_world_outbox_metrics",
    "list_ai_feed_eligible_worlds",
    "select_ai_feed_author",
    "get_ai_feed_author",
    "defer_ai_feed_post",
    "skip_ai_feed_post",
    "close_expired_ai_feed_slots",
    "complete_companion_world_outbox",
    "fail_companion_world_outbox",
]

_SQLITE_CONVERSATION_LOCKS: Dict[str, threading.Lock] = {}
_SQLITE_CONVERSATION_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# universe（一真人一 home world，§2.1）
# ---------------------------------------------------------------------------
def get_or_create_home_universe(
    *, platform_user_id: str, conn: Optional[Connection] = None
) -> Dict[str, Any]:
    """按真人 get-or-create home world，返回 universe 行 dict。

    幂等：靠 `UNIQUE(owner_platform_user_id)`（§2.1）——重复调用命中同一行、不新建
    （ON CONFLICT DO NOTHING 后 reselect），是 POST /worlds/home/bootstrap 幂等的硬保证。
    """
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO universes(id, owner_platform_user_id, status, onboarding_state)
            VALUES (?, ?, 'active', 'preparing')
            ON CONFLICT(owner_platform_user_id) DO NOTHING
            """,
            (_new_id("uni"), platform_user_id),
        )
        row = tx.execute(
            "SELECT * FROM universes WHERE owner_platform_user_id = ?",
            (platform_user_id,),
        ).fetchone()
    if row is None:  # 理论不可达（刚插/已存），防御式
        raise RuntimeError("home universe was not created")
    return dict(row)


def get_universe(
    *,
    universe_id: Optional[str] = None,
    owner_platform_user_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 universe_id 或 owner_platform_user_id 读一个 universe（二选一，均缺则报错）。"""
    if not universe_id and not owner_platform_user_id:
        raise ValueError("need universe_id or owner_platform_user_id")
    with _tx(conn) as tx:
        if universe_id:
            row = tx.execute(
                "SELECT * FROM universes WHERE id = ?", (universe_id,)
            ).fetchone()
        else:
            row = tx.execute(
                "SELECT * FROM universes WHERE owner_platform_user_id = ?",
                (owner_platform_user_id,),
            ).fetchone()
    return dict(row) if row else None


def lock_universe(*, universe_id: str, conn: Connection) -> Optional[Dict[str, Any]]:
    """在调用方事务内读取并锁住 world 行；PG 用 ``FOR UPDATE``，SQLite 验功能语义。"""
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = conn.execute(
        "SELECT * FROM universes WHERE id = ?" + suffix,
        (universe_id,),
    ).fetchone()
    return dict(row) if row else None


def set_universe_onboarding_state(
    *, universe_id: str, onboarding_state: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """更新一个 world 的 onboarding 状态并返回该行；调用方负责状态机合法性。"""
    with _tx(conn) as tx:
        tx.execute(
            """
            UPDATE universes
            SET onboarding_state = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (onboarding_state, universe_id),
        )
        row = tx.execute("SELECT * FROM universes WHERE id = ?", (universe_id,)).fetchone()
    return dict(row) if row else None


def mark_universe_legacy_primary(
    *,
    universe_id: str,
    legacy_primary_account_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """幂等写入 legacy primary 锚，但**不**改动 onboarding_state。

    D-A（2026-07-26）之后，微信老用户在 App 侧照常走选择角色页，所以带入既有角色时
    只落主账号锚（微信主动消息路由靠它），不再顺带把世界标成 confirmed。
    ``COALESCE`` 保证重复 bootstrap 不会改写已有锚。
    """
    with _tx(conn) as tx:
        tx.execute(
            """
            UPDATE universes
            SET legacy_primary_account_id = COALESCE(legacy_primary_account_id, ?),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (legacy_primary_account_id, universe_id),
        )
        row = tx.execute("SELECT * FROM universes WHERE id = ?", (universe_id,)).fetchone()
    return dict(row) if row else None


def mark_universe_legacy_confirmed(
    *,
    universe_id: str,
    legacy_primary_account_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """幂等写入 legacy primary 锚并把老用户 world 标为 confirmed。"""
    with _tx(conn) as tx:
        tx.execute(
            """
            UPDATE universes
            SET legacy_primary_account_id = COALESCE(legacy_primary_account_id, ?),
                onboarding_state = 'confirmed',
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (legacy_primary_account_id, universe_id),
        )
        row = tx.execute("SELECT * FROM universes WHERE id = ?", (universe_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# character_template（模板≠runtime account，§2.2）
# ---------------------------------------------------------------------------
def create_character_template(
    *,
    source_type: str,
    name: str,
    template_id: Optional[str] = None,
    owner_platform_user_id: Optional[str] = None,
    avatar_ref: Optional[str] = None,
    summary: Optional[str] = None,
    tags_json: Optional[str] = None,
    persona_seed_json: Optional[str] = None,
    persona_version: str = "v1",
    status: str = "active",
    initial_candidate_rank: Optional[int] = None,
    persona_key: Optional[str] = None,
    long_summary: Optional[str] = None,
    relationship_type: Optional[str] = None,
    personality_traits_json: Optional[str] = None,
    name_pool_json: Optional[str] = None,
    name_pool_version: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """新建一个角色模板，返回行 dict。source_type ∈ official|operations|user_created|generated。

    persona_seed_json 是实例化时写入 runtime account 的 SOUL/IDENTITY 种子，**绝不进 App DTO**
    （§2.2 / 客户端 §4.2）——candidates 端点须显式剔除该列。

    m0048 起额外承载结构化设定：``persona_key``（跨模板版本稳定的人设身份）、``long_summary``、
    ``relationship_type`` 与 ``personality_traits_json``；m0049 起再加 ``name_pool_json`` /
    ``name_pool_version``（运营实例名池，NAME-001）；均可空，老调用方无需改。
    """
    resolved_template_id = template_id or _new_id("tmpl")
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO character_templates(
                id, source_type, owner_platform_user_id, name, avatar_ref, summary,
                tags_json, persona_seed_json, persona_version, status, initial_candidate_rank,
                persona_key, long_summary, relationship_type, personality_traits_json,
                name_pool_json, name_pool_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_template_id,
                source_type,
                owner_platform_user_id,
                name,
                avatar_ref,
                summary,
                tags_json,
                persona_seed_json,
                persona_version,
                status,
                initial_candidate_rank,
                persona_key,
                long_summary,
                relationship_type,
                personality_traits_json,
                name_pool_json,
                name_pool_version,
            ),
        )
        row = tx.execute(
            "SELECT * FROM character_templates WHERE id = ?", (resolved_template_id,)
        ).fetchone()
    return dict(row)


def get_template(
    *, template_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """按 id 读一个角色模板；不存在返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM character_templates WHERE id = ?", (template_id,)
        ).fetchone()
    return dict(row) if row else None


def list_initial_character_templates(
    *, conn: Optional[Connection] = None
) -> List[Dict[str, Any]]:
    """返回 active 且设置 initial rank 的运营目录，按 rank 升序；完整性由领域层校验。"""
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT * FROM character_templates
            WHERE status = 'active' AND initial_candidate_rank IS NOT NULL
            ORDER BY initial_candidate_rank ASC, id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_available_character_template(
    *,
    template_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """读取可直接新增的 active 模板；user_created 模板只允许其 owner 使用。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT * FROM character_templates
            WHERE id = ? AND status = 'active'
              AND (owner_platform_user_id IS NULL OR owner_platform_user_id = ?)
            """,
            (template_id, owner_platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_character_template_for_owner(
    *,
    template_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 owner 可见性读取任意状态模板；他人私有模板与不存在统一返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT * FROM character_templates
            WHERE id = ?
              AND (owner_platform_user_id IS NULL OR owner_platform_user_id = ?)
            """,
            (template_id, owner_platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def insert_resident_draft(
    *,
    platform_user_id: str,
    draft_token: str,
    name: str,
    avatar_key: str,
    relationship_type: str,
    relationship_label: Optional[str],
    personality_traits_json: str,
    style_note: Optional[str],
    normalized_summary: str,
    persona_seed_json: str,
    safety_json: Optional[str],
    expires_at: str,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """落一条自建角色草稿。行内的文本必须**已过清洗器**，原文不入库。"""
    draft_id = _new_id("draft")
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO resident_drafts(
                id, platform_user_id, draft_token, name, avatar_key, relationship_type,
                relationship_label, personality_traits_json, style_note,
                normalized_summary, persona_seed_json, safety_json, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                platform_user_id,
                draft_token,
                name,
                avatar_key,
                relationship_type,
                relationship_label,
                personality_traits_json,
                style_note,
                normalized_summary,
                persona_seed_json,
                safety_json,
                expires_at,
            ),
        )
        row = tx.execute(
            "SELECT * FROM resident_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    return dict(row)


def get_resident_draft_by_token(
    *,
    draft_token: str,
    platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """owner-scoped 取草稿；他人的 token 与不存在统一返回 None（不泄漏存在性）。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM resident_drafts WHERE draft_token = ? AND platform_user_id = ?",
            (draft_token, platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_resident_draft_by_client_request(
    *,
    platform_user_id: str,
    client_request_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """IDEM-001：按 (platform_user_id, client_request_id) 找已消费草稿，供幂等重放。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT * FROM resident_drafts
            WHERE platform_user_id = ? AND client_request_id = ?
            """,
            (platform_user_id, client_request_id),
        ).fetchone()
    return dict(row) if row else None


def consume_resident_draft(
    *,
    draft_id: str,
    platform_user_id: str,
    client_request_id: str,
    resident_id: str,
    conn: Optional[Connection] = None,
) -> bool:
    """把草稿标为 consumed 并钉住结果；已被消费时返回 False（调用方走幂等回放）。

    WHERE 带 ``status='open'`` 使并发双发只有一笔能赢，另一笔回放同一 resident。
    """
    with _tx(conn) as tx:
        cursor = tx.execute(
            """
            UPDATE resident_drafts
            SET status = 'consumed',
                client_request_id = ?,
                resident_id = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ? AND platform_user_id = ? AND status = 'open'
            """,
            (client_request_id, resident_id, draft_id, platform_user_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0


def get_or_create_legacy_template(
    *, conn: Optional[Connection] = None
) -> Dict[str, Any]:
    """幂等建立带入专用哨兵模板；不含 persona，绝不改写既有账号人设。

    ``name`` 是 legacy 居民展示名的最后兜底（见 ``LEGACY_RESIDENT_DEFAULT_NAME``），
    命中既有哨兵串时就地自愈，运营改过的名字不覆盖。
    """
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO character_templates(
                id, source_type, name, persona_seed_json, persona_version, status
            ) VALUES (?, 'operations', ?, NULL, 'legacy', 'active')
            ON CONFLICT(id) DO NOTHING
            """,
            (LEGACY_CHARACTER_TEMPLATE_ID, LEGACY_RESIDENT_DEFAULT_NAME),
        )
        tx.execute(
            "UPDATE character_templates SET name = ? WHERE id = ? AND name = 'legacy'",
            (LEGACY_RESIDENT_DEFAULT_NAME, LEGACY_CHARACTER_TEMPLATE_ID),
        )
        row = tx.execute(
            "SELECT * FROM character_templates WHERE id = ?",
            (LEGACY_CHARACTER_TEMPLATE_ID,),
        ).fetchone()
    if row is None:
        raise RuntimeError("legacy template was not created")
    return dict(row)


# ---------------------------------------------------------------------------
# universe_resident（关系实例，容量真相 §2.3）
# ---------------------------------------------------------------------------
def create_resident(
    *,
    universe_id: str,
    character_template_id: str,
    template_version: str,
    origin: str,
    status: str = "candidate",
    runtime_account_id: Optional[str] = None,
    joined_at: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """新建一个 resident（关系实例），返回行 dict。

    origin ∈ preset|custom|mailbox|legacy；status ∈ candidate|active|offline|dismissed。
    runtime_account_id 激活后指向 account、candidate 期为空；偏唯一索引
    ux_universe_residents_runtime 保证一个 runtime account 至多一个 resident（§2.3）。
    容量真相 = status='active' 计数（D-07），不在本原语校验上限（由 confirm/建居民在
    world row lock 下判，M2-C）。
    """
    resident_id = _new_id("res")
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO universe_residents(
                id, universe_id, character_template_id, template_version,
                runtime_account_id, origin, status, joined_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resident_id,
                universe_id,
                character_template_id,
                template_version,
                runtime_account_id,
                origin,
                status,
                joined_at,
            ),
        )
        row = tx.execute(
            "SELECT * FROM universe_residents WHERE id = ?", (resident_id,)
        ).fetchone()
    return dict(row)


def get_or_create_candidate_resident(
    *,
    universe_id: str,
    character_template_id: str,
    template_version: str,
    origin: str,
    suggested_display_name: Optional[str] = None,
    naming_version: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """幂等快照一条非 legacy candidate；已存在 active/dismissed 关系也原样返回、不复活。

    ``suggested_display_name`` / ``naming_version`` 只随 INSERT 落一次（NAME-001）：
    ``ON CONFLICT DO NOTHING`` 意味着已存在的候选原样返回，选名算法或名池版本之后怎么变，
    都不会改写已经发给客户端的名字。
    """
    if origin == "legacy":
        raise ValueError("legacy origin is not a candidate")
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO universe_residents(
                id, universe_id, character_template_id, template_version, origin, status,
                suggested_display_name, naming_version
            ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                _new_id("res"),
                universe_id,
                character_template_id,
                template_version,
                origin,
                suggested_display_name,
                naming_version,
            ),
        )
        row = tx.execute(
            """
            SELECT * FROM universe_residents
            WHERE universe_id = ? AND character_template_id = ? AND origin <> 'legacy'
            """,
            (universe_id, character_template_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("candidate resident was not created")
    return dict(row)


def list_candidate_residents(
    *,
    universe_id: str,
    statuses: Sequence[str] = ("candidate",),
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出 world 的非 legacy 候选关系并附模板内部字段；严格按 universe_id 隔离。"""
    status_list = list(statuses)
    if not status_list:
        return []
    placeholders = ",".join("?" for _ in status_list)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT
                r.id AS resident_id, r.universe_id, r.character_template_id,
                r.template_version, r.runtime_account_id, r.origin, r.status,
                r.suggested_display_name, r.naming_version,
                t.source_type, t.owner_platform_user_id, t.name, t.avatar_ref,
                t.summary, t.tags_json, t.persona_seed_json, t.persona_version,
                t.status AS template_status, t.initial_candidate_rank,
                t.persona_key, t.long_summary, t.name_pool_json, t.name_pool_version,
                c.id AS conversation_id, c.state AS conversation_state
            FROM universe_residents r
            JOIN character_templates t ON t.id = r.character_template_id
            LEFT JOIN ai_conversations c ON c.resident_id = r.id
            WHERE r.universe_id = ? AND r.origin <> 'legacy'
              AND r.status IN ({placeholders})
            ORDER BY COALESCE(t.initial_candidate_rank, 999999) ASC, r.created_at ASC, r.id ASC
            """,
            (universe_id, *status_list),
        ).fetchall()
    return [dict(row) for row in rows]


def activate_candidate_resident(
    *,
    universe_id: str,
    resident_id: str,
    runtime_account_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """把本 world 的 candidate 原子激活并绑定 runtime；非 candidate 不做隐式状态跃迁。"""
    with _tx(conn) as tx:
        tx.execute(
            """
            UPDATE universe_residents
            SET runtime_account_id = ?, status = 'active',
                joined_at = COALESCE(joined_at, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ? AND universe_id = ?
              AND status = 'candidate' AND runtime_account_id IS NULL
            """,
            (runtime_account_id, resident_id, universe_id),
        )
        row = tx.execute(
            "SELECT * FROM universe_residents WHERE id = ? AND universe_id = ?",
            (resident_id, universe_id),
        ).fetchone()
    if row is None or row["status"] != "active" or row["runtime_account_id"] != runtime_account_id:
        return None
    return dict(row)


def dismiss_unselected_candidate_residents(
    *,
    universe_id: str,
    selected_template_ids: Sequence[str],
    conn: Optional[Connection] = None,
) -> int:
    """把未选择的 candidate 标为 dismissed；只作用于目标 world、绝不影响 active/legacy。"""
    selected = list(selected_template_ids)
    keep_clause = ""
    params: List[Any] = [universe_id]
    if selected:
        keep_clause = f"AND character_template_id NOT IN ({','.join('?' for _ in selected)})"
        params.extend(selected)
    with _tx(conn) as tx:
        cursor = tx.execute(
            f"""
            UPDATE universe_residents
            SET status = 'dismissed',
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE universe_id = ? AND status = 'candidate' AND origin <> 'legacy'
              {keep_clause}
            """,
            tuple(params),
        )
    return int(cursor.rowcount)


def get_or_create_legacy_resident(
    *,
    universe_id: str,
    runtime_account_id: str,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """幂等把一个既有 account 映射为 active legacy resident。"""
    template = get_or_create_legacy_template(conn=conn)
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO universe_residents(
                id, universe_id, character_template_id, template_version,
                runtime_account_id, origin, status, joined_at
            ) VALUES (
                ?, ?, ?, 'legacy', ?, 'legacy', 'active',
                strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            )
            ON CONFLICT DO NOTHING
            """,
            (_new_id("res"), universe_id, template["id"], runtime_account_id),
        )
        row = tx.execute(
            "SELECT * FROM universe_residents WHERE runtime_account_id = ?",
            (runtime_account_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("legacy resident was not created")
    result = dict(row)
    if str(result["universe_id"]) != str(universe_id) or result["origin"] != "legacy":
        raise ValueError("runtime account already belongs to another resident")
    return result


def count_active_residents(
    *, universe_id: str, conn: Optional[Connection] = None
) -> int:
    """返回该世界 status='active' 的居民数（容量真相，D-07；offline/dismissed 不计位）。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT COUNT(*) AS cnt FROM universe_residents "
            "WHERE universe_id = ? AND status = 'active'",
            (universe_id,),
        ).fetchone()
    return int(row["cnt"]) if row else 0


def list_residents(
    *,
    universe_id: str,
    statuses: Sequence[str] = ("active", "offline"),
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """按 universe_id 列出指定状态的居民（默认 active+offline），按创建时间升序。"""
    status_list = list(statuses)
    if not status_list:
        return []
    placeholders = ",".join("?" for _ in status_list)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"SELECT * FROM universe_residents "
            f"WHERE universe_id = ? AND status IN ({placeholders}) "
            f"ORDER BY created_at ASC, id ASC",
            (universe_id, *status_list),
        ).fetchall()
    return [dict(r) for r in rows]


def list_resident_details_for_owner(
    *,
    owner_platform_user_id: str,
    statuses: Sequence[str] = ("active", "offline"),
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """owner-scoped 列出居民与模板/conversation；越权 owner 得空列表。"""
    status_list = list(statuses)
    if not status_list:
        return []
    placeholders = ",".join("?" for _ in status_list)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT
                r.id AS resident_id, r.universe_id, r.character_template_id,
                r.template_version, r.runtime_account_id, r.origin, r.status,
                COALESCE(p.display_name, t.name) AS name, t.avatar_ref,
                t.persona_key,
                c.id AS conversation_id, c.state AS conversation_state
            FROM universe_residents r
            JOIN universes u ON u.id = r.universe_id
            JOIN character_templates t ON t.id = r.character_template_id
            LEFT JOIN profiles p ON p.account_id = r.runtime_account_id
            JOIN ai_conversations c ON c.resident_id = r.id
            WHERE u.owner_platform_user_id = ? AND r.status IN ({placeholders})
            ORDER BY r.created_at ASC, r.id ASC
            """,
            (owner_platform_user_id, *status_list),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# ai_conversation（跨 session 稳定会话 ID，§2.4）
# ---------------------------------------------------------------------------
def create_ai_conversation(
    *,
    universe_id: str,
    resident_id: str,
    owner_platform_user_id: str,
    runtime_account_id: str,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """为一个 resident 建稳定会话 ID，返回行 dict。

    幂等锚：`ux_ai_conversations_resident(resident_id)` 唯一（§2.4）——同一 resident 重复建
    命中冲突。本原语走 ON CONFLICT DO NOTHING 后 reselect，重放返回既有会话、不抛错。
    owner_platform_user_id 为越权校验锚（§3.4-2 越权→404）。
    """
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO ai_conversations(
                id, universe_id, resident_id, owner_platform_user_id,
                runtime_account_id, state
            )
            VALUES (?, ?, ?, ?, ?, 'active')
            ON CONFLICT(resident_id) DO NOTHING
            """,
            (
                _new_id("conv"),
                universe_id,
                resident_id,
                owner_platform_user_id,
                runtime_account_id,
            ),
        )
        row = tx.execute(
            "SELECT * FROM ai_conversations WHERE resident_id = ?", (resident_id,)
        ).fetchone()
    if row is None:  # 理论不可达
        raise RuntimeError("ai_conversation was not created")
    result = dict(row)
    expected = {
        "universe_id": universe_id,
        "owner_platform_user_id": owner_platform_user_id,
        "runtime_account_id": runtime_account_id,
    }
    if any(str(result[key]) != str(value) for key, value in expected.items()):
        raise ValueError("resident conversation ownership mismatch")
    return result


def insert_resident_welcome_message(
    *,
    runtime_account_id: str,
    resident_id: str,
    text: str,
    conn: Connection,
) -> bool:
    """在新居民的 App 会话里落一条欢迎语，作为第一条 assistant 消息（CONTENT-001）。

    必须在调用方事务内执行：建号、激活、建会话与这条消息要么一起成功要么一起回滚，绝不
    出现「有居民但没有开场白」的中间态。所以这里**不能**调
    ``get_or_create_account_active_session``——它自己 ``connect()`` 开新连接，SQLite 下与
    外层写事务互锁、PG 下看不到尚未提交的 account 行。

    改为就地 upsert ``__app_active__`` session：其余字段留空，等真实一轮对话用
    ``COALESCE`` 补齐；``business_day`` 留 NULL 也不会被判成跨业务日而触发会话轮转。

    幂等锚是 ``ux_messages_account_message``（UNIQUE(account_id, message_id)）：
    ``message_id`` 由 resident id 决定，重放时 ``insert_message`` 吞掉冲突返回 None。
    返回值表示**本次是否真的写入**，供调用方区分首次与重放。
    """
    from app.db.accounts import insert_message

    clean_text = str(text or "").strip()
    if not clean_text:
        return False
    conn.execute(
        """
        INSERT INTO sessions(account_id, session_key, metadata_json, updated_at)
        VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        ON CONFLICT(account_id, session_key) DO NOTHING
        """,
        (
            runtime_account_id,
            APP_ACTIVE_SESSION_KEY,
            json.dumps({"created_reason": "resident_welcome"}, ensure_ascii=False),
        ),
    )
    session = conn.execute(
        "SELECT id FROM sessions WHERE account_id = ? AND session_key = ?",
        (runtime_account_id, APP_ACTIVE_SESSION_KEY),
    ).fetchone()
    if session is None:  # 理论不可达
        raise RuntimeError("app active session was not created")
    inserted = insert_message(
        account_id=runtime_account_id,
        session_id=int(session["id"]),
        message_id=f"welcome-{resident_id}",
        reply_to_message_id=None,
        direction="outbound",
        role="assistant",
        message_type="text",
        content=clean_text,
        conn=conn,
    )
    return inserted is not None


def get_conversation(
    *,
    conversation_id: str,
    owner_platform_user_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 id 读会话；传 owner_platform_user_id 则做 owner-scoped 过滤（非本人→None，供 §3.4-2 防枚举）。"""
    with _tx(conn) as tx:
        if owner_platform_user_id is not None:
            row = tx.execute(
                "SELECT * FROM ai_conversations WHERE id = ? AND owner_platform_user_id = ?",
                (conversation_id, owner_platform_user_id),
            ).fetchone()
        else:
            row = tx.execute(
                "SELECT * FROM ai_conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
    return dict(row) if row else None


def resolve_conversation_for_owner(
    *,
    conversation_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """owner-scoped 解析稳定 conversation 到 world/resident/runtime；越权与不存在均 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT id AS conversation_id, universe_id, resident_id,
                   owner_platform_user_id, runtime_account_id, state
            FROM ai_conversations
            WHERE id = ? AND owner_platform_user_id = ?
            """,
            (conversation_id, owner_platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def list_conversations_for_owner(
    *,
    owner_platform_user_id: str,
    cursor_conversation_id: Optional[str] = None,
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """owner-scoped 列出 AI conversations；cursor 必须同 owner，否则返回空页。"""
    from app.db.accounts import summarize_app_conversations

    clean_limit = max(1, min(int(limit), 100))
    cursor_clause = ""
    params: List[Any] = [owner_platform_user_id]
    with _tx(conn) as tx:
        if cursor_conversation_id:
            cursor_row = tx.execute(
                """
                SELECT updated_at, id FROM ai_conversations
                WHERE id = ? AND owner_platform_user_id = ?
                """,
                (cursor_conversation_id, owner_platform_user_id),
            ).fetchone()
            if cursor_row is None:
                return []
            cursor_clause = (
                "AND (c.updated_at < ? OR (c.updated_at = ? AND c.id < ?))"
            )
            params.extend(
                [cursor_row["updated_at"], cursor_row["updated_at"], cursor_row["id"]]
            )
        params.append(clean_limit)
        rows = tx.execute(
            f"""
            SELECT c.id AS conversation_id, c.universe_id, c.resident_id,
                   c.runtime_account_id, c.state, c.updated_at,
                   c.last_read_message_id,
                   COALESCE(p.display_name, t.name) AS resident_name,
                   t.avatar_ref, r.status AS resident_status, r.origin
            FROM ai_conversations c
            JOIN universe_residents r ON r.id = c.resident_id
            JOIN character_templates t ON t.id = r.character_template_id
            LEFT JOIN profiles p ON p.account_id = c.runtime_account_id
            WHERE c.owner_platform_user_id = ?
              {cursor_clause}
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        items = [dict(row) for row in rows]
        # 预览与未读一次批量取回；早期版本在这里逐行查最近消息，是明确的 N+1（CONV-001）。
        summary = summarize_app_conversations(
            read_cursors={
                str(item["runtime_account_id"]): item["last_read_message_id"]
                for item in items
            },
            conn=tx,
        )
        for item in items:
            stats = summary.get(str(item["runtime_account_id"])) or {}
            # 媒体消息的预览取 caption/占位，不能用 content——那一列含 VL 描述（D-2）。
            item["last_preview"] = stored_content_preview(
                raw_content_json=stats.get("last_content_json"),
                fallback_text=stats.get("last_preview"),
            )
            item["last_message_at"] = stats.get("last_message_at")
            item["unread"] = int(stats.get("unread") or 0)
    return items


def advance_conversation_read_cursor(
    *,
    conversation_id: str,
    owner_platform_user_id: str,
    last_message_id: int,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """owner-scoped 推进已读游标，返回推进后的 ``{last_read_message_id, unread}``。

    游标**只前进不回退**，并向该会话 App scope 内的最大消息 id 收敛——客户端传一个很大的数
    不会把未来的消息也标成已读。不存在或越权返回 ``None``（由调用方统一成 not_found）。

    刻意不改 ``updated_at``：它是列表的排序键与 cursor 锚，标记已读不应该让会话跳到最前面。
    """
    from app.db.accounts import summarize_app_conversations

    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT id, runtime_account_id, last_read_message_id
            FROM ai_conversations
            WHERE id = ? AND owner_platform_user_id = ?
            """,
            (conversation_id, owner_platform_user_id),
        ).fetchone()
        if row is None:
            return None
        runtime_account_id = str(row["runtime_account_id"])
        current = int(row["last_read_message_id"] or 0)
        stats = summarize_app_conversations(
            read_cursors={runtime_account_id: current}, conn=tx
        ).get(runtime_account_id) or {}
        latest = int(stats.get("last_message_id") or 0)
        target = max(current, min(int(last_message_id), latest))
        if target > current:
            tx.execute(
                """
                UPDATE ai_conversations
                SET last_read_message_id = ?
                WHERE id = ? AND owner_platform_user_id = ?
                  AND (last_read_message_id IS NULL OR last_read_message_id < ?)
                """,
                (target, conversation_id, owner_platform_user_id, target),
            )
            unread = int(
                (
                    summarize_app_conversations(
                        read_cursors={runtime_account_id: target}, conn=tx
                    ).get(runtime_account_id)
                    or {}
                ).get("unread")
                or 0
            )
        else:
            unread = int(stats.get("unread") or 0)
    return {"last_read_message_id": target or None, "unread": unread}


@contextmanager
def try_conversation_transaction_lock(
    conversation_id: str, *, conn: Connection
) -> Iterator[bool]:
    """在调用方事务内非阻塞获取 conversation 锁。

    PG 锁随 ``conn`` 的事务提交/回滚释放；SQLite 复用 turn 的进程锁，供 M4
    offline 组合事务与既有 turn 在同一 ``conv:`` 边界上串行。
    """
    key = str(conversation_id)
    if is_postgres():
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(?) AS acquired",
            (advisory_lock_key("conv:" + key),),
        ).fetchone()
        yield bool(row and row["acquired"])
        return
    with _SQLITE_CONVERSATION_LOCKS_GUARD:
        lock = _SQLITE_CONVERSATION_LOCKS.setdefault(key, threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


@contextmanager
def try_conversation_turn_lock(conversation_id: str) -> Iterator[bool]:
    """非阻塞获取 conversation single-flight 锁，并在上下文退出时释放。"""
    with connect() as conn:
        with try_conversation_transaction_lock(
            conversation_id, conn=conn
        ) as acquired:
            yield acquired


def list_active_account_ids_for_user(
    *, platform_user_id: str, conn: Optional[Connection] = None
) -> List[str]:
    """按既有最早 binding 顺序列出真人在朝夕的 active legacy account。"""
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT b.account_id
            FROM account_owner_bindings b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.platform_user_id = ? AND b.status = 'active'
              AND b.app_id = ? AND a.app_id = ? AND a.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            """,
            (platform_user_id, ZHAOXI_APP_ID, ZHAOXI_APP_ID),
        ).fetchall()
    return [str(row["account_id"]) for row in rows]


def resolve_resident_memory_scope(
    *, runtime_account_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """按 runtime account 解析受管 resident/universe；form-A account 返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT id AS resident_id, universe_id, status
            FROM universe_residents
            WHERE runtime_account_id = ? AND status IN ('active', 'offline')
            """,
            (runtime_account_id,),
        ).fetchone()
    return dict(row) if row else None


def resolve_resident_proactive_scope(
    *, runtime_account_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """解析 world resident 的真人级主动触达角色；form-A account 返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT r.id AS resident_id, r.universe_id, r.status,
                   u.legacy_primary_account_id
            FROM universe_residents r
            JOIN universes u ON u.id = r.universe_id
            WHERE r.runtime_account_id = ? AND r.status IN ('active', 'offline')
            """,
            (runtime_account_id,),
        ).fetchone()
    return dict(row) if row else None


def list_human_proactive_account_ids_for_user(
    *, platform_user_id: str, conn: Optional[Connection] = None
) -> List[str]:
    """列出朝夕真人级聚合范围：active owner bindings + resident runtime accounts。"""

    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT account_id FROM (
                SELECT b.account_id AS account_id
                FROM account_owner_bindings b
                JOIN accounts a ON a.id = b.account_id
                WHERE b.platform_user_id = ? AND b.status = 'active'
                  AND b.app_id = ? AND a.app_id = ? AND a.status = 'active'
                UNION
                SELECT r.runtime_account_id AS account_id
                FROM universes u
                JOIN universe_residents r ON r.universe_id = u.id
                WHERE u.owner_platform_user_id = ?
                  AND r.runtime_account_id IS NOT NULL
            ) owned
            ORDER BY account_id ASC
            """,
            (
                platform_user_id,
                ZHAOXI_APP_ID,
                ZHAOXI_APP_ID,
                platform_user_id,
            ),
        ).fetchall()
    return [str(row["account_id"]) for row in rows]


def get_human_proactive_owner_scope(
    *, runtime_account_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """把 world runtime account 解析到 owner/home universe；form-A 返回 None。"""

    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT r.id AS resident_id, r.status AS resident_status,
                   r.universe_id, u.owner_platform_user_id,
                   u.legacy_primary_account_id, u.status AS universe_status,
                   u.onboarding_state, pu.created_at AS owner_created_at
            FROM universe_residents r
            JOIN universes u ON u.id = r.universe_id
            JOIN platform_users pu ON pu.id = u.owner_platform_user_id
            WHERE r.runtime_account_id = ?
              AND r.status IN ('active', 'offline')
            """,
            (runtime_account_id,),
        ).fetchone()
    return dict(row) if row else None


def get_owner_last_inbound_at(
    *, platform_user_id: str, conn: Optional[Connection] = None
) -> Optional[str]:
    """聚合朝夕 owner bindings 与 resident account 的最近真人入站时间。"""

    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT MAX(m.created_at) AS last_inbound_at
            FROM messages m
            WHERE m.direction = 'inbound' AND m.role = 'user'
              AND m.account_id IN (
                  SELECT b.account_id
                  FROM account_owner_bindings b
                  JOIN accounts a ON a.id = b.account_id
                  WHERE b.platform_user_id = ? AND b.status = 'active'
                    AND b.app_id = ? AND a.app_id = ? AND a.status = 'active'
                  UNION
                  SELECT r.runtime_account_id
                  FROM universes u
                  JOIN universe_residents r ON r.universe_id = u.id
                  WHERE u.owner_platform_user_id = ?
                    AND r.runtime_account_id IS NOT NULL
              )
            """,
            (
                platform_user_id,
                ZHAOXI_APP_ID,
                ZHAOXI_APP_ID,
                platform_user_id,
            ),
        ).fetchone()
    return str(row["last_inbound_at"]) if row and row["last_inbound_at"] else None


def count_owner_inbound_after(
    *, platform_user_id: str, after: str, conn: Optional[Connection] = None
) -> int:
    """统计朝夕真人级聚合范围在给定时刻后的全部真实入站。"""

    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT COUNT(*) AS c FROM messages m
            WHERE m.direction = 'inbound' AND m.role = 'user'
              AND m.created_at > ?
              AND m.account_id IN (
                  SELECT b.account_id
                  FROM account_owner_bindings b
                  JOIN accounts a ON a.id = b.account_id
                  WHERE b.platform_user_id = ? AND b.status = 'active'
                    AND b.app_id = ? AND a.app_id = ? AND a.status = 'active'
                  UNION
                  SELECT r.runtime_account_id
                  FROM universes u
                  JOIN universe_residents r ON r.universe_id = u.id
                  WHERE u.owner_platform_user_id = ?
                    AND r.runtime_account_id IS NOT NULL
              )
            """,
            (
                after,
                platform_user_id,
                ZHAOXI_APP_ID,
                ZHAOXI_APP_ID,
                platform_user_id,
            ),
        ).fetchone()
    return int(row["c"] if row else 0)


def has_legacy_primary_weixin_route(
    *, universe_id: str, conn: Optional[Connection] = None
) -> bool:
    """判断 legacy primary 是否存在字段完整的真实微信主动路由。"""

    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT 1
            FROM universes u
            JOIN channel_bindings b ON b.account_id = u.legacy_primary_account_id
            WHERE u.id = ? AND u.legacy_primary_account_id IS NOT NULL
              AND b.channel = 'openclaw-weixin'
              AND b.channel_account_id IS NOT NULL AND b.channel_account_id <> ''
              AND b.chat_id IS NOT NULL AND b.chat_id <> ''
            LIMIT 1
            """,
            (universe_id,),
        ).fetchone()
    return row is not None


def select_human_app_speaker(
    *,
    platform_user_id: str,
    universe_id: str,
    lock_resident: bool = False,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按冻结排序选择 App 发声人；finalize 可要求锁定并复核 resident。"""

    with _tx(conn) as tx:
        for _attempt in range(2):
            row = tx.execute(
                """
                WITH candidates AS (
                SELECT r.id AS resident_id, r.runtime_account_id, r.joined_at,
                       c.id AS conversation_id,
                       (
                           SELECT MAX(m.created_at) FROM messages m
                           WHERE m.account_id = r.runtime_account_id
                             AND m.direction = 'inbound' AND m.role = 'user'
                       ) AS last_inbound_at,
                       (
                           SELECT MAX(m.created_at)
                           FROM messages m
                           JOIN sessions s ON s.id = m.session_id
                           WHERE m.account_id = r.runtime_account_id
                             AND s.account_id = r.runtime_account_id
                             AND (
                                 s.session_key = ?
                                 OR substr(s.session_key, 1, ?) = ?
                             )
                       ) AS last_app_activity_at
                FROM universe_residents r
                JOIN universes u ON u.id = r.universe_id
                JOIN ai_conversations c ON c.resident_id = r.id
                WHERE u.id = ? AND u.owner_platform_user_id = ?
                  AND u.status = 'active' AND u.onboarding_state = 'confirmed'
                  AND r.status = 'active' AND r.runtime_account_id IS NOT NULL
                )
                SELECT * FROM candidates
                ORDER BY
                    CASE WHEN last_inbound_at IS NULL THEN 1 ELSE 0 END ASC,
                    last_inbound_at DESC,
                    CASE WHEN last_app_activity_at IS NULL THEN 1 ELSE 0 END ASC,
                    last_app_activity_at DESC,
                    CASE WHEN joined_at IS NULL THEN 1 ELSE 0 END ASC,
                    joined_at DESC,
                    resident_id ASC
                LIMIT 1
                """,
                (
                    APP_ACTIVE_SESSION_KEY,
                    len(f"{APP_ACTIVE_SESSION_KEY}:"),
                    f"{APP_ACTIVE_SESSION_KEY}:",
                    universe_id,
                    platform_user_id,
                ),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            if not lock_resident:
                return result
            lock_suffix = " FOR UPDATE" if is_postgres() else ""
            locked = tx.execute(
                "SELECT id, status, runtime_account_id FROM universe_residents "
                "WHERE id = ? AND universe_id = ?" + lock_suffix,
                (result["resident_id"], universe_id),
            ).fetchone()
            if (
                locked is not None
                and locked["status"] == "active"
                and locked["runtime_account_id"] is not None
            ):
                return result
        return None


# ---------------------------------------------------------------------------
# L3 universe_memory_facts（append-only typed fact，§2.5 / ADR §6.4 / D-05 / D-06）
# ---------------------------------------------------------------------------
def append_universe_fact(
    *,
    universe_id: str,
    fact_type: str,
    payload_json: str,
    occurred_at: str,
    source_account_id: Optional[str] = None,
    source_resident_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> str:
    """向 L3 追加一条带 provenance 的结构化事实行，返回 fact id。

    **append-only（ADR §6.4）**：纯 INSERT、永不就地改写整段——同一 universe 多 resident 并发
    追加天然无写冲突、无 last-writer-wins（并发正确性以 PG 为证，D-12）。锚 universe_id（非
    account，D-06）。payload_json 是结构化事实体、**非逐字聊天原文**（D-05：不共享原文/检索）。
    compact（合并去重 + status='superseded'）由单 writer 另行执行（M2-B+），不在本写路径。

    **写入隔离校验（§2.5）**：传 source_resident_id 时，INSERT 前（同事务）校验该 resident 确属
    target universe_id——跨 universe（居民 B 往世界 A 写）或指向不存在的 resident 一律拒绝（抛
    ValueError 回滚），杜绝一个世界的居民污染另一个世界的共享记忆。source_account_id 无对应关系表，
    不在此校验（provenance-only）。
    """
    fact_id = _new_id("uf")
    with _tx(conn) as tx:
        # §2.5 写入隔离校验（与 INSERT 同事务，拒写即回滚）：source_resident_id 必属 target universe。
        if source_resident_id is not None:
            owner = tx.execute(
                "SELECT universe_id FROM universe_residents WHERE id = ?",
                (source_resident_id,),
            ).fetchone()
            if owner is None or str(owner["universe_id"]) != str(universe_id):
                raise ValueError(
                    f"source_resident_id {source_resident_id} does not belong to universe {universe_id}"
                )
        tx.execute(
            """
            INSERT INTO universe_memory_facts(
                id, universe_id, fact_type, payload_json,
                source_account_id, source_resident_id, source_message_id,
                occurred_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                fact_id,
                universe_id,
                fact_type,
                payload_json,
                source_account_id,
                source_resident_id,
                source_message_id,
                occurred_at,
            ),
        )
    return fact_id


def read_universe_facts(
    *,
    universe_id: str,
    fact_types: Optional[Sequence[str]] = None,
    status: str = "active",
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """读某世界的 L3 事实行（严格按 universe_id 锚，跨 universe 不可见），按创建时间升序。

    默认只取 status='active'（compact 后被合并行 status='superseded' 不再注入）。可选按
    fact_type 过滤（L3 读注入按 fact_type 渲染，ADR §6.1）。锚隔离是核心不变量：本函数
    绝不返回其他 universe 的行。
    """
    clauses = ["universe_id = ?"]
    params: List[Any] = [universe_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    fact_type_list = list(fact_types) if fact_types else []
    if fact_type_list:
        placeholders = ",".join("?" for _ in fact_type_list)
        clauses.append(f"fact_type IN ({placeholders})")
        params.extend(fact_type_list)
    where = " AND ".join(clauses)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"SELECT * FROM universe_memory_facts WHERE {where} "
            f"ORDER BY created_at ASC, id ASC",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def list_universe_ids_for_memory_compact(
    *,
    after_universe_id: Optional[str] = None,
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> Tuple[List[str], Optional[str]]:
    """稳定分页列出有 active L3 facts 的 universe，并返回下一页游标。"""
    clean_limit = max(1, min(int(limit), 1000))
    after_clause = "AND u.id > ?" if after_universe_id else ""
    params: List[Any] = []
    if after_universe_id:
        params.append(after_universe_id)
    params.append(clean_limit + 1)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT u.id
            FROM universes u
            WHERE EXISTS (
                SELECT 1 FROM universe_memory_facts f
                WHERE f.universe_id = u.id AND f.status = 'active'
            )
              {after_clause}
            ORDER BY u.id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    ids = [str(row["id"]) for row in rows[:clean_limit]]
    next_cursor = ids[-1] if len(rows) > clean_limit and ids else None
    # 游标已到尾部时同一轮从头恢复，避免恰逢进程重启时长期停在空页。
    if not ids and after_universe_id:
        return list_universe_ids_for_memory_compact(
            after_universe_id=None,
            limit=clean_limit,
            conn=conn,
        )
    return ids, next_cursor


def _canonical_fact_payload(payload_json: str) -> Optional[str]:
    """生成 compact 比较/落库共用的规范 JSON；坏 JSON 不参与自动合并。"""
    try:
        value = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return None

    def _normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: _normalize(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [_normalize(value) for value in item]
        if isinstance(item, str):
            return re.sub(r"\s+", " ", item).strip()
        return item

    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compact_universe_facts(
    *, universe_id: str, conn: Optional[Connection] = None
) -> Dict[str, Any]:
    """确定性折叠一个 universe 的 exact-normalized duplicate facts。

    每组重复项新插一条规范行，沿用最新 provenance，并把组内旧行全部标记为
    superseded；不物理删除。单 writer 重跑时只剩一条 active 行，因此结果幂等。
    """
    merged_fact_ids: List[str] = []
    superseded_count = 0
    with _tx(conn) as tx:
        if is_postgres():
            # 正常部署由 central scheduler 保证单 writer；同键事务锁额外兜住
            # admin run-once 与定时任务偶发重叠，避免生成两条 active merged 行。
            tx.execute(
                "SELECT pg_advisory_xact_lock(?)",
                (advisory_lock_key("l3-compact:" + str(universe_id)),),
            )
        rows = tx.execute(
            """
            SELECT * FROM universe_memory_facts
            WHERE universe_id = ? AND status = 'active'
            ORDER BY occurred_at ASC, created_at ASC, id ASC
            """,
            (universe_id,),
        ).fetchall()
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            canonical_payload = _canonical_fact_payload(str(row["payload_json"]))
            if canonical_payload is None:
                continue
            groups.setdefault(
                (str(row["fact_type"]), canonical_payload), []
            ).append(row)

        for (fact_type, canonical_payload), duplicates in sorted(groups.items()):
            if len(duplicates) < 2:
                continue
            latest = duplicates[-1]
            merged_id = _new_id("uf")
            tx.execute(
                """
                INSERT INTO universe_memory_facts(
                    id, universe_id, fact_type, payload_json,
                    source_account_id, source_resident_id, source_message_id,
                    occurred_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    merged_id,
                    universe_id,
                    fact_type,
                    canonical_payload,
                    latest.get("source_account_id"),
                    latest.get("source_resident_id"),
                    latest.get("source_message_id"),
                    latest["occurred_at"],
                ),
            )
            duplicate_ids = [str(item["id"]) for item in duplicates]
            placeholders = ",".join("?" for _ in duplicate_ids)
            tx.execute(
                f"""
                UPDATE universe_memory_facts
                SET status = 'superseded', superseded_by = ?
                WHERE universe_id = ? AND status = 'active'
                  AND id IN ({placeholders})
                """,
                (merged_id, universe_id, *duplicate_ids),
            )
            merged_fact_ids.append(merged_id)
            superseded_count += len(duplicate_ids)

    return {
        "universe_id": universe_id,
        "merged_groups": len(merged_fact_ids),
        "superseded_facts": superseded_count,
        "merged_fact_ids": merged_fact_ids,
    }


# ---------------------------------------------------------------------------
# M3 Feed / outbox（owner 锚为 universe/platform user，不复用 runtime account）
# ---------------------------------------------------------------------------
@contextmanager
def _m3_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用调用方事务或建立 M3 写事务；SQLite 用 IMMEDIATE 串行化读后写。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        if not is_postgres():
            own.execute("BEGIN IMMEDIATE")
        yield own


def claim_ai_feed_slot(
    *,
    universe_id: str,
    author_resident_id: str,
    ai_local_date: str,
    ai_slot: str,
    slot_window_end_at: str,
    claim_token: str,
    claimed_at: str,
    next_attempt_at: Optional[str] = None,
    stale_before: Optional[str] = None,
    max_attempts: Optional[int] = None,
    conn: Optional[Connection] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """原子占用或重领一个 universe/date/slot；返回 ``(row, acquired)``。

    INSERT 的 SELECT 同时验证 confirmed active world 与 active author 归属，避免调用方
    先查后写产生跨 universe 作者污染。唯一索引是多 worker 竞争的最终裁决者。
    """
    if ai_slot not in {"morning", "evening"}:
        raise ValueError("invalid ai_slot")
    post_id = _new_id("post")
    with _m3_write_tx(conn) as tx:
        eligible = tx.execute(
            """
            SELECT 1
            FROM universes u
            JOIN universe_residents r ON r.universe_id = u.id
            WHERE u.id = ? AND u.status = 'active'
              AND u.onboarding_state = 'confirmed'
              AND r.id = ? AND r.status = 'active'
            """,
            (universe_id, author_resident_id),
        ).fetchone()
        if eligible is None:
            return None, False
        tx.execute(
            """
            INSERT INTO universe_posts(
                id, universe_id, author_type, author_resident_id, source_type,
                content_type, status, ai_local_date, ai_slot, slot_window_end_at,
                attempt_count, claimed_at, claim_token, next_attempt_at
            )
            SELECT ?, u.id, 'resident', r.id, 'ai_feed', 'text', 'generating',
                   ?, ?, ?, 1, ?, ?, ?
            FROM universes u
            JOIN universe_residents r ON r.universe_id = u.id
            WHERE u.id = ? AND u.status = 'active'
              AND u.onboarding_state = 'confirmed'
              AND r.id = ? AND r.status = 'active'
            ON CONFLICT DO NOTHING
            """,
            (
                post_id,
                ai_local_date,
                ai_slot,
                slot_window_end_at,
                claimed_at,
                claim_token,
                next_attempt_at,
                universe_id,
                author_resident_id,
            ),
        )
        row = tx.execute(
            """
            SELECT * FROM universe_posts
            WHERE universe_id = ? AND source_type = 'ai_feed'
              AND ai_local_date = ? AND ai_slot = ?
            """,
            (universe_id, ai_local_date, ai_slot),
        ).fetchone()
        if row is None:
            return None, False
        result = dict(row)
        if str(result["id"]) == post_id:
            return result, True
        if stale_before is None or max_attempts is None:
            return result, False
        cursor = tx.execute(
            """
            UPDATE universe_posts
            SET claimed_at = ?, claim_token = ?, next_attempt_at = NULL,
                attempt_count = attempt_count + 1,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ? AND status = 'generating'
              AND slot_window_end_at > ?
              AND (claim_token IS NULL OR claimed_at <= ?)
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND attempt_count < ?
            """,
            (
                claimed_at,
                claim_token,
                result["id"],
                claimed_at,
                stale_before,
                claimed_at,
                max(1, int(max_attempts)),
            ),
        )
        if int(cursor.rowcount or 0) <= 0:
            return result, False
        reclaimed = tx.execute(
            "SELECT * FROM universe_posts WHERE id = ?", (result["id"],)
        ).fetchone()
        if reclaimed is None:
            raise RuntimeError("reclaimed AI feed post disappeared")
        return dict(reclaimed), True


def get_universe_post_for_owner(
    *,
    post_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 post id + platform owner 读取；他人世界与不存在统一返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT p.*,
                   CASE
                     WHEN p.author_type = 'human' THEN human.display_name
                     ELSE COALESCE(profile.display_name, template.name)
                   END AS author_name,
                   CASE
                     WHEN p.author_type = 'resident' THEN template.avatar_ref
                     ELSE NULL
                   END AS author_avatar_ref
            FROM universe_posts p
            JOIN universes u ON u.id = p.universe_id
            LEFT JOIN platform_users human ON human.id = p.author_platform_user_id
            LEFT JOIN universe_residents resident ON resident.id = p.author_resident_id
            LEFT JOIN character_templates template
              ON template.id = resident.character_template_id
            LEFT JOIN profiles profile
              ON profile.account_id = resident.runtime_account_id
            WHERE p.id = ? AND u.owner_platform_user_id = ?
            """,
            (post_id, owner_platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def publish_user_feed_post_with_outbox(
    *,
    owner_platform_user_id: str,
    client_request_id: str,
    text: str,
    request_fingerprint: str,
    published_at: str,
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """直接发布 owner 用户文字动态，并与 published outbox 同事务提交。"""
    post_id = _new_id("post")
    with _m3_write_tx(conn) as tx:
        world = tx.execute(
            """
            SELECT u.*,
                   EXISTS(
                       SELECT 1 FROM universe_residents r
                       WHERE r.universe_id = u.id AND r.status = 'active'
                   ) AS has_active_resident
            FROM universes u
            WHERE u.owner_platform_user_id = ?
            """,
            (owner_platform_user_id,),
        ).fetchone()
        if world is None:
            raise ValueError("world_not_ready")
        world_row = dict(world)
        if world_row["status"] != "active":
            raise ValueError("world_disabled")
        if (
            world_row["onboarding_state"] != "confirmed"
            or not bool(world_row["has_active_resident"])
        ):
            raise ValueError("world_not_ready")

        tx.execute(
            """
            INSERT INTO universe_posts(
                id, universe_id, author_type, author_platform_user_id,
                source_type, content_type, text, status, client_request_id,
                request_fingerprint, published_at
            )
            VALUES (?, ?, 'human', ?, 'user_post', 'text', ?, 'published', ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                post_id,
                world_row["id"],
                owner_platform_user_id,
                text,
                client_request_id,
                request_fingerprint,
                published_at,
            ),
        )
        post = tx.execute(
            """
            SELECT * FROM universe_posts
            WHERE universe_id = ? AND author_platform_user_id = ?
              AND source_type = 'user_post' AND client_request_id = ?
            """,
            (world_row["id"], owner_platform_user_id, client_request_id),
        ).fetchone()
        if post is None:
            raise RuntimeError("user feed post was not created")
        post_row = dict(post)
        if str(post_row.get("request_fingerprint") or "") != str(
            request_fingerprint
        ):
            raise ValueError("idempotency_conflict")
        created = str(post_row["id"]) == post_id

        payload_json = json.dumps(
            {
                "v": 1,
                "post_id": post_row["id"],
                "universe_id": post_row["universe_id"],
                "source_type": post_row["source_type"],
                "published_at": post_row["published_at"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        outbox_key = f"world-post-published:v1:{post_row['id']}"
        tx.execute(
            """
            INSERT INTO companion_world_outbox(
                id, universe_id, post_id, event_type, idempotency_key,
                payload_json, status, available_at
            ) VALUES (?, ?, ?, 'universe_post.published.v1', ?, ?, 'pending', ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                _new_id("wout"),
                post_row["universe_id"],
                post_row["id"],
                outbox_key,
                payload_json,
                post_row["published_at"],
            ),
        )
        outbox = tx.execute(
            "SELECT * FROM companion_world_outbox WHERE idempotency_key = ?",
            (outbox_key,),
        ).fetchone()
        if outbox is None:
            raise RuntimeError("user feed published outbox was not created")
        outbox_row = dict(outbox)
        if (
            str(outbox_row["post_id"]) != str(post_row["id"])
            or str(outbox_row["payload_json"]) != payload_json
        ):
            raise ValueError("outbox idempotency conflict")
        result = get_universe_post_for_owner(
            post_id=str(post_row["id"]),
            owner_platform_user_id=owner_platform_user_id,
            conn=tx,
        )
        if result is None:
            raise RuntimeError("published user post disappeared")
    return result, created


def publish_resident_intro_post_with_outbox(
    *,
    universe_id: str,
    author_resident_id: str,
    text: str,
    published_at: str,
    conn: Connection,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """发布一条居民自我介绍动态并挂 published outbox（CONTENT-002）。

    与 ``ai_feed`` 的「先 claim 再 publish」两步不同：这条动态的正文是运营定稿文案、不过
    LLM，没有生成失败与超窗需要表达，因此一步直接落 ``published``。

    刻意**不复核** ``onboarding_state='confirmed'``：确认候选的事务里居民先激活、世界的
    onboarding_state 最后才置 confirmed，若在此复核会把首次确认整体打回。归属与可见性仍受
    约束——``author_resident_id`` 必须属于本 universe 且 ``status='active'``，世界必须 active。

    幂等由 ``ux_universe_posts_resident_intro``（m0053，``WHERE source_type='resident_intro'``）
    裁决：重放返回既有行、``created=False``。前置不满足时返回 ``(None, False)``，由调用方跳过
    ——一条自我介绍动态不值得让整次确认失败。
    """
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("text is required")
    post_id = _new_id("post")
    with _m3_write_tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO universe_posts(
                id, universe_id, author_type, author_resident_id,
                source_type, content_type, text, status, published_at
            )
            SELECT ?, u.id, 'resident', r.id, 'resident_intro', 'text', ?, 'published', ?
            FROM universes u
            JOIN universe_residents r ON r.universe_id = u.id
            WHERE u.id = ? AND u.status = 'active'
              AND r.id = ? AND r.status = 'active'
            ON CONFLICT DO NOTHING
            """,
            (post_id, clean_text, published_at, universe_id, author_resident_id),
        )
        post = tx.execute(
            """
            SELECT * FROM universe_posts
            WHERE universe_id = ? AND author_resident_id = ?
              AND source_type = 'resident_intro'
            """,
            (universe_id, author_resident_id),
        ).fetchone()
        if post is None:
            return None, False
        post_row = dict(post)
        created = str(post_row["id"]) == post_id

        payload_json = json.dumps(
            {
                "v": 1,
                "post_id": post_row["id"],
                "universe_id": post_row["universe_id"],
                "source_type": post_row["source_type"],
                "published_at": post_row["published_at"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        outbox_key = f"world-post-published:v1:{post_row['id']}"
        tx.execute(
            """
            INSERT INTO companion_world_outbox(
                id, universe_id, post_id, event_type, idempotency_key,
                payload_json, status, available_at
            ) VALUES (?, ?, ?, 'universe_post.published.v1', ?, ?, 'pending', ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                _new_id("wout"),
                post_row["universe_id"],
                post_row["id"],
                outbox_key,
                payload_json,
                post_row["published_at"],
            ),
        )
    return post_row, created


def list_published_feed_posts_for_owner(
    *,
    owner_platform_user_id: str,
    cursor_published_at: Optional[str] = None,
    cursor_post_id: Optional[str] = None,
    limit: int = 21,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """按 tuple cursor 列出 owner home world 的 published posts。"""
    if bool(cursor_published_at) != bool(cursor_post_id):
        raise ValueError("invalid_cursor")
    clean_limit = max(1, min(int(limit), 51))
    cursor_clause = ""
    params: List[Any] = [owner_platform_user_id]
    if cursor_published_at and cursor_post_id:
        cursor_clause = (
            "AND (p.published_at < ? OR (p.published_at = ? AND p.id < ?))"
        )
        params.extend([cursor_published_at, cursor_published_at, cursor_post_id])
    params.append(clean_limit)
    with _tx(conn) as tx:
        world = tx.execute(
            """
            SELECT u.*,
                   EXISTS(
                       SELECT 1 FROM universe_residents r
                       WHERE r.universe_id = u.id AND r.status = 'active'
                   ) AS has_active_resident,
                   EXISTS(
                       SELECT 1 FROM universe_posts farewell
                       WHERE farewell.universe_id = u.id
                         AND farewell.post_type = 'farewell'
                         AND farewell.status = 'published'
                   ) AS has_published_farewell
            FROM universes u
            WHERE u.owner_platform_user_id = ?
            """,
            (owner_platform_user_id,),
        ).fetchone()
        if world is None:
            raise ValueError("world_not_ready")
        world_row = dict(world)
        if world_row["status"] != "active":
            raise ValueError("world_disabled")
        if (
            world_row["onboarding_state"] != "confirmed"
            or not (
                bool(world_row["has_active_resident"])
                or bool(world_row["has_published_farewell"])
            )
        ):
            raise ValueError("world_not_ready")
        rows = tx.execute(
            f"""
            SELECT p.*,
                   CASE
                     WHEN p.author_type = 'human' THEN human.display_name
                     ELSE COALESCE(profile.display_name, template.name)
                   END AS author_name,
                   CASE
                     WHEN p.author_type = 'resident' THEN template.avatar_ref
                     ELSE NULL
                   END AS author_avatar_ref
            FROM universe_posts p
            JOIN universes u ON u.id = p.universe_id
            LEFT JOIN platform_users human ON human.id = p.author_platform_user_id
            LEFT JOIN universe_residents resident ON resident.id = p.author_resident_id
            LEFT JOIN character_templates template
              ON template.id = resident.character_template_id
            LEFT JOIN profiles profile
              ON profile.account_id = resident.runtime_account_id
            WHERE u.owner_platform_user_id = ? AND p.status = 'published'
              {cursor_clause}
            ORDER BY p.published_at DESC, p.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def retire_feed_post_with_outbox(
    *,
    owner_platform_user_id: str,
    post_id: str,
    reason_code: str,
    deleted_at: str,
    expected_author_type: Optional[str] = None,
    forbid_post_types: Sequence[str] = (),
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """owner-scoped 下架 post 并原子追加 deleted outbox；返回 ``(post, changed)``。

    「删除自己的动态」与「隐藏 AI 居民动态」共用这一条终态写路径：状态恒为 ``deleted``，
    语义差异只落在 ``terminal_reason``（``owner_deleted`` / ``owner_hidden`` /
    ``admin_correction``），由调用方给定，本层不猜。主人 Feed 与访客 Feed 都只筛
    ``published``，因此一次写入即对双方同时生效。

    ``expected_author_type`` 与 ``forbid_post_types`` 是**事务内**的授权断言，不做先查后写：
    author 不匹配一律按 ``post_not_found`` 处理（不泄漏资源存在性），被禁 ``post_type``
    返回 ``post_not_hideable``。

    幂等按「首次写入者胜出」：重放时以**已落库**的 ``deleted_at``/``terminal_reason`` 重算
    outbox payload 再比对，本次请求带的时间与原因被忽略——否则跨秒重放会因 payload 不等而
    误判成 outbox 冲突（IDEM-002）。
    """
    with _m3_write_tx(conn) as tx:
        current = tx.execute(
            """
            SELECT p.* FROM universe_posts p
            JOIN universes u ON u.id = p.universe_id
            WHERE p.id = ? AND u.owner_platform_user_id = ?
            """,
            (post_id, owner_platform_user_id),
        ).fetchone()
        if current is None:
            raise ValueError("post_not_found")
        current_row = dict(current)
        if current_row["status"] not in {"published", "deleted"}:
            # generating / skipped 的 AI 行对主人不可见，按「不存在」处理，避免资源枚举。
            raise ValueError("post_not_found")
        if (
            expected_author_type is not None
            and str(current_row["author_type"]) != expected_author_type
        ):
            raise ValueError("post_not_found")
        if forbid_post_types and str(
            current_row.get("post_type") or "normal"
        ) in set(forbid_post_types):
            raise ValueError("post_not_hideable")

        changed = current_row["status"] == "published"
        if changed:
            tx.execute(
                """
                UPDATE universe_posts
                SET status = 'deleted', terminal_reason = ?, deleted_at = ?,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND universe_id = ? AND status = 'published'
                """,
                (reason_code, deleted_at, post_id, current_row["universe_id"]),
            )
            terminal_at, terminal_reason = deleted_at, reason_code
        else:
            # 重放：以首次落库的终态为准，本次请求的时间与原因不参与 payload。
            terminal_at = str(current_row["deleted_at"] or deleted_at)
            terminal_reason = str(current_row["terminal_reason"] or reason_code)

        payload_json = json.dumps(
            {
                "post_id": post_id,
                "deleted_at": terminal_at,
                "reason_code": terminal_reason,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        outbox_key = f"world-post-deleted:v1:{post_id}"
        tx.execute(
            """
            INSERT INTO companion_world_outbox(
                id, universe_id, post_id, event_type, idempotency_key,
                payload_json, status, available_at
            ) VALUES (?, ?, ?, 'universe_post.deleted.v1', ?, ?, 'pending', ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                _new_id("wout"),
                current_row["universe_id"],
                post_id,
                outbox_key,
                payload_json,
                terminal_at,
            ),
        )
        outbox = tx.execute(
            "SELECT * FROM companion_world_outbox WHERE idempotency_key = ?",
            (outbox_key,),
        ).fetchone()
        if outbox is None or str(outbox["post_id"]) != post_id:
            raise ValueError("outbox idempotency conflict")
        result = get_universe_post_for_owner(
            post_id=post_id,
            owner_platform_user_id=owner_platform_user_id,
            conn=tx,
        )
        if result is None:
            raise RuntimeError("retired post disappeared")
    return result, changed


def publish_ai_feed_post_with_outbox(
    *,
    post_id: str,
    claim_token: str,
    text: str,
    published_at: str,
    outbox_idempotency_key: str,
    payload: Dict[str, Any],
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把已 claim 的 AI post 与 published outbox 在同一事务提交。

    发布前复核 author 仍 active 且未越过窗口；重复调用只在正文和 immutable outbox
    完全一致时幂等返回。M3 默认直接发布，不创建 account-scoped moderation task。
    """
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("text is required")
    payload_json = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with _m3_write_tx(conn) as tx:
        lock_suffix = " FOR UPDATE" if is_postgres() else ""
        row = tx.execute(
            """
            SELECT p.*, r.status AS author_status
            FROM universe_posts p
            JOIN universe_residents r
              ON r.id = p.author_resident_id AND r.universe_id = p.universe_id
            WHERE p.id = ? AND p.source_type = 'ai_feed'
            """
            + lock_suffix,
            (post_id,),
        ).fetchone()
        if row is None:
            raise ValueError("ai feed post not found")
        current = dict(row)
        if current["status"] == "generating":
            if str(current.get("claim_token") or "") != str(claim_token):
                raise ValueError("claim token mismatch")
            if current["author_status"] != "active":
                raise ValueError("author is not active")
            if str(published_at) >= str(current["slot_window_end_at"]):
                raise ValueError("slot window closed")
            tx.execute(
                """
                UPDATE universe_posts
                SET text = ?, status = 'published', published_at = ?,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND status = 'generating' AND claim_token = ?
                """,
                (clean_text, published_at, post_id, claim_token),
            )
        elif current["status"] == "published":
            if str(current.get("text") or "") != clean_text:
                raise ValueError("published post content mismatch")
        else:
            raise ValueError("ai feed post is not publishable")

        outbox_id = _new_id("wout")
        tx.execute(
            """
            INSERT INTO companion_world_outbox(
                id, universe_id, post_id, event_type, idempotency_key,
                payload_json, status, available_at
            )
            VALUES (?, ?, ?, 'universe_post.published.v1', ?, ?, 'pending', ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                outbox_id,
                current["universe_id"],
                post_id,
                outbox_idempotency_key,
                payload_json,
                published_at,
            ),
        )
        outbox = tx.execute(
            "SELECT * FROM companion_world_outbox WHERE idempotency_key = ?",
            (outbox_idempotency_key,),
        ).fetchone()
        if outbox is None:
            raise RuntimeError("published outbox was not created")
        outbox_result = dict(outbox)
        if (
            str(outbox_result["post_id"]) != str(post_id)
            or str(outbox_result["universe_id"]) != str(current["universe_id"])
            or str(outbox_result["payload_json"]) != payload_json
        ):
            raise ValueError("outbox idempotency conflict")
        published = tx.execute(
            "SELECT * FROM universe_posts WHERE id = ?", (post_id,)
        ).fetchone()
    if published is None:
        raise RuntimeError("published post disappeared")
    return dict(published), outbox_result


def claim_companion_world_outbox(
    *,
    batch_size: int,
    now: str,
    claim_token: str,
    stale_before: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """claim 一批可投递/lease 过期 outbox；PG 以 SKIP LOCKED 防 worker 重叠。"""
    clean_limit = max(1, int(batch_size))
    with _m3_write_tx(conn) as tx:
        lock_suffix = " FOR UPDATE SKIP LOCKED" if is_postgres() else ""
        stale_clause = ""
        select_params: List[Any] = [now]
        if stale_before is not None:
            stale_clause = " OR (status = 'processing' AND claimed_at <= ?)"
            select_params.append(stale_before)
        select_params.append(clean_limit)
        rows = tx.execute(
            f"""
            SELECT id FROM companion_world_outbox
            WHERE (status = 'pending' AND available_at <= ?){stale_clause}
            ORDER BY available_at ASC, id ASC
            LIMIT ?
            """
            + lock_suffix,
            tuple(select_params),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        update_stale_clause = ""
        update_params: List[Any] = [now, claim_token, *ids]
        if stale_before is not None:
            update_stale_clause = " OR (status = 'processing' AND claimed_at <= ?)"
            update_params.append(stale_before)
        tx.execute(
            f"""
            UPDATE companion_world_outbox
            SET status = 'processing', attempts = attempts + 1,
                claimed_at = ?, claim_token = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id IN ({placeholders})
              AND (status = 'pending'{update_stale_clause})
            """,
            tuple(update_params),
        )
        claimed = tx.execute(
            f"""
            SELECT * FROM companion_world_outbox
            WHERE claim_token = ? AND status = 'processing'
              AND id IN ({placeholders})
            ORDER BY available_at ASC, id ASC
            """,
            (claim_token, *ids),
        ).fetchall()
    return [dict(row) for row in claimed]


def get_companion_world_outbox_metrics(
    *, now: str, conn: Optional[Connection] = None
) -> Dict[str, Any]:
    """返回 outbox 状态计数与最老未完成事件延迟，供 M3 heartbeat 使用。"""

    try:
        current = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
    except ValueError as err:
        raise ValueError("now must be Beijing naive database time") from err
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT status, COUNT(*) AS total, MIN(created_at) AS oldest_created_at
            FROM companion_world_outbox
            GROUP BY status
            """
        ).fetchall()
    counts = {status: 0 for status in ("pending", "processing", "delivered", "dead")}
    oldest_live: Optional[datetime] = None
    for row in rows:
        status = str(row["status"])
        counts[status] = int(row["total"] or 0)
        if status not in {"pending", "processing"} or not row["oldest_created_at"]:
            continue
        created_at = datetime.fromisoformat(str(row["oldest_created_at"]))
        if oldest_live is None or created_at < oldest_live:
            oldest_live = created_at
    lag_seconds = (
        max(0, int((current - oldest_live).total_seconds()))
        if oldest_live is not None
        else 0
    )
    return {**counts, "oldest_undelivered_lag_seconds": lag_seconds}


def list_ai_feed_eligible_worlds(
    *,
    inbound_since: str,
    after_universe_id: Optional[str] = None,
    limit: int = 20,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出近 7 日有任一渠道真人入站的 confirmed worlds，按 universe id 稳定分页。"""
    clean_limit = max(1, min(int(limit), 200))
    cursor_clause = ""
    params: List[Any] = [inbound_since, ZHAOXI_APP_ID, ZHAOXI_APP_ID]
    if after_universe_id:
        cursor_clause = "AND u.id > ?"
        params.append(after_universe_id)
    params.append(clean_limit)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            WITH candidates AS (
                SELECT u.id AS universe_id, u.owner_platform_user_id,
                       (
                           SELECT MAX(m.created_at)
                           FROM messages m
                           WHERE m.direction = 'inbound' AND m.role = 'user'
                             AND m.created_at >= ?
                             AND m.account_id IN (
                                 SELECT b.account_id
                                 FROM account_owner_bindings b
                                 JOIN accounts a ON a.id = b.account_id
                                 WHERE b.platform_user_id = u.owner_platform_user_id
                                   AND b.status = 'active'
                                   AND b.app_id = ?
                                   AND a.app_id = ?
                                   AND a.status = 'active'
                                 UNION
                                 SELECT owned.runtime_account_id
                                 FROM universe_residents owned
                                 WHERE owned.universe_id = u.id
                                   AND owned.runtime_account_id IS NOT NULL
                             )
                       ) AS last_inbound_at
                FROM universes u
                WHERE u.status = 'active' AND u.onboarding_state = 'confirmed'
                  AND EXISTS (
                      SELECT 1 FROM universe_residents active_resident
                      WHERE active_resident.universe_id = u.id
                        AND active_resident.status = 'active'
                        AND active_resident.runtime_account_id IS NOT NULL
                  )
                  {cursor_clause}
            )
            SELECT * FROM candidates
            WHERE last_inbound_at IS NOT NULL
            ORDER BY universe_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows if row["last_inbound_at"] is not None]


def select_ai_feed_author(
    *, universe_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """按 App 会话活动、joined_at DESC、resident id ASC 选择 active AI Feed 作者。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            WITH candidates AS (
                SELECT r.id AS resident_id, r.universe_id, r.runtime_account_id,
                       r.joined_at, COALESCE(p.display_name, t.name) AS name,
                       t.avatar_ref,
                       (
                           SELECT MAX(m.created_at)
                           FROM messages m
                           JOIN sessions s ON s.id = m.session_id
                           WHERE m.account_id = r.runtime_account_id
                             AND s.session_key = ?
                       ) AS app_last_activity_at
                FROM universe_residents r
                JOIN character_templates t ON t.id = r.character_template_id
                LEFT JOIN profiles p ON p.account_id = r.runtime_account_id
                WHERE r.universe_id = ? AND r.status = 'active'
                  AND r.runtime_account_id IS NOT NULL
            )
            SELECT * FROM candidates
            ORDER BY
              CASE WHEN app_last_activity_at IS NULL THEN 1 ELSE 0 END ASC,
              app_last_activity_at DESC,
              CASE WHEN joined_at IS NULL THEN 1 ELSE 0 END ASC,
              joined_at DESC,
              resident_id ASC
            LIMIT 1
            """,
            (APP_ACTIVE_SESSION_KEY, universe_id),
        ).fetchone()
    return dict(row) if row else None


def get_ai_feed_author(
    *,
    universe_id: str,
    resident_id: str,
    require_active: bool = True,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """读取 slot 已绑定的 resident author；发布前可强制 active 复核。"""
    active_clause = "AND r.status = 'active'" if require_active else ""
    with _tx(conn) as tx:
        row = tx.execute(
            f"""
            SELECT r.id AS resident_id, r.universe_id, r.runtime_account_id,
                   r.joined_at, r.status, COALESCE(p.display_name, t.name) AS name,
                   t.avatar_ref
            FROM universe_residents r
            JOIN character_templates t ON t.id = r.character_template_id
            LEFT JOIN profiles p ON p.account_id = r.runtime_account_id
            WHERE r.universe_id = ? AND r.id = ?
              AND r.runtime_account_id IS NOT NULL {active_clause}
            """,
            (universe_id, resident_id),
        ).fetchone()
    return dict(row) if row else None


def defer_ai_feed_post(
    *,
    post_id: str,
    claim_token: str,
    next_attempt_at: str,
    max_attempts: int,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """生成瞬时失败时释放 claim 并退避；达到上限则 terminal skipped。"""
    with _m3_write_tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM universe_posts WHERE id = ? AND source_type = 'ai_feed'",
            (post_id,),
        ).fetchone()
        if row is None:
            return None
        current = dict(row)
        if (
            current["status"] != "generating"
            or str(current.get("claim_token") or "") != str(claim_token)
        ):
            return current
        if int(current.get("attempt_count") or 0) >= max(1, int(max_attempts)):
            tx.execute(
                """
                UPDATE universe_posts
                SET status = 'skipped', terminal_reason = 'retry_exhausted',
                    claim_token = NULL, next_attempt_at = NULL,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND status = 'generating' AND claim_token = ?
                """,
                (post_id, claim_token),
            )
        else:
            tx.execute(
                """
                UPDATE universe_posts
                SET claimed_at = NULL, claim_token = NULL, next_attempt_at = ?,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND status = 'generating' AND claim_token = ?
                """,
                (next_attempt_at, post_id, claim_token),
            )
        updated = tx.execute(
            "SELECT * FROM universe_posts WHERE id = ?", (post_id,)
        ).fetchone()
    return dict(updated) if updated else None


def skip_ai_feed_post(
    *,
    post_id: str,
    reason: str,
    claim_token: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """把 generating AI slot CAS 为 skipped；可选 token 防旧 worker 覆盖。"""
    token_clause = ""
    params: List[Any] = [reason, post_id]
    if claim_token is not None:
        token_clause = "AND claim_token = ?"
        params.append(claim_token)
    with _m3_write_tx(conn) as tx:
        tx.execute(
            f"""
            UPDATE universe_posts
            SET status = 'skipped', terminal_reason = ?, claim_token = NULL,
                next_attempt_at = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ? AND status = 'generating' {token_clause}
            """,
            tuple(params),
        )
        row = tx.execute(
            "SELECT * FROM universe_posts WHERE id = ?", (post_id,)
        ).fetchone()
    return dict(row) if row else None


def close_expired_ai_feed_slots(
    *, now: str, limit: int = 100, conn: Optional[Connection] = None
) -> int:
    """关闭已越过 slot window 的 generating 行；绝不跨窗口补发。"""
    clean_limit = max(1, min(int(limit), 1000))
    with _m3_write_tx(conn) as tx:
        lock_suffix = " FOR UPDATE SKIP LOCKED" if is_postgres() else ""
        rows = tx.execute(
            """
            SELECT id FROM universe_posts
            WHERE source_type = 'ai_feed' AND status = 'generating'
              AND slot_window_end_at <= ?
            ORDER BY slot_window_end_at ASC, id ASC
            LIMIT ?
            """
            + lock_suffix,
            (now, clean_limit),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = tx.execute(
            f"""
            UPDATE universe_posts
            SET status = 'skipped', terminal_reason = 'window_closed',
                claim_token = NULL, next_attempt_at = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE status = 'generating' AND id IN ({placeholders})
            """,
            tuple(ids),
        )
    return int(cursor.rowcount or 0)


def complete_companion_world_outbox(
    *,
    outbox_id: str,
    claim_token: str,
    delivered_at: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """以 claim token CAS 完成 outbox；旧 worker 不得覆盖新 lease。"""
    with _m3_write_tx(conn) as tx:
        tx.execute(
            """
            UPDATE companion_world_outbox
            SET status = 'delivered', delivered_at = ?, claim_token = NULL,
                last_error = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ? AND status = 'processing' AND claim_token = ?
            """,
            (delivered_at, outbox_id, claim_token),
        )
        row = tx.execute(
            "SELECT * FROM companion_world_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
    return dict(row) if row else None


def fail_companion_world_outbox(
    *,
    outbox_id: str,
    claim_token: str,
    error: str,
    next_attempt_at: str,
    max_attempts: int,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """outbox handler 失败时退避或转 dead；更新严格受当前 claim token 保护。"""
    with _m3_write_tx(conn) as tx:
        tx.execute(
            """
            UPDATE companion_world_outbox
            SET status = CASE WHEN attempts >= ? THEN 'dead' ELSE 'pending' END,
                available_at = ?, claim_token = NULL, claimed_at = NULL,
                last_error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ? AND status = 'processing' AND claim_token = ?
            """,
            (
                max(1, int(max_attempts)),
                next_attempt_at,
                str(error or "")[:1000],
                outbox_id,
                claim_token,
            ),
        )
        row = tx.execute(
            "SELECT * FROM companion_world_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
    return dict(row) if row else None
