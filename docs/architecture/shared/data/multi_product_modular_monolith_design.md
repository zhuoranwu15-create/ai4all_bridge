# 技术设计：多产品模块化单体 —— 产品边界、身份隔离与分阶段迁移（架构决策记录）

更新时间：2026-07-25
状态：**MP-01～MP-06 已生产发布；MP-07A 四批目录迁移已完成开发与聚焦回归，待分批评审合并。开放问题 O-1～O-7 的结论不变，Fatetell 业务仍等待 PRD。** 本文用于在 Fatetell（命理类产品）开发前冻结「产品级模块边界 + 身份/计费隔离」的架构决策与迁移顺序。
核查基线：Phase 1 merge commit `f4baa3b`。当前 max migration = `m0046`（`app/db/_core.py`）。

路由口径：后端 `APIRouter` 前缀是 `/v1`（`app_api.py:52`），公网 Nginx 映射为 `/api/v1`。下文写 `/api/v1/...` 指公网口径，对应后端 `/v1/...`；实现与测试勿混用。

关联后端设计：
- [`architecture/overview.md`](../../overview.md) §2 已确立四层概念模型与依赖方向，本文是其在「多产品」维度上的延伸。
- [`companion_world_3_0_refactor_design.md`](../../products/zhaoxi/companion_world_3_0_refactor_design.md)（D-01…D-14）：Agent Runtime 形态无关 + Companion World 产品领域层的既有重构，本文**叠加而非推翻**。
- [`identity_model_and_wechat_binding.md`](../access/identity_model_and_wechat_binding.md)、[`companion_world_account_model_reconciliation.md`](../../../archive/deliveries/companion_world/companion_world_account_model_reconciliation.md)：真人身份与账号收敛模型。
- [`multi_product_modular_monolith_implementation_plan.md`](../../../plans/shared/multi_product_modular_monolith_implementation_plan.md)：当前 Phase 1 的 MP-01…MP-06 精简工单；Fatetell 产品接入等待 PRD 后再拆。

审计 provenance：本文由一轮 codex 架构审计 + 逐条代码核查形成，采纳其方向与优先级，并在四处做了修正（见 §9）。

---

## 0. 摘要（TL;DR）

后端要从「服务单一产品朝夕相伴」演进到「同一模块化单体服务多个产品」，下一个产品是 **Fatetell（命理类）**。

核心判断：**不拆微服务、不为每个产品复制 Agent Runtime、不做全仓大搬家。** 真正优先的是补齐 `app_id` 的**认证、查询、计费与数据隔离**——这几处是会随第二个产品上线立刻爆的越权/串号 bug，优先级高于目录重组。

三条已冻结的产品输入（本轮评审确认）：

1. **身份共享**：同一手机号 = 跨产品同一 `platform_user`；`platform_users` 保持平台全局，隔离锚点是其下的 `app_id`。
2. **计费完全按产品隔离**：钱包（wallet）、订阅、配额、成本核算、**邀请/推荐关系**全部按 `(platform_user_id, app_id)` 切开——比业界「钱包全局共享」的默认更彻底。「新客」也按首次创建该产品 membership 判断，而不是按是否首次创建全局 `platform_user` 判断。
3. **Fatetell 复用完整 Agent Runtime**：会话 + 记忆 + 主动消息全套，因此它是 Runtime 端口（`AgentRuntimePort` / `MemorySink` / `ProactiveDeliveryAdapter`）的第二个真实调用方，会兑现 3.0 D-02「方法从真实调用点长出」的价值。

---

## 1. 目标与非目标

### 目标

- 在 Fatetell 开工**前**冻结四层身份模型与产品边界。
- 补齐 `app_id` 隔离基座：session 作用域、账号解析、计费子树、邀请关系。
- 让「新增一个产品**不需要改 Runtime 核心**」成为可验证的架构不变量。
- 朝夕相伴现有微信 + Native App + Web 行为**零回归**。

### 非目标

- **不**拆微服务，保持模块化单体 + 中心单 writer（沿用 3.0 结论）。
- **不**做全仓一次性大搬家；Phase 1 原地完成隔离，上线后只分批迁移归属明确的朝夕模块（见 §7）。
- **不**为 Fatetell 复制一套 Runtime。
- **不**在本阶段建 `runtime_ownerships` 投影表（推迟，见 §9.3）。

