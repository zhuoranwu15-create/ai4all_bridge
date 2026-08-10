# 技术设计：系统 3.0 — Agent Runtime 分层与 Companion World 产品领域层（架构决策记录）

更新时间：2026-08-04
归属：**`product:mingchan`**。2026-08-04 已完成 Companion World 仓库代码与文档归属迁移；生产
启用和验证仍按[拆分计划](../../../plans/shared/zhaoxi_mingchan_product_split_plan.md)执行。

状态：**M0/M1/M2-C、M3、M4 与 M5 的历史能力均已完成；2026-08-04 已迁入独立鸣蝉产品域，
仓库开发与开发机验证完成，首次生产启用待执行。** 本文保留各里程碑交付时的 default-off 灰度记录，它们是历史
交付记录，不代表当前生产能力位；鸣蝉当前注册仍为 disabled，后续契约以
[`app_api_handoff.md`](../../../products/mingchan/app_api_handoff.md)为准。3.0 后端重构开发闭环已完成，
本文继续作为架构决策与代码接缝入口。
核查基线：`origin/main@3403380`（PR #48 merge commit）。M5-0=`1c981be`、M5-1=`99dd92b`、M5-2=`c372479`、M5-3=`82073ad`、M5-4=`c19c6b8`、M5-5=`b1e8911`；最终门禁为 unit `571 passed / 978 deselected`、SQLite `1521 passed / 30 skipped`、PostgreSQL `1546 passed / 5 skipped`。

关联产品 PRD（客户端仓库）：
- [`ai_companion_universe_prd.md`](../../../../../ai4all-companion-app-rn/docs/product/ai_companion_universe_prd.md)

关联后端设计：
- [`identity_model_and_wechat_binding.md`](../../shared/access/identity_model_and_wechat_binding.md)、[`web_app_channel_access_design.md`](../../shared/access/web_app_channel_access_design.md)
- [`agent_context_files.md`](../../agent-runtime/agent_context_files.md)、[`dreaming_memory_design.md`](../../agent-runtime/dreaming_memory_design.md)、[`relationship_state_design.md`](../zhaoxi/relationship_state_design.md)
- [`thick_node_postgres_refactor.md`](../../shared/data/thick_node_postgres_refactor.md)、[`multi_node_access_refactor.md`](../../shared/access/multi_node_access_refactor.md)

历史 build spec 与实施记录已集中归档到
[`docs/archive/deliveries/companion_world/`](../../../archive/deliveries/companion_world)；本 ADR 是唯一当前接手入口。

---

## 接手说明（当前权威基线）

后续同事从本文开始，不从本地草稿或历史聊天接手。阅读与执行优先级如下：

1. **本文 ADR**：产品/架构冻结决策、里程碑状态、已知偏差与下一阶段前置门。
2. **[`companion_world_p1_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_p1_backend_spec.md)**：M2-C 已实现的 schema、DTO、错误码、锁序与 backfill 细节。
3. **[`companion_world_m3_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_m3_backend_spec.md)**：M3 已完成的三表、状态机、API、幂等、PG 锁序、可观测与 rollout 契约。
4. **[`companion_world_m4_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_m4_backend_spec.md)**：M4-0 已冻结的 lifecycle/mailbox schema、状态机、API、锁序、安全策略与 rollout 契约。
5. **[`companion_world_m5_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_m5_backend_spec.md)**：M5-0 已冻结的 invite/pending/visit/human chat schema、双侧容量、ACL、锁序与保留契约。
6. **[`../../archive/deliveries/companion_world/companion_world_m5_implementation_plan.md`](../../../archive/deliveries/companion_world/companion_world_m5_implementation_plan.md)**：M5-0…M5-5 的已完成归档计划。
7. **[`../../archive/deliveries/companion_world/companion_world_m4_implementation_plan.md`](../../../archive/deliveries/companion_world/companion_world_m4_implementation_plan.md)**：M4 已完成的归档计划与最终门禁记录。
8. **[鸣蝉首次生产启用检查单](../../../ops/products/mingchan/production_first_enablement.md)**：clean-start precheck/cleanup、部署、对账、开关与回退步骤。
9. **[`../../archive/deliveries/companion_world/companion_world_m2c_implementation_plan.md`](../../../archive/deliveries/companion_world/companion_world_m2c_implementation_plan.md)**、**[`../../archive/deliveries/companion_world/companion_world_m3_implementation_plan.md`](../../../archive/deliveries/companion_world/companion_world_m3_implementation_plan.md)**：已完成归档，仅作实施 provenance。

当前代码地图：

- API/composition root：`app/bootstrap/http.py`、`app/products/mingchan/manifest.py`、
  `app/products/mingchan/api/*`。
- 纯领域层：`app/products/mingchan/domain/companion_world/`。
- 产品用例与持久化：`app/products/mingchan/application/*`、
  `app/products/mingchan/infrastructure/*`。
- 主动 App inbox 与产品工具：`app/products/mingchan/application/proactive.py`、
  `app/products/mingchan/infrastructure/app_inbox.py`、`app/products/mingchan/tools/*`。
- Runtime 接缝：`app/agent_runtime/{ports,adapter}.py`；跨产品 turn engine 位于
  `app/agent_runtime/turns/service.py`，产品差异通过鸣蝉 `turn_services.py` 注入。
- 产品 turn composition：`app/products/mingchan/application/companion_world_turn.py` 负责 World 资源、
  L3 读取、渲染和 App turn 输入组装。
- Feed/内容调度：`app/products/mingchan/jobs/world_content/*`、
  `scripts/run_mingchan_world_content_scheduler.py`（独立中心进程，default-off；已先行迁入鸣蝉）。
- 通知收件箱：`app/products/mingchan/domain/notifications.py`、
  `app/products/mingchan/infrastructure/{app_inbox,notifications}.py`、
  `app/products/mingchan/api/notifications.py`。
- Lifecycle/mailbox/visit 后台：`app/products/mingchan/jobs/world_lifecycle/{evidence,scheduler}.py`、
  `scripts/run_mingchan_world_lifecycle_scheduler.py` 与 `app/products/mingchan/api/admin_world.py`；
  同一鸣蝉 central worker 还承接 visit expiry、通知清理和媒体维护。
- 数据与迁移：产品 SQL adapter 位于 `app/products/mingchan/infrastructure/persistence/`；共享 schema
  migration registry 位于 `app/db/_core.py`。
- 运营工具：`scripts/import_companion_world_presets.py`、`scripts/import_companion_world_mailbox_catalog.py`；
  旧 carry-in backfill 已删除，clean-start 使用拆分计划中的 precheck/cleanup 工具。
- 核心测试：`tests/test_companion_world_*.py`、鸣蝉身份/通知/API 契约测试与
  `tests/test_layer_boundaries.py`。

**M3 前偏差收口状态（接手时不得忽略）**：

1. **D-06 已收口（m0031 同批）**：Runtime adapter 仅保留形态无关 `send_turn`；conversation owner 解析继续由 World repository 完成。产品拆分后，L3 DB 读取、产品渲染与 `ChannelTurnInput` 组装位于 `app/products/mingchan/application/companion_world_turn.py`；层级门禁禁止 Runtime/Platform 反向 import 产品包和产品间互相 import。
2. **后台回滚控制已补齐**：`COMPANION_WORLD_P1_ENABLED` 仍只门控 World API/auth；新增 `COMPANION_WORLD_L3_BACKGROUND_ENABLED` 独立停 L3 读/写/compact，`COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED` 独立控制 world-aware proactive safety。三者默认分别为 false/true/true，发布运行手册在 flag=false 部署期显式把 L3 设 false，开量时再启用；回滚无需删除 world 数据或临时发版。
3. **D-09 已收口（m0031）**：`platform_users.daily_limit/rpm_limit` 成为 canonical override；一致历史值自动回填，冲突历史值在清理前按最严格有效值强制，Admin 写入会传播到该真人所有 runtime account。`reclaim_expired_reservations` 已接 central proactive scheduler，node 不重复扫描。开 flag 前仍须让只读对账返回空结果，证明不存在待清理遗留冲突。

---

## 0. 摘要（TL;DR）

“朝夕相伴” App 引入的不是又一个 channel，而是一个新的**产品领域**：一个真人拥有一个私人世界、世界里有 1–10 位平等 AI 居民。这把系统从 1:1（用户↔AI）推向多对多。

3.0 的核心单一改动：**在稳定的 Agent Runtime 之上新增一个产品领域层（Companion World），由领域层持有产品实体、权限/ACL、共享范围与生命周期；Agent Runtime 保持形态无关（form-agnostic）。** 不永久 fork，不推倒重写，短期模块化单体。

关键判断（已核对代码）：三层 context 在现有实现里**物理上已分散到不同载体**，L1（人设/使命）、L2（关系）天然 per-account 独立、无需多对多改造；3.0 真正要新建的只有 **L3（关于用户的记忆）的 universe 级共享承载 + 一个加性注入块**。改造面比“多对多”一词听起来小得多。

---

## 1. 背景与问题

1.0 是微信单 Agent 个人陪伴 bot；2.0 加了 `platform_user` 真人身份、App/Web session、owner binding、渠道能力表、`/api/v1`、SQLite/PG 双后端与生产多节点。但核心业务模型仍是 `platform_user → first/default account → Soul/Memory/session/turn` 的 1:1 形态。

“朝夕相伴”要求：一人一世界、世界内 1–10 位平等居民（无主角色）、每居民独立关系/会话/Soul/Memory、世界动态、可不可逆离场、私密信箱、限时访客与真人一对一聊天。核心矛盾：现有 `account` 同时承担 **AI 关系运行时 / 产品默认账号 / Profile-Memory owner / 计费钱包锚点 / 渠道绑定对象** 五重身份；在多居民下它们不再是同一件事。

底稿 §3 逐条核查的**重构前问题与当前状态**（代码依据见后文落地说明）：

- 旧 App API 曾建立在“第一个 account”假设上；多产品 Phase 1 后已改为固定 zhaoxi audience + 产品级入口账号 resolver，M2-C World API 使用 session user + conversation/resident ID。
- 默认建号会重复赠权、拆散余额；M1 钱包已上迁真人，M2 resident runtime 使用 no-binding/no-grant 原语。
- binding 容量与 resident 容量混杂；M2 已以 world row lock + `universe_residents.status='active'` 作为容量真相。
- 旧 App turn single-flight 仍是进程内锁；M2-C World turn 已使用 PG advisory single-flight，legacy 端点未强制迁移。
- P1 universe/resident/conversation/L3 与 M3 世界动态/通知、M4 生命周期/信箱、M5 访客/真人会话均已落地并已生产启用；下文的 default-off 表述记录各里程碑交付时的发布策略。

---

## 2. 版本脉络

- **1.0**：微信单 Agent 陪伴（OpenClaw 接入、Soul/Memory/Dreaming、主动消息、moderation/限流/计费）。
- **2.0**：平台化底座（真人身份、App/Web session、owner binding、渠道能力表、双后端、多节点）。
- **3.0（本文）**：把已有能力收敛为可复用 **Agent Runtime**，其上新增 **Companion World 产品领域层**，确立**三接入形态 A/B/C** 与**三层 context L1/L2/L3**。本质是把“通用 Agent 运行能力”与“具体产品领域/权限/共享模型”分层。

---

## 3. 决策记录（ADR 主表）

> 定级：**P1 阻断** = 第二个居民落地前必须完成；**架构** = 方向性约束，贯穿全程。每条附代码依据。

### D-01 分层重构，不 fork、不重写（架构）
- **决策**：保留现有仓库与生产底座，做有边界的渐进式重构：接入/产品 API 层 → 产品领域层 → Agent Runtime → 平台基础层（见 §4）。短期模块化单体，拆服务由后续真实复用/扩缩容信号决定（沿用底稿 §13）。
- **理由**：fork 是源码动作、不形成架构边界，且会让 Soul/Memory/Turn 修复、moderation/计费规则、DB 迁移双份漂移；重写要重新承担 account 隔离、prompt 组装、记忆生命周期、moderation、幂等/限流/计费、双后端多节点、微信零回归等已解决的风险。新产品缺的是上层领域，不是另一套 LLM 封装。
- **影响**：以 characterization test 保护现状后逐步抽边界；分层边界由 CI 中的 stdlib AST 测试强制（见 D-12），不引入 `import-linter` 依赖。

### D-02 account 重定位为 `agent_runtime_id`（架构）
- **决策**：概念上把 `account_id` 定位为“一个用户与一个 AI 关系实例的隔离运行容器”。**暂不做全仓库 rename**，通过 `AgentRuntimePort` facade 隐藏旧命名。Companion World 只保存 `universe_resident.runtime_account_id` 映射，不跨层读 Runtime 的 Memory/Session。
- **理由**：全仓库机械 rename 高风险、零收益；facade 足以隔离命名。
- **影响**：同一官方模板进入两个用户世界 → 生成两个 resident + 两个 runtime account，私聊/Memory 不共享。Port 方法从真实调用点长出，不提前落死（`set_read_only`/`configure_persona` 等暂无调用方的方法标注待定）。

