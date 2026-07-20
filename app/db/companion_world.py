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

from app.db._backend import Connection
from app.db._core import _new_id, _tx, connect

__all__ = [
    "get_or_create_home_universe",
    "get_universe",
    "create_character_template",
    "get_template",
    "create_resident",
    "count_active_residents",
    "list_residents",
    "create_ai_conversation",
    "get_conversation",
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
                tags_json, persona_seed_json, persona_version, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return dict(row)


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
    """
    fact_id = _new_id("uf")
    with _tx(conn) as tx:
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
