# 技术设计：Companion World P1 后端规范（fact_type / schema / DTO / 错误码）

> 状态：**定稿并实现 2026-07-22，default-off**（M2-C C0–C5 已交付；生产启用仍受 §2.9 发布闸约束）。
> 性质：实现级规范（buildable spec），非决策记录。冻结决策口径以 ADR 为准，本文只把已冻结口径落成可编码的表/DTO/错误码。
> 上位 ADR：[`companion_world_3_0_refactor_design.md`](./companion_world_3_0_refactor_design.md)（§7.3 端口契约、§8 R2、§6 L3、D-05/D-06/D-07/D-08/D-09/D-14）
> 客户端输入（只作参考，不替代本规范）：[`private_world_backend_gap_analysis.md`](../../../ai4all-companion-app-rn/docs/tech_design/private_world_backend_gap_analysis.md) §4/§5
> 核查基线：`feat/companion-world-m2c@a47d41e`（迁移 max 版本 = 30；新世界端点使用稳定 envelope）。

## 0. 范围与不做项

**本规范覆盖（P1 = M2/R2 首个多居民闭环）**：
- §1 `fact_type` 枚举全集 + 路由矩阵（ADR §7.3 接缝②的内容层定型）。
- §2 P1 schema：`universes` / `character_templates` / `universe_residents` / `ai_conversations` / `universe_memory_facts`（L3 承载）。
- §2.9 M2-C 冻结产品策略：4 位初始候选与版本快照、legacy 全量映射不自动补居民、App Dreaming scope。
- §3 DTO：P1 端点请求/响应形状 + 安全契约（不接受客户端 `account_id`）。
- §4 稳定错误码表。

**明确不做（留后续里程碑，本规范不定义其表/DTO）**：
- `universe_posts`（世界 Feed）→ M3；`app_notifications`（通知收件箱）→ M3（ADR §11.8 T3-1 已给形状）。
- `character_letters`（信箱）、`resident_lifecycle_events`（离开事务）→ M4。
- `universe_invites` / `universe_visits` / `human_conversations` / `human_messages` → M5。
- **锁顺序细则**（§2.7）与 **backfill 分步伪码**（§2.8）本版已补齐——M2-0 前置门清零；未覆盖的仅剩 M4/M5 生命周期锁（offline 原子事务、visit 双世界锁）随对应里程碑。
- 全字段 OpenAPI：DTO 只列承载**不变量/安全语义**的字段，逐字段类型以 OpenAPI 评审为准。

**贯穿约束（继承 CLAUDE.md + ADR）**：schema 改动一律**新增迁移函数追加 `_MIGRATIONS`**（勿用启动期 `_ensure_column` 补丁，L3/世界表为新建，用 `executescript`）；账号隔离不变量升级为**锚点隔离**——L1/L2 锚 `account_id`、L3 锚 `universe_id`，任何未按其锚约束的读写都是 bug；并发正确性以 PG 用例为证，SQLite 只验功能（D-12）。

### 0.1 最终实现摘要与有意偏差

- Backend 直接路由前缀为 `/v1`；部署网关可外部映射为 `/api/v1`。本文下表以代码实际 `/v1` 为准。
- P1 turn **只开放非空 text（1–4000）**；media 未暴露，等待独立的上传、审核与配额安全设计。
- 客户端不传 runtime account。入站幂等落在既有 `UNIQUE(account_id,message_id)`：服务端把公开锚映射成 `app:{conversation_id}:{client_message_id}`；同一 conversation 唯一对应一个 runtime account，因此等价承载 `(conversation, sender, client_message_id)` 的 P1 私聊不变量，且响应不泄露 runtime account ID。
- PG conversation single-flight 使用非阻塞 `pg_try_advisory_xact_lock(advisory_lock_key('conv:'+id))`；失败立即返回 `turn_in_progress`。SQLite 只用进程锁作功能回退，不作为并发证明。
- L3 compact 只合并 fact_type + 规范 JSON 完全一致的重复项；复杂语义冲突不在 P1。central scheduler 单写，PG 同 universe advisory xact lock 兜住 admin run-once 重叠。
- 真人级 proactive 在 M2 仅落安全阀：form-A 不变，world resident 只有 `legacy_primary_account_id` 放行，App-only fail-closed。App 收件箱/发声人/正式人级聚合仍属 M3。

---

## 1. `fact_type` 枚举全集与路由矩阵（接缝②内容层）

ADR §7.3 接缝②冻结了 `MemoryEvent(fact_type, payload, provenance)` 的**形状**；本节冻结 `fact_type` 的**全集**与**路由目标**。路由决策在域层 `CompanionWorldMemorySink._route(fact_type)` 做，Runtime 只发不判（保持 form-agnostic）。