> **落地说明（账号模型对齐决策 B 已交付 2026-07-21，并入 main #42 前）**：canonical 决策/改动面记录 = [`../../archive/deliveries/companion_world/companion_world_account_model_reconciliation.md`](../../../archive/deliveries/companion_world/companion_world_account_model_reconciliation.md)。要点：
> - **「account」一词两义**（历史命名债）：对 main #42（App/账号收敛）= 用户 App 账号（一手机号 × 一 App ≤ 1 个 active）；对朝夕相伴 = 每个 AI 居民一行 runtime `account`（一真人 1–10 个）。两者共用 `accounts` + `account_owner_bindings` 表。
> - **`account_owner_bindings` 是微信接入（形态 A）独有的产物**，记录「微信渠道把某 account 绑到某真人」，**不是通用「用户账号」机制**。纯朝夕相伴用户（无微信）零 owner_binding；居民都不发 binding（否则撞 #42 的 `ux_owner_binding_active_user_app`）。
> - **决策 B**：「account → 真人」解析加**世界 fallback**——owner_binding →（无则）`universe_residents.runtime_account_id → universes.owner_platform_user_id` →（无则）回退 account_id。canonical `accounts.resolve_owner_platform_user_id`，收口 `_resolve_quota_subject` / `get_platform_user_id_for_account` / `get_wallet_summary` / `wipe_account_data` sibling 检查。居民经 **M1-5 内部建号原语** `create_resident_runtime_account`（建 account + 世界映射，不发 binding / 不赠权 / 不占 owner-binding/App-account 容量；激活后仍计入 world active 容量）。B 不破 #42（居民从不建 active binding → 唯一索引永不触发）。

### D-03 三种接入形态 A/B/C（架构）
- **决策**：显式建模接入形态。**A（微信）** 1 真人 ↔ 1 Agent，无世界、无共享问题；**B（朝夕相伴）** 1 真人 ↔ 1 世界 ↔ N Agent（1–10），共享 L3、独立 L1/L2；**C（未来）** 绑定关系与共享范围未知，**不提前设计**，仅要求架构不写死形态假设。
- **理由**：把“共享/隔离范围”变成显式的形态维度，避免每次新产品都改内核。
- **影响**：见 §7 形态 C 接缝手册。

### D-04 三层 context 模型 L1/L2/L3（架构）
- **决策**：把今天塌在 per-account 一层的 context 显式拆为 **L1 人设/使命**（1 模板→N 关系，实例锚 runtime account，模板带版本）、**L2 关系状态**（1 per 用户×Agent，锚 runtime account）、**L3 用户记忆**（1 per universe，同世界共享）。映射见 §5。
- **理由**：L1/L2/L3 的基数与共享语义不同，混在一层无法支撑形态 B。
- **影响**：L1/L2 现状已 per-account 独立、白拿；真正新建的是 L3 universe 级承载（见 §6）。

### D-05 L3 = 关于用户的沉淀记忆全量共享，无字段级白名单；但不共享聊天原文与检索（冻结 2026-07-18，边界精化 2026-07-19）
- **决策**：形态 B 同一 universe 内——
  - **共享（L3，进全部 resident 上下文）**：一切“关于真人用户的持久事实/画像 / 长期沉淀记忆”（USER 资料段、MEMORY 用户记忆段、八字等派生画像），默认对该世界全部 resident 可见，**不做字段级白名单**。
  - **不共享（保持 per-resident/per-account 隔离）**：各 resident 与用户的**原始聊天记录**（`messages`/session 逐字稿），以及对这些原文的**检索 / 证据回放能力**（`tool_evidence_replay` / retrieval）。一个居民读不到另一个居民的私聊原文。
  - L2（关系专属记忆）与 L1（人设/使命）保持 per-resident 隔离。用户对 L3 的可见/可编辑/可清空为产品交互层，另做，不阻断后端 L3 落地。
- **产品意象（背后假设）**：同一虚拟世界里的多个 AI 角色之间会“**八卦**”——他们共享的是**对这个用户的认知/了解**（沉淀记忆），而不是彼此的私聊逐字稿。“世界里的居民都认识同一个我”，但各自和我聊过什么仍是私密的。
- **理由**：字段级白名单在无明确需求时是过度设计；而把逐字原文与检索一并共享则会越界——既泄露私密对话，又让不同居民失去各自独立的相处质感。共享“沉淀后的用户认知”恰好实现“都认识同一个我”，同时守住私聊边界。
- **影响**：L3 承载只上提**沉淀记忆层**（USER/MEMORY 用户事实段、派生画像），**不**上提 `messages`/session 原文，也**不**让 retrieval 跨 runtime account。测试需验证：①用户事实类整体进 L3、关系/人设类不进 L3；②居民 A 的聊天原文/检索结果不出现在居民 B 的上下文。⚠️ 客户端 gap-analysis §4.2「P1 默认隔离/仅白名单」仍与此相反；M2-C 后端已按本决策实现，客户端镜像修订现为**生产开 flag 前置门**，不是待后端再次选择的开放问题。

### D-06 共享范围属产品域层，Runtime form-agnostic；L3 锚 `universe_id`（架构）
- **决策**：**共享范围是产品域层的组合行为，绝不编码进 Agent Runtime。** Runtime 只暴露“读某 runtime account 的 per-account context（L1+L2）+ 接受一个注入端口把外部 shared-context 喂进 prompt build”。形态 A 域层不注入（退化 1:1）、形态 B 按 `universe_id` 注入、形态 C 自定义范围复用同一 Runtime。L3 锚 `universe_id` 而非 `platform_user_id`。
- **理由**：把共享语义留在域层，未来“一人多世界”天然按世界隔离，跨世界共享再叠加一层即可，属加性演进、不重写。
- **影响**：Runtime 满足“不反向依赖 Companion World”；注入端口是唯一新接缝。

### D-07 容量真相脱离建号 binding 计数（P1 阻断，冻结 2026-07-19）
- **决策【已冻结】**：active 容量以 `universe_residents.status='active'` 为唯一真相，在 world row lock 下计数；offline/dismissed 保留 account 与 binding 仅供只读历史、不计位。建号函数的 binding 上限与居民容量**解耦**（新增 no-limit / no-grant 建号路径）。
- **依据**：`billing.py:2090` 建号前 `COUNT(*) account_owner_bindings WHERE status='active' >= 10` 抛错。
- **影响**：成本事件仍记实际 resident runtime，余额扣减归同一真人 billing owner。

> **落地说明（核对 2026-07-19，M1 pre-work 结论：计数解耦事实已满足，no-cap/no-grant 路径随 M2 接线）**：核对现状代码——建号容量校验 `billing.py:2087-2095` **本就只数 `status='active'` binding**（非「历史 binding 全计」），离场路径 `wipe_account_data`（`lifecycle.py:224`）直接 **DELETE binding**、`/web/me/unbind` 把 active 归零，赠权已按真人幂等（`new-user-grant-{platform_user_id}`，D-14/M1-7）。故 D-07 的「active 容量真相、历史 binding 不占位」在 M1 现状**事实已满足**，无独立编码。真正剩余的 M1 pre-work = §12 M2 本体点名的 **M1-5「no-cap/no-grant 内部建号路径」**（M2 在 world lock 下用 `universe_residents.status` 自校容量，故建号侧不要 10 闸再打架）——该内部路径随 **M2 建居民**一并接线（M1 不引入 universe 表，R1a「不依赖任何 universe 表」）。**（更新 2026-07-21：M1-5 已随账号模型对齐决策 B 交付 = `billing.create_resident_runtime_account`，建 account + `universe_residents` 映射、不发 binding/不赠权/不占容量；容量真相仍 = `count_active_residents`。见 D-02 落地说明。）**

### D-08 legacy resident 保留离开豁免（P1 阻断，**冻结 2026-07-19 = 保留豁免 + 修订 PRD**）
- **决策【已冻结 = 保留豁免】**：微信 legacy 居民与 App legacy 居民共享同一 runtime account，“legacy 普通居民 + 微信零回归 + 不可逆 offline”三者互斥。**取「保留豁免」**——legacy 居民永不 offline，App 仅展示、不提供离开入口；工程上微信零回归天然满足（不动共享 runtime account）。**须同步修订客户端 PRD `ai_companion_universe_prd.md:634`「来源不构成豁免」记为已知偏离**（本轮不动客户端仓库，去客户端仓库时改；§10.4 已标注）。
- **backfill【§10.2 已冻结 2026-07-21】**：同一真人的**全部 active binding** 各映射一个 `origin='legacy'` resident；最早 active binding 只写 `legacy_primary_account_id`（兼容旧 `/chat/*`，不是主角色）。不重放 onboarding、不自动补 4 位预设；无 active binding 按新用户 4 候选流程；满 10 全量保留并显示容量已满，不删历史、不强制降级。
- **依据**：`account_owner_bindings` 历史上允许一真人多 account；只映射“第一个”会让其余既有 Soul/Profile/Session/Message/Memory 在新 App 中消失。全量加性映射才能同时满足历史保留、账号隔离与回滚安全。

### D-09 限流按用户聚合须原子预占/回滚（P1，配额键冻结 2026-07-19）
- **决策【已冻结 = platform_user】**：daily/RPM 配额键**按 `platform_user` 聚合**（一个真人一套配额，多居民共享，不随居民数放大）。配套：daily 计数改**原子预占 + 失败回滚**（消除 `turn_service.py` 读—处理—+1 的 TOCTOU），RPM advisory 锁的 key 从 runtime account 迁到 `platform_user_id`。billing wallet 归属同真人（见 D-14 钱包上迁 + D-07 建号解耦）。
- **配额 schema + 退款语义（冻结 2026-07-19，补 Codex #4）**：
  1. **schema 上迁用户级**：`daily_usage` 唯一键 `(account_id,date)`（`_core.py:635`）、`rpm_hits` 仅 `account_id`（`:1625`）→ 迁 `(platform_user_id,date)` / `rpm_hits.platform_user_id`（或通用 `subject_type/subject_id`，最终形态 M2-0 定型）。
  2. **override 冲突（已收口）**：m0031 以 `platform_user` 为 canonical override；一致历史值自动回填，冲突遗留值在运营清理前按最严格有效值强制，切换 resident 不可绕过。
  3. **退款矩阵 = 仅成功计费的 turn 消耗**：moderation 拦截 / 模型失败 / 网络断 / 重复消息**一律回滚预占**，只有真正产生 LLM 计费的 turn 扣配额。
  4. **reservation 与入站幂等同事务**：去重（`(conversation_id,sender_id,client_message_id)` 唯一）先于预占，**同一事务**，重试不吃配额。
  5. **崩溃悬挂**：进程崩溃残留 reservation 带 TTL；reserve 时 prune-on-touch，批量回收同时由 central proactive scheduler 执行。
  6. **PG 锁顺序**：两 resident 并发同一 `platform_user` 按单键 advisory 锁串行；PG 用例覆盖并发预占不超卖、回滚不误伤他人配额。
- **依据**：daily 路径 `turn_service.py:1065` 读、`:1185` 处理后才 +1，**无 advisory 锁**（RPM 路径 `rate_limiter.py:52` 有 `pg_advisory_xact_lock`）；TOCTOU 今天被 per-account 单飞掩盖，聚合到用户后跨居民无单飞将暴露。
- **同源提示**：D-07/D-09/重复赠权同源于“建号函数把 owner binding 当计费锚点+容量上限+配额键三重身份”，P1 须一并解耦。

> **落地说明（M1-3 + M1-4 键上迁已交付 2026-07-19；原子预占/退款/TTL 拆下一刀）**：与用户拍板「键上迁优先」——本刀只把配额**计数键**上迁到 `platform_user`（达成「一真人一套配额、多居民共享」核心目标 + 翻转 characterization 接缝 6），**不含** §D-09 的原子预占 + 回滚 / 退款矩阵 / TTL 回收；那部分动机（跨居民并发 TOCTOU）M2 多居民才出现，拆下一刀、M2 前置补齐。已交付：
> 1. **迁移** `_migration_0026_daily_usage_platform_user`（版本 26，`_core.py`；并主干后由原 m0023 重排）：`daily_usage` 加 `platform_user_id` 列（无 FK）+ 从最早 active binding 回填 + 合并同 (真人,date) 多行（求和入 MIN(id)、删其余）+ 加唯一索引 `ux_daily_usage_user_date(platform_user_id,date)`，幂等。瞬态计数无历史余额可损坏，故**无需 precheck**（owner 解析不变式已由 D-14 precheck 作同一 M1 发布闸覆盖）。
> 2. **daily**（`accounts.py` 三函数）：对外仍收 `account_id`，内部 `_resolve_quota_subject`（同连接/事务解析，孤儿号回退 account_id）按真人聚合；`increment_daily_usage` 的 `ON CONFLICT` arbiter 迁到 `(platform_user_id,date)`，`account_id` 列继续写作创建来源。调用方（turn_service daily 路径、admin、`test_db_rate_limit.py`）零改动。
> 3. **RPM**（`turn_service.py`）：RPM 检查前解析 `quota_subject = get_platform_user_id_for_account(...) or account_id` 传入通用 `check_rpm`；`rpm_hits` 表 schema 不动（列语义变为不透明 subject，与 web IP-keyed 调用共命名空间不撞），存量行 30–60s 自然过期、无需迁移。
> 4. **约束兼容与后续收口**：①**保留 `UNIQUE(account_id,date)` 只加 `(platform_user_id,date)` 唯一索引**，不删旧约束/不表重建，避开 SQLite 12 步重建 / PG DROP CONSTRAINT；改按真人聚合后每 (真人,date) 至多一行、旧唯一仍满足。②M2-C 时遗留的 account-level override 已由 m0031 上迁 `platform_user`；account 列仅保留兼容副本和冲突迁移依据。
> 测试：翻转 `test_characterization_baseline.py` 接缝 6 为共享终态、新增 `test_daily_quota_migration_m0026.py`（回填/合并/幂等）+ `test_db_rate_limit.py` 共享 RPM。门禁 SQLite + PG 双档全绿。