---

## 2. 四层概念模型（冻结）

```text
platform_user（真人，平台全局，跨产品共享手机号/登录）
├── product_membership: zhaoxi            # (platform_user_id, app_id) 一行
│   ├── channel: weixin / native / web    # 传输渠道，不是产品
│   ├── 朝夕产品领域
│   │   └── Companion World
│   ├── 独立计费域（wallet/订阅/配额/referral 按 app_id 切）
│   └── N 个隔离 Agent Runtime（account_id）
│
└── product_membership: fatetell          # (platform_user_id, app_id) 一行
    ├── channel: native / web / ...
    ├── Fatetell 产品领域（命理：盘/命格…留在产品域，绝不进 Runtime）
    ├── 独立计费域
    └── N 个隔离 Agent Runtime（account_id）
```

| 概念 | 定义 | 作用域锚点 |
|---|---|---|
| `platform_user` | 真人的全平台身份，跨产品共享手机号与登录 | 平台全局 |
| `app_id` / product_id | 产品边界，例：`zhaoxi`、`fatetell` | 产品级 |
| `product_membership` | 一个真人在一个产品内的成员关系与状态 | `(platform_user_id, app_id)` |
| `channel` | 传输渠道：`weixin` / `native` / `web`，**不是产品** | 渠道级 |
| `account_id` | 一个 AI 关系实例的 Runtime 隔离容器 | runtime 级 |

`product_membership` 的存在性本身有语义：**无行 = 未加入产品**；有行时 `status` 仅允许 `active | disabled`。不存在 `status='none'` 的占位行。session、入口账号创建以及计费/referral 写入都必须校验对应 membership 为 active。membership 生命周期与产品自己的 onboarding FSM 仍是两个概念。

**Companion World 是 zhaoxi 产品下的业务领域，不是系统顶层。** Agent Runtime 只处理单个 Agent 的 turn/prompt/tools/session/memory primitives，不认识世界、算命盘或产品名。

> **account 数量口径（与 MP-05 对齐，避免 P1 冲突）**：每个 `(user, app)` 至多一个 active 的**直接绑定/入口账号**（`account_owner_bindings` 语义，形态 A 微信独有，见 `_core.py:482`）；一个产品领域可拥有 **N 个 Runtime account**（如朝夕居民 form-B account 不发 binding，由产品域 ownership 模型解析到真人）。二者不矛盾：≤1 约束的是"入口/绑定账号"，不是全部 Runtime account。

> 与 `architecture/overview.md §2` 的关系：那里已有「接入与产品 API → 产品领域 → AgentRuntimePort → 平台服务」的四层。本文在「平台服务」和「产品领域」之间显式插入 `app_id` 作为强隔离维度，并把 Companion World 明确降为 zhaoxi 产品域的子领域。

---

## 3. 决策记录（MP = Multi-Product，与 3.0 的 D-xx 并存）

