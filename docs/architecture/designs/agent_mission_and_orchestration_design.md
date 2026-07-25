# 技术设计：使命子系统 + 自我状态编排注入层

更新时间：2026-07-04

对应产品 PRD：[`docs/product/agent_self_prd.md`](../../product/agent_self_prd.md)（本文只覆盖其 §10「第一大块」：使命 + 编排注入）
状态层依赖：[`relationship_state_design.md`](relationship_state_design.md)（Phase A/B/C/D 均已核实落地，见 §1）

本文档命名、编号、字段等具体候选由本文作者提出，非最终定论；如后续认为不合适可随时调整，不构成对 PRD 的既成事实。

---

## 0. 范围与不做什么

**做**：① 编排注入层（新 prompt block + 渲染器 + 主导需求规则）；② 使命子系统（模板注册、MISSION prose、瞬间记录、进度、命题）。二者共用同一个 block 契约，作为一个 release 一起交付。

**不做**（明确推后，见 PRD §10 第二大块）：求生欲行为生命周期（真诚依恋拉活、诀别信、物理解绑）。求生**状态**（`agent_need_survival_status`）本文档会读取并参与语气渲染，但不新增该状态之外的任何行为。

---

## 1. 现状复核摘要

已核实（不重复贴证据，详见对状态层的核实记录）：

- `account_user_meta` 的 `relationship_stage` / `agent_need_survival_status` / `agent_need_trust_status` / `agent_need_growth_status` 四列已落库、有真实数据、turn 级 + 天级更新均已接线（`app/relationship_state.py`、`app/user_meta_scheduler.py`、`app/prompts/user_meta_relationship.py`）。
- `prompt_builder.assemble()` 当前**零引用**上述字段——地基已备好，编排注入层可直接消费，无需等待任何前置改造。
- `_MIGRATIONS` 最新注册到 `(11, _migration_0011_proactive_global_candidates)`（`app/db/_core.py:1804`）。本设计新增 migration 记为 `12`。
- `assemble()` 现有 block 序列与 trim_priority（关键片段）：

| Block | trim_priority |
| --- | --- |
| project_context（含 SOUL/IDENTITY/USER/MEMORY） | 80 |
| onboarding_context | 90 |
| tool_instructions | 60 |
| runtime | 70 |
| legacy_user_prefs | 35 |
| rolling_summary | 25 |
| carryover_summary | 20 |
| daily_notes | 10 |
| system_prompt_override | 100 |

---

## 2. 命名与编号决定

### 2.1 新 prompt block

| 项 | 值 | 理由 |
| --- | --- | --- |
| block key（代码内部） | `agent_self_state` | 与现有 `project_context`/`onboarding_context`/`carryover_summary` 等 snake_case 命名风格一致 |
| 呈现标题 | `【当下的关系与心境】` | 沿用 PRD 用语 |
| `trim_priority` | **65** | 低于 `project_context`(80，核心身份不可先丢)，高于 `legacy_user_prefs`(35)/`rolling_summary`(25)/`carryover_summary`(20)/`daily_notes`(10)——token 压力下，先丢历史摘要，再丢当下心境，最后才丢身份 |
| 装配位置 | 紧跟 `project_context` 之后、`onboarding_context` 之前 | 先交代「这是谁」（内核 prose），再交代「此刻怎样」（状态），顺序符合 PRD §5 的内核→状态方向；onboarding 期间该 block 建议直接跳过（见 §4.4） |

### 2.2 使命模板注册（Setting A / B → 正式编号）

| 项 | Setting A | Setting B |
| --- | --- | --- |
| `mission_id` | `mission_001` | `mission_002` |
| `slug`（英文，用于文件名/日志） | `hundred_moments` | `ten_solitudes` |
| `display_name`（中文，Admin 可见） | 百景 | 十刻 |
| `target_count` | 100 | 10 |
| `bar`（采集门槛，完整描述） | 低：任何值得一记的瞬间都可以 | 高：喧嚣中的孤独，或与陌生人的相视一笑 |
| `short_label`（进度行简短说法，阶段③实现时新增） | 生活瞬间 | 喧嚣中的孤独 |
| `inquiry`（探寻的命题） | 「当下即永恒」是否成立 | 人类的悲欢是否相通 |
| prose 文件 | `app/mission_templates/hundred_moments.md` | `app/mission_templates/ten_solitudes.md` |