> **落地说明（原子预占下半刀已交付 2026-07-19）**：补齐 item 3–6 的原子预占 + 回滚/退款矩阵 + TTL（键上迁下半刀）。已交付：
> 1. **迁移** `_migration_0027_daily_quota_reservations`（版本 27，`_core.py`；并主干后由原 m0024 重排）：新增独立预占表 `daily_quota_reservations(id, platform_user_id, date, account_id, created_at, expires_at)` + 两索引。空表迁移、无回填、幂等。**不改 `daily_usage` schema**——`message_count` 语义保持「已确认成功计费的计数」，`get_daily_usage`/`get_usage_last_7_days`/admin 展示零改动。
> 2. **4 个原语**（`accounts.py`）：`reserve_daily_quota`（PG 取单键 advisory 锁 `pg_advisory_xact_lock('quota:'||platform_user)`【P1 §2.7 L3】→ prune-on-touch 清过期 → 按 `message_count + 活跃 reservation 数 < limit` 原子校验 → 插预占行，`limit<=0` 不限；已满返回 `None`）、`confirm_daily_quota`（删预占行 + `increment_daily_usage`，`DELETE rowcount` 守幂等不双记）、`rollback_daily_quota`（删行、不计数）、`reclaim_expired_reservations`（批量兜底回收）。共享 `advisory_lock_key`（抽 `rate_limiter._advisory_key` 到 `_core`，避免 `app.db`→`rate_limiter` 循环导入）。
> 3. **turn_service 接线**：`_persist_and_screen_inbound` 入站 tx 内、`insert_message`（去重）**之后**、同一 `conn` 调 `reserve_daily_quota` 取代旧「入站即 +1」（满足「去重先于预占同事务」，重试不吃配额）；预占失败（在途 race 到满）返回 `rate_limited`。`_finalize_turn` 计费谓词处**一处**收敛：`should_charge → confirm` 否则 `rollback`（**配额消耗 ⟺ 钱包计费**）。保留 `_prepare_turn` 的 daily 读检做 fast-path（常见「已满」无插入即拒）。退款矩阵落地为：moderation 拦截 / 模型失败 / 特殊命令 / 未计费 onboarding 一律 rollback、不扣 daily。
> 4. **TTL**：reserve 时 prune-on-touch（清本真人已过期悬挂，活跃真人自愈，TTL 默认 15min ≫ turn 秒级，保证 cap 计数正确）；崩溃后再无来信的残留由 central proactive scheduler 调用 `reclaim_expired_reservations` 分批清理，步骤失败隔离并进入 heartbeat 错误。
> 5. **配额边界**：override 来源已由 m0031 上迁真人；「网络断」按「对 LLM 调用失败(generation_error)」口径归入回滚，出站发送失败在计费之后、配额随钱包一致（不改钱包侧语义）。
> 6. **已知偏离（2026-07-23 review 记录，低危、不阻断发布）**：item 3 的「配额消耗 ⟺ 钱包计费」在实现上以 **`should_charge` 谓词**为准，而非 `record_chat_usage_charge` 的**实际扣款结果**。因此当 `should_charge=True` 但扣款函数返回 `None`（孤儿号无 `platform_user` 或零 token）或抛异常并被 catch 时，配额仍被 `confirm`、钱包未扣；这与本 item 3 及 §D-09「系统失败→回滚」的字面口径存在细微不一致。该方向对 quota 是 fail-closed，不会超卖或通过故障绕过限额，但会形成少扣与一次 quota 消耗。后续需在“仅实际扣款成功才 confirm”与“接受保守偏离并补失败指标/告警”之间单独定案；当前先如实登记，不在 closeout 中改 money 语义。
> 测试：新增 `test_daily_quota_reservation.py`（reserve/confirm/rollback/TTL/幂等/多号共享/回滚不误伤 + **PG 并发不超卖**，SQLite 下 skip 并发用例）、`test_turn_rate_limit.py::test_failed_turn_rolls_back_daily_reservation`（失败 turn 回滚不消耗、不泄漏）。门禁 SQLite 全量 + PG lane（§9 硬门禁：并发不超卖只在 PG 算数）。

### D-10 世界内容 ≠ 主动消息（架构）
- **决策**：世界动态、离别动态、信箱由独立 world-content/lifecycle job 生成、审核、持久化（`universe_posts` 等），客户端主动拉取 Feed；**不通过打开 `CHANNEL_APP.supports_proactive` 实现**。是否 Push 后续独立设计。
- **依据**：`channels.py` `CHANNEL_APP` `supports_proactive=False`、`tdai_enabled=False`；开启会误启当前无 APNs/FCM 投递能力的提醒工具。

### D-11 真人聊天分表，不进 Agent Runtime（架构）
- **决策**：`human_conversations`/`human_messages` 与 AI `messages` 分表，`(conversation_id, sender_id, client_message_id)` 唯一；从结构上保证真人消息不进 LLM/Soul/Dreaming/AI Memory/moderation prompt。真人内容另做独立内容安全/举报/封禁。
- **结构护栏（2026-07-23 review 补强）**：`tests/test_layer_boundaries.py::test_ai_paths_do_not_import_human_chat_storage` 不仅禁止 AI 路径直接 import human-chat storage/platform/domain 完整模块，还从 `app/db/companion_world_human_chat.py::__all__` 静态枚举再导出符号，禁止 `from app.db import insert_human_message` 这类经 `app/db/__init__.py import *` 绕过完整模块前缀的写法；`__all__` 无法解析时门禁自身 fail-closed。该测试继续只用 AST，不在扫描时 import 业务模块。

### D-12 并发正确性以 PG 为证；分层边界 CI 强制（架构）
- **决策**：竞态类不变量（并发确认、10/11 位竞争、双花 code、slot 竞争、offline 与新 turn 并发、L3 并发写）只在 PostgreSQL 测试算数，SQLite 只验功能正确性。使用 `tests/test_layer_boundaries.py` 的 stdlib AST 契约禁止 `app.domains.companion_world.*` 依赖 `app.db.*`/`app.turn_service`，并禁止 `app.agent_runtime.*` 反向 import `app.domains.*`，纳入 CI；不引入新依赖。
- **依据**：SQLite 单写者串行会假绿；分层边界不靠纪律靠工具。

### D-13 主动消息沿 L2/真人级分裂；真人级触达上提产品域层；App 投递面 = 拉取式通知/收件箱（架构 + 投递面冻结 2026-07-18）
- **决策**：主动消息按触发本质切两半（详见 §11）：
  - **per-resident 义务**（`USER_REMINDER`、`COMPANION_FOLLOWUP/commitment`、`CONTENT_INVITATION_RESPONSE`、`TASK_RESULT`）= L2 对齐，**留 Agent Runtime 侧、继续 per-account**。
  - **真人级触达**（`NEW_USER_REACTIVATION`、`CONTENT_INVITATION`、`COMPANION_FOLLOWUP/account_check`）= 类比 L3，**上提产品域层**：按 universe 聚合去重、跨 `account_owner_bindings` 聚合预算与活跃判断、新增"发声人(resident)选择"。
  - **投递面（已冻结 = A）**：App 侧落**拉取式通知/收件箱**，与世界 Feed（D-10）分离入口；不依赖 push、不误开 `supports_proactive`。微信 legacy 居民走原 `send_weixin_text` 不变。APNs/FCM 真 push 作为后续独立能力，不阻断本决策。
- **依据**：原 proactive 主体仍按 `account_id` 键，唯一出口 `dispatch_proactive_text` 受 `supports_proactive` 门控，App channel 保持 false。M2-C 只新增 `human_level_proactive_allowed` 的 World 查询安全阀，尚未改变预算/due/活跃聚合模型；因此完整真人级上提仍属 M3。
- **影响**：与 D-09（限流聚合）同源；M2-C 先上防 N× 安全阀（真人级仅对 legacy primary 居民触发），M3 再完成领域上提。详见 §11。

### D-14 钱包主键上迁 `platform_user`，一真人一钱包；预检 + 自动合并少量存量（P1 阻断，冻结 2026-07-19）
- **决策**：`entitlement_wallets` 的唯一键从 `account_id` 上迁到 `platform_user_id`——**一个真人一个钱包，全部居民共用一份余额**；新客赠权按真人发一次，居民 2…N 走 no-grant 建号（与 D-07 建号解耦同处）。`entitlement_ledger`/`cost_events` **保留 `account_id`** 列，分居民成本/质量分析不丢。**不走"短期映射表"补丁**（那既留债又给不出单一余额，且与 D-09 配额按真人聚合不自洽）。
- **依据（已核代码）**：`entitlement_wallets.account_id` 现为 `UNIQUE` 主查键，钱包全程 `WHERE account_id=?` / `ON CONFLICT(account_id)`（`billing.py:626-673` 等），故今天"一 account 一钱包"；但表/流水/成本行**已冗余带 `platform_user_id`**（`_core.py:393-462`）。分歧集中：钱包解析在 `billing.py` 单文件 ~10 处 SQL；对外 API（`get_wallet_balance_shell_micros`/`get_wallet_summary`/`list_wallet_ledger`/`record_chat_usage_charge`）**保留 `account_id` 入参、内部 account→user→wallet 解析**，故 `turn_service.py:1924`、`relationship_state.py:130/182`、web/admin 读侧几乎不动。
- **为何现在改（口径修正 2026-07-19，Codex）**：建号允许**每真人 ≤10 account**（`billing.py:2090`），故「一真人一钱包」**并非当前不变量**——多 account 老用户已有多钱包，**原「零合并」说法作废**。但**现在改仍最省**：多居民尚未上线、fork 程度最低，需合并的只是历史多 account 的少量存量；趁早远比多居民放大后（N 世界 × N 居民钱包）合并便宜。与 A2/D-09（配额按 `platform_user`）同锚、自洽。
- **迁移方案（冻结 = 预检 + 自动合并，2026-07-19）**：
  1. **生产预检 SQL**（M1 前置门，必须在生产 PG 跑，本地 SQLite 仅 2 user 不代表生产）：`SELECT platform_user_id, COUNT(*) FROM entitlement_wallets WHERE status='active' GROUP BY platform_user_id HAVING COUNT(*)>1` + 余额/ledger 分布，产出多钱包用户清单。**已交付（M1-0，2026-07-19）= `scripts/precheck_wallet_migration.py`（只读，退出码 0=PASS/1=BLOCK 作发布闸）**：除盘点（多钱包用户 + 多次赠权用户）外，硬校验迁移所依赖的四条数据不变量并阻断——`ambiguous_owner`（单 account >1 active binding，归属歧义）/`orphan_wallet`（active 钱包 account 无 active binding）/`owner_drift`（钱包 `platform_user_id` 与唯一 active 归属人不一致，会并错人）/`primary_undefined`（多钱包用户最早 active binding 对应 account 无 active 钱包，选主取不到）；负余额为 WARN。判定逻辑单测见 `tests/test_precheck_wallet_migration.py`（5 用例，四条阻断各注入验证）。
  2. **自动合并**：迁移脚本对多钱包用户做**余额求和 + ledger/cost_events 归并到选主钱包（选主 = 最早 active binding 对应钱包）+ 其余钱包置 `status='merged'` 保留审计**；单钱包用户零合并直迁。两条路径同一脚本覆盖、可重复运行。
  3. **目标 DDL**：加 `UNIQUE(platform_user_id) WHERE status='active'`（局部唯一索引）；**保留** `UNIQUE(account_id)`（落地决策 2026-07-19，见下「落地说明」——因改按真人 get-or-create 后永不为同一真人插第二钱包行、`account_id` 事实上仍唯一，故不删，避开 SQLite 表重建 / PG `DROP CONSTRAINT` 的零先例后端分叉）；`account_id` 列语义变「创建来源」；`ledger`/`cost_events` 的 `account_id` 列一并保留（分居民成本分析）。
  4. **赠权幂等键**：`new-user-grant-{account_id}` → `new-user-grant-{platform_user_id}`（`billing.py:815`），防上迁后每居民重复赠权。
  5. **实际部署形态**：最终采用 stop-start 单迁移 m0025，双唯一并存；不删除 `UNIQUE(account_id)`，无需 SQLite 表重建或 PG contract 阶段。
- **代价**：动 money 路径，须 PG 并发测试兜底（跨居民同时扣款不双扣、`idempotency_key` 唯一守住、合并迁移幂等）；实际范围锁在 `billing.py` + m0025 + billing/PG 并发测试。
- **同源提示**：D-07（容量脱建号计数）/D-09（配额键）/D-14（钱包键）/重复赠权四者同源——建号函数把 owner binding 当"计费锚点 + 容量上限 + 配额键"三重身份，P1 一并解耦。