- **MP-01（身份全局，隔离靠 app_id）**：`platform_users` 保持平台全局。真人跨产品同一身份，一切产品级隔离由 `app_id` 分层实现。禁止为「隔离」而 fork 真人身份。
- **MP-02（计费全隔离）**：wallet / subscription / quota / cost 核算 / **referral 关系与奖励** 一律按 `(platform_user_id, app_id)` 隔离。朝夕的充值、券、邀请奖励绝不进入 Fatetell，反之亦然。邀请码资格、新客赠权和内部 `is_new_membership` 一律以「本次是否首次创建该 `(platform_user_id, app_id)` membership」为准；旧 API 如继续返回 `is_new_user` 字段，其语义也切为产品级新成员。已有朝夕身份的真人首次加入 Fatetell 仍是 Fatetell 新成员，可使用 Fatetell 邀请码并获得 Fatetell 新客权益。
- **MP-03（Runtime 形态无关，产品差异经端口传入）**：Runtime 内**禁止**出现 `if app_id == "zhaoxi": ... elif "fatetell": ...`。产品差异经端口注入。**已冻结的 4 接缝**（`ports.py`）：① context 注入（`ChannelTurnInput.extra_blocks`）② `MemorySink` ③ `ProactiveDeliveryAdapter` ④ `UnitOfWork`。**候选扩展点（尚未落地，勿当已冻结）**：`ContextProvider` / `ToolPolicy` / `AfterTurnHook` / `RuntimePersona(Blueprint)`——随 Fatetell 真实调用点长出（D-02）。此为 3.0 D-06 在多产品下的强化。
- **MP-04（session 产品作用域，不信客户端；引入 SessionPrincipal）**：不能只给 `platform_user_sessions` 加 `app_id` 列——改造前 `get_platform_user_by_session_token` 只 `SELECT pu.*`，会丢掉 session 身份与作用域。须引入 `SessionPrincipal{session_id, platform_user_id, app_id, expires_at}`，**所有产品 API 依赖 principal 而非裸 platform_user**，且 resolver 必须同时确认对应 membership 为 active：旧 `/v1/*` **及旧 `/web/*`** 强制 `app_id=zhaoxi`；新 `/v1/products/{app_id}/*` 强制路径 app_id == principal.app_id；app_id 只来自注册表与服务端路由，**绝不**取自任意 Header。须**收口全部 token resolver 直接调用点**。
- **MP-05（入口账号解析必带 app_id）**：以 `get_active_bound_account_for_user_in_app(platform_user_id, app_id)`（或 `get_primary_entry_account`）取代裸 `get_first_active_account_for_user`；命名须明确它解析的是**绑定/入口账号**、不解析产品全部 Runtime account。「≤1 active」不变量收紧为 per-`(user, app)` 的**入口账号**（见 §2 account 数量口径）。
- **MP-06（依赖红线由 AST 门禁强制）**：见 §4，把现有 `tests/test_layer_boundaries.py` 从硬编码 `companion_world` 泛化为通用产品边界门禁。
- **MP-07（API 显式产品命名空间）**：新产品走 `/api/v1/products/{app}/*`；旧 `/api/v1/*` 保留为朝夕兼容别名，不做破坏性迁移。
- **MP-08（增量迁移，先实体化已知边界）**：Phase 1 不物理搬迁；生产发布后可先把归属明确的朝夕垂直切片移入 `app/products/zhaoxi/`。Fatetell 仍是第一个按新结构从零开发的产品，不提前创建空骨架或猜测契约。
- **MP-09（所有权投影推迟）**：`runtime_ownerships` 投影表推迟到 Fatetell 所有权模型明确后再建，且建则作为**唯一**投影（朝夕回填为其中一个 writer），不维护双事实源（见 §9.3）。
- **MP-10（product_membership 为计费规范锚点）**：计费归属以 `product_memberships` 为规范锚，对 `(platform_user_id, app_id)` 建唯一约束。所有落到子表的冗余 `app_id` 写入时**必须校验**与 `accounts.app_id` / wallet / membership 一致，不能只加列——否则仍可能写出跨产品错账。✅ 钱包锚点已澄清（修正初稿事实）：`entitlement_wallets` 早由 **D-14 / 迁移 `m0025`** 上迁为**真人级**（局部唯一 `ux_entitlement_wallets_user_active ON (platform_user_id) WHERE status='active'`，`_core.py:2048`；`account_id NOT NULL UNIQUE` 仅存"创建来源"、已非有效锚，`billing.py:652` 按 `platform_user_id` get-or-create），与 D-09 真人级聚合**本就一致、无冲突**。多产品化只需把该局部唯一扩为 `(platform_user_id, app_id)` 并加 `app_id` 列（O-5 已定）。

---

## 4. 依赖红线（AST 门禁强制）

在 `tests/test_layer_boundaries.py` 现有机制上泛化，新增/强化以下规则：

1. `app.agent_runtime.*` 永不 import `app.products.*`（及任何具体产品域）。
2. `app.platform.*` 永不 import 某个具体产品。
3. `app.products.zhaoxi.*` 与 `app.products.fatetell.*` 互不 import。
4. 产品 domain 不直接 import FastAPI、SQL repository 或 `app.turn_service`（须经端口/application service）。
5. 只有 `bootstrap/` composition root 可同时看见产品实现与平台实现。
6. Runtime 层源码禁止出现 `app_id == "<产品名>"` 之类的产品分支（新增一条基于 AST 的字符串/比较检查）。