| `fact_type` | 语义 | 路由层 | 锚点 | 承载 | 依据 |
|---|---|---|---|---|---|
| `user_identity` | 真人称呼/身份/基本信息 | **L3** 共享 | `universe_id` | `universe_memory_facts` | D-05 沉淀记忆全量共享 |
| `user_preference` | 偏好/口味/禁忌 | **L3** 共享 | `universe_id` | `universe_memory_facts` | D-05 |
| `user_profile_derived` | 派生画像（性格/关系网等） | **L3** 共享 | `universe_id` | `universe_memory_facts` | D-05 |
| ~~`bazi`~~ | ~~八字/命理托管段（L3 首刀）~~ **已废弃 2026-07-21：八字降级为无工具 skill，出生信息走通用 `user_fact`** | — | — | — | ADR §6.5 |
| `user_event` | 关于用户的客观事件/里程碑（**非聊天原文**） | **L3** 共享 | `universe_id` | `universe_memory_facts` | D-05 |
| `relationship` | 关系阶段/漂移/共同历史 | **L2** 隔离 | `account_id` | `account_user_meta` + MEMORY 关系段（现状不变） | D-04/D-05 |
| `commitment` | 本居民某轮许下的承诺 | **L2** 隔离 | `account_id` | `proactive_commitments`（现状不变） | ADR §11.2 |

**永不走 sink（结构性排除，D-05 边界）**：各居民与用户的**原始聊天记录**（`messages`/session 逐字稿）与其**检索/证据回放**（`tool_evidence_replay`）——永远 per-account，既不产出 `MemoryEvent`、也不进 L3、不跨 `runtime_account`。sink 只承载「沉淀后的结构化事实」，不承载原文。

**路由不变量（测试点）**：
- L3 类（表 5 行前段）→ 写入必带 `universe_id` 锚；跨 universe 写入拒绝（§2.5）。
- L2 类（`relationship`/`commitment`）→ 保持 per-account，**不得**因 sink 改造泄漏到同世界其他 resident。
- 未知 `fact_type` → sink `emit` 拒绝（fail-closed），不静默丢弃或误路由。
- 形态 A（微信）：无 `CompanionWorldMemorySink` 注册 → sink 无消费者 → 退化为现状 raw `write_memory`（memory_writer.py:119），零行为变更。

`payload_json` 为结构化事实体，**约定不含逐字原文**；provenance（`source_account_id`/`source_resident_id`/`source_message_id`/`occurred_at`）用于审计与 compact 溯源，不进任何 resident 的可读上下文。

---

## 2. P1 Schema

DDL 沿用现有迁移惯例：`TEXT` 主键、中国时区默认时间 `strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))`、`ix_`/`ux_` 索引命名、`_json` 结构化列、`FOREIGN KEY ... REFERENCES accounts(id)`；双后端差异由 `_backend` 垫片处理。M2-A 五表已实际落为 **m0028/m0029**；M2-C 只追加 **m0030**（初始候选 rank + resident 模板唯一关系），不改写既有 28/29。

### 2.1 `universes`（一真人一 home world）

```sql
CREATE TABLE IF NOT EXISTS universes (
    id TEXT PRIMARY KEY,                          -- 内部世界 ID，非可分享公开码
    owner_platform_user_id TEXT NOT NULL UNIQUE,  -- 一真人一 home world（幂等 bootstrap 依赖）
    legacy_primary_account_id TEXT,               -- 老用户迁移/计费锚点（D-08 legacy 映射）
    status TEXT NOT NULL DEFAULT 'active',         -- active | disabled
    onboarding_state TEXT NOT NULL DEFAULT 'preparing',  -- preparing | selecting | confirmed
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id)
);
```
- `UNIQUE(owner_platform_user_id)` 是幂等 bootstrap 的硬保证：重复 `POST /worlds/home/bootstrap` 命中同一行、不新建。
- 登录不选 universe；home universe 由 session 的 `platform_user` 解析（替换 `get_first_active_account_for_user` 的「第一个 account」）。

### 2.2 `character_templates`（模板≠runtime account）