> **落地说明（M1-1 + M1-7 已交付 2026-07-19）**：
> - **保留 `UNIQUE(account_id)`，只加局部唯一索引**（不走 item3 原文的「删约束」）。理由：billing 改按 `platform_user` get-or-create 钱包后，永不会为同一真人插入第二个钱包行，`account_id` 事实上仍唯一、保留无害；据此**完全避开** SQLite 12 步表重建 / PG `DROP CONSTRAINT` 这个本仓库零先例、money 表首次引入的高风险后端分叉。日后若要 item3 的纯「非唯一」语义，可用一条清理迁移补删。
> - 因不删旧约束，item5 的 expand/migrate/contract **收敛为单个迁移** `_migration_0025_wallet_unique_platform_user`（`app/db/_core.py`，版本 25；并主干后由原 m0022 重排）：合并存量多钱包老用户（选主=最早 active binding 对应钱包、余额求和、ledger/cost_events 归并到主、其余置 `status='merged'`）→ 加局部唯一索引；幂等、可重复执行。stop-start 部署（本仓库现状），无滚动窗口。
> - **连带 M1-7**：赠权幂等键 `new-user-grant-{account_id}` → `new-user-grant-{platform_user_id}`（`billing.py`），钱包并份后同一真人第二个号不二次赠贝壳。
> - billing 解析 6 处（`_ensure_wallet_in_conn` / 两个 charge 幂等分支 / `get_wallet_summary` / `list_wallet_ledger`）由 `WHERE account_id` 改按 `platform_user_id AND status='active'`（`list_wallet_ledger` 流水视图随之按真人聚合）。对外 API 的 `account_id` 入参与 ledger/cost_events 的 `account_id` 列均不动。
> - 测试：`tests/test_wallet_migration_m0025.py`（合并正确性 + 幂等）、`tests/test_characterization_baseline.py`（接缝 5 翻转为终态）、`tests/test_precheck_wallet_migration.py`（构造迁移前多钱包态）。**M1 整体发布仍以 M1-6 PG 并发闸为准**（本刀已过既有 PG billing 套件）。
>
> **落地说明（M1-6 钱包侧 PG 并发硬闸已交付 2026-07-19，#11）**：§9 发布闸的钱包侧显式并发用例补齐 = `tests/test_billing_concurrency_pg.py`（全部 PG-only，SQLite 下 skip）。证三条不变量在真 PG 上成立：①**跨居民并发扣款不丢更新**——同真人两 account 交替对共享钱包并发 `record_chat_usage_charge`（各不同 `idempotency_key`），余额恰为初始 − 各笔求和、`cost_events`/`entitlement_ledger` 各 N 行（靠 `balance = balance + ?` 原子自增，非读—改—写）；②**同 `idempotency_key` 并发恰扣一次**——8 worker 抢同一 key，`cost_events` 与 `entitlement_ledger`（`usage-charge-{key}`）双 UNIQUE 挡下双记，落库各 1 行、余额只扣一次，败者抛 `IntegrityError` 由 `connect()` 回滚半途扣减（不双记、不污染；真实 turn 路径经 `insert_message` 去重不可达此竞争、`turn_service` 亦 try/except 吞掉）；③**跨真人并发互不误伤**——两真人各自钱包扣减独立、无串扰（账号隔离核心不变量）。配额侧的并发不超卖/回滚不误伤已由 D-09 下半刀 `test_daily_quota_reservation.py::test_concurrent_reserve_no_oversell_pg` 覆盖。**至此 §9 钱包 + 配额并发硬闸齐备，M1 仅余 M1-5「no-cap/no-grant 内部建号路径」随 M2 建居民接线。**

### D-15 M3 Feed、通知、发声人与 App-only 触达口径（产品冻结 2026-07-22）

- **Feed 首版**：只生成文字动态，不生成图片、音视频、外链或工具结果。生成频率按 `universe` 聚合，不按 resident 扇出：每个北京自然日最多 2 条，上午、傍晚各最多 1 条；仅 `onboarding_state='confirmed'`、至少一位 active resident 且真人最近 7 天有任一渠道入站的世界可生成。窗口内允许因无合格内容或系统失败而跳过，错过后不补发、不跨日追赶。M3 用户文字动态默认直接发布，不接现有 account-scoped moderation；保留 post 下架接缝，审核主体/策略/测试后续按 post/world/platform user 单独设计，AI 内容不得误处罚 resident runtime account。
- **通知收件箱**：与 Feed 分表、分入口、分红点；红点只等于当前真人未过期 `unread` 数量。拉取列表不自动已读，首版提供单条已读与全部已读。`read` 自 `read_at` 起保留 7 天，`unread` 自 `created_at` 起保留 30 天；列表/计数即时排除逻辑过期行，不等待物理清理。每个 `platform_user` 最多保留 200 条，写入事务内超限时先删最旧 `read`、仍超限再删最旧 `unread`，central scheduler 再分批清理到期行并对账上限。所有查询、标记与清理均以 `platform_user_id` 约束，不接受客户端 `account_id`。
- **真人级发声人**：App 收件箱优先选择最近收到用户入站消息的 active resident；没有入站历史时依次按最近会话活动、`joined_at DESC`、`resident_id ASC` 确定，保证结果稳定。计划到投递之间若居民已非 active，必须在投递事务前重选。首版不提供用户指定“谁来找我”。微信只能由具备真实微信投递路由的 `legacy_primary_account_id` 对应 resident 发声，不借其通道冒充其他居民。
- **App-only 真人级触达**：产品能力首版启用，但只写 App 拉取式收件箱，不做 APNs/FCM/system push；新增独立开关，代码默认关闭后灰度开启。开启后仍须同时通过用户主动触达开关、跨居民/跨渠道活跃判断、quiet hours、现有策略预算与内容安全；App-only 真人级类别合计每个 `platform_user` 滚动 24 小时最多 1 条。独立开关关闭时继续 fail-closed，但不影响 App 的 per-resident reminder/commitment 写收件箱。
- **已知软一致性（2026-07-23 review）**：legacy proactive 的部分 cooldown/观测状态仍保存在各 runtime account 行；确定性 App 发声人切换时，账号局部 cooldown 可能随发声人变化。投递层按 `platform_user` 的 owner 行锁、滚动 24 小时 reservation 与投递前 speaker 重选仍保证真人级消息不因居民数发生 N× spam，因此不阻断发布。若后续产品要求“发声人切换也保持完全连续的真人级 cooldown/指标”，再把对应状态上迁到 `platform_user`，不要复制跨账号状态。
- **范围切割**：§10.7 原先并列的 `character_letters`/mailbox 触发、冷却、过期、待处理上限属于 M4，本次不冻结，也不再作为 M3 开工门。

---

## 4. 目标分层与依赖方向

```text
接入与产品 API 层   WeChat/OpenClaw · Companion App API · Web · Future App API
产品领域层          Companion World（universe/resident/feed/lifecycle/mailbox/visit/human chat） · Future domains
Agent Runtime       runtime account · Soul/Identity/Profile · session/message · prompt/turn/model/tool · Memory/Dreaming · AI moderation
平台基础层          platform user/auth · entitlement/billing · DB/migration/repo · scheduler/outbox · observability
```

依赖方向：`API adapter → product domain → AgentRuntimePort → 现有实现`，域层旁挂 `→ platform services`。**Agent Runtime 不反向依赖 Companion World；微信 adapter 不读 world 表；human chat 不进 Runtime。**

`AgentRuntimePort` 只从真实调用点长出。P1 实际端口为 `send_turn`；conversation owner 解析当前由 World repository 完成。`configure_persona` / `set_read_only` / `get_runtime_summary` 等没有真实调用方的方法不提前落死，M4 再为 offline/read-only 补端口。

当前代码组织以本文“接手说明”的代码地图为准。目标依赖方向不变：`app/agent_runtime/adapter.py` 仅保留形态无关 `send_turn`，World conversation/L3 composition 已归 `app/platform/companion_world_turn.py`；Feed/通知/visit 字段不得进入 Runtime。

---

## 5. 三层 context 模型（映射现有载体，已核对代码）

| 层 | 内容 | 基数/锚点 | 当前物理载体（核对后） | 形态 B 行为 | 独立性现状 |
|---|---|---|---|---|---|
| **L1 人设/使命** | persona、口吻、边界、mission | 1 模板→N 关系；实例锚 runtime account；模板带版本 | `account_profile_files`（SOUL/IDENTITY/MISSION）+ `account_mission` 表 | 各 Agent 独立 | 已 per-account 独立 ✅ |
| **L2 关系状态** | 关系阶段、马斯洛需求、共同历史、关系漂移 | 1 per 用户×Agent，锚 runtime account | `account_user_meta` 行（已脱离文件）+ MEMORY.md 关系性片段 | 各 Agent 独立 | 已 per-account 独立 ✅ |
| **L3 用户记忆** | 关于真人的持久事实/画像（沉淀记忆；**不含聊天原文**） | 1 per **universe**，同世界共享 | P1 新事实落 `universe_memory_facts`；既有 per-account USER.md/MEMORY.md 仍保留，不做存量全量搬迁 | 同世界多 Agent 共享 | **P1 typed sink 已落地；历史文件仍按原路径保留** |

**关键判断**：L1/L2 在现有 per-account 隔离下天然独立，无需多对多改造；3.0 真正要新建的是 L3 的 universe 级承载，以及把今天混在 SOUL/MEMORY 里的层拆出来。

**L3 共享边界（D-05 精化，2026-07-19）**：L3 只共享**对用户的沉淀记忆**（USER/MEMORY 用户事实段、派生画像）。各 resident 的**原始聊天记录（`messages`/session 逐字稿）与检索/证据回放（`tool_evidence_replay`）不进 L3、不跨 runtime account**——居民之间“八卦”对用户的认知，但读不到彼此的私聊原文。因此 L3 上提的是“沉淀层”，`messages` 表仍严格 per-account。

现状承载的代码事实（供实现参考）：
- 账号级 profile 文件已下沉 DB 表 `account_profile_files`，**主键 `(account_id, filename)`**，account 隔离硬编码在每条 SQL（迁移 m0004 `_core.py:1592-1613`，门面 `profile_storage.py`，读写 `user_profiles.py:135,211`）。
- 关系四状态 + 派生画像在 `account_user_meta`，使命在 `account_mission`；由 `agent_self_state.build_agent_self_state_block`（`agent_self_state.py:117`）渲染后经 `assemble(agent_self_state=...)` 注入（`turn_service.py:619`，`prompt_builder.py:413`）。
- MEMORY.md / USER.md 继续承载 per-account Dreaming 结果；八字托管段和专属 CRUD 已移除，出生信息按普通记忆处理。M2-C 只把**新产生且成功应用**的四类用户事实经 typed sink 追加到 universe L3，不回写或批量搬迁历史文件。

---

## 6. L3 落地：接缝与并发（已核对代码）

### 6.1 注入接缝（最小、零核心改动）
经 `prompt_builder` 的 `extra_blocks` 加性钩子（`prompt_builder.py:477-480`，`tdai_recall` 已在用 `turn_service.py:1392-1426`）：turn 组装前构造 `ContextBlock(name="universe_l3", ...)` 追加。L3 数据源另起模块，类比 `agent_self_state.py`“只渲染、不算不写”，返回文本、由 turn_service 包成 block。**无需改 `prompt_builder.py` 或任何现有 block**（`assemble` 声明式，`token_budget=None` 默认零行为变更 `:113-136`）。

### 6.2 存储接缝（不要复用 `account_profile_files`）
`account_profile_files` 主键 `(account_id, filename)`、account 隔离硬编码 → universe 级共享**不进此表**（否则每 account 复制一份）。实际落点为 `universe_memory_facts` + `app/db/companion_world.py` 原语、`app/platform/companion_world_memory.py` 写/compact adapter，以及 `app/platform/companion_world_turn.py::read_companion_world_context()` 组合 DB 读取与域层纯渲染。

### 6.3 写入路由（别照搬 dreaming 的读-改-写）
- **原始沉淀**：复用 `append_file` 的 SQL 侧原子追加（`profile_storage.py:58-84`，PG 多节点并发不丢）→ universe 级 raw 追加安全。
- **压缩维护**：新挂一条 dreaming 类比链路，但**不能照搬 `_apply_memory_item`（`dreaming.py:530`）**——它是“读(一事务)→Python 改→写整文件(另一事务)”，读写间无行锁/无 advisory/`version` 列存而未用作 CAS；现状不炸仅因单 account 天级串行。

### 6.4 并发保护（L3 多 resident 并发写的硬要求）——**已冻结 = append-only typed fact/event + 单 writer compact（2026-07-19）**
同一 universe 多 resident 并发写 L3 会 last-writer-wins 覆盖整文件。**决策**：L3 落**追加型 typed fact/event 存储**——各 resident 只 append 带 provenance（来源 resident/turn、时间、fact_type）的**结构化事实行**，永不就地改写整文件；**压缩/去重由单 writer 异步 compact**（挂 §6.6 DreamingScheduler 单例）合并成稳定视图。如此既保留 provenance、天然规避整文件覆盖，又与 §6.3「原始沉淀走 `append_file` SQL 侧原子追加、压缩另起单 writer」一致。**弃用**：全程 `FOR UPDATE`/advisory 串行写（吞吐差、仍是整文件模型）、`version` 乐观 CAS（多 writer 高冲突下退化为忙等）——二者仅作 compact writer 内部实现细节可选，不作为写入路径主模型。并发正确性以 PG 为证（D-12）：同一 universe 两 resident 并发 append 不互相覆盖、compact 幂等。

**当前语义边界与后续债（2026-07-23 review）**：已实现的 `compact_universe_facts` 只按 `fact_type + canonical payload JSON` 折叠 exact-normalized duplicate；它不判断语义改写、事实冲突或陈旧事实，也不会自动 supersede 这些行。上文“稳定视图”目前只表示并发不丢写、重复 compact 幂等且字面重复可确定性折叠，不表示已经形成“当前事实真相”。这不阻断首轮 default-off/小流量发布，但在 L3 量级或 prompt 注入占比持续增长前，须独立设计 supersession/retention 策略、active-fact/token 上限和增长观测，避免长期单调膨胀。

### 6.5 最低成本切口与顺序
- ~~**八字托管段**已结构化/工具管理/有独立 read/write/preserve 通道 → 改指 universe 存储，是 **L3 首刀**~~ **（作废 2026-07-21：八字托管段整体移除、八字降级为无工具 skill、出生信息走普通记忆；L3 首刀改由 `user_identity` / `user_preference` / `user_profile_derived` / `user_event` 四类事实承载，见 §12 落地说明）**。
- USER.md 用户画像拆分成本更高（与 dreaming 整文件读写耦合 `dreaming.py:373`）→ 二期。分流点即现有 `ALLOWED_TARGET_FILES` 的 USER.md/MEMORY.md 选择 + 八字段。