现状：MP-01～MP-06 已建立通用门禁；MP-07A 移除共享 Platform 对朝夕 adapter 的冻结例外，并把产品 domain 扫描收口到 `app/products/*/domain/`。

---

## 5. 代码现状核查（本文事实基线）

| 断言 | 核实 | 位置 |
|---|---|---|
| session 无 app_id/audience，`X-App-ID` 全仓零出现 | ✅ 属实 | `_core.py:1466`（`platform_user_sessions` 仅 4 列） |
| 账号解析器不带 app_id | ✅ 改造前属实，MP-05 已移除 | 原 `get_first_active_account_for_user` |
| `app_api.py` 混装通用认证/账号 bootstrap/朝夕聊天/直调 Runtime | ✅ 属实 | `app_api.py:30-49`、`:340` |
| 边界门禁只硬编码 companion_world（域层→Runtime 向） | ⚠️ 部分 | `test_layer_boundaries.py`；反向门禁已通用 |
| `accounts.app_id` 已存在、建号写路径已按 `(platform_user_id, app_id)` 唯一 | ✅ 已就位 | migration 0022；`billing.py:2091` |
| `agent_runtime/ports.py` 是「薄 send_turn facade」 | ❌ 误读：薄是 D-02 刻意决定，4 接缝形状已冻结 | `ports.py` |
| `run_turn_for_account` 调用面 | 仅 2 处（收口成本低） | `app_api.py:340`、`agent_runtime/adapter.py:13` |

---

## 6. Schema 变更清单（Phase 1 核心，最高风险迁移）

计费全隔离（MP-02）意味着**整棵计费/权益/邀请子树都要落 `app_id`**。下列为候选表，需逐表核实主键/唯一索引/backfill 与并发锁序（迁移编号从 `m0037` 起）：

| 表 | 现状键（`_core.py`） | 目标 |
|---|---|---|
| `platform_user_sessions` | `platform_user_id` | + `app_id`（NOT NULL DEFAULT 'zhaoxi'），token 校验带 app_id |
| `product_memberships`（新增） | 无 | `(platform_user_id, app_id)` 唯一；`status=active|disabled`；独立 nullable `daily_limit` / `rpm_limit`；`settings_json` 只放非强类型扩展配置 |
| `subscriptions` | **无唯一约束**，仅 `ix (platform_user_id, updated_at)` 按时间取最新（含历史多行）| 加 `app_id`；保留状态历史，只加局部唯一 `(platform_user_id, app_id) WHERE status='active'`。迁移前只归并重复 active：最新一行保留 active，其余标 `superseded`，正常 cancelled/expired 历史不删（O-6） |
| `entitlement_wallets` | **真人级锚**：局部唯一 `ux_..._user_active ON (platform_user_id) WHERE status='active'`（D-14/m0025）；`account_id NOT NULL UNIQUE` 为创建来源残留、已非有效锚 | 加 `app_id` 列 + 局部唯一扩为 `(platform_user_id, app_id) WHERE status='active'` + 存量 backfill `zhaoxi`（O-5 已定）|
| `entitlement_ledger` | 关联 wallet；`idempotency_key` 全局唯一 | 冗余**不可变** `app_id`（便于财务审计），写入时校验与 wallet 一致（O-2）；唯一键切为 `(app_id, idempotency_key)` |
| `cost_events` | 无 app_id；`idempotency_key` 全局唯一 | + `app_id`（产品级核算，否则无法分产品算账）；唯一键切为 `(app_id, idempotency_key)` |
| `daily_usage` | 按用量锚 | + `app_id`（按产品计日配额）|
| `daily_quota_reservations` | 预留 | + `app_id` |
| RPM 滑窗 | `turn_service.py:1068` 解析成全局 `platform_user`（D-09 跨居民共享），`rate_limiter.check_rpm` 与 web IP-keyed 共享 | subject 改为 `(platform_user_id, app_id)`；表/查询/advisory lock 同步 |
| `meaningful_message_reviews` | 无 app_id | 随计费/审核链落 `app_id`（referral 奖励释放依赖它）|
| `referral_codes` | `(platform_user_id, code_type)` | + `app_id`，个人码唯一索引带 app_id（MP-02：邀请各产品独立）|
| `referral_relationships` | `invitee_platform_user_id` 是列级全局 `UNIQUE` | + `app_id`，唯一性改为 `(invitee_platform_user_id, app_id)`，奖励**注册/校验/释放/后台查询全调用链**同产品；SQLite 必须重建表才能移除旧列级唯一约束 |