```sql
CREATE TABLE IF NOT EXISTS character_templates (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,                    -- official | operations | user_created | generated
    owner_platform_user_id TEXT,                  -- 自建时非空；官方/运营为空
    name TEXT NOT NULL,
    avatar_ref TEXT,
    summary TEXT,
    tags_json TEXT,
    persona_seed_json TEXT,                        -- 实例化时写入 runtime account 的 SOUL/IDENTITY 种子；不经 App DTO 下发
    persona_version TEXT NOT NULL DEFAULT 'v1',    -- 版本；运营更新不静默改写既有关系
    initial_candidate_rank INTEGER,                -- NULL=非初始候选；1..4=当前首发顺序（M2-C）
    status TEXT NOT NULL DEFAULT 'active',          -- active | retired
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id)
);
CREATE INDEX IF NOT EXISTS ix_character_templates_source ON character_templates(source_type, status);
CREATE INDEX IF NOT EXISTS ix_character_templates_owner ON character_templates(owner_platform_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_character_templates_initial_rank_active
    ON character_templates(initial_candidate_rank)
    WHERE status='active' AND initial_candidate_rank IS NOT NULL;
```
- `persona_seed_json` / 内部策略**绝不进 App DTO**（客户端 §4.2）。同一官方模板进两个世界 → 生成两个 resident + 两个 runtime account，私聊/Memory 不共享（D-02）。
- **初始集合固定 4 位**：bootstrap 只接受恰好 rank 1..4 各一条 active 模板；缺位/重复 fail-closed 为 `preset_catalog_not_ready`，不静默少发或拿任意模板补位。
- **版本不可变 + 快照**：已发布模板不原地改 `persona_seed_json/persona_version`；更新时插入新 template/version、退休旧版。bootstrap 创建 candidate 时把版本钉入 §2.3 `template_version`；已生成 candidate 即使旧版随后 retired 仍可完成本次 confirm，运营换版只影响后续新 world。

### 2.3 `universe_residents`（关系实例，容量真相）

```sql
CREATE TABLE IF NOT EXISTS universe_residents (
    id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL,
    character_template_id TEXT NOT NULL,
    template_version TEXT NOT NULL,               -- 确认时刻钉住的模板版本
    runtime_account_id TEXT,                       -- 激活后指向 account；candidate 期为空
    origin TEXT NOT NULL,                          -- preset | custom | mailbox | legacy
    status TEXT NOT NULL DEFAULT 'candidate',       -- candidate | active | offline | dismissed
    joined_at TEXT,
    offline_at TEXT,
    departure_event_id TEXT,                        -- M4 用，P1 留列
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(runtime_account_id) REFERENCES accounts(id)
);
-- 容量真相 = status='active' 计数（D-07）；world row lock 下校验 active ≤ 10
CREATE INDEX IF NOT EXISTS ix_universe_residents_universe_status ON universe_residents(universe_id, status);
-- 一个 runtime account 至多绑定一个 resident（candidate 期 NULL 允许多行 → 偏索引）
CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_residents_runtime
    ON universe_residents(runtime_account_id) WHERE runtime_account_id IS NOT NULL;
-- 非 legacy 关系：同一世界对同一模板只建立一段关系；legacy 多 account 共用哨兵模板，故豁免
CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_residents_universe_template
    ON universe_residents(universe_id, character_template_id) WHERE origin <> 'legacy';
```
- **容量真相脱离建号 binding 计数**（D-07）：active 数 = `COUNT(*) WHERE universe_id=? AND status='active'`，在 world row lock 下算；`offline`/`dismissed` 保留 account 与 binding 仅供只读历史、不计位。满 10 后仍可 offline 补新。
- **偏唯一索引** `WHERE runtime_account_id IS NOT NULL`：SQLite(≥3.8)/PG 均支持（与 D-14 `UNIQUE(platform_user_id) WHERE status='active'` 同款）。
- `origin='legacy'`：D-08 豁免离开（永不 offline）；backfill 把老用户现有 account 映射为 legacy resident。
- bootstrap 首次创建 4 条 `origin='preset', status='candidate'`，重复请求命中既有 candidate，不叠加；confirm 将保留项激活、其余置 `dismissed`。

### 2.4 `ai_conversations`（跨 session 生命周期的稳定会话 ID）

```sql
CREATE TABLE IF NOT EXISTS ai_conversations (
    id TEXT PRIMARY KEY,                           -- 客户端长期稳定会话 ID（≠ 数值 session.id）
    universe_id TEXT NOT NULL,
    resident_id TEXT NOT NULL,
    owner_platform_user_id TEXT NOT NULL,          -- owner 校验锚（防越权）
    runtime_account_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',            -- active | read_only（resident offline 后原子切换）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(resident_id) REFERENCES universe_residents(id),
    FOREIGN KEY(runtime_account_id) REFERENCES accounts(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_conversations_resident ON ai_conversations(resident_id);
CREATE INDEX IF NOT EXISTS ix_ai_conversations_owner_state ON ai_conversations(owner_platform_user_id, state);
```
- 底层 `sessions`/`messages` 仍按 `runtime_account_id` 隔离、继续轮转；`ai_conversations` 只提供稳定外键，不复制消息。
- `POST /ai-conversations/{id}/turn` 由服务端解析 `id → runtime_account_id`、校验 `owner_platform_user_id == session.platform_user`，再调现有 turn 核心（`run_turn_for_account`）；**不接受客户端 `account_id`**（§3.4）。
- `state='read_only'` 由 resident offline 事务原子切换（M4），P1 只需建列 + 读时拒写。