### 6.6 调度挂载
L3 天级压缩挂 `scripts/run_proactive_scheduler.py` 单例 DreamingScheduler 旁，沿用“中心扫全量、节点不重复”单 writer 纪律。**§10.6 已冻结（2026-07-21）= App scope 纳入定时扫描**：把 `__app_active__` 登记进 `DEFAULT_ACTIVE_SESSION_KEYS`，每位 resident 的 L1/L2 Dreaming 仍按 runtime account 独立；universe L3 compact 按 `universe_id` 单 writer 执行，不因居民数重复 compact。保留 batch/幂等/监控，不产生主动消息或用户可见通知。否则 App session 永不定时轮转，Dreaming 会被懒轮转压到每位居民次日首条请求延迟上。

---

## 7. 形态 B 的架构应对 / 形态 C 接缝手册

### 7.1 形态 B 目前架构如何应对
1. **运行时保持 1:1 不变**：每 resident = 一个 runtime account，L1/L2 继续按 `account_id` 隔离，turn/moderation/session 零改动。
2. **新增 universe 级 L3 存储**：键 `universe_id`，与任一 runtime account 解耦（§6.2）。
3. **prompt 加一个加性注入块**：由产品域层读 L3 后经 `extra_blocks` 注入，Runtime 不认识 universe（§6.1）。
4. **memory_writer/dreaming 输出按层路由**：用户事实→L3 共享，关系专属→per-account L2（§6.3/§6.5）。
5. **L3 压缩/冲突独立**：universe 级压缩 + 并发保护（§6.4）。
6. **隐私边界**：共享用户事实是形态 B 预期行为；须守住 (a) 关系原文/L2 不串到别的 Agent；(b) L3 永不跨 universe（对访客、他人世界零泄漏，复用 visit ACL）。

### 7.2 形态 C 接缝手册（把 D-06 细化为可复用范式）
三个稳定接缝：**注入端口**（Runtime 只读 per-account L1+L2 + 接受外部 shared-context 注入）；**范围锚点**（共享/隔离边界由域层锚点决定，B=`universe_id`，C 可用 `org_id`/`room_id`/`family_id`，各建自己的 `*_storage`/`*_context`，不动 Runtime）；**加性迁移**（新表加性、新 API 走 `/api/v1` 新路径、API/auth、L3 后台与 proactive safety 使用正交开关，关入口 flag 可退回上一 API/auth 形态）。

新形态四问决策清单：① 绑定关系（一人↔几 Agent、锚点是什么）；② 共享范围（L3 共享到哪个锚、L1/L2 是否仍独立，默认独立）；③ 内容/社交面（世界动态/生命周期/访客/真人聊天各走独立持久化+ACL，不进 Runtime、不进 AI `messages`）；④ 计费/容量/配额（锚 `platform_user` 钱包、容量真相放域层 status 表、不借建号 binding 计数）。

反模式（明确禁止）：把形态假设写进 `turn_service`/`prompt_builder`/`account_profile_files`（违反 D-01/D-06，AST 边界测试拦截结构性依赖，语义 review 仍必需）；给 Runtime 表加 form-specific 列（`character_id`/`world_id`，应用域层关系表 `universe_residents.runtime_account_id` 映射）；复用 referral/campaign 旧 code 通道承载新形态邀请/共享语义。

---

### 7.3 Runtime↔World 端口契约（方向与事务边界冻结 2026-07-19；方法签名初稿 2026-07-19，M2-0 定稿）

> Codex review：ADR 只列了 `AgentRuntimePort` 方法名，未冻结**接缝契约**与**事务边界**；且 §11.8 T3-2 让 Runtime 出口直写带 `universe_id`/`resident_id` 的通知表，**依赖方向反了**。M2 编码前须先把下述四接缝冻结（任务 M2-0），不能只列方法名。核对到的现状缺口：`ChannelTurnInput` 无 shared-context 入参（`turn_service.py:2104`）；`extra_blocks` 只在 turn 内由 TDAI 生成（`:1392`）；after-turn memory hook 只有 `account_id`、无法 form-agnostic 路由到 universe（`:1584`）。

**四个稳定接缝（方向恒为 `域层 → Runtime`，Runtime 不反向依赖 World）**

1. **turn 外部 context 注入（入向）**：`send_turn(...)` 增可选 `extra_context: list[ContextBlock]`；`ChannelTurnInput` 加 `extra_blocks` 字段，经已有 `prompt_builder.extra_blocks` 加性钩子注入（§6.1）。域层读 L3 后构造 block 传入；Runtime 只消费、不认识 `universe_id`。TDAI 现有 `extra_blocks` 路径不变、与此并存。
2. **after-turn typed memory sink（出向，typed）**：after-turn 钩子从「只给 `account_id`」升级为发出 **typed memory event**（`fact_type` + payload + provenance）。**路由决策（per-account L2 vs universe L3）在域层做**。composition 注册 sink：未映射 world 的 form-A account 安全 no-op；legacy backfill 后的 form-A runtime 会向其 world追加 L3，但 legacy turn 不注入 L3。该行为不受 API/auth flag 控制，由独立 `COMPANION_WORLD_L3_BACKGROUND_ENABLED` 控制。
3. **proactive delivery port（出向，修依赖反转）**：Runtime/proactive 核心只发 **typed proactive intent**；**投递 adapter**（`WeixinAdapter=send_weixin_text` / `AppInboxAdapter=写 app_notifications`）在域层/平台层解析目标并落库。**Runtime 侧不再直写 `app_notifications`**（撤销 T3-2 的反向依赖），`app_notifications` 的 `universe_id`/`resident_id` 由 AppInboxAdapter 填。跨边界持久性用 outbox（§8 R3）。
4. **事务边界（防孤儿账号）——冻结 = 共享 UoW 单事务优先**：
   - `create_runtime + resident`：runtime account 插入与 `universe_residents` 插入**同一 PG 事务提交**（厚节点同库，无跨库障碍）。**禁止**各 DB 方法自开事务导致「建号成功、resident 插入失败留孤儿账号」。
   - `offline + read-only + farewell`：单原子事务（沿用 §8 R4 / D-13），departure event 唯一约束保证恰好一条 farewell。
   - 跨异步/跨节点边界（如 Feed 生成、通知投递）不能塞进同一事务的，走**事务 outbox + 幂等消费 + 补偿**（Saga），领域状态与 outbox 同事务写。
   - money 路径留平台层（M1 已上迁 `platform_user` 钱包），**不进 Runtime 事务**，经幂等键（`idempotency_key`）跨事务对齐。

**方法签名初稿（已核对现有入口类型，M2-0 定稿；`*` 号为加性字段/新类型，现有签名保持不变）**

四接缝均复用/包住现有真实入口，无一处推翻现签名：接缝①复用 `ContextBlock`（`prompt_builder.py:74`）、只给 `ChannelTurnInput`（`turn_service.py:2105`）加一个默认空字段；②③④为新 typed port，Runtime 侧只发不解析。

```python
# ── 接缝① turn 外部 context 注入（入向）───────────────────────────────
# 复用现有 ContextBlock（prompt_builder.py:74），不新增类型：
#   ContextBlock(name, text, section='volatile', char_limit=None, trim_priority=50)
@dataclass
class ChannelTurnInput:                 # turn_service.py:2105，仅加一字段（加性，默认空 → 形态 A 零行为变更）
    ...
    extra_blocks: list[ContextBlock] = field(default_factory=list)   # * 域层注入（L3 等）
# 组装处把 ctx.extra_blocks 与现有 _tdai_extra_blocks（turn_service.py:1392）并入同一
#   assemble(extra_blocks=[*ctx.extra_blocks, *_tdai_extra_blocks])   （prompt_builder.py:477-480 钩子）

class AgentRuntimePort(Protocol):       # * 域层唯一入口，方向恒 域层→Runtime
    def send_turn(self, ctx: ChannelTurnInput) -> OpenClawTurnResponse: ...
    #   L3 经 ctx.extra_blocks 携带；Runtime 只消费、不认识 universe_id。
    # P1 live 端口实际只用 send_turn；conversation owner 解析在 World repository，runtime 建号在
    #   platform repository 的共享 UoW 内完成；set_read_only 随 M4 落定。
    #   configure_persona / get_runtime_summary 无调用方 → 暂不落死（D-02）。

# ── 接缝② after-turn typed memory sink（出向，typed；raw write_memory 不变）──
@dataclass(frozen=True)
class MemoryProvenance:                  # *
    source_account_id: str              # 事件来源 runtime account（provenance，永不丢）
    turn_message_id: str | None
    session_id: int | None
    business_day: str
    occurred_at: str                    # ISO8601，由调用侧注入
@dataclass(frozen=True)
class MemoryEvent:                       # *
    fact_type: str                      # M2-0 定稿：四类 user_* → L3；relationship/commitment → L2
    payload: dict                       # 结构化事实，非逐字原文（原文仍 per-account，D-05）
    provenance: MemoryProvenance
class MemorySink(Protocol):              # * 域层实现；Runtime hook 只 emit
    def emit(self, event: MemoryEvent) -> None: ...     # 路由 L2/L3 在域层，Runtime 不认识 universe
# 落点：新增一条 _AFTER_TURN_HOOKS（turn_service.py:1609 旁）读 _AfterTurnContext 发 MemoryEvent；
#   未映射 world 的 form-A account 由 sink writer 安全 no-op；legacy backfill 后会追加 L3，但 legacy prompt 不注入。
# 域层 CompanionWorldMemorySink：user_identity/preference/profile_derived/event→L3 append；relationship/commitment 留 L2。

# ── 接缝③ proactive delivery port（出向，修依赖反转）─────────────────
@dataclass(frozen=True)
class ProactiveIntent:                   # * Runtime/proactive 核心只产出「谁要主动说什么」，不解析投递介质
    platform_user_id: str               # 真人级键（人级去重/预算锚，与 D-09 同源）
    speaker_account_id: str             # 发声 resident 的 runtime account
    category: str                       # USER_REMINDER / NEW_USER_REACTIVATION / ...
    text: str
    idempotency_key: str
    universe_id: str | None = None      # 形态 B 有；形态 A（微信）None
    resident_id: str | None = None
    product_category: str | None = None
    metadata: dict = field(default_factory=dict)
@dataclass(frozen=True)
class DeliveryResult:                    # *
    status: str                         # sent | enqueued | inbox | skipped
    channel: str                        # weixin | app_inbox
    ref_id: str | None                  # outbound_message.id 或 app_notification.id
class ProactiveDeliveryAdapter(Protocol):    # *
    def deliver(self, intent: ProactiveIntent) -> DeliveryResult: ...
# WeixinAdapter.deliver → 现 dispatch_proactive_text 微信分支 / send_weixin_text（outbound.py:288/330，零回归）
# AppInboxAdapter.deliver → 写 app_notifications（T3-1），填 universe_id/resident_id；Runtime 不直写该表
# _select_route（common.py:40）按目标真人可达渠道选 adapter；App 目标不再回 channel_not_proactive。

# ── 接缝④ 事务边界（防孤儿账号；冻结 = 共享 UoW 单事务优先）───────────
class UnitOfWork(Protocol):              # * 单 PG 事务边界，同一 UoW 内多次 repo 写同事务提交/回滚
    conn: Any                           # 同一连接/游标，注入各 repo
    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, *exc) -> None: ...
def create_resident_with_runtime(        # *
    *, universe_id: str, platform_user_id: str,
    character_template_id: str, uow: UnitOfWork,     # runtime account 插入 + universe_residents 插入同事务
) -> ResidentHandle: ...
# offline+read-only+farewell 同一 UoW（§8 R4，departure event 唯一约束保证恰好一条 farewell）；
# 跨异步/跨节点边界走事务 outbox（领域状态与 outbox 同事务写，worker 幂等消费 + Saga 补偿）；
# money 路径留平台层（M1 已上迁 platform_user 钱包），经 idempotency_key 跨事务对齐，不进 Runtime 事务。
```

签名边界说明：以上冻结的是**接缝形状**（字段/类型/方向/事务归属），`fact_type` 枚举全集、错误码、索引/唯一约束、backfill 算法以 M2-0 正式后端规范为准。P1 实现没有为尚无调用方的端口补空壳方法；`UnitOfWork` / proactive delivery DTO 当前主要是后续边界约束。实际 live 调用以顶部代码地图和 §12 M2-C 落地说明为准。

## 8. 渐进式重构顺序

- **R0 冻结边界与保护现状**：微信/App 单 Agent/Memory/计费补 characterization test（只能钉确定性接缝：prompt 组装/session 轮转/持久化/moderation，钉不住模型输出）；写本 ADR；冻结 resident 计费口径；冻结 App auth 不接收旧 code；接线 stdlib AST 分层门禁。防 N× 安全阀因必须读取 World 映射，实际下沉到 M2-C C5（见 §11.8）。
- **R1 计费上迁（先行、独立发布）+ Agent Runtime facade**：
  - **R1a 计费/配额锚点上迁（从原 R2「一并解耦」抽出，独立先行）**：把钱包唯一键上迁 `platform_user`（D-14）、daily 原子预占 + RPM 锁键迁 `platform_user`（D-09）、容量脱建号计数（D-07）。**不依赖任何 universe 表**，可在领域层之前单独上线并发布。**注（口径修正 2026-07-19）**：建号允许每真人 ≤10 account，故存量可能已有多钱包老用户，**非无条件零合并**——须先跑生产预检、对多钱包用户自动合并（D-14 迁移方案），单钱包用户直迁。PG 并发为发布闸（§9）。仍必须先行：多居民上线后变 N 世界 × N 居民钱包，合并成本远高于现在。
  - **R1b Agent Runtime facade**：**仅新建** `AgentRuntimePort` + adapter 供**新代码**调用；**不**强推 legacy 微信/App 路径改走 facade（`turn_service` 最有状态，待第二个真实消费者再回收）。P1 live 端口只复用 `send_turn`；conversation owner 解析由 World repository 完成，no-grant runtime 建号由 platform repository 在共享 UoW 内完成。