**连带改动（易漏，务必纳入 Phase 1）**：

- `platform_user` upsert、membership 首建、referral 关系创建与邀请码 `used_count` 消费必须在同一事务中；真人 upsert 使用 `ON CONFLICT(phone)` 收口并发全新注册，只有本次真正创建了 membership 才消费邀请码。已有全局身份但首次进入新产品不能再走「existing user → 丢弃 invite_code」的旧分支。
- 产品级入口账号 resolver 调用的 `grant_new_user_shells` 与 `retry_qualified_referral_rewards_for_user` 必须 app-scoped——否则朝夕邀请会给另一产品发券。新客赠权幂等键也必须产品化；为避免朝夕存量二次赠权，zhaoxi 继续识别既有 `new-user-grant-{platform_user_id}` 键，新产品使用含 `app_id` 的键。
- **配额 override 与 D-09 冲突**：`platform_users.daily_limit/rpm_limit` 当前是 canonical 真人级 override（3.0 D-09）。计费全隔离后配额锚变为 `(user, app)`。**已定（O-1）**：在 `product_memberships` 使用独立 nullable `daily_limit` / `rpm_limit` 列，存量值复制到 zhaoxi membership；迁移期读取 membership 优先、旧 `platform_users`/`accounts` 字段 fallback，contract 后停止 fallback。`settings_json` 不承载这两个强类型字段。若仍需平台级反滥用总上限，另设 `platform_abuse_limit` 字段，不与产品级 override 混用同一字段。
- `daily_usage` 的旧局部唯一索引 `(platform_user_id, date)` 必须在 contract 阶段替换为 `(platform_user_id, app_id, date)`；RPM advisory lock key 也必须包含 app_id，不能只改查询条件。
- `wipe_account_data` 判断共享钱包/daily 是否可删除时，范围必须从「真人是否还有任意产品账号」改为「真人在当前 app 是否还有 active runtime account」；不得因另一产品仍有账号而错误保留本产品数据，也不得删除另一产品资产。
- **所有冗余 `app_id` 写入即校验一致性**（MP-10）：落子表的 app_id 必须与 `accounts.app_id` / wallet / membership 对齐，不能只加列。
- 存量数据一律按 `app_id='zhaoxi'` 对齐（沿用 migration 0022 既定默认）。

---

## 7. 目标目录（增量落地，非一次到位）

保持模块化单体，「产品优先、产品内再分层」。Phase 1 已先完成数据与鉴权隔离；生产发布后，MP-07A 在不改变行为的前提下提前实体化朝夕已知边界。**仍不新建 `products/fatetell/`、不挂 Fatetell 路由，也不猜测其 Runtime 契约。**

```text
app/
├── bootstrap/                     # composition root（新建）
│   ├── product_registry.py        # 产品注册与 include_router 组合
│   ├── http.py
│   └── schedulers.py
├── products/
│   └── zhaoxi/
│       ├── manifest.py            # 既有朝夕路由组合；不改变 URL
│       ├── api/                    # bridge/debug/产品管理端 + Companion World routers
│       ├── application/           # World + onboarding/memory/mission/relationship 用例
│       ├── domain/                # Companion World 与 mission 纯领域层/模板
│       ├── infrastructure/        # 产品 persistence/repositories/adapters + profile/SOUL
│       ├── proactive/             # 朝夕主动消息垂直切片（contract/recall/delivery/store）
│       ├── tools/                  # 朝夕专属工具 handlers
│       └── jobs/                   # world content/lifecycle + dreaming/user-meta jobs
├── agent_runtime/                 # 跨产品，保持形态无关（原位）
│   ├── context/                   # turn context、窗口裁剪、摘要与证据回灌
│   ├── llm/                       # 模型调用、provider 选择与协议适配
│   └── persistence/               # Runtime 账号画像持久化适配器
├── platform/                      # 跨产品平台能力（identity/auth/billing/quota/channels/…）
│   ├── auth/                      # token、身份、验证码与短信
│   ├── gateways/                  # OpenClaw 与接入节点网关
│   ├── media/                     # ASR、图片理解
│   ├── moderation/                # 跨产品审核规则/provider/worker/persistence
│   ├── observability/             # 告警与脱敏
│   ├── quota/                     # 产品级 RPM 限流
│   └── search/                    # Web Search、TDAI adapters
└── turn_service.py / prompt_builder.py / reminder_utils.py
                                 # 等 Fatetell 真实调用点后再泛化的过渡模块
```