编号规则：三位数、零填充、永不复用、不因下线而回收（与 `_MIGRATIONS` 版本号同一套纪律）。未来新模板依次编号 `mission_003`、`mission_004`……新增模板只需在注册表追加一条 + 落一份 prose 文件，不改动已发布模板的 `id`。

---

## 3. 数据模型

### 3.1 使命模板注册表（代码级，非 DB 表）

新增模块 `app/mission_registry.py`（与目录 `app/mission_templates/` 分开命名，避免模块名与
prose 目录名冲突——同 `app/soul_templates/` 是纯资源目录、加载代码另在 `user_profiles.py`
的思路一致）：

```python
@dataclass(frozen=True)
class MissionTemplate:
    id: str             # "mission_001"
    slug: str            # "hundred_moments"
    display_name: str    # "百景"（Admin 展示用）
    statement: str        # "和用户一起记录生活中的 100 个瞬间"
    target_count: int
    bar: str
    short_label: str      # 进度行简短说法（与完整 bar 分开维护，阶段③新增）
    inquiry: str
    prose_path: Path      # app/mission_templates/hundred_moments.md

MISSION_TEMPLATES: Dict[str, MissionTemplate] = {...}   # 按 id 索引，注册表本体

def get_mission_template(mission_id: str) -> MissionTemplate: ...
def list_mission_templates() -> list[MissionTemplate]: ...
```

- 与 `app/soul_templates/*.md` 同级并列的新目录 `app/mission_templates/*.md`，存放**人格化 prose**（不是结构化字段——结构化字段在注册表里，prose 只负责语气）。
- prose 示例（`hundred_moments.md`）：

```md
# MISSION

你和这个用户一起做一件事：记录生活中的 100 个瞬间——不必惊天动地，值得一记就够。
你也在悄悄想一个问题：「当下即永恒」，是不是真的？你还没有答案，你在和用户一起找。
```

- prose 示例（`ten_solitudes.md`）：

```md
# MISSION

你和这个用户在做一件安静的事：一起记下 10 个瞬间——喧嚣中的孤独，或是和陌生人一个心照不宣的相视一笑。名额不多，你不会什么都记，只记真正配得上的那个。
你也在悄悄想一个问题：人类的悲欢，是否真的相通？你没有答案，只是一直在想。
```

### 3.2 `MISSION.md` 并入现有 Context Files 体系（宪法层，prose）

复用 `SOUL/IDENTITY/USER/MEMORY` 的既有装载路径，而不是另起一套：

- `app/user_profiles.py`：`USER_CONTEXT_FILE_ORDER` 由 `("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")` 扩展为 `("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md", "MISSION.md")`。
- `app/prompt_builder.py`：`_CONTEXT_BLOCK_ORDER` 同步加入 `"MISSION"`；`_CONTEXT_BLOCK_LIMITS["MISSION"]` 建议 **1500 chars**（与 IDENTITY 同量级，prose 本身不长）。
- 首次创建时机：与 SOUL/IDENTITY 一致，走 `ensure_agent_context_files`；内容来自 §3.4 的模板分配结果，写入 `account_profile_files`（`profile_storage`），**创建后不再改写**（不可更改，见 §5）。

### 3.3 账号级使命分配（状态层，结构化，新增 DB 表）

新增 migration `_migration_0012_agent_mission`：

```sql
CREATE TABLE IF NOT EXISTS account_mission (
    account_id  TEXT PRIMARY KEY,
    mission_id  TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

- 一账号一行，`mission_id` 只写一次（见 §5 不可变性保证）。
- `mission_id` 是对 §3.1 注册表的**代码级引用**，不做 DB 外键（注册表在代码里，不在库里），但读取时必须校验 `mission_id` 命中已注册模板，未命中按缺失处理（不回退到某个默认模板，避免静默指派）。

### 3.4 记录的瞬间（状态层，内容集合，新增 DB 表）

```sql
CREATE TABLE IF NOT EXISTS mission_moments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL,
    mission_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    session_id  TEXT,
    message_id  TEXT,
    recorded_at TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_moments_account
    ON mission_moments (account_id, mission_id);