- **R2 P1 Companion World**：universe/template/resident/ai_conversation；固定 4 位版本快照的幂等 bootstrap + 1–10 居民确认；resident runtime 创建不重复赠权（**站在 R1a 已上迁的计费基座上，仅把容量真相落到 `universe_residents.status` 表**）；显式 AI conversation history/turn（不接受客户端 `account_id`）；老用户全部 active binding backfill（不自动补居民，D-08/§10.2）；L3 首刀（通用 user typed fact + App Dreaming scope，§6.5/.6）。
- **R3 Feed 与异步事件**：`universe_posts`、world-content scheduler/outbox、user/AI 文字动态与审核。事件用事务 outbox（领域状态与 outbox 同事务提交、worker 幂等消费）。**App 通知收件箱 + 真人级 proactive 上提域层（任务 T3-1～T3-8，见 §11.8）。**
- **R4 Lifecycle 与 Mailbox**：cooldown/audit/safety freeze、last-resident protection、offline+farewell+read-only 原子事务、letters 与接受事务。
- **R5 Visit 与 Human Chat**：三 slot、高熵 code、绝对过期与 ACL；访客只读 Feed；独立 human messages、到期只读、举报/拉黑。

不并行上线 R2–R5，每阶段应能在没有下一阶段时形成完整、可回滚体验。

---

## 9. 不变量与测试门禁

**隔离（沿用现有核心不变量：任何未按 `account_id`/锚点约束的读写都是 bug）**
- L1/L2 隔离：resident A 的人设/使命/关系记忆不进入 resident B。
- L3 写入只允许来自本 universe 内 resident 的受管路由；跨 universe 写入拒绝。
- L3 共享：A 世界新学到的用户事实，同世界另一 resident 可见、他人世界不可见。
- 访客（visit）读取路径永不触达 L3/L2/任何 runtime Memory。
- human message 永不进入 AI message/prompt/dreaming/memory。

**容量与生命周期**：初始集合不为 0、active ≤ 10；来信投递校验 active `<8`、接受校验 active `<10`；普通条件不能移除最后一位；一次 departure 恰好一条 farewell（departure event 唯一约束）。

**并发（以 PG 为证，SQLite 只验功能，D-12）**：并发确认居民、第 10/11 位竞争、两次接受同一封信、两人兑换同一 code、第 3/4 slot 竞争、撤销与 Feed/发送并发、offline 与新 turn 并发、**同一 universe 两 resident 并发写 L3 不相互覆盖**。

**契约**：App auth 不接受世界码/referral/campaign；新接口不接受任意 `account_id` 选居民；错误码稳定 + request_id/server_time。

**双后端与迁移**：SQLite 聚焦 + PG 真实事务均过；backfill 可重复运行、不重复建 world/resident/grant；关 P1 flag 可退回 legacy API/auth 入口但不删除 world 数据，L3 后台与 proactive safety 分别由独立开关控制。

**PG 保真门禁（硬，2026-07-19；2026-08-10 收敛为唯一主测试后端）**：凡涉及**锁 / 事务 / 钱包扣款 / 容量 / 配额预占**的 3.0 新逻辑，必须由 PostgreSQL 测试证明，因为 SQLite 无法复现 `FOR UPDATE`、advisory lock 和真实事务隔离语义（D-12）。当前 `make test` 与 CI 都固定使用 pytest-postgresql，并以预迁移模板加速；不再维护重复 SQLite 全量档。

---

## 10. 产品冻结项

1. **【已冻结 2026-07-21 = 4 位 + 版本快照】**：新用户 bootstrap 固定返回 4 位运营预设候选，可删减至至少 1 位、无主角色；已发布模板版本不可原地改写，更新须发新版本。bootstrap 将 `template_id + persona_version` 钉在 candidate/resident，运营换版只影响后续新世界，不漂移已选择/已确认关系。
2. **【已冻结 2026-07-21 = 全部 legacy 映射、不自动补居民】**：全部 active binding 映射 legacy resident；最早一条仅作兼容锚；不重放 onboarding、不自动补 4 位预设。无 active binding 走新用户流程；满 10 全保留并禁新增（D-08）。
3. **【已冻结 2026-07-23 = 不允许】**：首次确认后用户不能主动移除 active AI；只允许确认前 `candidate→dismissed`。后续 `active→offline` 只由内部 lifecycle + full admin 审批提交，App 不提供 departure/remove API。
4. **【已冻结 = 保留豁免】** legacy resident 豁免离开（D-08）；客户端 PRD:634 仍写“来源不构成豁免”，必须在生产开 flag 前修订。
5. **【已冻结 = platform_user】daily/RPM 配额按真人聚合，多居民共享一套配额（D-09）**；daily 原子预占+回滚、RPM 锁键/计数键、user-level override 来源与 reservation central TTL 回收均已完成（m0031）。
6. **【已冻结 2026-07-21 = 纳入】**：`__app_active__` 加入 `DEFAULT_ACTIVE_SESSION_KEYS`；L1/L2 per-runtime Dreaming，L3 per-universe 单 writer compact（§6.6）。
7. **【已冻结 2026-07-22 = 文字、universe 级每日最多 2 条】**：上午/傍晚各最多 1 条；仅已确认、至少一位 active resident、真人最近 7 天有任一渠道入站的世界生成；允许跳过、不补发。`character_letters`/mailbox 策略归 M4，不再混入 M3 门槛（D-15）。
8. **【已冻结 2026-07-23 = 保守 shadow + 人工提交】**：inactivity=连续 60 天无 owner→resident 入站；value mismatch=滚动 30 天内至少 3 次独立证据且跨度至少 14 天；cooldown=7 天；最近危机/自伤/高度脆弱信号后 freeze 30 天。自动流程只生成候选，所有不可逆 offline 首版均需 full admin 批准；普通最后居民永久阻断，severe-abuse 例外仍需显式批准。提交前可取消/驳回；提交后禁止 `offline→active`，纠错只追加审计并可隐藏 farewell。Mailbox 同批冻结：active `<8` 投递、`<10` 接受、每世界 open=1、30 天投递间隔/TTL、defer 不续期、同角色不重投、仅运营版本化目录。详见 M4 spec。
9. **【已冻结 2026-07-23】** B 跨好友世界 `pending + active` visit 合计最多 3 个，可主动取消 pending；邀请码兑换只创建 pending，A 二次接受后才建立 ACL；visit 终止后真人聊天立即只读且默认保留，手动删除仅 self-hide，举报证据独立按合规期限保留。派生参数：invite 24h、pending 7d、accept 后 visit 30d，均绝对到期不续期；任一方 block 立即终止访问与 chat 写权限。详见 M5 spec。
10. **【已冻结】L3 共享范围 = 关于用户的沉淀记忆全量共享、无字段级白名单；但不共享聊天原文与检索（D-05，边界精化 2026-07-19）**。共享=对用户的沉淀认知（USER/MEMORY 事实段、派生画像）；不共享=各居民私聊逐字稿（`messages`）与 `tool_evidence_replay` 检索。产品意象：居民“八卦”对用户的认知，读不到彼此私聊。用户对 L3 的可见/编辑/清空为产品交互层、另做。
11. **【已冻结 2026-07-22 = Feed/通知分离 + 显式已读 + 分层保留】**：App 主动消息投递面仍为拉取式通知/收件箱（D-13）；Feed 与通知入口、红点独立，列表拉取不自动已读，提供单条/全部已读。已读保留 7 天、未读保留 30 天，每真人硬上限 200；写入事务按“最旧已读优先、再最旧未读”维持上限，central scheduler 清到期行并对账（D-15、§11.3）。
12. **【已冻结 2026-07-22 = 最近用户入站 resident】**：App 真人级 proactive 由最近收到用户入站的 active resident 发声；无入站历史按最近会话活动、`joined_at DESC`、`resident_id ASC` 回退，投递前失活则重选；首版不支持用户指定。微信固定使用可真实投递的 legacy primary，不冒充其他居民（D-15、§11.4）。
13. **【已冻结 2026-07-22 = App-only 首版启用、独立 flag 默认关闭灰度】**：只进拉取式收件箱、不做系统 Push；遵守用户开关、跨渠道活跃、quiet hours、策略预算与内容安全，真人级合计每真人滚动 24 小时最多 1 条。flag 关闭时 fail-closed，不影响 per-resident reminder/commitment（D-15、§11.6）。

---

## 11. 主动消息（proactive）子系统改造

> 历史 review 结论：原 proactive 全层按 `account_id` 键，一个真人 N 居民会形成 N 条独立管线。M2-C C5 先以 world-aware 安全阀限制真人级触达；M3-4/M3-5 已进一步交付 App 收件箱、真人级预算/活跃聚合、按真人折叠 due 扫描及确定性发声人。底层候选状态仍兼容 account 存储，但 scheduler facade 与投递策略已按 owner 聚合；per-resident 义务继续保持原键。

### 11.1 两个正交轴（改造总纲）

主动消息沿**两个正交轴**拆，不可混谈：
- **轴一 归层**（决定调度/预算/扫描单位，防 N× 打扰）：per-resident 义务 vs 真人级触达。
- **轴二 投递面**（决定可达性）：微信 push（legacy 居民）vs App 拉取式收件箱（App 居民）。

二者正交：App 用户的 reminder = per-resident(轴一) + 收件箱(轴二)；微信 legacy 的拉活 = 真人级(轴一) + 微信(轴二)。

### 11.2 归层：六类映射（轴一）

| 类别 | 触发本质 | 归层 | 多居民下的问题 | 改造 |
|---|---|---|---|---|
| `USER_REMINDER` | 用户显式设定（对某居民） | **L2 关系专属**，留 per-account | 用户主动设的，多居民各有天经地义 | 键不变（投递面另解） |
| `COMPANION_FOLLOWUP`/commitment | 本居民某轮许下的承诺 | **L2 关系专属**，留 per-account | 无 | 键不变 |
| `COMPANION_FOLLOWUP`/account_check | 系统判断该主人该跟进 | **真人级**（混在同一 category） | 每居民各触发 | 上提，按 universe 聚合 |
| `NEW_USER_REACTIVATION` | 系统判断真人沉默 | **真人级** | N 倍拉活 | 上提，每人每窗口 ≤1 |
| `CONTENT_INVITATION` | 系统判断真人可能想看某内容 | **真人级** | N 倍邀请 | 上提，按 universe 去重 |
| `CONTENT_INVITATION_RESPONSE`/`TASK_RESULT` | 对已有交互的回执 | **L2**（跟随原对话居民） | 无 | 键不变 |

原则同构：主动消息按 L2/真人级切开，正对应 context 的 L2/L3 分裂——per-resident 义务是"这个 Agent 欠/在延续某事"（留 Runtime）；真人级触达是"这个**真人**沉默了/可能想看某内容"（进产品域层）。

### 11.3 投递面 = App 拉取式通知/收件箱（已冻结 = A，轴二）

- **决策**：App 侧主动消息落一个**持久化的拉取式通知/收件箱**，客户端主动拉取；与世界 Feed（D-10）**分离入口**——Feed = 世界里发生了什么，通知 = 某个 AI 主动找你。不依赖 push、不误开 `supports_proactive`（`channels.py:98` 保持 False）。微信 legacy 居民继续走 `send_weixin_text`（`outbound.py:288`）原路。
- **落点**：唯一出口 `dispatch_proactive_text`（`outbound.py:362`）新增一条投递分支——解析到 App 目标时写一行通知记录（新表，人级键 `platform_user_id` + 发声 `resident_id` + `category` + `status(unread/read)` + payload + `idempotency_key` + `created_at/read_at`，过期按 D-15 的状态/时间规则计算），而非返回 `channel_not_proactive` 取消；`_select_route`（`common.py:40`）相应放行 App 目标到该分支。
- **客户端**：新增 `GET /api/v1/notifications`（all/unread 状态过滤 + 游标分页，响应带独立 `unread_count`）、单条已读与全部已读；拉取不隐式已读，与 `/worlds/home/feed` 分离。
- **保留/红点**：Feed 与通知分别计算红点；通知只统计未过期 `unread`。已读保留 7 天、未读保留 30 天，每真人最多 200 条；写入事务内按最旧已读→最旧未读维持硬上限，central scheduler 单写清理到期行并对账。清理失败隔离并进入 scheduler heartbeat，不阻塞其他阶段。
- **覆盖范围**：收件箱同时承接 App 用户的 per-resident（reminder/commitment，轴一 L2）与真人级（拉活/邀请）消息——投递面与归层正交，两类都可能落 App 用户、都进收件箱。

### 11.4 真人级触达上提产品域层：三接缝 + 发声人

真人级类别从 Runtime 侧上提到 `companion_world` 域层：
1. **预算聚合**：`evaluate_outbound_policy`（`policy.py:145`）的计数（`get_outbound_daily_usage` `:270`、`count_total_...` `:309`、avoidance `:360-390`）对真人级类别跨 `account_owner_bindings` 扇出到 `platform_user`——**与 D-09 同源、同批解**。per-resident 类别保持 per-account。
2. **扫描单位**：真人级 due 队列（`list_due_reactivation_candidate_accounts` `proactive.py:2213`、`list_due_proactive_account_states` `:2183`、候选存 `proactive_account_state.metadata_json`）改按 universe 聚合，每人每窗口 ≤1。per-resident due 队列（`reminders`/`proactive_commitments`）不动。
3. **发声人选择（新职责，已冻结）**：App 优先选择最近收到用户入站消息的 active resident；无入站历史时按最近会话活动、`joined_at DESC`、`resident_id ASC` 稳定回退，投递前失活须重选。首版不支持用户指定。微信受真实 route 约束，只能选择 `legacy_primary_account_id` 对应 resident，不借其通道冒充其他居民（§10.12/D-15）。

per-resident 义务（reminder/commitment）仍留在现有 Runtime 侧管线，不上提。