MP-07A 已分四批执行：Companion World 垂直切片；共享 Platform/Runtime 能力；
onboarding/memory/mission/relationship；以及 proactive、moderation、产品 persistence、
产品 routes/tools。`app.db` 继续作为懒加载兼容 façade，外部 URL、`app.main:app` 与
`scripts/run_*.py` 入口不变。

---

## 8. 分阶段迁移计划

**Phase 0 —— 本 ADR + 前置子决策（当前）**
- 冻结本文决策（MP-01…MP-10）。
- 前置子决策（O-1…O-7）**已全部定案**（见 §11）：override 下沉 membership、钱包/订阅锚点、ledger 隔离、记忆隔离、membership 生命周期及产品级新客语义。
- 产出后即可开工 Phase 1。

**Phase 1 —— 多产品安全/隔离基座（真正优先，风险最高）**

> ⚠️ Phase 1 同时触及认证、钱包、配额、referral，且生产是**中心 PG 多节点写**——**这不是一次普通 migration，而是一串按闸发布**。计费/配额/referral 各自拆成独立 migration + 独立发布闸，按 expand/contract 顺序推进：
>
> `product_memberships + backfill` → 加新列/新索引（可空/无约束）→ 旧代码兼容、双写 → 切新读路径 → 隔离回归 + 数据一致性校验 → 收紧 `NOT NULL`/唯一约束 → 最后移除旧约束与 fallback。

1. **`product_memberships (platform_user_id, app_id, status, daily_limit, rpm_limit, settings_json, created_at, updated_at)` + backfill**（提到首步；对 `(platform_user_id, app_id)` 唯一，作计费规范锚 MP-10）+ 轻量 product registry。无行=未加入，行状态仅 `active|disabled`；存量真人回填 active zhaoxi membership，配额 override 同步复制。
2. Session 作用域：引入 `SessionPrincipal`，`platform_user_sessions` 加 `app_id`，resolver 校验 active membership，鉴权强制 URL app_id == principal.app_id，旧 `/v1/*` 与 `/web/*` 固定 zhaoxi，收口全部 token resolver 调用点（MP-04）。
3. 入口账号解析：`get_active_bound_account_for_user_in_app`；≤1-active 不变量与告警收紧为 per-(user, app) 入口账号（MP-05）。
4. **计费闸（独立发布）**：钱包局部唯一扩为 `(platform_user_id, app_id)`（O-5 已定）+ 订阅采用「状态历史、至多一条 active」（O-6）+ 计费子树落 `app_id`（§6）+ ledger/cost 幂等唯一键切为 `(app_id, idempotency_key)` + `grant_new_user_shells` app-scoped + account wipe 改为 app-scoped。
5. **配额闸（独立发布）**：RPM subject 改 `(user, app)`（§6 RPM 行）+ override 下沉 membership（O-1）。
6. **referral 闸（独立发布）**：首次 membership 才消费本产品邀请码；注册/校验/释放/后台查询全链 app-scoped，并移除 `invitee_platform_user_id` 的旧全局唯一约束（MP-02 / O-7）。
7. AST 边界门禁泛化（MP-06，§4 规则）。

**Phase 2A —— 朝夕目录边界实体化（生产发布后，当前）**
- 归位 Companion World 的 api/application/domain/infrastructure/jobs，外部 URL 与部署入口不变。
- 由 `bootstrap/http.py` 和 `products/zhaoxi/manifest.py` 组合路由。
- 归位朝夕 proactive、产品 persistence/routes/tools；共享内容审核进入 `platform/moderation`。
- 只做行为零变更迁移；旧 `app.db` 懒加载 façade 保留，防止一次移动同时变更存储契约。