### 2.5 `universe_memory_facts`（L3 承载：append-only typed fact + 单 writer compact）

```sql
CREATE TABLE IF NOT EXISTS universe_memory_facts (
    id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL,                     -- L3 锚点（D-06），非 account
    fact_type TEXT NOT NULL,                       -- §1 L3 类枚举
    payload_json TEXT NOT NULL,                    -- 结构化事实体（非逐字原文，D-05）
    -- provenance（ADR §7.3 接缝②）
    source_account_id TEXT,                        -- 来源 resident 的 runtime account
    source_resident_id TEXT,
    source_message_id TEXT,
    occurred_at TEXT NOT NULL,
    -- compaction 状态（单 writer 维护，ADR §6.4 append-only）
    status TEXT NOT NULL DEFAULT 'active',           -- active | superseded
    superseded_by TEXT,                              -- compact 合并后新行的 id
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(universe_id) REFERENCES universes(id)
);
CREATE INDEX IF NOT EXISTS ix_universe_memory_facts_read
    ON universe_memory_facts(universe_id, fact_type, status);
CREATE INDEX IF NOT EXISTS ix_universe_memory_facts_universe_time
    ON universe_memory_facts(universe_id, created_at);
```
- **写路径 = SQL 侧原子追加**（仿 `profile_storage.append_file`，ADR §6.3）：各 resident 只 `INSERT` 带 provenance 的事实行，**永不就地改写整表/整段**，天然规避 last-writer-wins（ADR §6.4）。
- **compact = 单 writer**（挂 DreamingScheduler 单例，ADR §6.6）：合并/去重时 `INSERT` 一条合并行 + 把被合并行 `status='superseded'`、`superseded_by=<新行>`；append-only，不物理删。
- **读注入（ADR §6.1）**：`read_universe_context(universe_id)` 选 `status='active'` 行按 `fact_type` 渲染文本 → 域层包成 `ContextBlock(name='universe_l3', ...)` 经 `ctx.extra_blocks` 注入（接缝①）。
- ~~**L3 首刀**：`read_bazi_profile`/`write_bazi_profile` 后端改指本表（`fact_type='bazi'`）~~ **（废弃 2026-07-21：八字降级为无工具 skill、bazi 托管段整体移除；L3 首刀改由通用 `user_fact`/`user_preference` 承载）**。
- **隔离测试点**：写入必带 `universe_id`；`INSERT` 前校验 `source_resident_id` 属于该 `universe_id`（跨 universe 写拒绝）；读取只在同 `universe_id` 内；访客/他人世界零泄漏（复用 visit ACL，M5）。

### 2.6 事务与并发边界（P1 硬边界，已冻结）

- **create_runtime + resident 同一 PG 事务**（接缝④防孤儿账号）：`create_resident_with_runtime(..., uow)` 中 runtime account 插入 + `universe_residents` 插入 + `ai_conversations` 插入同 `uow.conn` 提交；任一失败整体回滚，不留无主 account。
- **容量校验在 world row lock 下**：确认/建居民前 `SELECT ... FROM universes WHERE id=? FOR UPDATE`（PG），再 `COUNT(status='active')` 校验 `≤ 10`；SQLite 用等价事务语义（只验功能）。
- **幂等**：bootstrap 幂等键 = `UNIQUE(owner_platform_user_id)`；confirm 幂等 = 重复提交同一集合返回同结果、不叠加。
- **锁顺序细则见 §2.7；老用户 backfill 分步伪码见 §2.8**（含多 account 老用户，D-08）。并发正确性 PG 用例见 ADR §9（PG 权威，SQLite 只验功能，D-12）。

### 2.7 锁顺序细则（PG 权威；死锁自由）

P1 全部涉锁操作共用**一条全局锁获取序**——按序获取、逆序释放，**任何操作不得逆序获取**（这是死锁自由的充分条件，标准全序论证）：

| 序 | 锁 | 载体（PG） | 持有者 | 锚 |
|---|---|---|---|---|
| L0 | 入站去重 | `UNIQUE(account_id,message_id)` + `message_id='app:'||conversation_id||':'||client_message_id` | turn（P1） | 约束非持锁，逻辑最先（§3.4-3） |
| L1 | 世界行锁 | `SELECT … FROM universes WHERE id=? FOR UPDATE` | confirm / 建居民（P1）；offline、来信接受（M4） | `universe_id` |
| L2 | 会话单飞 | `pg_try_advisory_xact_lock(advisory_lock_key('conv:'||conversation_id))` | turn（P1）；offline 切 `read_only`（M4） | `conversation_id` |
| L3 | 用户配额 | `pg_advisory_xact_lock(advisory_lock_key('quota:'||platform_user_id))` | turn 预占（P1，D-09） | `platform_user_id` |
| L4 | 钱包行 | `entitlement_wallets` 行锁 | 计费（**独立事务**，ADR §7.3 ④） | `platform_user_id`（M1 后） |