### 11.5 活跃判断跨渠道聚合（M3-5 已修复）

旧路径只看 `get_account_last_inbound_at(channel=WEIXIN)` 与微信 touch-state，导致 App 活跃用户可能被误判沉默。M3-5 已改为跨 owner active bindings 与全部 resident runtime accounts 聚合真人入站；planning、候选后置取消与 avoidance/budget 均使用 owner 范围，App inbound 不再遗漏。per-resident reminder/commitment 仍只读所属 runtime account，避免扩大 L2 义务范围。

### 11.6 分期与防 N× 安全阀

- **M2-C C5（已完成）安全阀**：真正聚合前先加 world-aware 人级闸——form-A account 不受影响；world resident 仅 `legacy_primary_account_id` 放行，非 primary 与 App-only 均 fail-closed。生成侧先拦以节省 LLM，投递侧再拦作防御纵深；per-resident reminder/commitment 不经过此闸。
- **M3-5（已完成 2026-07-22）**：真人级 proactive 已上提纯领域 scope/route/speaker 决策；due 扫描按 platform user 折叠，活跃/预算跨 owner bindings + 全 resident runtime accounts 聚合。真实微信 legacy primary 优先；无真实微信路由时仅双 flag 开启可抢 App reservation，成功 visible 才消耗滚动 24 小时额度。投递事务锁 owner、重选并锁 resident；现有候选默认强绑定，失活 cancel，显式通用内容才允许重选。M3-4 的 per-resident reminder/commitment 保持独立。

### 11.7 新增不变量与测试点

- 一个真人 N 居民：真人级类别（拉活/邀请）每人每窗口 **≤1 次**，非 N 次（PG 并发下亦然）。
- per-resident（reminder/commitment）保持每居民独立，不被人级闸误杀。
- App 目标不再命中 `channel_not_proactive` 取消，而是落通知收件箱一行；微信 legacy 仍走 `send_weixin_text`。
- App 活跃用户不被判为沉默（跨渠道 last-active）。
- 通知收件箱与世界 Feed 数据面分离：通知不进 Feed、Feed 不进通知。
- App-only 真人级 flag 关闭时零真人级通知；开启时同一真人滚动 24 小时合计 ≤1，per-resident reminder/commitment 不计入该真人级上限。
- 通知拉取不改变状态；单条/全部已读只影响当前 `platform_user`。清理严格按 7/30 天和 200 条上限执行，不跨真人删除。

### 11.8 M2 安全阀 / M3 具体任务项

> 规范：schema 改动**新增迁移函数追加 `_MIGRATIONS`**（勿用启动期 `_ensure_column`，见 `_core.py`）；新 router 需在 `tests/conftest.py` 的 `fresh_db`/`client` 两 fixture 各补一行 `patch("app.routers.<模块>.settings", ...)`；并发正确性以 PG 用例为证、SQLite 只验功能（D-12）。真人级类别 = `NEW_USER_REACTIVATION` + `CONTENT_INVITATION` + `COMPANION_FOLLOWUP/account_check`；per-resident 类别 = `USER_REMINDER` + `COMPANION_FOLLOWUP/commitment` + `CONTENT_INVITATION_RESPONSE` + `TASK_RESULT`。

**M2-C C5 — 防 N× 安全阀（已完成；不动 per-resident 类别）**

- [x] **T0-1 world-aware 判定 helper**：`app.platform.companion_world_repository.human_level_proactive_allowed(account_id)`；form-A 无 resident 映射时恒 True，world resident 仅 `legacy_primary_account_id` 相等时 True。
- [x] **T0-2 人级闸（生成侧，省 LLM）**：reactivation/content invitation 统一规划入口与 account-check 决策在生成前过滤。
- [x] **T0-3 人级闸（投递侧，防御纵深）**：reactivation dispatch 与 account-check execute 再校验；被拦截项 skip/clear，不计作成功投递。
- [x] **T0-4 App-only fail-closed**：App-only world 没有 legacy primary，真人级生成和投递均被拦截。
- [x] **T0-5 characterization + 多居民回归测试**：`tests/test_proactive_companion_world_gate.py` 覆盖 form-A 不变、仅 legacy primary 放行、App-only 拦截，以及 reminder/commitment 不误杀。

**R3-A — 文字 Feed + 事务 outbox**

- [x] **T3-F1 schema + 存储原语（M3-1）**：m0033 已新增 `universe_posts` 与 World 专用事务 outbox；状态/outbox 原语同事务，Feed 与 `app_notifications` 分表。用户 API/完整状态机继续由 M3-2 接线。
- [x] **T3-F2 owner API（M3-2）**：`GET /api/v1/worlds/home/feed` opaque cursor 分页与 POST 用户文字直接发布已交付；session user 服务端解析 home universe，客户端不能提交 owner/account/universe，响应不泄漏 runtime 字段。Feed 独立 flag 默认关闭。
- [x] **T3-F3 AI generation scheduler（M3-3）**：独立中心进程按 `universe` + 北京自然日/窗口原子 claim，上午/傍晚各最多 1 条、全天最多 2 条；只扫描 confirmed、存在 active resident、真人最近 7 天任一渠道有入站的世界。跨 batch 游标避免后页饥饿并按 slot 重置；失败可跳过，错过不补发。
- [x] **T3-F4 文字与作者选择（M3-3）**：首版生成器只接 resident 名/日期/slot，不读取私聊、L3 或 runtime account 内容；作者绑定本 universe active resident，发布前失活则 skip、不换署名；Feed 审核继续按 D-15 延后单独设计。
- [x] **T3-F5 双后端/PG 门禁（M3-3）**：覆盖同世界同窗口及 stale reclaim 单 winner、outbox worker claim 不重叠/旧 token CAS、用户/AI 发帖 owner 隔离、7 日活跃、两窗口与不补发；SQLite 全量 `1454/13`、PG 全量 `1462/5`。

**R3-B — App 拉取式通知收件箱 + 真人级触达上提域层**

- [x] **T3-1 新表 `app_notifications`（M3-1）**：m0033 已按最终 M3 spec 落 `platform_user_id/universe_id/resident_id`、visible/reserved/cancelled、显式 read/expiry、每真人幂等键与清理索引；不存 `runtime_account_id`，与 Feed 分表。
- [x] **T3-2 投递分支（M3-4）**：per-resident reminder/commitment 的 App 目标发 typed intent 给 `AppInboxAdapter`，由 adapter 解析 `platform_user_id/universe_id/resident_id` 并写通知；proactive 核心不直接写表。真实微信路由保持优先，`CHANNEL_APP.supports_proactive=false` 未修改；真人级 App source 继续 fail-closed 等待 M3-5。
- [x] **T3-3 DB helpers（M3-4）**：`app/products/mingchan/infrastructure/persistence/notifications.py` 已交付 visible 幂等写、all/unread tuple cursor、未读数、单条/read-all、7/30 天与 200 条 cleanup；所有 helper 强制 `platform_user_id` 锚，淘汰顺序为最旧 read → 最旧 unread，并保护当前事务新插入行。
- [x] **T3-4 API router（M3-4）**：`GET /api/v1/notifications`、独立 unread-count、单条 read 与 read-all 已交付；session 解析 owner、拒绝 account/runtime/universe 注入、与 Feed 分面、`Cache-Control: no-store` 和稳定信封均有测试。
- [x] **T3-5 真人级上提 `companion_world` 域层（M3-5）**：领域 scope/route 决策已落地；scheduler facade 将 account-state 扫描按 `platform_user` 折叠，预算、avoidance 与入站取消跨 owner 全账号聚合。App-only 真人级以 hidden reservation 保证滚动 24 小时合计上限 1，per-resident 义务不计入。
- [x] **T3-6 发声人选择 helper（M3-5）**：App 按最近真人入站、最近 App conversation activity、`joined_at DESC NULLS LAST`、`resident_id ASC` 确定；投递事务锁 owner 后重选并锁 resident。微信固定真实 legacy primary route，不允许借通道冒充；强绑定内容失活 cancel，显式通用内容才允许重选。
- [x] **T3-7 跨渠道活跃聚合（M3-5）**：真人级"是否沉默"已跨 owner bindings 与全部 resident runtime accounts 聚合所有渠道入站，App inbound 纳入口径；planning、dispatch 后置取消和 policy avoidance 已统一使用该范围。
- [x] **T3-8a central cleanup（M3-4）**：central proactive scheduler 每轮分批执行 reservation lease、7/30 天 TTL 与 200 条对账，node 不注入；单 owner/步骤失败隔离并进入 heartbeat，cleanup 不受 inbox flag 关闭影响。
- [x] **T3-8b App-only 真人级独立 flag（M3-5）**：新增 `COMPANION_WORLD_APP_ONLY_HUMAN_PROACTIVE_ENABLED=false`，关闭时保持真人级 App-only fail-closed，微信与 App per-resident 投递不受影响。
- [x] **T3-9 正交 + 并发测试（M3-4/M3-5，PG）**：已覆盖 App reminder/commitment 入箱、微信 legacy 真人级原路、App-only 双 flag、每真人滚动 24 小时单条、owner-scoped read/cleanup、Feed/通知分面、speaker 失活 cancel/reselect，以及同真人 N resident 并发 reservation/visible 不超配。

---

## 12. 开发计划（里程碑）

> 本节把 §3 冻结决策 + §8 重构顺序落成可交付里程碑。**关键重排（2026-07-19）**：原 R2「建号一并解耦」中的计费/配额上迁抽为**独立先行里程碑 R1a/M1**，趁 fork 最浅（多居民未上线）先于领域层单独上线；多钱包老用户走预检+自动合并（理由见 R1a、D-14）。

**里程碑 ↔ §8 R 映射**

| 里程碑 | 对应 R | 目标 | 产品冻结门槛 |
|---|---|---|---|
| **M0** 冻结·脚手架 | R0 | 保护现状 + 分层门禁（防 N× 后移 M2-C） | ✅ 已交付 |
| **M1** 计费/配额锚点上迁 | R1a | 钱包/配额/RPM 锚 `platform_user` | ✅ 已交付；override/批量 TTL 回收于 M3 前置 m0031 收口 |
| **M2** Runtime facade + P1 多居民 | R1b+R2 | universe/resident/conversation + L3 首刀 | ✅ 已交付代码，default-off |
| **M3** Feed + 通知收件箱 + 真人级 proactive 上提 | R3 | outbox + App 收件箱 + 去 N× | ✅ M3-0…M3-6 已完成，default-off |
| **M4** Lifecycle + Mailbox | R4 | offline+farewell 原子事务、信箱 | ✅ M4-0…M4-6 完成 |
| **M5** Visit + Human Chat | R5 | 三 slot/高熵 code/ACL、真人分表 | ✅ M5-0…M5-5 完成，default-off |

M2–M5 不并行，每阶段无下一阶段仍是完整可回滚体验。**M0/M1/M2-C、M3、M4 与 M5 均已交付代码；P1/M3/M4/M5 保持默认关闭，生产启用仍受模板、backfill、客户端版本、evidence retention 和现场对账发布闸约束。**

### M0 — 冻结·脚手架·现状 characterization（已完成）

> 修订（Codex review 2026-07-19）：①PG lane + 阻塞式 CI **已存在**（`Makefile:31`/`tests.yml:35`），删除「转阻塞式」任务。②分层边界用 **stdlib AST 测试**,不引入 import-linter（CLAUDE.md 禁擅自加依赖；如需 import-linter 另行批准）。③**防 N× 安全阀移到 M2**——它必须 World-aware 判 primary（needs `universe_residents`）；用「最早 active account」判 primary 会误伤今天的多 account 用户、并复用文档要消除的默认账号假设。M0 无 resident 建号路径 → 此刻不存在 N× 风险。

> **交付状态（2026-07-19）：M0 全部完成。** 见 `tests/test_layer_boundaries.py`（脚手架存在性 + AST 边界门）与 `tests/test_characterization_baseline.py`（M1 安全网）。普查确认接缝 1/2/4/7/8/9 已被现有测试充分钉住，M0-1 只补三处 M1 会翻转的确定性现状（消息幂等、一人多号钱包/赠权、一人多号 daily 计数）；M0-5 现状由 `test_reactivation`/`test_proactive_*`/`test_reminders*` 等既有 115 用例承载，不造冗余测试。

| 交付 | 落点 | 出口标准 | 状态 |
|---|---|---|---|
| characterization 测试钉现状 | 微信/App 单 Agent：prompt 组装、session 轮转、持久化、moderation、钱包扣款、daily/RPM 计数 | 确定性接缝全绿（模型输出不钉） | ✅ 既有覆盖 + `test_characterization_baseline.py` 补 3 处 M1 缺口 |
| 分层边界测试（stdlib AST，不加依赖） | 禁 `app.domains.companion_world.*` import `app.db.*`/`app.turn_service`，纳入 CI | CI 阻塞（D-12），零新依赖 | ✅ `test_layer_boundaries.py`（负向探针验证门可失败） |
| 目录脚手架 | `app/domains/companion_world/`、`app/agent_runtime/`、`app/platform/` 空骨架 | AST 边界测试能识别层 | ✅ 四个空骨架包就位 |
| 现状 proactive characterization | 单 account 微信用户：reminder 触发、reactivation 每窗口一次、budget/quiet 生效 | 钉住现状（防 N× 安全阀连同多居民回归下沉 M2） | ✅ 既有 proactive 套件（115 用例）即现状钉板 |

### M1 — 计费/配额锚点上迁（已完成；依赖 M0 钱包/配额 characterization）

实际范围：`billing.py` + `accounts.py` + `rate_limiter.py` + `turn_service.py` daily 路径 + m0025/m0026/m0027 三个迁移；PG 并发门禁已交付。