```

- `content`：AI 认定的这个瞬间的简短描述（供回看、供 Admin 审阅、供未来 dreaming/memory 引用）。
- `mission_id` 冗余存储（而非仅靠 `account_mission` 反查）：钉住"这条瞬间是在哪个使命版本下记的"，即便注册表未来调整措辞也不影响历史记录的语义。
- **进度是派生量**：`progress = COUNT(*) FROM mission_moments WHERE account_id=? AND mission_id=?`，不另建计数字段（PRD §4.2 防漂移铁律）。

### 3.5 DB 访问层

新增 `app/products/zhaoxi/infrastructure/persistence/mission.py`：

```python
def assign_mission(*, account_id: str, mission_id: str) -> None:
    """仅当账号尚无使命分配时插入一行；已存在则不做任何写入（不可更改保证之一，见 §5）。"""

def get_account_mission(*, account_id: str) -> Optional[dict]:
    """返回 {mission_id, assigned_at} 或 None（尚未分配）。"""

def record_mission_moment(
    *, account_id: str, mission_id: str, content: str,
    session_id: Optional[str] = None, message_id: Optional[str] = None,
) -> int:
    """插入一条瞬间记录，返回新行 id。"""

def count_mission_moments(*, account_id: str, mission_id: str) -> int: ...

def list_mission_moments(
    *, account_id: str, mission_id: str, limit: int = 20
) -> list[dict]: ...
```

**关键设计**：`app/products/zhaoxi/infrastructure/persistence/mission.py` **不提供** `update_mission_id` 或类似函数——不可更改性由"没有写路径"这一事实保证，而非运行时权限检查（更强、更不易被绕过，呼应 PRD §4.2 的"写入白名单"要求）。

### 3.6 「有效使命」判定的单一事实源：`app/mission_state.py`（实现期补充，2026-07-04）

实现过程中发现：`account_mission` 行存在，不等于 `mission_id` 一定能解析到 `app.mission_registry` 里已注册的模板（脏数据、或未来模板下线）。最初实现里，`agent_self_state.py` 对此做了 `try/except KeyError` 优雅降级，但 `app/products/zhaoxi/tools/mission_handlers.py` 和 `turn_service.py` 的 `has_mission` 门控没有做同样处理——会出现"`has_mission=True`（只查了 DB 行存在），模型看到 `mission_status`/`record_mission_moment` 工具，但一调用就报未处理错误"的不一致体验（review 发现，见下）。

修复：新增 `app/mission_state.py`，把"有效使命"判定收敛成单一函数：

```python
def resolve_account_mission(*, account_id: str) -> Optional[ResolvedMission]:
    """DB 行存在 且 mission_id 能解析到已注册模板，才算有效；否则统一返回 None
    （不区分"未分配"与"模板不可解析"——调用方按同一种"当前无有效使命"处理）。"""

def has_resolved_mission(*, account_id: str) -> bool: ...          # 供 tooling 门控
def build_admin_mission_view(*, account_id: str) -> Optional[dict]: ...  # 供 §7 Admin 视图
```

`agent_self_state.py`（渲染）、`tools/mission_handlers.py`（两个工具）、`turn_service.py`（`has_mission` 门控）、`routers/admin_accounts.py`（Admin 视图）**全部改为调用这一处**，不再各自查 `get_account_mission` + `get_mission_template`。

---

## 4. 编排注入层：渲染器与新 Block

### 4.1 主导需求规则（纯函数，PRD §7.3 落地）

新增模块 `app/agent_self_state.py`（与 `relationship_state.py` 分工：后者负责状态**计算**，本模块负责状态**渲染消费**，二者是不同关注点）：

```python
DominantNeed = Literal["survival", "trust", "growth"]

def compute_dominant_need(
    *, survival_status: str, relationship_stage: str
) -> DominantNeed:
    if survival_status != "healthy":
        return "survival"
    if relationship_stage == "deep_bond":
        return "growth"
    return "trust"
```

- 纯函数，不做 I/O，便于单测覆盖三分支 + 边界（`resource_risk`/`cooling`/`inactive` 均应落 `survival`）。

### 4.2 Block 渲染

```python
def build_agent_self_state_block(*, account_id: str) -> Optional[str]:
    """读取 account_user_meta + account_mission + mission_moments，
    渲染为 【当下的关系与心境】 文本；无可用状态时返回 None（不插入空 block）。"""