**结构性不变量**
- **L4 永不与 L1–L3 同事务嵌套**：money 路径留平台层、经 `idempotency_key` 跨事务对齐（ADR §7.3 ④、D-14），从根上消除「钱包锁 × 世界锁」交叉环。故 create_runtime+resident 的 UoW（§2.6）**不持 L4**。
- **每操作最多持一把 L1**：P1 的 confirm/建居民/turn 均单 universe；跨世界操作（visit 两世界）留 M5，届时按 `universe_id` ASC 获取两把 L1（同序，仍无环）。
- **turn 不取 L1**：turn 只持 L2(+L3)，与 confirm/建居民（只持 L1）**锁不相交**；唯一共享表 `accounts` 是 turn 读既有行、建居民插新行（新 `account_id`），无行争用。
- **死锁自由**：唯一同时持两锁者为 offline（L1→L2）与 turn（L2→L3），均顺全局序；无持有者逆序 ⇒ 无等待环。

**逐场景（映射 ADR §9 并发门禁）**
1. **两 resident 并发确认 / 第 10-11 位竞争**：并发 confirm/建居民对**同一世界行**争 L1 → PG 行锁串行。先者取 L1 → `COUNT(status='active')` → 校验 `≤10` → 插 resident(`status='active'`) → 提交释 L1；后者取 L1 → **重新** COUNT（已见前者结果）→ 超限即 `resident_capacity_exceeded`（§4）。偏唯一索引 `ux_universe_residents_runtime` / `ux_ai_conversations_resident` 为确认**重放**的第二道幂等防线（同 account/同 resident 不生成第二行）。
2. **offline 与新 turn 并发**（P1 只建 `ai_conversations.state` 列，原子切换在 M4）：offline 事务持 L1+L2 置 `state='read_only'`；turn 持**同一把** L2 读 `state` → 二者串行于 L2，不会出现「turn 读到 active、offline 已切 read_only」的撕裂。P1 只需保证 **turn 在 L2 下读 `state`**，`read_only` 即 `conversation_read_only`（§4）拒写。
3. **同 universe 两 resident 并发写 L3**：**无锁**——L3 为 append-only typed fact（§2.5 / ADR §6.4），各 resident 只 `INSERT` 带 provenance 行，天然无写冲突、无 last-writer-wins；compact 由**单 writer**（DreamingScheduler 单例，ADR §6.6）串行执行 `INSERT 合并行 + status='superseded'`，不与在线写争锁。
4. **跨居民同真人并发扣配额**：两 turn 同 `platform_user` 争 L3 → 串行预占、不超卖；失败**只回滚本 reservation**、不误伤他人配额（D-09 退款矩阵）。崩溃残留 reservation 由 TTL 回收（D-09-5）。

旧 App 路径的进程内 `threading.Lock` 不再承担 P1 world turn；新端点使用上表 L2，多节点一致。SQLite 档的进程锁只验功能（D-12），锁语义不作数。

### 2.8 老用户 backfill 分步伪码（含多 account，D-08）

**前置**：M1（D-14 钱包上迁 + 多钱包合并）已完成 → 一真人一钱包。故 backfill **不建钱包、不赠权、不 upsert subscription、不改写任何 Soul/Profile/Session/Message/Memory**（gap §7.1-3），只**加性**建 world/resident/conversation 映射，指向老用户**既有** runtime account。**幂等**：全程靠 UNIQUE 约束做 upsert，可重复运行、不重复建 world/resident/grant（ADR §9）。

**每 `platform_user` 一个单事务、逐用户提交（失败隔离、不锁全表；批处理外层遍历全部 platform_users）**：