| 交付 | 落点 | 出口标准（PG 必过） |
|---|---|---|
| **D-14** 钱包唯一键 `account_id → platform_user_id` | m0025 单迁移合并多钱包并加 active `platform_user_id` 唯一索引；保留旧 account 唯一约束；billing 内部 account→user→wallet 解析 | 多钱包用户预检+自动合并、单钱包直迁；对外 API 保 `account_id` 入参 |
| **D-14** 赠权按真人一次 + no-grant 建号 | `create_ai4all_account_for_user` 拆无赠权路径 | 居民 2…N 不重复赠贝壳、不拆余额 |
| **D-09** daily 原子预占 + 回滚 | m0026 计数键 + m0027 reservation；m0031 真人 override + central TTL 回收；入站去重后同事务 reserve，计费谓词统一 confirm/rollback | 消除 TOCTOU；PG 并发不超卖；多居民不可通过切换 runtime 绕 override |
| **D-09** RPM 锁 key 迁 `platform_user_id` | turn 前解析真人 quota subject，复用通用 RPM advisory 锁 | 多居民共享一套 RPM |
| **D-07** 容量脱建号计数 | resident runtime 不建 owner binding；world row lock 下按 active resident 计数 | account/binding 容量不参与 resident 上限，active resident 始终 ≤10 |
| PG 并发测试 | 跨居民同时扣款不双扣、`idempotency_key` 唯一守住 | §9 硬门禁 |

### M2 — Runtime facade + P1 多居民（§10.1/.2/.6 已冻结；**M2-C implemented / default-off**）

**前置 M2-0（编码前必做）**：定稿 §7.3 四接缝签名（初稿已写入「方法签名初稿」2026-07-19，均核过现有入口类型）+ 产出**正式 P1 后端规范**。规范已定稿 [`companion_world_p1_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_p1_backend_spec.md)（`fact_type` 枚举全集 + 路由矩阵、5 张 P1 表 DDL/索引/唯一约束、DTO + 安全契约、稳定错误码表、**锁顺序细则 §2.7 + 老用户 backfill 分步伪码 §2.8**）；**M2-0 前置门清零**（剩余 offline 原子事务/visit 双世界锁随 M4/M5）。客户端 gap 文档只作输入、不替代后端规范。

本体：`AgentRuntimePort`（R1b）+ universe/template/resident/ai_conversation 表 + **固定 4 位、版本快照**的幂等 bootstrap + 1–10 确认事务（world row lock，容量真相 = `status='active'`）+ resident 建号走 M1-5 的 no-grant/no-owner-binding-cap 内部路径（world lock 保护，容量此处校验）+ 显式 conversation history/turn（不接受客户端 `account_id`）+ **L3 首刀**（`read_companion_world_context()` + `prompt_builder.extra_blocks` 加性注入；四类 user typed fact sink，append-only §6.4）+ **全部 active binding 映射且不自动补居民**的老用户 backfill（D-08）+ App Dreaming scope 定时扫描（§6.6）+ **防 N× 安全阀（从 M0 下沉）**：只让 legacy primary 继续真人级微信触达，App-only 首版 fail-closed；per-resident reminder/commitment 不误杀。出口：跨 resident 不串线、L3 同世界可见他人世界不可见、真人级每人每窗口 ≤1；API/auth、L3 和 safety 可独立回滚。

**落地说明（M2-A 数据基座+端口骨架已交付 2026-07-20）**：M2 按「产品门」拆刀落地——本刀 = **净空第一刀（当时不触 §10.1/.2/.6 冻结门）**，只铺**数据基座 + 端口形状**，**零 turn 路径改动、零行为变更**。交付：①迁移 `m0025_companion_world_core`（实际并主干后为 m0028）+ `m0026_universe_memory_l3`（实际为 m0029）；②`app/db/companion_world.py` 底层 repo 原语；③`app/agent_runtime/ports.py` 四接缝形状。M2-C 的 §10.1/.2/.6 已于 2026-07-21 冻结；[`../../archive/deliveries/companion_world/companion_world_m2c_implementation_plan.md`](../../../archive/deliveries/companion_world/companion_world_m2c_implementation_plan.md) 已执行完毕并归档，不再作为下一步工作计划。

**落地说明（M2-B1 L3 读注入接缝已交付 2026-07-21；M3 前收口 2026-07-22）**：M2-B 先交付纯渲染与 `ChannelTurnInput.extra_blocks` 加性接缝；P1 当时暂把 L3 I/O 放入 Runtime。M3 前置现已按 D-06 收口：域层 `render_universe_l3_block(facts)` 只做纯渲染，`app/platform/companion_world_turn.py` 负责 World DB 读取、渲染组合和 App turn 输入组装，Runtime adapter 只消费规范化 `ChannelTurnInput`。**form-A no-op** 保持：微信入口不填 `extra_blocks`。~~**M2-B 剩余**：B2 八字段改指~~ → **已作废（2026-07-21），见下条落地说明**。

**落地说明（八字降级为无工具 skill，替代原 B2 2026-07-21）**：应产品要求，八字不再拥有专属存储与工具，降级为与 tianqi/weather 同形态的**无工具 skill**（仅 `app/skills/bazi/SKILL.md` + 参考表，排盘靠 LLM 现算 + `web_search`）。已删除：4 个 bazi CRUD 工具（`get/update/clear/delete_bazi_profile`，涉及 `definitions.py`/`registry.py`/`tools.__init__`）、`app/tools/bazi_profile_handlers.py`、`user_profiles.py` 的 `read_bazi_profile`/`write_bazi_profile`/`_BAZI_PROFILE_*` 及 `write_context_file` 的 `preserve_bazi_profile` 通道、`tests/test_bazi_profile_tools.py`。出生信息改走**普通记忆**（Dreaming 蒸馏进 USER.md/MEMORY.md）；存量 MEMORY.md 受管段清爽移除、不迁移（作为普通文本留存，下次 Dreaming 全量重写时自然处理）。**故原 B2「八字段改指 universe 存储」彻底作废**，`fact_type='bazi'` 从路由矩阵移除；L3 首刀由 `user_identity` / `user_preference` / `user_profile_derived` / `user_event` 四类事实承载。

**落地说明（M2-C C0–C5 已交付 2026-07-22）**：实施提交依次为 `7471ca8`（m0030 + App Dreaming scope）、`2f419de`（领域/UoW）、`ec980ad`（API/import/backfill/auth）、`f5fd3c5`（conversation/history/text turn + PG single-flight）、`244d7a7`（typed sink + L3 compact）、`a47d41e`（防 N× proactive）。最终实现遵守以下边界：

- resident runtime 不发 owner binding、不赠权；account/profile + resident + conversation 同一事务。
- App owner API 只接受 session user + 路径 resource ID；history 只读目标 runtime 的 App scope；P1 turn 只开放文字，媒体后续另做安全评审。
- conversation 单飞使用 PG `pg_try_advisory_xact_lock` 非阻塞锁，SQLite 仅进程锁功能回退；锁内重读 `state`。
- Dreaming 仅把已应用的结构化用户事实送入 L3；relationship/commitment 与未知类型不共享；compact 只折叠 exact-normalized duplicate，central scheduler 单写并以同键 advisory lock 兜底重叠。
- M2 安全阀仅允许 legacy primary 承担真人级 proactive；App-only fail-closed，reminder/commitment 不误杀。M3 才正式实现 App 收件箱、跨居民预算/活跃聚合与发声人选择。
- `COMPANION_WORLD_P1_ENABLED` 只门控新 World API 与 auth 切换；L3 后台链路和 world-aware proactive 安全阀不随 flag 关闭。回滚为关闭 flag、停止新入口并保留加性数据；若要求恢复 secondary legacy resident 的旧真人级 proactive 行为，必须另做明确操作/代码门控，不能仅依赖关 flag。
- 上述 carry-in/backfill 是历史交付口径，产品拆分后不再执行。鸣蝉首次启用采用 clean-start：先完成
  precheck/cleanup 演练、客户端 namespace 切换和双后端门禁，再按
  [首次生产启用检查单](../../../ops/products/mingchan/production_first_enablement.md)发布。
- M3 前置收口门禁（2026-07-22）：unit 566 passed；SQLite 1434 passed / 8 skipped；PostgreSQL 1438 passed / 4 skipped；`git diff --check` 通过。

### M3（M3-0…M3-6 已完成）、M4（M4-0…M4-6 已完成）与 M5

- **M3**：`universe_posts` + 事务 outbox（领域状态与 outbox 同事务、worker 幂等），按 universe 每日两窗口生成文字动态；App 通知收件箱 **T3-1…T3-9**（显式已读、7/30 天、200 条）; 真人级 proactive 上提域层（预算/活跃按 platform_user 聚合、universe 级 due 队列、确定性发声人、App-only 独立灰度 flag）。
- **M4**：M4-0 已冻结 cooldown/audit/safety、last-resident、不可逆纠错和 mailbox 策略；M4-1 已交付 m0034、owner-scoped DB 原语、纯领域 DTO/ports 与 default-off 配置；M4-2 已交付 evidence/review/scheduler；M4-3 已交付 offline 原子事务；M4-4 已交付 signed catalog、world-lock delivery/expiry 与 owner 私密读取/处理；M4-5 已交付 no-binding/no-grant letter accept 单事务；M4-6 已补运行手册、只读对账、catalog-retire PG 竞态与最终双后端门禁。三 flag 仍默认关闭，详见 [`companion_world_m4_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_m4_backend_spec.md) 归档。
- **M5**：M5-0…M5-5 已完成并归档：m0035、一次性 invite、pending owner approval、双侧容量、visit 强事务、visitor-only Feed、expiry/block、独立 human message/read/hide/report、限流、运行手册与最终双后端门禁均已交付，两个 flag default-off。A 世界 invite/pending/active 共用三 slot，B 跨世界 pending+active 也最多 3；human messages 以 conversation sequence 保序且不进 AI 路径。详见 [`companion_world_m5_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_m5_backend_spec.md)。

### 2026-07-23 外部 Review 终判与采纳项

- **终判**：在 `origin/main@3403380` 范围内未发现阻断合入的架构性问题或正确性 bug；分层、账号/世界隔离、钱包/配额、L3、生命周期、访客/真人聊天和 proactive 的核心不变量均有对应实现与双后端门禁。该结论只支持“代码就绪”，不替代生产迁移、数据对账、客户端口径和开旗授权。
- **已立即补强**：D-11 AST 门禁封住 `app.db` 的 human-chat 符号再导出路径，见 D-11；对应聚焦测试必须随 closeout 变更通过。
- **已登记非阻断债**：D-09 扣款结果与 quota confirm 的低危语义偏离（D-09 item 6）；L3 仅 exact-normalized 去重、尚无语义 supersession/retention（§6.4）；真人级发声人切换时 account-local cooldown/观测的软一致性（D-15）。双键/兼容列继续按 D-09/D-14 的既定加性迁移策略保留，待独立清理信号，不在本轮做高风险表重建。
- **长期拓扑约束**：当前正确性依赖“中心单 writer + PG 行锁/advisory lock/唯一约束”——L3 compact、Feed/outbox、通知清理、lifecycle/mailbox/visit expiry 都要求只有指定 central-capable scheduler 扫描。现有模块化单体/厚节点拓扑满足该假设；出现多地域 active-active、按世界分片、调度拆服务或单 scheduler 吞吐不足信号时，必须先重做任务所有权、lease/fencing 与跨分片锁设计，再扩拓扑。

### 关键风险与门槛

1. **产品冻结项**是各里程碑硬前置：M2-C、M3、M4 与 M5 的对应 §10 门均已清零，3.0 后端开发已完成。M2-C PR #45、M3 PR #46、M4 PR #47 与 M5 PR #48 均已合并；PR #48 双后端 CI 已通过。
2. **客户端口径冲突（生产阻断）**：gap-analysis §4.2「默认隔离/白名单」与 D-05「全量共享沉淀记忆」相反，PRD 仍称来源不构成 legacy 离开豁免。M2-C 后端已实现，客户端文档与实现必须在开 flag 前镜像对齐。
3. **money 路径**：M1 是唯一动扣款的里程碑，PG 并发测试是发布闸，SQLite 绿不作数（§9）；D-14 钱包生产预检必须 PASS，D-09 item 6 的低危偏离保持可观测且不得误写成“实际扣款成功才 confirm”。
4. **App scope 漏扫**（§6.6）已冻结修复：M2-C 将 `__app_active__` 纳入 `DEFAULT_ACTIVE_SESSION_KEYS`，并补 App scope 每日轮转回归。
5. **D-09 发布对账**：m0031 已把 override 来源上迁真人并接 central TTL 回收；开 flag 前仍须完成遗留 account 副本与 canonical user override 的只读对账。
6. **生产开旗硬闸**：首次启用 precheck/cleanup 演练、钱包与 override 对账、正式模板、客户端鸣蝉
   namespace、report evidence retention、双后端回归和各里程碑只读对账未全部清零前，只能判定
   “仓库开发完成”，不得启用鸣蝉产品或 worker。

---

## 13. 参考

- P1 后端实现规范（fact_type / schema / DTO / 错误码，M2-0 交付物）：[`companion_world_p1_backend_spec.md`](../../../archive/deliveries/companion_world/companion_world_p1_backend_spec.md)
- 客户端数据/API 初稿：[`private_world_backend_gap_analysis.md`](../../../../../ai4all-companion-app-rn/docs/tech_design/private_world_backend_gap_analysis.md)（§4.2 共享上下文口径需按 D-05 镜像更新）
- 客户端 PRD 与客户端架构：`ai_companion_universe_prd.md`、`mobile_client_architecture.md`
- 现存实现依据：见本文顶部“接手说明”的当前代码地图；历史路径只在归档 build spec 中保留。