**Phase 2B —— 新产品命名空间与接入（延期至 Fatetell PRD 冻结后）**
- 挂 `/api/v1/products/{app}/*`；`/api/v1/*` 保留朝夕别名（MP-07）。
- 拆 `app_api.py`：抽通用 auth/OTP/session bootstrap 到平台入口。

**Phase 3 —— Runtime 调用收口 + 主动消息契约去 Companion World 化（延期至真实产品调用点出现）**
- 产品 turn 经 `AgentRuntimePort` / application service，不再直调 `run_turn_for_account`（面很小，仅 2 处）。
- **`ProactiveIntent` 去朝夕概念化**：现契约直接带 `universe_id` / `resident_id`（`ports.py:61`，泄漏 form-B 领域概念）。改为通用载荷（如 `product_context: dict`），朝夕字段降为其一种取值，为 Fatetell 复用主动消息扫清概念泄漏。

**Phase 4 —— 用 Fatetell 验证架构成立（延期至 Fatetell PRD 冻结后）**
- `products/fatetell/` 作为 Runtime 端口第二个真实调用方，从其真实需求接线 context 注入 / `MemorySink` / `ProactiveDeliveryAdapter` 三接缝，并按需催生候选扩展点（`ContextProvider` / `ToolPolicy` / persona）。
- **边界成立判据（已按 codex 修正为相对口径）**：Runtime 层不出现 Fatetell/朝夕**特定类型、import 或 `app_id` 分支**即算成立。**第二产品（Fatetell）首次接入允许做一次通用化改动**（如 §Phase3 的主动消息去 CW 化）；到**第三个产品**才应做到基本零改 Runtime。绝对"零修改"不作为验收门。
- `runtime_ownerships` 若确需，在此阶段作为唯一投影建立（MP-09）。

---

## 9. 我对 codex 审计的四处修正（决策留痕）

1. **降级「全仓 products/ 大搬家」为增量、且只对 Fatetell（MP-08）**：本仓正处 3.0 重构收尾，搬 `domains/companion_world` 会动数百处 import、与在途重构打架，且在有第二产品域码前收益近零。新东西住新房子，装修中的一家不搬。
2. **不「加厚 facade」，而「从 Fatetell 真实调用点接线接缝」**：`ports.py` 薄是 D-02 刻意决定，**已冻结的是 context 注入 / `MemorySink` / `ProactiveDeliveryAdapter` / `UnitOfWork` 四接缝**（`ContextProvider`/`ToolPolicy`/`AfterTurnHook`/`RuntimePersona` 是候选扩展点、尚未落地）。Fatetell 复用完整 Runtime 恰好会真实接线前三个接缝，并按需催生候选扩展点——这是设计特性。（本条亦修正我初稿把候选端口写成"已冻结"的错误。）
3. **`runtime_ownerships` 推迟（MP-09）**：朝夕现靠 resident→universe→owner fallback 解析真人；此时新增所有权投影会造双事实源。等 Fatetell 所有权模型明确后建为唯一投影。
4. **计费范围升级为产品决策并已拍板为「全隔离」**：codex 默认钱包全局；本轮确认 wallet/订阅/配额/referral 全按产品切，故 §6 迁移面比 codex 估计更大。

---

## 10. 验收标准

**当前 Phase 1 验收**：生产注册表只启用 zhaoxi；测试通过依赖注入增加 `test_product`，验证同一手机号在两个产品作用域下的
`token` / `account` / `messages` / `memory` / `onboarding` / `quota` / `wallet` / `referral`
全部按既定策略隔离；朝夕现有**微信 + Native App + Web** 行为保持不变。Phase 1 不创建 Fatetell manifest、路由、领域对象、MemorySink 或 scheduler。

**最终第二产品验收（PRD 后）**：把 `test_product` 隔离矩阵替换/扩展为真实 Fatetell API 与领域调用，完成 Runtime、记忆和主动消息的端到端验证。