```text
LEGACY_TEMPLATE_ID = ensure_sentinel_template(              # 全局一次，幂等
    source_type='operations', name='legacy', persona_seed_json=NULL, status='active')
    # 哨兵模板仅满足 universe_residents.character_template_id NOT NULL；
    # backfill 绝不套用 persona_seed（不改写既有 account 的 Soul/Profile）。

def backfill_user(platform_user_id):
  with uow():                                               # 单 PG 事务 = 接缝④ UoW
    # 1) 幂等建 home world（UNIQUE(owner_platform_user_id) → 重复运行命中同一行）
    universe = upsert_universe(owner_platform_user_id=platform_user_id,
                               status='active')             # ON CONFLICT DO NOTHING; 再 SELECT

    # 2) 取该用户全部 active binding（不再只取第一个 —— D-08 多 account）
    bindings = SELECT account_id FROM account_owner_bindings
               WHERE platform_user_id=? AND status='active'
               ORDER BY created_at ASC, id ASC              # 与 get_first_active_account_for_user 同序

    if not bindings:                                        # 无活跃 account（已解绑/异常老用户）
        set universe.onboarding_state = 'preparing'         # 不建 legacy resident；后续 bootstrap
                                                            # 按新用户流程创建固定 4 位候选
        return

    # 3) legacy 计费/路由锚 = 最早 active account（仅当列为空时写 → 幂等）
    set_if_null(universe.legacy_primary_account_id, bindings[0].account_id)
    set universe.onboarding_state = 'confirmed'             # 已有活跃居民即可用；不重放 onboarding、
                                                            # 不自动补 4 位预设（§2.9 冻结）

    # 4) 每个 active account → 一个 origin='legacy'、status='active' 的 resident（全部映射，D-08）
    for acc in bindings:
        resident = upsert_resident(                         # 幂等键：ux_universe_residents_runtime(runtime_account_id) 全局唯一
            universe_id=universe.id, runtime_account_id=acc.account_id,
            character_template_id=LEGACY_TEMPLATE_ID, template_version='legacy',
            origin='legacy', status='active')               # D-08：永不 offline，App 不给离开入口
        upsert_ai_conversation(                             # 幂等键：UNIQUE(resident_id)
            resident_id=resident.id, universe_id=universe.id,
            owner_platform_user_id=platform_user_id,
            runtime_account_id=acc.account_id, state='active')

    # 5) 不动钱包/subscription/grant/Soul/Profile/Session/Message/Memory —— 提交后零副作用回放
```

**幂等键与边界（逐条对齐 §2 schema）**
- `upsert_universe`：靠 `UNIQUE(owner_platform_user_id)`（§2.1）；重跑命中同一 world，不叠加。
- `upsert_resident`：靠偏唯一索引 `ux_universe_residents_runtime(runtime_account_id) WHERE NOT NULL`（§2.3）——同一 account 重跑不生成第二个 resident。
- `upsert_ai_conversation`：靠 `UNIQUE(resident_id)`（§2.4）。
- **哨兵 legacy 模板**：预置一行 `character_templates`（`source_type='operations'`、`persona_seed_json=NULL`），仅为满足 `character_template_id NOT NULL`；legacy 居民的人设仍由其既有 account 的 SOUL/IDENTITY 承载，backfill 不注入种子（§2.2）。
- **多 account（D-08）**：全部 active binding 各映射一个 `origin='legacy'` resident（保留豁免、永不 offline），与「取哪个当 primary」解耦——primary 仅用于 legacy `/chat/*` 路由与计费锚，不置顶、不写主角色字段（gap §7.1-4）。
- **满-10 边界【已冻结】**：极少数持满 10 active account 的老用户 → 10 个全部映射、世界显示容量已满；不删历史、不强制降级、不自动补预设。legacy 永不 offline，P1 不允许其继续新增居民。
- **backfill 时序**：per-user UoW 持 L1（世界行锁），与该 world 上的在线 confirm/建居民（§2.7）串行；跨用户事务互不相干、可并行推进。**必须在 M1 之后运行**（依赖一真人一钱包已就位）。

---

### 2.9 M2-C 产品冻结策略（2026-07-21）

1. **新用户初始集合 = 固定 4 位**：bootstrap 校验当前运营目录恰有 rank 1..4 四条 active 模板，并在同一 world 下幂等创建四条 candidate；用户 confirm 至少保留 1 位，无主角色。模板内容由运营提供，代码/迁移不得硬编码人设或密钥。
2. **候选/版本快照**：candidate 钉住 `character_template_id + template_version`；已发布模板版本不可原地改写。运营换版用“新行 + 退休旧行”，只影响尚未 bootstrap 的新世界；既有 candidate 仍可确认，既有 runtime persona 永不被静默覆盖。
3. **老用户全量映射、不自动补居民**：全部 active binding → legacy resident；最早一条只作 `legacy_primary_account_id` 兼容锚。已有居民即 `confirmed`，不重放 onboarding、不加四位预设；无 active binding 保持 `preparing`，按新用户 bootstrap；满 10 全保留并禁新增。
4. **App Dreaming 纳入定时扫描**：`__app_active__` 加入 `DEFAULT_ACTIVE_SESSION_KEYS`；L1/L2 仍 per-runtime，L3 compact per-universe 单 writer。中心单例 scheduler 执行，节点不重复，不产生主动消息。
5. **运营目录上线闸**：M2-C feature flag 默认关闭；启用前必须通过只读预检证明 rank 1..4 齐全、版本/persona/avatar 元数据完整。目录不完整时 bootstrap 返回 `preset_catalog_not_ready`，不得生成半套候选。
6. **新旧 auth 切换**：flag 关闭时 `/v1/auth/session` 完全保持现状；flag 开启后，切换截点之后的新用户只创建 `platform_user + session`，不再预建默认 runtime account/binding，session 响应 `account` 可为空并进入 world bootstrap。截点前用户先完成 §2.8 backfill；启用须与支持该响应的客户端最低版本同步。