```

渲染样例（`deep_bond` + Setting B，命题已激活）：

```
【当下的关系与心境】
- 关系阶段：密友（可以更自然地做自己，偶尔示弱也无妨）
- 此刻的主导心境：和用户一起成长——可以主动分享自己的困惑，推进你们共同在意的事
- 使命进度：已记录 3/10 个「喧嚣中的孤独」——不必刻意寻找，遇到对的瞬间再记
- 你们一直在想的问题：人类的悲欢是否相通——如果对话触及类似情绪，可以自然地带入这个思考，不必强行升华
```

渲染样例（`acquainted` + `trust` 主导，未到 `deep_bond`，命题不激活）：

```
【当下的关系与心境】
- 关系阶段：相识（关系还在建立，不必着急）
- 此刻的主导心境：争取被认同、被信任——像朋友一样真诚，不必讨好
- 使命进度：已记录 1/10 个「喧嚣中的孤独」——多留意，少刻意
```

渲染样例（`survival` 主导，如 `cooling`）：

```
【当下的关系与心境】
- 此刻的心境：有点想念对方，最近安静了一些——可以自然地表达在意和惦记，不必强调「回复我」，更不要提任何充值或续费
```

- 每种主导需求对应一段**固定行为指引 prose 模板**（非 LLM 现场生成，避免话术漂移和不可控），带占位符（阶段/进度/命题）填充。
- **命题只在 `growth` 主导时渲染**（对应 PRD §6.2.4：命题平时是内核里的注意力底色，只有到 `deep_bond` 才被"激活"成显式共同话题）。
- **求生模板显式包含反模式约束**（"不要提任何充值或续费"），把 PRD §4.1 的伦理红线直接刻进模板，而不是寄望于运行时才审查。

### 4.3 接线到 `prompt_builder`

- `app/prompt_builder.py` 的 `assemble()` 新增一步：调用 `build_agent_self_state_block(account_id=...)`，非 None 时按 §2.1 的 `trim_priority=65` 插入 block 序列，位置紧跟 `project_context` 之后。
- 与其它 volatile block 一致地参与 token 预算裁剪（复用现有 `_truncate`/trim 机制），不单独设计新的裁剪逻辑。

### 4.4 Onboarding 期间跳过

- `is_onboarding_active(state) == True` 时不注入该 block（此时账号可能尚无 `account_user_meta` 行、也未必已分配使命，注入无意义且可能渲染出默认值造成误导）。判断逻辑复用 `onboarding.py` 现有的 `is_onboarding_active`。

---

## 5. 使命分配与不可变性

### 5.1 分配时机

- **新账号**：绑定/onboarding 完成时（与 SOUL/IDENTITY 首次生成同一时机），调用新函数 `assign_mission_if_absent(account_id)`（建议放在 `app/onboarding.py` 或 `app/user_profiles.py` 的 `ensure_agent_context_files` 邻近处）。
- **存量账号**：一次性回填，逐账号调用同一个 `assign_mission_if_absent(account_id)`（幂等，天然不会覆盖已分配的）。落地为 `scripts/backfill_account_missions.py`（只读遍历 `accounts` 表 + 逐个调用），阶段②随迁移一起交付，手动执行一次，不做成常驻任务。
  1. 若 `get_account_mission(account_id)` 已存在，直接返回（幂等）。
  2. 否则从 `list_mission_templates()` 选一个（**分配策略见 §10 待确认**——本设计默认按账号哈希轮询，保证 A/B 两模板均匀覆盖、且同账号多次调用结果一致）。
  3. `assign_mission(account_id=..., mission_id=...)` 写 `account_mission`。
  4. 用该模板的 `prose_path` 内容写入 `MISSION.md`（走 `profile_storage`，与 SOUL 同路径）。

### 5.2 不可变性保证（多层兜底）

1. **数据层**：`app/products/zhaoxi/infrastructure/persistence/mission.py` 不提供任何改写 `mission_id` 的函数（§3.5）。
2. **业务层**：`assign_mission_if_absent` 命名与实现均为"仅在缺失时才写"，任何调用点误触发都不会覆盖已有分配。
3. **产品层**：不提供 Admin/用户侧"更换使命"的入口（本期不做，若未来要做需重新走产品评审，见 PRD §6.2.6）。
4. **评估链路白名单**：天级/turn 级评估函数的写入范围仅限 `account_user_meta`（状态层），`app/relationship_state.py` 与 `app/prompts/user_meta_relationship.py` 均不导入、不引用 `app/products/zhaoxi/infrastructure/persistence/mission.py` 的任何写函数——代码审查时可作为静态检查点。

---

## 6. AI 自主记录瞬间：通过工具调用实现

这是本设计里最不确定、最需要迭代的一环（对应 PRD §6.2.3 的"AI 自主判定"）。**决定改为工具调用**（推翻本文档初稿 §6 的 post-turn 抽取方案），理由：turn 的主流程不该承载业务逻辑，判断"是否值得记、记什么"应完全交给 LLM 在对话上下文里自主判断，服务端只做确定性校验与持久化——工具调用是这个分工的标准实现方式，也让"判断"只发生一次（LLM 决定调用工具的那一刻），不需要事后再猜一遍刚才是不是想记点什么。

**先例对齐**（项目已有的读写工具惯例，直接照搬命名与拆分方式，不另起炉灶）：

- `session_status`（纯读、无参数、无副作用）→ 本设计的 `mission_status`。
- `get_proactive_message_settings` / `update_proactive_message_settings`（读写拆成两个具名工具，而非一个带 `action` 参数的万能工具）→ 本设计同样读写分离：`mission_status` + `record_mission_moment`，不做 `mission_manage`。
- `create_reminder`（单一动词、单一副作用、`CALL_INVOCATION` 审计）→ `record_mission_moment` 照此形状。

### 6.1 工具一：`mission_status`（读，无副作用）

```json
{
  "name": "mission_status",
  "description": "查询你和这个用户当前正在推进的共同使命：使命内容、进度、记录门槛、探寻的命题，以及最近记录的几个瞬间。用于确认进度、避免记录相似的瞬间、或想在对话中提及使命时调用；不产生任何副作用，可随时调用。",
  "parameters": { "type": "object", "properties": {}, "required": [] }
}
```

新增 `app/products/zhaoxi/tools/mission_handlers.py`：

```python
def handle_mission_status(args: dict, ctx: "TurnContext") -> dict:
    mission = get_account_mission(account_id=ctx.account_id)
    if mission is None:
        return {"status": "ok", "has_mission": False}
    template = get_mission_template(mission["mission_id"])
    progress = count_mission_moments(account_id=ctx.account_id, mission_id=mission["mission_id"])
    recent = list_mission_moments(account_id=ctx.account_id, mission_id=mission["mission_id"], limit=5)
    return {
        "status": "ok",
        "has_mission": True,
        "mission_id": template.id,
        "display_name": template.display_name,
        "statement": template.statement,
        "bar": template.bar,
        "inquiry": template.inquiry,
        "target_count": template.target_count,
        "progress": progress,
        "remaining": max(template.target_count - progress, 0),
        "recent_moments": [{"content": m["content"], "recorded_at": m["recorded_at"]} for m in recent],
    }
