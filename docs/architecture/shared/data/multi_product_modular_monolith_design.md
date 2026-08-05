# 技术设计：多产品模块化单体 —— 产品边界、身份隔离与分阶段迁移（架构决策记录）

更新时间：2026-07-25
状态：**MP-01～MP-06 已生产发布；MP-07A～MP-07F 已完成本地开发与聚焦回归，待分批评审合并。生产注册表仍只启用 `zhaoxi`；真实第二产品等待 PRD，不预设为 Fatetell 或 Nooki。** 本文冻结任意新产品共用的「产品级模块边界 + 身份/计费隔离」架构决策。
核查基线：Phase 1 merge commit `f4baa3b`。当前 max migration = `m0046`（`app/db/_core.py`）。

> **2026-08-04 补充裁决：** 第二产品已冻结为鸣蝉（`mingchan`），Native App / Companion World
> 属于鸣蝉，朝夕只拥有微信/OpenClaw 与 Web/H5 业务。本文关于“朝夕包含 Native App/World”或
> “真实第二产品尚未确定”的叙述已被
> [朝夕 / 鸣蝉拆分计划](../../../plans/shared/zhaoxi_mingchan_product_split_plan.md)取代；平台级模块边界与
> 身份/计费隔离规则继续有效。

路由口径：旧朝夕入口继续使用 `/v1/*`，规范产品入口由 manifest 固定挂载到 `/api/v1/products/<app_id>/*`；同时保留反代剥离 `/api` 后的 `/v1/products/<app_id>/*` 兼容路径。`app_id` 由服务端注册表和固定 router 决定，不接受客户端 Header 动态选择。

关联后端设计：
- [`architecture/overview.md`](../../overview.md) §2 已确立四层概念模型与依赖方向，本文是其在「多产品」维度上的延伸。
- [`companion_world_3_0_refactor_design.md`](../../products/mingchan/companion_world_3_0_refactor_design.md)（D-01…D-14）：Agent Runtime 形态无关 + Companion World 产品领域层的既有重构，本文**叠加而非推翻**。
- [`identity_model_and_wechat_binding.md`](../access/identity_model_and_wechat_binding.md)、[`companion_world_account_model_reconciliation.md`](../../../archive/deliveries/companion_world/companion_world_account_model_reconciliation.md)：真人身份与账号收敛模型。
- [`multi_product_modular_monolith_implementation_plan.md`](../../../plans/shared/multi_product_modular_monolith_implementation_plan.md)：MP-01…MP-07 的实施记录与真实第二产品延期范围。
- [`adding-product.md`](../../../guides/adding-product.md)：真实新产品开工时的接入清单。

审计 provenance：本文由一轮 codex 架构审计 + 逐条代码核查形成，采纳其方向与优先级，并在四处做了修正（见 §9）。

---

## 0. 摘要（TL;DR）

后端已经从「目录和运行时默认服务单一产品」演进为「同一模块化单体可显式组合多个产品」。Fatetell 与 Nooki 都只是候选需求；哪个先冻结 PRD，哪个才成为真实第二产品。

核心判断：**不拆微服务、不为每个产品复制 Agent Runtime、不按猜测创建产品骨架。** `app_id` 的认证、查询、计费与数据隔离已经由 MP-01～MP-06 生产验证；目录所有权、turn 注入、工具策略和固定产品 namespace 已由 MP-07A～MP-07F 在本地代码中实体化。

三条已冻结的平台规则：

1. **身份共享**：同一手机号 = 跨产品同一 `platform_user`；`platform_users` 保持平台全局，隔离锚点是其下的 `app_id`。
2. **计费完全按产品隔离**：钱包（wallet）、订阅、配额、成本核算、**邀请/推荐关系**全部按 `(platform_user_id, app_id)` 切开。「新客」按首次创建该产品 membership 判断，而不是按是否首次创建全局 `platform_user` 判断。
3. **共享 Runtime、显式产品注入**：新产品复用同一个 Agent Runtime，通过 `ProductTurnServices`、`ToolPolicy` 和显式 `app_id` 提供产品差异；MemorySink、主动消息等只在真实需求出现时接线，不提前猜测。

---

## 1. 目标与非目标

### 目标

- 在任意真实第二产品开工**前**冻结四层身份模型与产品边界。
- 补齐 `app_id` 隔离基座：session 作用域、账号解析、计费子树、邀请关系。
- 让「新增一个产品**不需要改 Runtime 核心**」成为可验证的架构不变量。
- 朝夕相伴现有微信 + Native App + Web 行为**零回归**。