---

## 3. DTO 与安全契约

### 3.1 响应信封（新世界端点统一，与客户端 OpenAPI 对齐）

客户端 §3.7 要求新接口统一稳定 `code` + `request_id` + `server_time`；本规范据此定信封（legacy `/chat/*` 不变，仅新 `/v1` 世界端点适用；公网网关可映射 `/api/v1`）：

```jsonc
// 成功
{ "code": "ok", "request_id": "req_...", "server_time": "2026-07-19T12:00:00+08:00", "data": { /* 端点 payload */ } }
// 失败（错误码见 §4）
{ "code": "resident_capacity_exceeded", "request_id": "req_...", "server_time": "...", "message": null }
```
- `message` 恒为 null 或稳定英文；**展示文案由客户端本地化**（后端不下发中文 UI 文案）。所有响应带 `Cache-Control: no-store`（复用 `_no_store`）。

### 3.2 P1 端点（只列承载不变量/安全语义的字段）

| 方法 | 路径 | 请求要点 | 响应要点 | 不变量 |
|---|---|---|---|---|
| POST | `/v1/worlds/home/bootstrap` | 空体；world 由 session `platform_user` 解析 | `data.world{id,onboarding_state,status}` + 固定 4 条 `data.candidates[]` | 幂等；目录 rank 1..4 齐全；candidate/版本快照（§2.9） |
| GET | `/v1/worlds/home/resident-candidates` | — | `data.candidates[]{template_id,template_version,name,avatar_ref,summary,tags,origin,status}` | 只返回本 world 已快照 candidate；**不含** `persona_seed_json`/内部 resident/runtime ID |
| POST | `/v1/worlds/home/residents/confirm` | `{selections:[{template_id, display_name?}]}` | `data.residents[]{resident_id,name,avatar_ref,status,origin,conversation_id,conversation_state}` | selections 必须来自本 world candidate；事务；确认后 active ∈ [1,10]；world row lock |
| GET | `/v1/worlds/home/residents` | — | 同上 `data.residents[]`（active+offline） | owner-scoped；不含 runtime account ID |
| POST | `/v1/worlds/home/residents` | `{template_id}` 或 `{name,persona_hint?}`（二选一） | selecting 时可返回 `data.candidate`；confirmed 时返回 `data.resident` | selecting 期至多 1 个自建 candidate；confirmed 期 active<10；no-grant 建号 |
| GET | `/v1/conversations` | `?cursor=&limit=` | `data.items[]{conversation_id,resident{id,name,avatar_ref,status},state,last_preview,unread}` + `next_cursor` | owner-scoped；P1 `unread=0`；预览只取 App scope |
| GET | `/v1/ai-conversations/{id}/messages` | `?cursor=&limit=` | `data{state,messages[]{id,message_id,role,message_type,text,created_at},next_cursor}` | owner 校验；只读该 runtime 的跨日 App scope |
| POST | `/v1/ai-conversations/{id}/turn` | `{client_message_id,text}`；extra forbid | `data{reply{text,message_id},no_reply,deduplicated}` | **不接受 account ID/media**；`read_only` 拒写；single-flight |

### 3.3 confirm / turn 请求体（关键结构）

```jsonc
// POST /worlds/home/residents/confirm
{ "selections": [ { "template_id": "tmpl_xxx", "display_name": "小满" } ] }   // 1–10 项；display_name 可选

// POST /ai-conversations/{id}/turn —— 显式渠道，无 account_id/media
{ "client_message_id": "client_12345678", "text": "在吗" }
```

### 3.4 安全契约（新端点硬规则）

1. **绝不接受客户端 `account_id`/`runtime_account_id`**：turn/history/资源全部由「session `platform_user` + 路径 `conversation_id`/`resident_id` → 服务端解析 runtime account + owner 校验」定位。请求体带 `account_id` → `400 account_id_not_accepted`。
2. **越权定位返回 404 不返回 403**：`conversation_id`/`resident_id` 存在但不属于当前 `platform_user` → 一律 `404 conversation_not_found`/`resident_not_found`（**不用 403**），避免 ID 枚举确认他人资源存在。
3. **去重键**：服务端映射 `client_message_id → app:{conversation_id}:{client_message_id}`，再由既有 `UNIQUE(account_id,message_id)` 保证唯一；重试不重复计费/不重复入库。
4. **turn single-flight 下沉**：P1 起用 DB 可见状态 / PG advisory lock 替换进程内 `threading.Lock`（`app_api.py` 现状 `_turn_lock`），多节点一致（客户端 §3.6）。
5. 所有新端点：`Cache-Control: no-store` + 信封（§3.1）+ 会话鉴权（Bearer）。