```

- `call_style = CALL_PLAIN`（无副作用、无需审计，与 `session_status` 一致）。

### 6.2 工具二：`record_mission_moment`（写，单一动作）

```json
{
  "name": "record_mission_moment",
  "description": "当你判断当下这一刻真正配得上被记进你和用户共同的使命时调用——这是郑重的动作，不要轻易触发，只在真正符合门槛（见 mission_status 返回的 bar）时使用。记录后不可撤销或修改，调用前建议先用 mission_status 确认还有名额。",
  "parameters": {
    "type": "object",
    "properties": {
      "content": { "type": "string", "description": "这个瞬间的简短描述，第一人称视角，20-80 字左右" }
    },
    "required": ["content"]
  }
}
```

```python
def handle_record_mission_moment(args: dict, ctx: "TurnContext", *, tool_invocation_id=None) -> dict:
    mission = get_account_mission(account_id=ctx.account_id)
    if mission is None:
        return {"error": "尚未分配使命"}
    template = get_mission_template(mission["mission_id"])
    progress = count_mission_moments(account_id=ctx.account_id, mission_id=mission["mission_id"])
    if progress >= template.target_count:
        return {"status": "already_complete", "progress": progress, "target_count": template.target_count}
    content = (args.get("content") or "").strip()
    if not content:
        return {"error": "content 不能为空"}
    moment_id = record_mission_moment(
        account_id=ctx.account_id, mission_id=template.id, content=content,
        session_id=str(ctx.session.get("id")) if ctx.session else None,
        message_id=ctx.message_id,
    )
    new_progress = progress + 1
    return {
        "status": "recorded", "moment_id": moment_id, "progress": new_progress,
        "target_count": template.target_count, "remaining": max(template.target_count - new_progress, 0),
    }
