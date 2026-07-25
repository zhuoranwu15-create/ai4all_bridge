# Companion World M2-C 实施计划

> 状态：**已完成并归档（2026-07-22）**。本文件保留为实施 provenance，不再是后续待执行计划；后续接手以 [`../../../architecture/designs/companion_world_3_0_refactor_design.md`](../../../architecture/products/zhaoxi/companion_world_3_0_refactor_design.md) 为入口。
>
> 决策冻结：2026-07-21（ADR §10.1/.2/.6）
>
> 代码基线：`main@3f42ee1`（PR #44 已合并）
>
> 工作分支/核查基线：`feat/companion-world-m2c@6000c0b`，Draft PR #45

## 1. 目标

在不改微信 turn/prompt/投递入口的前提下，交付朝夕相伴形态 B 的第一个完整多居民闭环；backfill 后 L3 后台沉淀与 proactive 安全阀会作用于 legacy resident，实际边界以上位 ADR 为准：

- 新用户获得一个 home universe 和固定 4 位、版本快照的预设候选；确认后生成 1–10 位独立 runtime resident。
- 老用户全部 active binding 加性映射为 legacy resident，不重写历史、不自动补居民。
- App 使用显式 `conversation_id` 访问历史和发 turn，服务端解析 runtime account，客户端不能传 `account_id`。
- 同世界居民共享新增的用户沉淀事实 L3，私聊原文、检索、L1/L2 继续隔离。
- App session 纳入每日 Dreaming；真人级 proactive 不因 N 个居民放大。
- 新 World API 与 auth 切换由默认关闭的 feature flag 门控，可退回 legacy App/微信入口。L3 后台链路与 world-aware proactive 安全阀不是该 flag 的子开关；真实回滚边界见上位 ADR“接手说明”。

## 2. 已冻结口径

1. 新用户初始目录固定 4 位；可删减至至少 1 位，无主角色。
2. 已发布模板不可原地改写；bootstrap 快照 `template_id + persona_version`，换版只影响后续新世界。
3. 老用户全部 active binding 映射 legacy resident；不自动补 4 位预设；无 active binding 走新用户流程；满 10 全保留并禁新增。
4. `__app_active__` 纳入定时 Dreaming；L1/L2 per-runtime，L3 per-universe 单 writer compact。
5. legacy resident 永不 offline；App 不提供其离开入口。

## 3. 范围边界

### 本轮包含

- bootstrap / candidates / confirm / resident list / resident create。
- AI conversation list / history / turn。
- runtime account + resident + conversation 同事务创建。
- 老用户幂等 backfill 与预设目录导入/预检脚本。
- L3 读注入 live 接线、Dreaming typed fact 加性写入、最低限度幂等 compact。
- App Dreaming scope、M2 防 N× 安全阀、feature flag 与发布观测。

### 本轮不包含

- Feed、App 通知收件箱、真人级 proactive 完整上提（M3）。
- AI 离开、farewell、信箱（M4）。
- 访客、邀请码、真人聊天（M5）。
- 用户主动移除已确认 resident（ADR §10.3 未冻结）。
- 语义冲突型 L3 智能合并；P1 compact 只做确定性重复折叠，复杂冲突留后续。
- 删除 legacy `/v1/chat/*`、旧 account/profile/session/message 数据或 owner binding。

## 4. 依赖方向

保持现有门禁：

```text
app/routers/companion_world.py
  → app/domains/companion_world/service.py（纯领域编排）
      → 注入的 WorldRepository Protocol
  → app/agent_runtime/adapter.py（turn composition）

app/platform/companion_world_repository.py
  → app/db/companion_world.py / billing.py / accounts.py
  → 同一 UoW 内插 runtime account + 激活 resident + 建 conversation

app/agent_runtime/adapter.py
  → app/turn_service.py + Runtime DB 入口
```

`app.domains.companion_world.*` 不得 import `app.db.*` 或 `app.turn_service`；`app.agent_runtime.*` 不得反向 import `app.domains.*`；`tests/test_layer_boundaries.py` 继续作为阻塞门禁。P1 adapter/L3 I/O 仍含 World 专用参数和 DB 读取，已在上位 ADR 登记为 M3 前需收敛的 D-06 有界实现债。

## 5. 分批实施

M2-C 各批已串行提交；各批均可独立测试。feature flag 关闭时新 World API/auth 行为不启用，但 C4 L3 后台链路与 C5 proactive 安全阀按 world 数据生效，不能表述为所有 live 行为不变。

### C0：冻结 schema 补丁 + App Dreaming scope