补充：
- 跨产品串号回归：持 zhaoxi session 访问注入的 `test_product` namespace 必须被拒；真实 Fatetell namespace 在 PRD 后复用此门禁（MP-04）。
- 产品级新客回归：已有 zhaoxi 身份的真人首次加入 `test_product` 时，`is_new_membership=true`，可消费该产品邀请码并只获得一次该产品新客赠权；再次进入不重复消费/赠权。PRD 后用 Fatetell 重跑同一矩阵。
- subscription 状态历史回归：同一 `(user, app)` 可保留多条 cancelled/expired/superseded 历史，但并发下至多一条 active；另一 app 的 active subscription 不受影响。
- billing 幂等回归：两个产品可使用相同裸 `idempotency_key` 并各自恰好入账一次；同一产品重放仍只入账一次。
- membership 停服回归：disabled/missing membership 在任何 turn 副作用前被明确拒绝并告警；同手机号全新注册和 referral 主路径并发不重复建关系、消费邀请码或发奖。
- 真实 Fatetell 的记忆/主动消息隔离延期到 PRD 后验证；Phase 1 只验证现有 account-scoped memory 不跨 app 查询，不提前实现 Fatetell MemorySink/主动投递。
- AST 门禁 CI 绿：Runtime 无产品分支、产品间无互 import（MP-06）。
- Runtime 层不含 Fatetell/朝夕特定类型、import 或 `app_id` 分支（相对口径的边界判据；Fatetell 首次接入允许一次通用化，见 Phase 4）。

---

## 11. 开放问题 → 决策（本轮全部定案）

- **O-1（配额 override 语义）✅ 已定**：override 下沉为 `product_memberships.daily_limit/rpm_limit` 两个独立 nullable 列，不放 `settings_json`。存量 `platform_users` 值复制到 zhaoxi membership；迁移期 membership 优先、旧字段 fallback，contract 后停止 fallback。如需平台级反滥用总上限，另设 `platform_abuse_limit` 字段，不与产品级 override 混用同一字段。
- **O-2（`entitlement_ledger` 隔离方式）✅ 已定**：冗余**不可变** `app_id` 便于财务审计，写入时校验与 wallet 一致（不止靠 `wallet_id` 传导）。
- **O-3（跨产品共享记忆）✅ 已定**：**默认禁止**跨产品共享记忆；未来如确有需求，走用户授权的显式导入/导出，**不**共享 L3。Fatetell 无 L3 需求时，其 MemorySink 只写 per-account L1/L2。
- **O-4（membership 生命周期 vs onboarding）✅ 已定**：不把存量朝夕用户一律回填成 onboarded。`product_membership` 生命周期与 runtime account onboarding 是**两个概念**；无 membership 行表示未加入，存在时 `status` 只允许 `active|disabled`，不存 `none` 占位行。session、建号、计费与 referral 写入必须要求 active membership；产品专属 onboarding FSM 不放 membership。
- **O-5（钱包锚点）✅ 已定（并修正初稿事实）**：钱包早已是**真人级**（D-14/m0025 局部唯一 `(platform_user_id) WHERE status='active'`，`account_id` 仅创建来源），**不存在** "per-account vs membership" 两难。多产品化 = 局部唯一扩为 `(platform_user_id, app_id) WHERE status='active'` + 加 `app_id` 列 + 存量 backfill `zhaoxi`，与 MP-10 / D-09 一致。
- **O-6（订阅历史模型）✅ 已定**：采用「状态历史」：同一 `(platform_user_id, app_id)` 可保留多条 cancelled/expired/superseded 历史，但至多一条 `status='active'`，用局部唯一索引保证。迁移仅处理重复 active（按 `updated_at DESC, id DESC` 留最新，其余标 `superseded`），不删除正常历史；本阶段不新增 `subscription_events`，正式支付需要完整逐事件审计时再单独设计。
- **O-7（产品级新客与邀请）✅ 已定**：新客资格以首次创建 `(platform_user_id, app_id)` membership 为准，不以首次创建全局 `platform_user` 为准。已有朝夕身份者首次加入 Fatetell 仍可使用 Fatetell 邀请码并获得 Fatetell 新客权益；user upsert、membership 首建、referral 建立和邀请码消费必须同事务，重复进入不得重复消费或赠权。