### 非目标

- **不**拆微服务，保持模块化单体 + 中心单 writer（沿用 3.0 结论）。
- **不**做全仓一次性大搬家；Phase 1 原地完成隔离，上线后只分批迁移归属明确的朝夕模块（见 §7）。
- **不**为任一产品复制一套 Runtime。
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
└── product_membership: <new_app>         # (platform_user_id, app_id) 一行
    ├── channel: native / web / ...
    ├── 新产品领域（业务状态机留在产品域，绝不进 Runtime）
    ├── 独立计费域
    └── N 个隔离 Agent Runtime（account_id）
```

| 概念 | 定义 | 作用域锚点 |
|---|---|---|
| `platform_user` | 真人的全平台身份，跨产品共享手机号与登录 | 平台全局 |
| `app_id` / product_id | 产品边界，例：`zhaoxi`、未来真实产品 ID | 产品级 |
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
- **MP-02（计费全隔离）**：wallet / subscription / quota / cost 核算 / **referral 关系与奖励** 一律按 `(platform_user_id, app_id)` 隔离。一个产品的充值、券与邀请奖励绝不进入另一个产品。邀请码资格、新客赠权和内部 `is_new_membership` 一律以「本次是否首次创建该 `(platform_user_id, app_id)` membership」为准；旧 API 如继续返回 `is_new_user` 字段，其语义也切为产品级新成员。
- **MP-03（Runtime 形态无关，产品差异经端口传入）**：Runtime 内**禁止**出现按具体产品名分支。`ChannelTurnInput.app_id`、`ProductTurnServices`、`ProductPromptContext`、`AfterTurnHook` 与 `ToolPolicy` 已落地；`MemorySink`、`ProactiveDeliveryAdapter`、`UnitOfWork` 的协议继续保留。第二产品的 memory/proactive 接线和新的 persona 扩展点仍须从真实调用点长出（D-02）。
- **MP-04（session 产品作用域，不信客户端；引入 SessionPrincipal）**：`SessionPrincipal{session_id, platform_user_id, app_id, expires_at}` 已落地，所有产品 API 依赖 principal 而非裸 platform user，resolver 同时确认对应 membership 为 active。旧 `/v1/*` 及 `/web/*` 固定 `app_id=zhaoxi`；每个产品 manifest 固定 `/api/v1/products/<app_id>/*`，并通过 `require_product_session(expected_app_id)` 校验 audience。`app_id` 只来自注册表与服务端路由，绝不取自任意 Header。
- **MP-05（入口账号解析必带 app_id）**：以 `get_active_bound_account_for_user_in_app(platform_user_id, app_id)`（或 `get_primary_entry_account`）取代裸 `get_first_active_account_for_user`；命名须明确它解析的是**绑定/入口账号**、不解析产品全部 Runtime account。「≤1 active」不变量收紧为 per-`(user, app)` 的**入口账号**（见 §2 account 数量口径）。
- **MP-06（依赖红线由 AST 门禁强制）**：见 §4，把现有 `tests/test_layer_boundaries.py` 从硬编码 `companion_world` 泛化为通用产品边界门禁。
- **MP-07（API 显式产品命名空间）**：新产品走 manifest 固定的 `/api/v1/products/<app_id>/*`；旧 `/api/v1/*` 保留为朝夕兼容别名，不做破坏性迁移。朝夕固定 namespace 已落地。
- **MP-08（增量迁移，先实体化已知边界）**：归属明确的朝夕垂直切片已移入 `app/products/zhaoxi/`，共享能力归入 `agent_runtime/` 与 `platform/`；不为候选产品创建空骨架或猜测契约。
- **MP-09（所有权投影推迟）**：`runtime_ownerships` 投影表推迟到真实第二产品所有权模型明确后再建，且建则作为**唯一**投影（朝夕回填为其中一个 writer），不维护双事实源（见 §9.3）。
- **MP-10（product_membership 为计费规范锚点）**：计费归属以 `product_memberships` 为规范锚，对 `(platform_user_id, app_id)` 建唯一约束。所有落到子表的冗余 `app_id` 写入时**必须校验**与 `accounts.app_id` / wallet / membership 一致，不能只加列——否则仍可能写出跨产品错账。✅ 钱包锚点已澄清（修正初稿事实）：`entitlement_wallets` 早由 **D-14 / 迁移 `m0025`** 上迁为**真人级**（局部唯一 `ux_entitlement_wallets_user_active ON (platform_user_id) WHERE status='active'`，`_core.py:2048`；`account_id NOT NULL UNIQUE` 仅存"创建来源"、已非有效锚，`billing.py:652` 按 `platform_user_id` get-or-create），与 D-09 真人级聚合**本就一致、无冲突**。多产品化只需把该局部唯一扩为 `(platform_user_id, app_id)` 并加 `app_id` 列（O-5 已定）。

---

## 4. 依赖红线（AST 门禁强制）

在 `tests/test_layer_boundaries.py` 现有机制上泛化，新增/强化以下规则：

1. `app.agent_runtime.*` 永不 import `app.products.*`（及任何具体产品域）。
2. `app.platform.*` 永不 import 某个具体产品。
3. 任意 `app.products.<app_id>.*` 之间互不 import。
4. 产品 domain 不直接 import FastAPI、SQL repository 或 `app.turn_service`（须经端口/application service）。
5. 只有 `bootstrap/` composition root 可同时看见产品实现与平台实现。
6. Runtime 层源码禁止出现 `app_id == "<产品名>"` 之类的产品分支（新增一条基于 AST 的字符串/比较检查）。

现状：MP-01～MP-06 已建立通用门禁；MP-07A～MP-07F 又覆盖产品 domain、共享 tools、真实 turn engine、薄 `main.py` 和根兼容 façade，并以中性 `test_product` 跑通最小真实 turn。

---

## 5. 代码现状核查（本文事实基线）

| 断言 | 核实 | 位置 |
|---|---|---|
| session audience 与 active membership | ✅ 已落地 | `SessionPrincipal`、`require_product_session(expected_app_id)` |
| 入口账号与 turn scope | ✅ 已落地 | resolver 必带 `app_id`；turn 在副作用前拒绝 account/product 错配 |
| 产品 API composition | ✅ 朝夕已迁移 | `products/zhaoxi/api/`、`products/zhaoxi/manifest.py` |
| Runtime 产品注入 | ✅ 已落地 | `agent_runtime/turns/service.py` + `ProductTurnServices` |
| 产品工具隔离 | ✅ 已落地 | 共享 `app/tools/` + 产品 `ToolPolicy` / catalog |
| 固定产品 namespace | ✅ 朝夕已提供 | `/api/v1/products/zhaoxi/*`；旧 `/v1/*` 保持兼容 |
| 第二产品业务实现 | ⏸ 等待真实 PRD | 不创建 Fatetell/Nooki 占位目录 |

---

## 6. Schema 隔离清单（MP-01～MP-06 已生产交付）

计费全隔离（MP-02）要求**整棵计费/权益/邀请子树都落 `app_id`**。下表保留改造前风险与已交付不变量，供新产品开发和后续 migration 审查复用：

| 表 | 改造前风险 | 已交付不变量 |
|---|---|---|
| `platform_user_sessions` | `platform_user_id` | + `app_id`（NOT NULL DEFAULT 'zhaoxi'），token 校验带 app_id |
| `product_memberships`（新增） | 无 | `(platform_user_id, app_id)` 唯一；`status=active|disabled`；独立 nullable `daily_limit` / `rpm_limit`；`settings_json` 只放非强类型扩展配置 |
| `subscriptions` | **无唯一约束**，仅 `ix (platform_user_id, updated_at)` 按时间取最新（含历史多行）| 加 `app_id`；保留状态历史，只加局部唯一 `(platform_user_id, app_id) WHERE status='active'`。迁移前只归并重复 active：最新一行保留 active，其余标 `superseded`，正常 cancelled/expired 历史不删（O-6） |
| `entitlement_wallets` | **真人级锚**：局部唯一 `ux_..._user_active ON (platform_user_id) WHERE status='active'`（D-14/m0025）；`account_id NOT NULL UNIQUE` 为创建来源残留、已非有效锚 | 加 `app_id` 列 + 局部唯一扩为 `(platform_user_id, app_id) WHERE status='active'` + 存量 backfill `zhaoxi`（O-5 已定）|
| `entitlement_ledger` | 关联 wallet；`idempotency_key` 全局唯一 | 冗余**不可变** `app_id`（便于财务审计），写入时校验与 wallet 一致（O-2）；唯一键切为 `(app_id, idempotency_key)` |
| `cost_events` | 无 app_id；`idempotency_key` 全局唯一 | + `app_id`（产品级核算，否则无法分产品算账）；唯一键切为 `(app_id, idempotency_key)` |
| `daily_usage` | 按用量锚 | + `app_id`（按产品计日配额）|
| `daily_quota_reservations` | 预留 | + `app_id` |
| RPM 滑窗 | subject 只有全局 `platform_user`，会跨产品共享用量 | subject 为 `(platform_user_id, app_id)`；表/查询/advisory lock 同步 |
| `meaningful_message_reviews` | 无 app_id | 随计费/审核链落 `app_id`（referral 奖励释放依赖它）|
| `referral_codes` | `(platform_user_id, code_type)` | + `app_id`，个人码唯一索引带 app_id（MP-02：邀请各产品独立）|
| `referral_relationships` | `invitee_platform_user_id` 是列级全局 `UNIQUE` | + `app_id`，唯一性改为 `(invitee_platform_user_id, app_id)`，奖励**注册/校验/释放/后台查询全调用链**同产品；SQLite 必须重建表才能移除旧列级唯一约束 |

**已交付并需持续保持的连带不变量**：

- `platform_user` upsert、membership 首建、referral 关系创建与邀请码 `used_count` 消费必须在同一事务中；真人 upsert 使用 `ON CONFLICT(phone)` 收口并发全新注册，只有本次真正创建了 membership 才消费邀请码。已有全局身份但首次进入新产品不能再走「existing user → 丢弃 invite_code」的旧分支。
- 产品级入口账号 resolver 调用的 `grant_new_user_shells` 与 `retry_qualified_referral_rewards_for_user` 必须 app-scoped——否则朝夕邀请会给另一产品发券。新客赠权幂等键也必须产品化；为避免朝夕存量二次赠权，zhaoxi 继续识别既有 `new-user-grant-{platform_user_id}` 键，新产品使用含 `app_id` 的键。
- **配额 override 与 D-09 冲突**：`platform_users.daily_limit/rpm_limit` 当前是 canonical 真人级 override（3.0 D-09）。计费全隔离后配额锚变为 `(user, app)`。**已定（O-1）**：在 `product_memberships` 使用独立 nullable `daily_limit` / `rpm_limit` 列，存量值复制到 zhaoxi membership；迁移期读取 membership 优先、旧 `platform_users`/`accounts` 字段 fallback，contract 后停止 fallback。`settings_json` 不承载这两个强类型字段。若仍需平台级反滥用总上限，另设 `platform_abuse_limit` 字段，不与产品级 override 混用同一字段。
- `daily_usage` 的旧局部唯一索引 `(platform_user_id, date)` 必须在 contract 阶段替换为 `(platform_user_id, app_id, date)`；RPM advisory lock key 也必须包含 app_id，不能只改查询条件。
- `wipe_account_data` 判断共享钱包/daily 是否可删除时，范围必须从「真人是否还有任意产品账号」改为「真人在当前 app 是否还有 active runtime account」；不得因另一产品仍有账号而错误保留本产品数据，也不得删除另一产品资产。
- **所有冗余 `app_id` 写入即校验一致性**（MP-10）：落子表的 app_id 必须与 `accounts.app_id` / wallet / membership 对齐，不能只加列。
- 存量数据一律按 `app_id='zhaoxi'` 对齐（沿用 migration 0022 既定默认）。

---

## 7. 目标目录（增量落地，非一次到位）

保持模块化单体，「产品优先、产品内再分层」。数据与鉴权隔离已生产发布；MP-07A～MP-07F 在不改变既有外部行为的前提下实体化朝夕、Runtime、Platform 与 composition root。**不新建任何候选产品目录，不挂占位路由，也不猜测产品业务契约。**

```text
app/
├── bootstrap/                     # composition root
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
│   ├── context/                   # PromptBuilder、turn context、窗口裁剪、摘要与安全资产
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
                                 # 仅旧 Python import 的 module-alias 兼容 façade
```

MP-07A 已分四批归位 Companion World、共享 Platform/Runtime、朝夕 application 与
proactive/routes/tools。MP-07B～MP-07F 又完成显式 turn scope、`ProductTurnServices`、
`ToolPolicy`、固定产品 namespace、composition root 和过渡 façade 收口。`app.db` 继续作为
懒加载兼容入口，SQLite 与 PostgreSQL 双后端、外部 URL、`app.main:app` 和 `scripts/run_*.py`
入口保持不变。

---

## 8. 分阶段迁移计划

**Phase 0 —— 本 ADR + 前置子决策（已完成）**
- 冻结本文决策（MP-01…MP-10）。
- 前置子决策（O-1…O-7）**已全部定案**（见 §11）：override 下沉 membership、钱包/订阅锚点、ledger 隔离、记忆隔离、membership 生命周期及产品级新客语义。
- 已作为 Phase 1 的决策输入。

**Phase 1 —— 多产品安全/隔离基座（已生产发布）**

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

**Phase 2A —— 朝夕目录边界实体化（MP-07A，已完成本地开发）**
- 归位 Companion World 的 api/application/domain/infrastructure/jobs，外部 URL 与部署入口不变。
- 由 `bootstrap/http.py` 和 `products/zhaoxi/manifest.py` 组合路由。
- 归位朝夕 proactive、产品 persistence/routes/tools；共享内容审核进入 `platform/moderation`。
- 只做行为零变更迁移；旧 `app.db` 懒加载 façade 保留，防止一次移动同时变更存储契约。

**Phase 2B —— 公共接入接缝（MP-07B～MP-07F，已完成本地开发）**
- `ChannelTurnInput.app_id` 与 account scope 在任何 turn 副作用前校验。
- `ProductTurnServices` 显式注入 session、profile/context、onboarding 与 after-turn hooks。
- `ToolPolicy` 固定每个产品可见、可执行的工具 catalog。
- 朝夕 manifest 已挂 `/api/v1/products/zhaoxi/*`；`/api/v1/*` 继续作为兼容别名。
- `app/main.py` 只保留 ASGI bootstrap；产品 routes 与 lifecycle 由 manifest 组合。

**Phase 3 —— 真实第二产品接入（延期至其 PRD 冻结）**
- 新建真实产品 manifest、固定 namespace、API、领域模型、repository、`ProductTurnServices` 和 `ToolPolicy`；只创建需求实际需要的目录。
- 若产品需要记忆或主动消息，再实现其 MemorySink / 投递 / scheduler，并按真实载荷把 `ProactiveIntent` 中仍存在的 Companion World 字段泛化。
- 产品领域 API、onboarding、persona、数据删除、隐私、计费与运营规则均在该产品工单中落地，不进入共享 Runtime。

**Phase 4 —— 用真实第二产品验证架构成立**
- **边界成立判据**：Runtime 层不出现具体产品类型、import 或 `app_id` 分支；产品工具互不可见；session、资产与 account 数据均按 app scope 隔离。
- 第二产品首次接入允许从真实调用点做一次必要的通用化改动；到第三个产品应基本不改 Runtime。绝对“零修改”不作为第二产品验收门。
- `runtime_ownerships` 若确需，在此阶段作为唯一投影建立（MP-09）。

---

## 9. 我对 codex 审计的四处修正（决策留痕）

1. **降级「全仓 products/ 大搬家」为增量迁移（MP-08）**：只迁移所有权明确的现有代码；候选产品没有真实领域码时不创建目录。
2. **不「加厚 façade」，而从真实调用点接线**：`ProductTurnServices`、`ToolPolicy` 和 after-turn hooks 已由现有 turn 调用点落地；MemorySink、主动投递与 persona 新抽象继续等待第二产品真实需求。
3. **`runtime_ownerships` 推迟（MP-09）**：朝夕现靠 resident→universe→owner fallback 解析真人；此时新增所有权投影会造双事实源。等真实第二产品所有权模型明确后建为唯一投影。
4. **计费范围升级为产品决策并已拍板为「全隔离」**：codex 默认钱包全局；本轮确认 wallet/订阅/配额/referral 全按产品切，故 §6 迁移面比 codex 估计更大。

---

## 10. 验收标准

**当前公共基座验收**：生产注册表只启用 zhaoxi；测试通过依赖注入增加 `test_product`，验证同一手机号在两个产品作用域下的
`token` / `account` / `messages` / `memory` / `onboarding` / `quota` / `wallet` / `referral`
全部按既定策略隔离；中性产品可用最小 `ProductTurnServices` 执行真实 turn 且只看到共享工具；朝夕现有**微信 + Native App + Web** 行为保持不变。公共基座不创建候选产品 manifest、路由、领域对象、MemorySink 或 scheduler。

**最终第二产品验收（PRD 后）**：把 `test_product` 隔离矩阵扩展为真实产品 API 与领域调用，并按该产品实际范围完成 Runtime、记忆和主动消息验证。

补充：
- 跨产品串号回归：持 zhaoxi session 访问注入的 `test_product` namespace 必须被拒；真实产品 namespace 复用此门禁（MP-04）。
- 产品级新客回归：已有 zhaoxi 身份的真人首次加入 `test_product` 时，`is_new_membership=true`，可消费该产品邀请码并只获得一次该产品新客赠权；再次进入不重复消费/赠权。真实产品接入时重跑同一矩阵。
- subscription 状态历史回归：同一 `(user, app)` 可保留多条 cancelled/expired/superseded 历史，但并发下至多一条 active；另一 app 的 active subscription 不受影响。
- billing 幂等回归：两个产品可使用相同裸 `idempotency_key` 并各自恰好入账一次；同一产品重放仍只入账一次。
- membership 停服回归：disabled/missing membership 在任何 turn 副作用前被明确拒绝并告警；同手机号全新注册和 referral 主路径并发不重复建关系、消费邀请码或发奖。
- 真实第二产品的记忆/主动消息隔离延期到 PRD 后验证；当前只验证 account-scoped memory 不跨 app 查询，不提前实现产品 MemorySink/主动投递。
- AST 门禁 CI 绿：Runtime 无产品分支、产品间无互 import（MP-06）。
- Runtime 层不含具体产品类型、import 或 `app_id` 分支（相对口径的边界判据；第二产品首次接入允许一次必要通用化，见 Phase 4）。

---

## 11. 开放问题 → 决策（本轮全部定案）

- **O-1（配额 override 语义）✅ 已定**：override 下沉为 `product_memberships.daily_limit/rpm_limit` 两个独立 nullable 列，不放 `settings_json`。存量 `platform_users` 值复制到 zhaoxi membership；迁移期 membership 优先、旧字段 fallback，contract 后停止 fallback。如需平台级反滥用总上限，另设 `platform_abuse_limit` 字段，不与产品级 override 混用同一字段。
- **O-2（`entitlement_ledger` 隔离方式）✅ 已定**：冗余**不可变** `app_id` 便于财务审计，写入时校验与 wallet 一致（不止靠 `wallet_id` 传导）。
- **O-3（跨产品共享记忆）✅ 已定**：**默认禁止**跨产品共享记忆；未来如确有需求，走用户授权的显式导入/导出，**不**共享 L3。新产品无 L3 需求时，其 MemorySink 只写 per-account L1/L2。
- **O-4（membership 生命周期 vs onboarding）✅ 已定**：不把存量朝夕用户一律回填成 onboarded。`product_membership` 生命周期与 runtime account onboarding 是**两个概念**；无 membership 行表示未加入，存在时 `status` 只允许 `active|disabled`，不存 `none` 占位行。session、建号、计费与 referral 写入必须要求 active membership；产品专属 onboarding FSM 不放 membership。
- **O-5（钱包锚点）✅ 已定（并修正初稿事实）**：钱包早已是**真人级**（D-14/m0025 局部唯一 `(platform_user_id) WHERE status='active'`，`account_id` 仅创建来源），**不存在** "per-account vs membership" 两难。多产品化 = 局部唯一扩为 `(platform_user_id, app_id) WHERE status='active'` + 加 `app_id` 列 + 存量 backfill `zhaoxi`，与 MP-10 / D-09 一致。
- **O-6（订阅历史模型）✅ 已定**：采用「状态历史」：同一 `(platform_user_id, app_id)` 可保留多条 cancelled/expired/superseded 历史，但至多一条 `status='active'`，用局部唯一索引保证。迁移仅处理重复 active（按 `updated_at DESC, id DESC` 留最新，其余标 `superseded`），不删除正常历史；本阶段不新增 `subscription_events`，正式支付需要完整逐事件审计时再单独设计。
- **O-7（产品级新客与邀请）✅ 已定**：新客资格以首次创建 `(platform_user_id, app_id)` membership 为准，不以首次创建全局 `platform_user` 为准。已有朝夕身份者首次加入任何新产品，仍可按该产品规则使用邀请码并获得新客权益；user upsert、membership 首建、referral 建立和邀请码消费必须同事务，重复进入不得重复消费或赠权。