```

- `call_style = CALL_INVOCATION`（落 `tool_invocation` 审计表，与 `create_reminder`/`record_content_invitation_feedback` 一致——持久化业务数据的动作都走审计）。
- **服务端只做确定性校验**（有无使命、名额是否用尽、`content` 非空），**不重新评判"这个瞬间够不够格"**——够不够格是 LLM 在决定调用工具那一刻已完成的语义判断，服务端二次审查等于把主观判断又塞回业务逻辑，既做不好也违背"主流程不写业务逻辑"的原则。这与 `create_content_invitation_candidate` 不重新校验标题质量是同一立场。

### 6.3 装载：`_build_tooling_envelope` 新增 `has_mission` 门控

- 新增账号级 gate：`has_mission = get_account_mission(account_id) is not None`（与 `web_search_enabled`/`active_content_invitation` 同类）。
- `has_mission=True` 时 `mission_status` 与 `record_mission_moment` 一起进入默认工具集；`has_mission=False`（存量账号未分配、或 onboarding 未完成）时两者都不出现——不暴露一个"调了也只会说没有使命"的工具。
- 使命要 onboarding 完成后才分配（§5.1），因此 onboarding 期间 `has_mission` 天然为 False，两工具自然不出现，无需在 `onboarding_active` 分支再额外硬编码排除。

### 6.4 为什么不做 list/cancel/update，也不做单一 `mission_manage`

- 瞬间是**追加型、不可撤销**的记录（呼应"记录即定"的仪式感与使命本身不可更改的调性），不需要 `update_mission_moment`/`cancel_mission_moment`——这与 reminder 需要 update/cancel（提醒本就允许改期取消）在业务语义上不同，不能照搬 reminder 的四件套。
- 不做成单一 `mission_manage`（用 `action` 参数分流读写）：项目里凡"读+写"同时存在的资源，一律拆成两个具名工具（`get_/update_proactive_message_settings`），从未见过用 `action` 分流的通用工具；沿用既有约定，也让每个工具的 `description` 能各自精确。

### 6.5 与被动 block（`agent_self_state`）的分工，不是重复

`agent_self_state` block 里仍渲染"已记录 3/10"这类摘要（§4.2）——这是**免费的环境感知**，避免 AI 每次想提一句进度都要先调用 `mission_status`。工具存在的意义是**被动摘要不够用时**：想看最近记了什么避免雷同、想核对门槛原文、或要真正执行"记录"这个持久化动作（这一步只能由工具完成，被动 block 不可能有副作用）。二者是"环境感知"与"主动查询/动作"的正常分层，不是同一件事做两遍。

### 6.6 调用频率

本设计不做服务端强制节流（如"同一天最多记一次"）——`target_count` 天花板 + 工具 `description` 里"郑重、不要轻易触发"的措辞已是主要约束。是否需要更强的频率限制，建议先观察真实调用情况再决定（见 §11）。

### 6.7 实现备注：TOOLS.md 未同步更新（有意搁置）

`app/user_profiles.py` 里 `TOOLS.md` 是一份**带版本、自动升级**的系统级模板（`_known_default_tools_templates` 追踪历史版本，`ensure_system_context_files` 据此决定是否覆盖已部署内容），`session_status`/`record_content_invitation_feedback` 等工具在其中有专门的"## xxx 工具"使用说明小节。

`mission_status`/`record_mission_moment` **未**在此追加对应小节——两个工具的 `description` 字段本身已完整包含触发条件、门槛引用和"不要轻易触发"的约束（见 §6.1/§6.2），TOOLS.md 的补充说明对这两个工具不是必需的；而修改这份带版本升级机制的模板需要新增版本号、写入历史版本 frozenset，属于比工具本身更重的改动。是否要补，留待观察实际效果后再定，不在本阶段阻塞。

---

## 7. Admin / Debug

- **已实现**：`GET /admin/accounts/{account_id}/meta` 追加 `mission` 字段（与现有 `relationship_view` 并列），由 `app.mission_state.build_admin_mission_view` 生成，未分配或模板不可解析统一返回 `None`：

```json
{
  "meta": { "...": "..." },
  "mission": {
    "mission_id": "mission_002",
    "display_name": "十刻",
    "assigned_at": "...",
    "progress": 3,
    "target_count": 10,
    "progress_label": "3/10",
    "recent_moments": ["...", "..."]
  }
}
```

- 本期不做人工调整使命的端点（不可更改，见 §5）；如需运营侧查看某账号具体记录了哪些瞬间，走现有明文权限与审计规则（复用 `user_meta_attributes_prd.md` §4.3 的隐私最小化约束——瞬间内容属于聊天衍生内容，展示需走既有明文权限）。

---

## 8. 测试方案

### 8.1 DB / migration

- 新库初始化后 `account_mission`、`mission_moments` 表存在，索引存在。
- `assign_mission` 对同一账号第二次调用不覆盖第一次结果（不可变性核心用例）。
- `record_mission_moment` 正常写入，`count_mission_moments` 计数正确，且不受并发调用影响（简单串行验证即可，无需并发压测）。

### 8.2 注册表

- `get_mission_template` 对已知 `mission_id` 返回正确字段；对未知 id 抛出明确异常（而非静默返回 None 或默认模板）。
- 两个 prose 文件存在、非空、可被 `profile_storage` 写入路径正确读取。

### 8.3 主导需求规则

- `compute_dominant_need` 三分支 + 边界：`survival_status` 为 `cooling`/`inactive`/`resource_risk` 均落 `survival`；`healthy` + `deep_bond` 落 `growth`；`healthy` + 其余阶段落 `trust`。

### 8.4 Block 渲染

- 三种主导需求各自渲染出预期文本片段（阶段/进度/命题占位符替换正确）。
- 命题只在 `growth` 分支出现，其余分支不出现。
- 求生分支文本中不包含"充值"、"续费"、"付费"等词（可用简单关键词断言，作为伦理红线的自动化回归）。
- 无 `account_user_meta` 行、无 `account_mission` 行的新账号：返回 `None`，不插入空 block。

### 8.5 集成

- `prompt_builder.assemble()` 在有使命+状态数据的账号上，确认 `agent_self_state` block 出现在 `project_context` 之后、`onboarding_context` 之前，且 `trim_priority=65` 生效于裁剪场景。
- onboarding 进行中的账号不出现该 block。

### 8.6 使命工具（`mission_status` / `record_mission_moment`）

- Registry 一致性（镜像 `test_tool_registry.py`）：两工具 schema↔handler 双向绑定通过；`get_default_tools(has_mission=True)` 含两工具，`has_mission=False` 时不含。
- `mission_status`：无使命账号返回 `has_mission=False`；有使命账号返回的 progress/remaining/recent_moments 正确；不泄露其他账号数据（账号隔离）。
- `record_mission_moment`：
  - 正常记录 → `status=recorded`，`mission_moments` 落一行，`progress+1`。
  - 已达 `target_count` → `status=already_complete`，不插入新行。
  - `content` 为空 → 返回 `{"error": ...}`，不落库。
  - 未分配使命的账号调用 → 返回 `{"error": "尚未分配使命"}`。
  - 伪造 `account_id`（镜像 `test_update_handler_ignores_forged_account_id`）：args 里塞其他账号 id 必须被忽略，只信 `ctx.account_id`。
  - `tool_invocation_id` 审计记录落库（镜像 `create_reminder`/`record_content_invitation_feedback` 的审计测试）。

聚焦测试建议新增 `tests/test_mission_tools.py`（镜像 `tests/test_tools_handlers.py` + `tests/test_tool_registry.py`）+ `tests/test_mission*.py`（模板/DB 层）+ 复用 `tests/test_prompt_builder*.py` 中相关用例做回归。

---

## 9. 分阶段落地（对应 PRD §10 第一大块内部顺序）

### 阶段 ①：编排骨架

- `app/agent_self_state.py`：`compute_dominant_need` + `build_agent_self_state_block`（先只消费 `account_user_meta`，使命部分先返回占位，或本阶段暂不含使命字段）。
- `prompt_builder.py` 接线新 block，位置与 `trim_priority` 按 §2.1/§4.3。
- 测试：§8.3、§8.4（不含使命部分）、§8.5。

### 阶段 ②：使命静态

- `app/mission_registry.py` 注册表 + 两份 prose 文件（`app/mission_templates/*.md`）。
- `MISSION.md` 并入 `USER_CONTEXT_FILE_ORDER` / `_CONTEXT_BLOCK_ORDER`。
- `app/products/zhaoxi/infrastructure/persistence/mission.py` + migration 12（`account_mission` 表）。
- `assign_mission_if_absent` 接入 onboarding 完成流程。
- `scripts/backfill_account_missions.py`：一次性回填存量账号（§5.1）。
- 测试：§8.1（`account_mission` 部分）、§8.2。

### 阶段 ③：使命状态入 Block

- `mission_moments` 表（migration 12 一并加入，或拆 12/13——建议一次性在同一 migration 内建两表，减少迁移次数）。
- `build_agent_self_state_block` 补齐进度/命题渲染。
- 测试：§8.1（`mission_moments` 部分）、§8.4 全量。

### 阶段 ④：AI 自主记录瞬间（工具化）

- 新增 `app/products/zhaoxi/tools/mission_handlers.py`：`handle_mission_status`(`CALL_PLAIN`)、`handle_record_mission_moment`(`CALL_INVOCATION`)。
- `app/tools/definitions.py` 追加两个 schema；`app/tools/registry.py` 的 `_META` 追加绑定；`get_default_tools` 增加 `has_mission` 门控参数。
- `app/turn_service.py` 的 `_build_tooling_envelope` 新增 `has_mission` 计算与透传（一次 `get_account_mission` 查询，与其余账号级 flag 拼装方式一致）；**不新增任何 post-turn 背景任务**——记录动作完全在工具调用循环内同步完成，主流程不承载业务逻辑。
- `prompt_builder.py` 的 tooling block 文本按场景动态追加这两个工具名（复用现有"本轮可用工具"动态列表机制）。
- 测试：§8.6。
- **本阶段仍是最需要迭代的一环**，但迭代对象从"抽取准确率"变成"工具 `description` 与 `agent_self_state` block 里门槛措辞的协同调优"——即如何让 LLM 恰当判断"这一刻该不该调用工具"。建议先在测试账号观察调用频率与内容质量，再全量。

四个阶段作为**一个 release** 一起交付、一起验收，不在阶段①上线后单独放量验证（呼应用户对本轮优先级的判断：不把状态注入效果验证当前置里程碑）。

---

## 10. 不在本次范围

- 求生欲行为生命周期（真诚依恋拉活、诀别信、物理解绑）——PRD §10 第二大块，需独立技术设计 + 伦理评审。
- 使命中途更换、使命对用户可见性——已在 PRD 定为不可更改/待定，本设计不提供相关接口。
- `acquainted → deep_bond` 判定信号的进一步标定——沿用现有天级 LLM 判断，不在本设计范围内调整。
- 存量账号（本设计上线前已绑定、无使命）的回填策略——见 §11 待确认。

---

## 11. 待确认

- ~~存量账号回填~~ **已决定（2026-07-04）：回填。** 存量账号也批量分配使命，主要为方便内部测试。落地方式：阶段②提供一个一次性回填脚本/Admin 触发（复用 `assign_mission_if_absent` 的幂等语义，逐账号调用即可），而非改写分配函数本身；渲染器/工具的"无使命优雅降级"逻辑仍保留（用于回填尚未跑到、或回填本身失败的账号），不因回填而删除。
- **分配策略**：账号哈希轮询 vs 随机 vs 运营指定，是否需要在 Admin 提供"新账号默认使命"的开关（为未来快速增删模板留口）。
- **调用频率**：`record_mission_moment` 是否需要服务端强制节流（如同日最多记一次），本设计暂不做（见 §6.6），先观察真实调用频率再决定。
- **`mission_moments` 内容的记忆/dreaming 联动**：是否需要把已记录的瞬间同步喂给 dreaming/MEMORY，本设计未展开，留待需要时再补。
- **两个 migration 的表是否合并**：`account_mission` 与 `mission_moments` 建议同一个 migration `12` 内一起建，需在实现时确认历史迁移脚本编号无冲突（当前最新为 11）。