| 文件 | 改动 |
|---|---|
| `app/db/_core.py` | 追加 migration m0030：`character_templates.initial_candidate_rank`、active rank 偏唯一索引、非 legacy `(universe_id, character_template_id)` 偏唯一索引；新增 `APP_ACTIVE_SESSION_KEY='__app_active__'` 并加入 `DEFAULT_ACTIVE_SESSION_KEYS`。不改 m0028/m0029。 |
| `app/channels.py` | 保持 App active key 字面量一致；只补交叉一致性注释/测试，不引入 channels↔db 循环。 |
| `tests/test_companion_world_schema.py` | 覆盖 m0030 幂等、rank 唯一、legacy 哨兵模板多映射豁免、非 legacy 同模板不重复。 |
| `tests/test_session_lifecycle.py` | 将现有微信+Web 轮转用例扩为微信+Web+App，确认 App session 被每日扫描且 scope 间不串线。 |

出口：SQLite/PG 均能从 1→30 顺序初始化；feature flag 尚未接线，不产生新 API 行为。

### C1：领域契约、SQL adapter 与原子建居民

| 文件 | 改动 |
|---|---|
| `app/domains/companion_world/contracts.py`（新） | `WorldRepository` Protocol、领域 DTO、稳定领域错误；不依赖 DB/FastAPI。 |
| `app/domains/companion_world/service.py`（新） | bootstrap/confirm/list/create/resolve conversation 的领域编排、容量和状态机校验。 |
| `app/platform/companion_world_repository.py`（新） | 用同一 `conn` 实现 world row lock、candidate 快照、resident 激活、conversation 创建、owner-scoped 查询与 backfill 原语。 |
| `app/agent_runtime/adapter.py`（新） | 实现 `AgentRuntimePort.send_turn/resolve_conversation_account`；Runtime 不认识 universe 共享策略。 |
| `app/db/companion_world.py` | 增加 initial rank 查询、幂等 candidate、owner-scoped resident/conversation、world lock、activate/dismiss、legacy upsert、L3 compact 原语；公共方法补注释。 |
| `app/db/billing.py` | 从现有 `create_resident_runtime_account` 抽出可接受外部 `conn` 的 runtime account 插入原语；旧公共函数继续复用并保持行为/测试。confirm 使用该原语把 account/profile + 既有 candidate 激活 + conversation 放入一个事务，失败不留孤儿。 |
| `app/db/accounts.py` | 新增按 `runtime_account_id + '__app_active__'` 跨日 session 游标读取消息的 helper，禁止混入微信/Web scope。 |

事务顺序：L1 world row lock → 校验 active/candidate → 插 runtime account/profile → 激活 resident → 插 ai_conversation → commit。钱包不进入该事务，居民不赠权、不建 owner binding。

### C2：bootstrap、confirm、自建与老用户 backfill

| 文件 | 改动 |
|---|---|
| `app/routers/companion_world.py`（新） | `/v1/worlds/home/bootstrap`、candidates、confirm、resident list/create；统一 `code/request_id/server_time` 信封、`Cache-Control: no-store`、404 防枚举。外部 `/api/v1/*` 继续由现有 prefix middleware 兼容。 |
| `app/main.py` | central/standalone 挂新 router；node 角色不暴露产品控制面。 |
| `app/config.py` | `companion_world_p1_enabled: bool = False`；默认关闭。 |
| `.env.example` | 说明 flag、启用前目录/回填/客户端版本前置条件。 |
| `app/routers/app_api.py` | flag 关闭保持现状；flag 开启后，新注册用户只创建 `platform_user + session`，不再创建默认 runtime account/binding，`account` 响应允许为空并引导 bootstrap。既有用户仍复用原 account。 |
| `scripts/import_companion_world_presets.py`（新） | 从受控 JSON 导入/更新四条运营模板元数据；支持 `--dry-run`，不在代码或迁移中硬编码人设。已发布版本只新增/退休，不原地改 persona。 |
| `scripts/backfill_companion_world.py`（新） | `--dry-run/--batch-size/--created-before/--resume-after`；逐 platform_user 单事务；全部 active binding 映射、最早写兼容锚、零 binding 留 preparing、满 10 记录但不报错。输出计数与异常账号清单，不打印聊天/手机号明文。 |
| `tests/test_companion_world_api.py`（新） | 4 candidate、版本快照、bootstrap/confirm 幂等、删至 1、空集合拒绝、容量 10/11、owner ACL、retired 快照仍可 confirm、自建 candidate 至多 1。 |
| `tests/test_companion_world_backfill.py`（新） | 0/1/N/10 binding、重复运行、历史不改写、无 grant/wallet 副作用、legacy 多 account 全映射。 |
| `tests/test_app_api.py` | flag 关闭旧 auth 字节级回归；flag 开启的新用户无默认 account、旧用户仍复用；响应与客户端版本门兼容。 |
| `tests/conftest.py` | `fresh_db` 与 `client` 两组 fixture 都 patch 新 router/settings；默认 flag false。 |