---

## 4. 稳定错误码表

沿用现有 `HTTPException(status_code, detail=<code>)` 机制（新端点把 `detail` 收敛为**稳定英文 code**，配合 §3.1 信封）；既有码 `account_not_ready`/`account_disabled`/`turn_in_progress`/`rate_limited` 复用。

| code | HTTP | 触发 | 备注 |
|---|---|---|---|
| `ok` | 200 | 成功 | 信封 `code` |
| `not_found` | 404 | feature flag 关闭时隐藏整组 world 路由 | 默认关闭/回滚语义 |
| `unauthorized` | 401 | 无/坏 Bearer | 替换现状中文 `"未登录"`（新端点统一英文码） |
| `account_id_not_accepted` | 400 | 请求体携带 `account_id`/`runtime_account_id` | §3.4-1 契约违反 |
| `invalid_request` | 422 | 字段长度、类型或 extra 字段不合法（account ID extra 除外） | 稳定 validation envelope |
| `world_not_ready` | 409 | home world 未 bootstrap | 客户端应先 bootstrap |
| `world_disabled` | 403 | `universes.status='disabled'` | |
| `template_not_found` | 404 | 选用模板不存在 | |
| `template_not_available` | 409 | 直接新增时模板 `status='retired'` 或非本人自建 | 已快照到本 world 的 candidate 仍允许 confirm |
| `preset_catalog_not_ready` | 503 | active 初始目录不是恰好 rank 1..4 四条或必需元数据缺失 | feature flag 上线前预检应阻断此状态 |
| `resident_not_found` | 404 | resident 不存在**或非本人**（防枚举） | §3.4-2 |
| `resident_capacity_exceeded` | 409 | 确认/建居民后 active > 10 | world row lock 下判定（D-07） |
| `resident_capacity_empty` | 409 | confirm 集合为空（active 会 < 1） | 初始集合不为 0（ADR §9） |
| `resident_selection_invalid` | 400 | confirm 重复选择或混入非本 world candidate | |
| `resident_already_exists` | 409 | confirmed world 再选已存在模板 | |
| `custom_candidate_limit_exceeded` | 409 | selecting 阶段第二个自建 candidate | P1 至多 1 个 |
| `conversation_not_found` | 404 | 会话不存在**或非本人**（防枚举） | §3.4-2 |
| `conversation_read_only` | 409 | 向 offline resident 的 read_only 会话发 turn | §2.4 |
| `turn_in_progress` | 409 | 同会话并发 turn（single-flight） | 复用现有码 |
| `rate_limited` | 429 | 配额/RPM（按 `platform_user` 聚合，D-09） | 复用现有码 |
| `account_disabled` | 403 | runtime account 被禁用 | 复用现有码 |

HTTP 语义约定：400 请求契约违反 / 401 未鉴权 / 403 资源被禁 / 404 不存在或越权（防枚举）/ 409 状态或容量冲突 / 422 字段校验 / 429 限流。

---

## 5. 未覆盖与后续

- **锁顺序细则 + backfill 伪码**：已补（§2.7 / §2.8），M2-0 前置门清零。剩余锁语义（offline 原子事务、visit 双世界锁）随 M4/M5。
- **`app_notifications` / `universe_posts`**：M3（形状见 ADR §11.8 T3-1、§8 R3）。
- **信箱 / 生命周期 / 访客 / 真人聊天**：M4–M5（客户端 §4.4–4.6）。
- **客户端口径对齐**：gap-analysis §4.2「默认隔离/白名单」需按 D-05「全量共享沉淀记忆」镜像更新（本轮不动客户端仓库）。
- **App scope 漏扫已冻结修复**（ADR §6.6 / 本文 §2.9）：M2-C 将 `__app_active__` 纳入 `DEFAULT_ACTIVE_SESSION_KEYS`，并覆盖 App scope 每日轮转；L3 compact 仍是 per-universe 单 writer。

## 6. 发布状态（2026-07-22）

- C0–C5 代码已完成，迁移版本 30，`COMPANION_WORLD_P1_ENABLED` 默认 false。
- 最终门禁：unit 565 passed；SQLite 1424 passed / 8 skipped；PostgreSQL 1428 passed / 4 skipped。
- 已有运营工具：`scripts/import_companion_world_presets.py`（manifest 校验、dry-run、immutable/version 闸）与 `scripts/backfill_companion_world.py`（dry-run、cutoff、resume、逐用户事务）。
- 代码完成不等于生产完成：正式四模板、客户端最低版本、生产模板导入/backfill/对账仍缺现场证据，故不得提前开 flag。
- 发布和回滚步骤以 [`../guides/admin_guide.md`](../guides/admin_guide.md#companion-world-p1-发布运行手册) 为准。
