"""app.db.companion_world — 朝夕相伴 P1 多居民数据访问层（M2-A 数据基座）。

承载 universe / character_template / universe_resident / ai_conversation /
universe_memory_facts（L3）五张 P1 表的底层 repo 原语。见
docs/tech_design/companion_world_p1_backend_spec.md §2 与 ADR §6/§7.3/D-05/D-06。

分层归属：本模块是**数据层**（app.db.*），非领域层——领域层
（app.domains.companion_world）受 tests/test_layer_boundaries.py 门禁约束不得直接
import 本模块，须经 app.agent_runtime 端口。二者同名不同包、互不干涉。

M2-A 只落存储 + 纯 DB 原语（无 live 调用方、零行为变更）；L3 读注入/sink 路由（M2-B）、
居民 bootstrap/confirm/backfill（M2-C，待 ADR §10.1/.2/.6 产品冻结）为后续刀。
"""
from typing import Any, Dict, List, Optional, Sequence

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

LEGACY_CHARACTER_TEMPLATE_ID = "tmpl_legacy"

__all__ = [
    "get_or_create_home_universe",
    "get_universe",
    "lock_universe",
    "set_universe_onboarding_state",
    "mark_universe_legacy_confirmed",
    "create_character_template",
    "get_template",
    "list_initial_character_templates",
    "get_available_character_template",
    "get_or_create_legacy_template",
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
    "get_conversation",
    "resolve_conversation_for_owner",
    "list_active_account_ids_for_user",
    "append_universe_fact",
    "read_universe_facts",
]


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
    owner_platform_user_id: Optional[str] = None,
    avatar_ref: Optional[str] = None,
    summary: Optional[str] = None,
    tags_json: Optional[str] = None,
    persona_seed_json: Optional[str] = None,
    persona_version: str = "v1",
    status: str = "active",
    initial_candidate_rank: Optional[int] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """新建一个角色模板，返回行 dict。source_type ∈ official|operations|user_created|generated。

    persona_seed_json 是实例化时写入 runtime account 的 SOUL/IDENTITY 种子，**绝不进 App DTO**
    （§2.2 / 客户端 §4.2）——candidates 端点须显式剔除该列。
    """
    template_id = _new_id("tmpl")
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO character_templates(
                id, source_type, owner_platform_user_id, name, avatar_ref, summary,
                tags_json, persona_seed_json, persona_version, status, initial_candidate_rank
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
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
            ),
        )
        row = tx.execute(
            "SELECT * FROM character_templates WHERE id = ?", (template_id,)
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


def get_or_create_legacy_template(
    *, conn: Optional[Connection] = None
) -> Dict[str, Any]:
    """幂等建立 backfill 专用哨兵模板；不含 persona，绝不改写既有账号人设。"""
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO character_templates(
                id, source_type, name, persona_seed_json, persona_version, status
            ) VALUES (?, 'operations', 'legacy', NULL, 'legacy', 'active')
            ON CONFLICT(id) DO NOTHING
            """,
            (LEGACY_CHARACTER_TEMPLATE_ID,),
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
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """幂等快照一条非 legacy candidate；已存在 active/dismissed 关系也原样返回、不复活。"""
    if origin == "legacy":
        raise ValueError("legacy origin is not a candidate")
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO universe_residents(
                id, universe_id, character_template_id, template_version, origin, status
            ) VALUES (?, ?, ?, ?, ?, 'candidate')
            ON CONFLICT DO NOTHING
            """,
            (_new_id("res"), universe_id, character_template_id, template_version, origin),
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
                t.source_type, t.owner_platform_user_id, t.name, t.avatar_ref,
                t.summary, t.tags_json, t.persona_seed_json, t.persona_version,
                t.status AS template_status, t.initial_candidate_rank,
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


def list_active_account_ids_for_user(
    *, platform_user_id: str, conn: Optional[Connection] = None
) -> List[str]:
    """按既有最早 binding 顺序列出一个真人的全部 active legacy account。"""
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT account_id FROM account_owner_bindings
            WHERE platform_user_id = ? AND status = 'active'
            ORDER BY created_at ASC, id ASC
            """,
            (platform_user_id,),
        ).fetchall()
    return [str(row["account_id"]) for row in rows]


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