切换要求：先在 flag=false 部署并回填存量，再以确定的 `created-before` 截点重跑；只有客户端最低版本已支持 `account: null + world bootstrap` 后才开 flag。回滚关 flag 后，未建 legacy account 的新用户需重新登录走兼容建号，已有世界数据不删除。

### C3：显式 AI conversation/history/turn + L3 读注入

| 文件 | 改动 |
|---|---|
| `app/routers/companion_world.py` | 增加 conversation list、history、turn；请求体拒绝 `account_id/runtime_account_id`。 |
| `app/platform/companion_world_repository.py` | `conversation_id → owner/universe/resident/runtime_account` 单次 owner-scoped 解析；越权与不存在统一 404。 |
| `app/agent_runtime/adapter.py` | 构造 `ResolvedIdentity + ChannelTurnInput`；调用 `read_universe_context(universe_id)` 后填 `extra_blocks`；App cap/计费/审核/配额继续复用现有 turn。 |
| `app/db/companion_world.py` | PG 用 `pg_try_advisory_xact_lock(advisory_lock_key('conv:'+conversation_id))` 做跨节点 single-flight；SQLite 仅保留进程锁作功能回退。锁失败映射 `turn_in_progress`。 |
| `tests/test_companion_world_conversations.py`（新） | owner ACL、防枚举、历史只含目标 resident 的 App scope、跨 resident/跨 universe 不串线、消息幂等、L3 同世界注入/他世界不可见。 |
| `tests/test_companion_world_concurrency_pg.py`（新） | 同 conversation 并发只一条进入；第 10/11 resident 竞争；confirm 重放不重复建 account/conversation/grant。 |

P1 无异步 App 主动消息，因此 conversation `unread=0` 是真实值；未读状态随 M3 通知收件箱再扩展。

### C4：Dreaming typed sink + L3 最低闭环

| 文件 | 改动 |
|---|---|
| `app/dreaming.py` | `long_term_memory_items` 增必填 `fact_type`；现有 per-account USER/MEMORY 应用保持不变（零回归），同时向可选通用 `MemorySink` 发结构化事件。原始聊天不进 event。 |
| `app/domains/companion_world/memory_sink.py`（新） | fail-closed 路由 `user_identity/user_preference/user_profile_derived/user_event → L3`；`relationship/commitment` 不写 L3。依赖注入 writer，不 import DB。 |
| `app/platform/companion_world_memory.py`（新） | 由 `source_account_id` 解析 resident/universe，校验同世界后 append provenance fact；非 resident/form-A 为 no-op。 |
| `app/dreaming_scheduler.py`、`app/session_lifecycle.py` | 从 composition root 注入 sink；中心单例扫描 App runtime；每轮附带 per-universe 确定性 compact batch。 |
| `scripts/run_proactive_scheduler.py`、`app/main.py` | 两种单例承载方式使用同一 sink 构造器；配置仍保证二者互斥。 |
| `app/db/companion_world.py` | P1 compact 只折叠 exact-normalized duplicate：append 合并行/保留最新 provenance，把旧行 superseded；幂等、不物理删。 |
| `tests/test_companion_world_memory_sink.py`（新） | fact_type 路由、未知类型拒绝、原文排除、跨 universe 拒绝、form-A no-op、同世界可见。 |
| `tests/test_dreaming.py` / `tests/test_dreaming_scheduler.py` | App scope、sink 注入、per-account 写零回归、compact 单 writer/幂等。 |

### C5：M2 防 N× 安全阀

| 文件 | 改动 |
|---|---|
| `app/platform/companion_world_repository.py` | 查询 account 是否为 world resident、是否为 `legacy_primary_account_id`。 |
| `app/proactive/orchestration/planning.py` | 真人级 reactivation/content invitation/account_check 在生成前：非 legacy primary 的 world resident 跳过；App-only 用户 fail-closed，避免烧 LLM。 |
| `app/proactive/delivery/dispatch.py`、`app/proactive/delivery/account_check.py` | 投递前再做同一防御检查。 |
| `tests/test_proactive_companion_world_gate.py`（新） | 一真人 N resident：真人级最多 primary 一次；per-resident reminder/commitment 不误杀；形态 A 单 account 行为不变。 |

本批不实现 M3 的“最近互动 resident 发声”和 App 收件箱；这些仍受 §10.12/.13 与 M3 范围约束。

### C6：发布闸与文档

| 文件 | 改动 |
|---|---|
| `docs/architecture/designs/../../../architecture/designs/companion_world_3_0_refactor_design.md` | 更新实际落地状态、偏差与回滚说明。 |
| `docs/architecture/designs/companion_world_p1_backend_spec.md` | 对齐最终 DTO/schema/错误码/锁实现。 |
| `docs/companion_world_3_0_重构简报.md` | 写入测试计数、迁移/回填/flag 状态和下一门槛。 |
| `docs/guides/admin_guide.md` 或新 runbook | 预设导入、backfill dry-run、切换顺序、观测、回滚。 |

## 6. 测试与验收

### 每批聚焦

- C0：schema migration + session lifecycle。
- C1/C2：world domain/repo/API/backfill，SQLite + PG。
- C3：conversation/ACL/turn + PG advisory/容量竞争。
- C4：Dreaming/sink/L3 append+compact。
- C5：proactive 分类正交回归。

### 合并前强制门禁

```bash
make test-unit
make test
make test-pg
git diff --check
```

PG 必须证明：

- runtime account + resident + conversation 任一失败整体回滚，无孤儿。
- 同 world 第 10/11 位竞争只有一个成功。
- confirm/重试不重复建 resident/account/conversation、不重复赠权。
- 同 conversation 多节点并发只一个 turn 进入。
- 两 resident 并发 append L3 不覆盖，compact 幂等。
- backfill 与在线 bootstrap/confirm 的 world lock 顺序无死锁。

### 账号隔离自审

- 所有 owner 资源从 session `platform_user_id` 解析，不接受客户端 account ID。
- resident/history/turn 越权统一 404，不能确认他人 ID 存在。
- App history 只读目标 runtime 的 `__app_active__` sessions，不混微信/Web/其他 resident。
- L3 仅同 universe 注入；访客路径不存在且不得复用 owner API。
- legacy 原 Soul/Profile/Session/Message/Memory/wallet 不重写。

## 7. 发布顺序与回滚

1. flag=false 部署代码和 m0030；跑双后端 CI。
2. 导入四条运营模板，先 `--dry-run`，再预检 rank/版本/元数据。
3. 记录切换截点，backfill `created_at <= cutoff` 的存量用户；核对 0/1/N/10 分布和异常清单。
4. 客户端最低版本支持新 auth/world API 后，小流量开启 `COMPANION_WORLD_P1_ENABLED`。
5. 观察 bootstrap/confirm 成功率、孤儿数、容量冲突、turn 锁冲突、L3 append/compact、Dreaming 时延。
6. 扩量前再跑 PG 并发档与 backfill 幂等复核。

回滚：关闭 flag，停止新 world API 与新用户无默认 account 路径；不删除 universe/resident/conversation/L3 数据。既有微信与 legacy `/chat/*` 保持原入口，但 L3 sink 与 proactive 安全阀不会随 flag 撤销。切换后新建且没有 legacy account 的少量用户，回退时重新登录由旧 auth 兼容建号；该数量必须由切换监控可枚举。

## 8. 生产启用前仍需的非代码输入

- 4 位首发角色的名称、头像引用、简介、三个标签、`persona_seed_json` 与 `persona_version`。
- 支持新 auth 响应和 world API 的客户端最低版本号及上线窗口。

这些输入未阻断 C0–C5 的代码/测试夹具开发，但仍阻断生产 flag 开启；实现不得自行编造正式角色内容。客户端还须同步 D-05 L3 全量共享沉淀记忆与 D-08 legacy 离开豁免口径。

## 9. 执行记录

已按 C0 → C1 → C2 → C3 → C4 → C5 → C6 串行完成：

- C0 `7471ca8`：m0030 + App Dreaming scope。
- C1 `2f419de`：World 领域/UoW/原子居民创建。
- C2 `ec980ad`：API、模板导入、legacy backfill、auth 切换。
- C3 `f5fd3c5`：conversation/history/text turn + PG single-flight。
- C4 `244d7a7`：typed memory sink + L3 compact。
- C5 `a47d41e`：真人级 proactive 防 N× 安全阀。
- C6 `6000c0b`：发布运行手册及文档收尾。

最终门禁：unit 565 passed；SQLite 1424 passed / 8 skipped；PostgreSQL 1428 passed / 4 skipped；`git diff --check` 通过。后续不得从本计划继续追加 C7；M3 起点、产品冻结项和已知偏差统一回到上位 ADR。

M2-C 归档后的 M3 前置收口不计作 C7：m0031 将 quota override 上迁 `platform_user` 并接 central TTL 回收；m0032 修复 PG RPM epoch 单精度；World/L3 turn composition 从 `app.agent_runtime` 移至 `app.platform`。当前状态与门禁以 ADR“接手说明”为准。
