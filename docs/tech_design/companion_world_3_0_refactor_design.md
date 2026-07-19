# 技术设计：系统 3.0 — Agent Runtime 分层与 Companion World 产品领域层（架构决策记录）

更新时间：2026-07-18
状态：**核心架构决策已冻结**；P1 schema/接口/迁移待技术评审后进入编码。含主动消息子系统改造（D-13 / §11）。本文为架构决策记录（ADR）性质，是 `docs/tmp/companion_app_backend_refactor_handoff.md` 讨论底稿的正式化产物，冻结口径以本文为准。
核查基线：`main@abe6e2b`

关联产品 PRD（客户端仓库）：
- [`ai_companion_universe_prd.md`](../../../ai4all-companion-app-rn/docs/product/ai_companion_universe_prd.md)

关联后端设计：
- [`identity_model_and_wechat_binding.md`](identity_model_and_wechat_binding.md)、[`web_app_channel_access_design.md`](web_app_channel_access_design.md)
- [`agent_context_files.md`](agent_context_files.md)、[`dreaming_memory_design.md`](dreaming_memory_design.md)、[`relationship_state_implementation_plan.md`](relationship_state_implementation_plan.md)
- [`thick_node_postgres_refactor.md`](thick_node_postgres_refactor.md)、[`multi_node_access_refactor.md`](multi_node_access_refactor.md)

工作底稿（provenance，含完整讨论与逐条代码核查）：`docs/tmp/companion_app_backend_refactor_handoff.md`（`docs/tmp/` 被 `.gitignore` 忽略，仅作过程记录，勿当规范）。

---

## 0. 摘要（TL;DR）

“朝夕相伴” App 引入的不是又一个 channel，而是一个新的**产品领域**：一个真人拥有一个私人世界、世界里有 1–10 位平等 AI 居民。这把系统从 1:1（用户↔AI）推向多对多。

3.0 的核心单一改动：**在稳定的 Agent Runtime 之上新增一个产品领域层（Companion World），由领域层持有产品实体、权限/ACL、共享范围与生命周期；Agent Runtime 保持形态无关（form-agnostic）。** 不永久 fork，不推倒重写，短期模块化单体。

关键判断（已核对代码）：三层 context 在现有实现里**物理上已分散到不同载体**，L1（人设/使命）、L2（关系）天然 per-account 独立、无需多对多改造；3.0 真正要新建的只有 **L3（关于用户的记忆）的 universe 级共享承载 + 一个加性注入块**。改造面比“多对多”一词听起来小得多。

---

## 1. 背景与问题

1.0 是微信单 Agent 个人陪伴 bot；2.0 加了 `platform_user` 真人身份、App/Web session、owner binding、渠道能力表、`/api/v1`、SQLite/PG 双后端与生产多节点。但核心业务模型仍是 `platform_user → first/default account → Soul/Memory/session/turn` 的 1:1 形态。

“朝夕相伴”要求：一人一世界、世界内 1–10 位平等居民（无主角色）、每居民独立关系/会话/Soul/Memory、世界动态、可不可逆离场、私密信箱、限时访客与真人一对一聊天。核心矛盾：现有 `account` 同时承担 **AI 关系运行时 / 产品默认账号 / Profile-Memory owner / 计费钱包锚点 / 渠道绑定对象** 五重身份；在多居民下它们不再是同一件事。

底稿 §3 逐条核查的现存问题（此处仅列结论，代码依据见底稿）：
- App API 建立在 `get_first_active_account_for_user()`“第一个 account”假设上，多居民后无法定位当前对话角色。
- 直接复用默认建号流程创建居民会**重复赠权 + 拆散余额**（`billing.py` 建号做 owner binding + subscription + wallet + 新客贝壳）。
- 建号前 `COUNT(*) account_owner_bindings ... status='active' >= 10` 抛错；offline 居民留 binding 读历史 → 用户满 10 后**永久无法补新居民**。
- App turn single-flight 用进程内 `threading.Lock`，多节点下非最终一致。
- 缺少 Universe/居民/世界动态/信箱/访客/真人会话的持久化与 ACL。

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
- **影响**：以 characterization test 保护现状后逐步抽边界；分层边界须由 CI（import-linter）强制（见 D-12）。

### D-02 account 重定位为 `agent_runtime_id`（架构）
- **决策**：概念上把 `account_id` 定位为“一个用户与一个 AI 关系实例的隔离运行容器”。**暂不做全仓库 rename**，通过 `AgentRuntimePort` facade 隐藏旧命名。Companion World 只保存 `universe_resident.runtime_account_id` 映射，不跨层读 Runtime 的 Memory/Session。
- **理由**：全仓库机械 rename 高风险、零收益；facade 足以隔离命名。
- **影响**：同一官方模板进入两个用户世界 → 生成两个 resident + 两个 runtime account，私聊/Memory 不共享。Port 方法从真实调用点长出，不提前落死（`set_read_only`/`configure_persona` 等暂无调用方的方法标注待定）。

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
- **影响**：L3 承载只上提**沉淀记忆层**（USER/MEMORY 用户事实段、派生画像），**不**上提 `messages`/session 原文，也**不**让 retrieval 跨 runtime account。测试需验证：①用户事实类整体进 L3、关系/人设类不进 L3；②居民 A 的聊天原文/检索结果不出现在居民 B 的上下文。⚠️ 客户端 gap-analysis §4.2「P1 默认隔离/仅白名单」口径与此相反，落地前需镜像对齐（本轮仍不动客户端仓库，待去客户端仓库时同步为本决策）。

### D-06 共享范围属产品域层，Runtime form-agnostic；L3 锚 `universe_id`（架构）
- **决策**：**共享范围是产品域层的组合行为，绝不编码进 Agent Runtime。** Runtime 只暴露“读某 runtime account 的 per-account context（L1+L2）+ 接受一个注入端口把外部 shared-context 喂进 prompt build”。形态 A 域层不注入（退化 1:1）、形态 B 按 `universe_id` 注入、形态 C 自定义范围复用同一 Runtime。L3 锚 `universe_id` 而非 `platform_user_id`。
- **理由**：把共享语义留在域层，未来“一人多世界”天然按世界隔离，跨世界共享再叠加一层即可，属加性演进、不重写。
- **影响**：Runtime 满足“不反向依赖 Companion World”；注入端口是唯一新接缝。

### D-07 容量真相脱离建号 binding 计数（P1 阻断，冻结 2026-07-19）
- **决策【已冻结】**：active 容量以 `universe_residents.status='active'` 为唯一真相，在 world row lock 下计数；offline/dismissed 保留 account 与 binding 仅供只读历史、不计位。建号函数的 binding 上限与居民容量**解耦**（新增 no-limit / no-grant 建号路径）。
- **依据**：`billing.py:2090` 建号前 `COUNT(*) account_owner_bindings WHERE status='active' >= 10` 抛错。
- **影响**：成本事件仍记实际 resident runtime，余额扣减归同一真人 billing owner。

### D-08 legacy resident 保留离开豁免（P1 阻断，**冻结 2026-07-19 = 保留豁免 + 修订 PRD**）
- **决策【已冻结 = 保留豁免】**：微信 legacy 居民与 App legacy 居民共享同一 runtime account，“legacy 普通居民 + 微信零回归 + 不可逆 offline”三者互斥。**取「保留豁免」**——legacy 居民永不 offline，App 仅展示、不提供离开入口；工程上微信零回归天然满足（不动共享 runtime account）。**须同步修订客户端 PRD `ai_companion_universe_prd.md:634`「来源不构成豁免」记为已知偏离**（本轮不动客户端仓库，去客户端仓库时改；§10.4 已标注）。
- **依据 / backfill**：`account_owner_bindings` 在 `platform_user_id` 无唯一约束、`get_first_active_account_for_user` 只取最早一个 → backfill 须显式处理已持 2+ account 的老用户（全部映射为 `origin=legacy` 居民，或仅第一个、其余处置随 §10.2）。
- **依据**：底稿 §9；`account_owner_bindings` 在 `platform_user_id` 无唯一约束、`get_first_active_account_for_user` 只取最早一个 → backfill 须显式处理已持 2+ account 的老用户（全部映射为 `origin=legacy` 居民，或仅第一个、其余如何处置）。

### D-09 限流按用户聚合须原子预占/回滚（P1，配额键冻结 2026-07-19）
- **决策【已冻结 = platform_user】**：daily/RPM 配额键**按 `platform_user` 聚合**（一个真人一套配额，多居民共享，不随居民数放大）。配套：daily 计数改**原子预占 + 失败回滚**（消除 `turn_service.py` 读—处理—+1 的 TOCTOU），RPM advisory 锁的 key 从 runtime account 迁到 `platform_user_id`。billing wallet 归属同真人（见 D-14 钱包上迁 + D-07 建号解耦）。
- **配额 schema + 退款语义（冻结 2026-07-19，补 Codex #4）**：
  1. **schema 上迁用户级**：`daily_usage` 唯一键 `(account_id,date)`（`_core.py:635`）、`rpm_hits` 仅 `account_id`（`:1625`）→ 迁 `(platform_user_id,date)` / `rpm_hits.platform_user_id`（或通用 `subject_type/subject_id`，最终形态 M2-0 定型）。
  2. **override 冲突**：多居民各自 account override 时，**以 `platform_user` 级 override 为准**，per-account override 对配额不再生效（消除「取哪个」歧义）。
  3. **退款矩阵 = 仅成功计费的 turn 消耗**：moderation 拦截 / 模型失败 / 网络断 / 重复消息**一律回滚预占**，只有真正产生 LLM 计费的 turn 扣配额。
  4. **reservation 与入站幂等同事务**：去重（`(conversation_id,sender_id,client_message_id)` 唯一）先于预占，**同一事务**，重试不吃配额。
  5. **崩溃悬挂**：进程崩溃残留 reservation 由 **TTL 过期回收**，不永久占位。
  6. **PG 锁顺序**：两 resident 并发同一 `platform_user` 按单键 advisory 锁串行；PG 用例覆盖并发预占不超卖、回滚不误伤他人配额。
- **依据**：daily 路径 `turn_service.py:1065` 读、`:1185` 处理后才 +1，**无 advisory 锁**（RPM 路径 `rate_limiter.py:52` 有 `pg_advisory_xact_lock`）；TOCTOU 今天被 per-account 单飞掩盖，聚合到用户后跨居民无单飞将暴露。
- **同源提示**：D-07/D-09/重复赠权同源于“建号函数把 owner binding 当计费锚点+容量上限+配额键三重身份”，P1 须一并解耦。

### D-10 世界内容 ≠ 主动消息（架构）
- **决策**：世界动态、离别动态、信箱由独立 world-content/lifecycle job 生成、审核、持久化（`universe_posts` 等），客户端主动拉取 Feed；**不通过打开 `CHANNEL_APP.supports_proactive` 实现**。是否 Push 后续独立设计。
- **依据**：`channels.py` `CHANNEL_APP` `supports_proactive=False`、`tdai_enabled=False`；开启会误启当前无 APNs/FCM 投递能力的提醒工具。

### D-11 真人聊天分表，不进 Agent Runtime（架构）
- **决策**：`human_conversations`/`human_messages` 与 AI `messages` 分表，`(conversation_id, sender_id, client_message_id)` 唯一；从结构上保证真人消息不进 LLM/Soul/Dreaming/AI Memory/moderation prompt。真人内容另做独立内容安全/举报/封禁。

### D-12 并发正确性以 PG 为证；分层边界 CI 强制（架构）
- **决策**：竞态类不变量（并发确认、10/11 位竞争、双花 code、slot 竞争、offline 与新 turn 并发、L3 并发写）只在 PostgreSQL 测试算数，SQLite 只验功能正确性。引入 `import-linter` 契约禁止 `app.domains.companion_world.*` 依赖 `app.db.*`/`app.turn_service` 等，纳入 CI。
- **依据**：SQLite 单写者串行会假绿；分层边界不靠纪律靠工具。

### D-13 主动消息沿 L2/真人级分裂；真人级触达上提产品域层；App 投递面 = 拉取式通知/收件箱（架构 + 投递面冻结 2026-07-18）
- **决策**：主动消息按触发本质切两半（详见 §11）：
  - **per-resident 义务**（`USER_REMINDER`、`COMPANION_FOLLOWUP/commitment`、`CONTENT_INVITATION_RESPONSE`、`TASK_RESULT`）= L2 对齐，**留 Agent Runtime 侧、继续 per-account**。
  - **真人级触达**（`NEW_USER_REACTIVATION`、`CONTENT_INVITATION`、`COMPANION_FOLLOWUP/account_check`）= 类比 L3，**上提产品域层**：按 universe 聚合去重、跨 `account_owner_bindings` 聚合预算与活跃判断、新增"发声人(resident)选择"。
  - **投递面（已冻结 = A）**：App 侧落**拉取式通知/收件箱**，与世界 Feed（D-10）分离入口；不依赖 push、不误开 `supports_proactive`。微信 legacy 居民走原 `send_weixin_text` 不变。APNs/FCM 真 push 作为后续独立能力，不阻断本决策。
- **依据**：`app/proactive/` 整层按 `account_id` 键、零引用 `platform_user`/`account_owner_bindings`/`account_user_meta`/`relationship_state`；唯一出口 `dispatch_proactive_text`（`outbound.py:362`）硬门控 `supports_proactive`（`CHANNEL_APP`=False `channels.py:98`）；`send_weixin_text`（`outbound.py:288`）写死微信、忽略 channel。→ 一个真人 N 居民 = N 条独立管线 + N 份独立预算（N× 打扰结构性保证）。
- **影响**：与 D-09（限流聚合）同源同批；R0 先上防 N× 安全阀（真人级仅对 legacy/primary 居民触发）。详见 §11。

### D-14 钱包主键上迁 `platform_user`，一真人一钱包；预检 + 自动合并少量存量（P1 阻断，冻结 2026-07-19）
- **决策**：`entitlement_wallets` 的唯一键从 `account_id` 上迁到 `platform_user_id`——**一个真人一个钱包，全部居民共用一份余额**；新客赠权按真人发一次，居民 2…N 走 no-grant 建号（与 D-07 建号解耦同处）。`entitlement_ledger`/`cost_events` **保留 `account_id`** 列，分居民成本/质量分析不丢。**不走"短期映射表"补丁**（那既留债又给不出单一余额，且与 D-09 配额按真人聚合不自洽）。
- **依据（已核代码）**：`entitlement_wallets.account_id` 现为 `UNIQUE` 主查键，钱包全程 `WHERE account_id=?` / `ON CONFLICT(account_id)`（`billing.py:626-673` 等），故今天"一 account 一钱包"；但表/流水/成本行**已冗余带 `platform_user_id`**（`_core.py:393-462`）。分歧集中：钱包解析在 `billing.py` 单文件 ~10 处 SQL；对外 API（`get_wallet_balance_shell_micros`/`get_wallet_summary`/`list_wallet_ledger`/`record_chat_usage_charge`）**保留 `account_id` 入参、内部 account→user→wallet 解析**，故 `turn_service.py:1924`、`relationship_state.py:130/182`、web/admin 读侧几乎不动。
- **为何现在改（口径修正 2026-07-19，Codex）**：建号允许**每真人 ≤10 account**（`billing.py:2090`），故「一真人一钱包」**并非当前不变量**——多 account 老用户已有多钱包，**原「零合并」说法作废**。但**现在改仍最省**：多居民尚未上线、fork 程度最低，需合并的只是历史多 account 的少量存量；趁早远比多居民放大后（N 世界 × N 居民钱包）合并便宜。与 A2/D-09（配额按 `platform_user`）同锚、自洽。
- **迁移方案（冻结 = 预检 + 自动合并，2026-07-19）**：
  1. **生产预检 SQL**（M1 前置门，必须在生产 PG 跑，本地 SQLite 仅 2 user 不代表生产）：`SELECT platform_user_id, COUNT(*) FROM entitlement_wallets WHERE status='active' GROUP BY platform_user_id HAVING COUNT(*)>1` + 余额/ledger 分布，产出多钱包用户清单。**已交付（M1-0，2026-07-19）= `scripts/precheck_wallet_migration.py`（只读，退出码 0=PASS/1=BLOCK 作发布闸）**：除盘点（多钱包用户 + 多次赠权用户）外，硬校验迁移所依赖的四条数据不变量并阻断——`ambiguous_owner`（单 account >1 active binding，归属歧义）/`orphan_wallet`（active 钱包 account 无 active binding）/`owner_drift`（钱包 `platform_user_id` 与唯一 active 归属人不一致，会并错人）/`primary_undefined`（多钱包用户最早 active binding 对应 account 无 active 钱包，选主取不到）；负余额为 WARN。判定逻辑单测见 `tests/test_precheck_wallet_migration.py`（5 用例，四条阻断各注入验证）。
  2. **自动合并**：迁移脚本对多钱包用户做**余额求和 + ledger/cost_events 归并到选主钱包（选主 = 最早 active binding 对应钱包）+ 其余钱包置 `status='merged'` 保留审计**；单钱包用户零合并直迁。两条路径同一脚本覆盖、可重复运行。
  3. **目标 DDL**：加 `UNIQUE(platform_user_id) WHERE status='active'`（局部唯一索引）；**保留** `UNIQUE(account_id)`（落地决策 2026-07-19，见下「落地说明」——因改按真人 get-or-create 后永不为同一真人插第二钱包行、`account_id` 事实上仍唯一，故不删，避开 SQLite 表重建 / PG `DROP CONSTRAINT` 的零先例后端分叉）；`account_id` 列语义变「创建来源」；`ledger`/`cost_events` 的 `account_id` 列一并保留（分居民成本分析）。
  4. **赠权幂等键**：`new-user-grant-{account_id}` → `new-user-grant-{platform_user_id}`（`billing.py:815`），防上迁后每居民重复赠权。
  5. **expand/migrate/contract 滚动兼容**：先加 `UNIQUE(platform_user_id)`（expand，双唯一并存、新旧进程兼容）→ 回填合并 + 代码切 account→user→wallet 解析（migrate）→ 删 `UNIQUE(account_id)`（contract）。
- **代价**：动 money 路径，须 PG 并发测试兜底（跨居民同时扣款不双扣、`idempotency_key` 唯一守住、合并脚本幂等）；范围锁在 `billing.py` + 迁移函数（追加 `_MIGRATIONS`：SQLite 表重建 / PG `ALTER CONSTRAINT`，分 expand/contract 两步）+ billing 测试。
- **同源提示**：D-07（容量脱建号计数）/D-09（配额键）/D-14（钱包键）/重复赠权四者同源——建号函数把 owner binding 当"计费锚点 + 容量上限 + 配额键"三重身份，P1 一并解耦。

> **落地说明（M1-1 + M1-7 已交付 2026-07-19）**：
> - **保留 `UNIQUE(account_id)`，只加局部唯一索引**（不走 item3 原文的「删约束」）。理由：billing 改按 `platform_user` get-or-create 钱包后，永不会为同一真人插入第二个钱包行，`account_id` 事实上仍唯一、保留无害；据此**完全避开** SQLite 12 步表重建 / PG `DROP CONSTRAINT` 这个本仓库零先例、money 表首次引入的高风险后端分叉。日后若要 item3 的纯「非唯一」语义，可用一条清理迁移补删。
> - 因不删旧约束，item5 的 expand/migrate/contract **收敛为单个迁移** `_migration_0022_wallet_unique_platform_user`（`app/db/_core.py`，版本 22）：合并存量多钱包老用户（选主=最早 active binding 对应钱包、余额求和、ledger/cost_events 归并到主、其余置 `status='merged'`）→ 加局部唯一索引；幂等、可重复执行。stop-start 部署（本仓库现状），无滚动窗口。
> - **连带 M1-7**：赠权幂等键 `new-user-grant-{account_id}` → `new-user-grant-{platform_user_id}`（`billing.py`），钱包并份后同一真人第二个号不二次赠贝壳。
> - billing 解析 6 处（`_ensure_wallet_in_conn` / 两个 charge 幂等分支 / `get_wallet_summary` / `list_wallet_ledger`）由 `WHERE account_id` 改按 `platform_user_id AND status='active'`（`list_wallet_ledger` 流水视图随之按真人聚合）。对外 API 的 `account_id` 入参与 ledger/cost_events 的 `account_id` 列均不动。
> - 测试：`tests/test_wallet_migration_m0022.py`（合并正确性 + 幂等）、`tests/test_characterization_baseline.py`（接缝 5 翻转为终态）、`tests/test_precheck_wallet_migration.py`（构造迁移前多钱包态）。**M1 整体发布仍以 M1-6 PG 并发闸为准**（本刀已过既有 PG billing 套件）。

---

## 4. 目标分层与依赖方向

```text
接入与产品 API 层   WeChat/OpenClaw · Companion App API · Web · Future App API
产品领域层          Companion World（universe/resident/feed/lifecycle/mailbox/visit/human chat） · Future domains
Agent Runtime       runtime account · Soul/Identity/Profile · session/message · prompt/turn/model/tool · Memory/Dreaming · AI moderation
平台基础层          platform user/auth · entitlement/billing · DB/migration/repo · scheduler/outbox · observability
```

依赖方向：`API adapter → product domain → AgentRuntimePort → 现有实现`，域层旁挂 `→ platform services`。**Agent Runtime 不反向依赖 Companion World；微信 adapter 不读 world 表；human chat 不进 Runtime。**

`AgentRuntimePort`（最小面，从真实调用点长出）：`create_runtime` / `send_turn` / `list_history` / `configure_persona` / `set_read_only` / `get_runtime_summary`。

代码组织目标（不要求一次搬完，先建 facade 与新领域，旧实现逐步被包住）：`app/agent_runtime/{facade,ports,existing_runtime}.py`、`app/platform/{identity,billing,events}/`、`app/domains/companion_world/{models,repository,services/*,policies,events}.py`、`app/routers/app_{auth,bootstrap,worlds,residents,conversations,mailbox,visits,human_chat}.py`。

---

## 5. 三层 context 模型（映射现有载体，已核对代码）

| 层 | 内容 | 基数/锚点 | 当前物理载体（核对后） | 形态 B 行为 | 独立性现状 |
|---|---|---|---|---|---|
| **L1 人设/使命** | persona、口吻、边界、mission | 1 模板→N 关系；实例锚 runtime account；模板带版本 | `account_profile_files`（SOUL/IDENTITY/MISSION）+ `account_mission` 表 | 各 Agent 独立 | 已 per-account 独立 ✅ |
| **L2 关系状态** | 关系阶段、马斯洛需求、共同历史、关系漂移 | 1 per 用户×Agent，锚 runtime account | `account_user_meta` 行（已脱离文件）+ MEMORY.md 关系性片段 | 各 Agent 独立 | 已 per-account 独立 ✅ |
| **L3 用户记忆** | 关于真人的持久事实/画像（沉淀记忆；**不含聊天原文**） | 1 per **universe**，同世界共享 | 散在 USER.md + MEMORY.md **八字托管段**（`account_profile_files`，per-account、冗余） | 同世界多 Agent 共享 | **唯一还粘在 per-account 文件、需上提 ❗** |

**关键判断**：L1/L2 在现有 per-account 隔离下天然独立，无需多对多改造；3.0 真正要新建的是 L3 的 universe 级承载，以及把今天混在 SOUL/MEMORY 里的层拆出来。

**L3 共享边界（D-05 精化，2026-07-19）**：L3 只共享**对用户的沉淀记忆**（USER/MEMORY 用户事实段、派生画像）。各 resident 的**原始聊天记录（`messages`/session 逐字稿）与检索/证据回放（`tool_evidence_replay`）不进 L3、不跨 runtime account**——居民之间“八卦”对用户的认知，但读不到彼此的私聊原文。因此 L3 上提的是“沉淀层”，`messages` 表仍严格 per-account。

现状承载的代码事实（供实现参考）：
- 账号级 profile 文件已下沉 DB 表 `account_profile_files`，**主键 `(account_id, filename)`**，account 隔离硬编码在每条 SQL（迁移 m0004 `_core.py:1592-1613`，门面 `profile_storage.py`，读写 `user_profiles.py:135,211`）。
- 关系四状态 + 派生画像在 `account_user_meta`，使命在 `account_mission`；由 `agent_self_state.build_agent_self_state_block`（`agent_self_state.py:117`）渲染后经 `assemble(agent_self_state=...)` 注入（`turn_service.py:619`，`prompt_builder.py:413`）。
- MEMORY.md = 八字托管段（结构化、工具管理、`preserve_bazi_profile` 独立通道 `user_profiles.py:190-228`）+ 通用长期记忆（Dreaming 读写）；USER.md = 用户称呼/身份/偏好（`dreaming.py:373`，`ALLOWED_TARGET_FILES` `:111`）。

---

## 6. L3 落地：接缝与并发（已核对代码）

### 6.1 注入接缝（最小、零核心改动）
经 `prompt_builder` 的 `extra_blocks` 加性钩子（`prompt_builder.py:477-480`，`tdai_recall` 已在用 `turn_service.py:1392-1426`）：turn 组装前构造 `ContextBlock(name="universe_l3", ...)` 追加。L3 数据源另起模块，类比 `agent_self_state.py`“只渲染、不算不写”，返回文本、由 turn_service 包成 block。**无需改 `prompt_builder.py` 或任何现有 block**（`assemble` 声明式，`token_budget=None` 默认零行为变更 `:113-136`）。

### 6.2 存储接缝（不要复用 `account_profile_files`）
`account_profile_files` 主键 `(account_id, filename)`、account 隔离硬编码 → universe 级共享**不进此表**（否则每 account 复制一份）。新建 universe 级表（以 `universe_id` 为键、无 account_id 维度）+ 仿 `profile_storage.py` 写 `universe_storage.py` 门面 + `read_universe_context()`，喂给 §6.1 注入块。

### 6.3 写入路由（别照搬 dreaming 的读-改-写）
- **原始沉淀**：复用 `append_file` 的 SQL 侧原子追加（`profile_storage.py:58-84`，PG 多节点并发不丢）→ universe 级 raw 追加安全。
- **压缩维护**：新挂一条 dreaming 类比链路，但**不能照搬 `_apply_memory_item`（`dreaming.py:530`）**——它是“读(一事务)→Python 改→写整文件(另一事务)”，读写间无行锁/无 advisory/`version` 列存而未用作 CAS；现状不炸仅因单 account 天级串行。

### 6.4 并发保护（L3 多 resident 并发写的硬要求）——**已冻结 = append-only typed fact/event + 单 writer compact（2026-07-19）**
同一 universe 多 resident 并发写 L3 会 last-writer-wins 覆盖整文件。**决策**：L3 落**追加型 typed fact/event 存储**——各 resident 只 append 带 provenance（来源 resident/turn、时间、fact_type）的**结构化事实行**，永不就地改写整文件；**压缩/去重由单 writer 异步 compact**（挂 §6.6 DreamingScheduler 单例）合并成稳定视图。如此既保留 provenance、天然规避整文件覆盖，又与 §6.3「原始沉淀走 `append_file` SQL 侧原子追加、压缩另起单 writer」一致。**弃用**：全程 `FOR UPDATE`/advisory 串行写（吞吐差、仍是整文件模型）、`version` 乐观 CAS（多 writer 高冲突下退化为忙等）——二者仅作 compact writer 内部实现细节可选，不作为写入路径主模型。并发正确性以 PG 为证（D-12）：同一 universe 两 resident 并发 append 不互相覆盖、compact 幂等。

### 6.5 最低成本切口与顺序
- **八字托管段**已结构化/工具管理/有独立 read/write/preserve 通道（`user_profiles.py:190-228`）→ 把 `read_bazi_profile`/`write_bazi_profile` 后端从 MEMORY.md 内嵌段改指 universe 存储，对 prompt 侧几乎透明，是 **L3 首刀**。
- USER.md 用户画像拆分成本更高（与 dreaming 整文件读写耦合 `dreaming.py:373`）→ 二期。分流点即现有 `ALLOWED_TARGET_FILES` 的 USER.md/MEMORY.md 选择 + 八字段。

### 6.6 调度挂载
L3 天级压缩挂 `scripts/run_proactive_scheduler.py` 单例 DreamingScheduler 旁，沿用“中心扫全量、节点不重复”单 writer 纪律。若 L3 引入新 active session scope，**必须在 `DEFAULT_ACTIVE_SESSION_KEYS`（`_core.py:27`）登记**，否则永不轮转——现状 App `__app_active__`（`channels.py:30` 定义、`:93` 使用）就漏在每日扫描外（扫描键仅 `__account_active__`+`__web_active__`），dreaming 被懒轮转压到用户下次请求延迟上；多居民后每居民各触发一次首条卡顿，P1 需决策 App scope 是否纳入定时扫描。

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
三个稳定接缝：**注入端口**（Runtime 只读 per-account L1+L2 + 接受外部 shared-context 注入）；**范围锚点**（共享/隔离边界由域层锚点决定，B=`universe_id`，C 可用 `org_id`/`room_id`/`family_id`，各建自己的 `*_storage`/`*_context`，不动 Runtime）；**加性迁移**（新表加性、新 API 走 `/api/v1` 新路径、feature flag 门控、关 flag 退回上一形态）。

新形态四问决策清单：① 绑定关系（一人↔几 Agent、锚点是什么）；② 共享范围（L3 共享到哪个锚、L1/L2 是否仍独立，默认独立）；③ 内容/社交面（世界动态/生命周期/访客/真人聊天各走独立持久化+ACL，不进 Runtime、不进 AI `messages`）；④ 计费/容量/配额（锚 `platform_user` 钱包、容量真相放域层 status 表、不借建号 binding 计数）。

反模式（明确禁止）：把形态假设写进 `turn_service`/`prompt_builder`/`account_profile_files`（违反 D-01/D-06，import-linter 拦截）；给 Runtime 表加 form-specific 列（`character_id`/`world_id`，应用域层关系表 `universe_residents.runtime_account_id` 映射）；复用 referral/campaign 旧 code 通道承载新形态邀请/共享语义。

---

### 7.3 Runtime↔World 端口契约（方向与事务边界冻结 2026-07-19；方法签名初稿 2026-07-19，M2-0 定稿）

> Codex review：ADR 只列了 `AgentRuntimePort` 方法名，未冻结**接缝契约**与**事务边界**；且 §11.8 T3-2 让 Runtime 出口直写带 `universe_id`/`resident_id` 的通知表，**依赖方向反了**。M2 编码前须先把下述四接缝冻结（任务 M2-0），不能只列方法名。核对到的现状缺口：`ChannelTurnInput` 无 shared-context 入参（`turn_service.py:2104`）；`extra_blocks` 只在 turn 内由 TDAI 生成（`:1392`）；after-turn memory hook 只有 `account_id`、无法 form-agnostic 路由到 universe（`:1584`）。

**四个稳定接缝（方向恒为 `域层 → Runtime`，Runtime 不反向依赖 World）**

1. **turn 外部 context 注入（入向）**：`send_turn(...)` 增可选 `extra_context: list[ContextBlock]`；`ChannelTurnInput` 加 `extra_blocks` 字段，经已有 `prompt_builder.extra_blocks` 加性钩子注入（§6.1）。域层读 L3 后构造 block 传入；Runtime 只消费、不认识 `universe_id`。TDAI 现有 `extra_blocks` 路径不变、与此并存。
2. **after-turn typed memory sink（出向，typed）**：after-turn 钩子从「只给 `account_id`」升级为发出 **typed memory event**（`fact_type` + `scope_anchor` + payload + provenance）。**路由决策（per-account L2 vs universe L3）在域层做**，Runtime 不写 World 表、不认识 universe。形态 A 无 sink 消费者=退化 1:1。
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
    # P1 实际只需 create_runtime(no-grant, M1-5) / send_turn / resolve_conversation_account
    #   (conversation_id→runtime_account_id) / set_read_only(offline)；
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
    fact_type: str                      # 枚举 M2-0 定稿：user_fact/preference/bazi(→L3) | relationship(→L2) | ...
    payload: dict                       # 结构化事实，非逐字原文（原文仍 per-account，D-05）
    provenance: MemoryProvenance
class MemorySink(Protocol):              # * 域层实现；Runtime hook 只 emit
    def emit(self, event: MemoryEvent) -> None: ...     # 路由 L2/L3 在域层，Runtime 不认识 universe
# 落点：新增一条 _AFTER_TURN_HOOKS（turn_service.py:1609 旁）读 _AfterTurnContext 发 MemoryEvent；
#   形态 A 无 sink 消费者注册 → 退化 1:1（现 raw write_memory 继续，memory_writer.py:119）。
# 域层 CompanionWorldMemorySink：user_fact→universe_l3.append_fact（append-only §6.4）；relationship→per-account L2。

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

签名边界说明：以上冻结的是**接缝形状**（字段/类型/方向/事务归属），`fact_type` 枚举全集、错误码、索引/唯一约束、backfill 算法仍属 M2-0 正式后端规范产出（§12 M2-0），不在此处定死。

## 8. 渐进式重构顺序

- **R0 冻结边界与保护现状**：微信/App 单 Agent/Memory/计费补 characterization test（只能钉确定性接缝：prompt 组装/session 轮转/持久化/moderation，钉不住模型输出）；写本 ADR；**冻结** resident 计费口径（钱包上迁 platform_user，D-14；配额按真人聚合，D-09；建号解耦容量，D-07）——**口径在此冻结，代码实现抽为独立先行里程碑 R1a（见下、§12 M1）**；冻结 App auth 不接收旧 code（`invite_code`/`campaign_code` 仅从 App DTO 移除，Web/运营 referral/campaign 另议）；接线 import-linter；**主动消息防 N× 安全阀（任务 T0-1～T0-5，见 §11.8）**。
- **R1 计费上迁（先行、独立发布）+ Agent Runtime facade**：
  - **R1a 计费/配额锚点上迁（从原 R2「一并解耦」抽出，独立先行）**：把钱包唯一键上迁 `platform_user`（D-14）、daily 原子预占 + RPM 锁键迁 `platform_user`（D-09）、容量脱建号计数（D-07）。**不依赖任何 universe 表**，可在领域层之前单独上线并发布。**注（口径修正 2026-07-19）**：建号允许每真人 ≤10 account，故存量可能已有多钱包老用户，**非无条件零合并**——须先跑生产预检、对多钱包用户自动合并（D-14 迁移方案），单钱包用户直迁。PG 并发为发布闸（§9）。仍必须先行：多居民上线后变 N 世界 × N 居民钱包，合并成本远高于现在。
  - **R1b Agent Runtime facade**：**仅新建** `AgentRuntimePort` + adapter 供**新代码**调用；**不**强推 legacy 微信/App 路径改走 facade（`turn_service` 最有状态，待第二个真实消费者再回收）。P1 实际只需 Runtime 三件事：不赠权建号、`conversation_id→runtime_account_id` 解析、复用现有 turn。
- **R2 P1 Companion World**：universe/template/resident/ai_conversation；幂等 bootstrap + 1–10 居民确认；resident runtime 创建不重复赠权（**站在 R1a 已上迁的计费基座上，仅把容量真相落到 `universe_residents.status` 表**）；显式 AI conversation history/turn（不接受客户端 `account_id`）；老用户 primary account backfill（含多 account 老用户，D-08）；L3 首刀（八字段上提 universe 存储，§6.5）。
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

**双后端与迁移**：SQLite 聚焦 + PG 真实事务均过；backfill 可重复运行、不重复建 world/resident/grant；关 P1 flag 可安全退回 legacy 单 Agent。

**PG 保真门禁（硬，2026-07-19；基础设施已就位）**：凡涉及**锁 / 事务 / 钱包扣款 / 容量 / 配额预占**的 3.0 新逻辑，**必须有 PG lane 测试**，**SQLite 档通过不作数**。理由：SQLite 无法复现 `FOR UPDATE`/advisory lock/真事务隔离语义（D-12）。**注（核对 2026-07-19）：PG lane 与阻塞式 CI 已存在**——`Makefile:31` `test-pg`、`.github/workflows/tests.yml:35` `pg-tests`（无 `continue-on-error`，push/PR 到 main 强制）。故 M0 **无需**「转阻塞式」动作，只需**为 M1/M2 新逻辑补 PG 用例**。SQLite 仅保留 dev 秒级反馈（`make test-unit`）+ 生产回滚通道；**是否彻底删除 SQLite 为独立清爽性任务、不阻断本重构**（后端分歧现仅 `_backend.py` 545 行 + `is_postgres()` 13 处，删除收益有限而代价是每日测试速度/本地零配置，条件成熟再单独评估）。

---

## 10. 待产品冻结项

1. 初始预设居民数量与版本策略。
2. 老用户迁移后是否补充其他居民；已持多 account 老用户的居民映射策略（D-08）。
3. 用户能否主动移除 active AI（须与 AI 自主离开区分）。
4. **【已冻结 = 保留豁免】** legacy resident 豁免离开（D-08）；须修订客户端 PRD:634「来源不构成豁免」记为已知偏离。
5. **【已冻结 = platform_user】daily/RPM 配额按真人聚合，多居民共享一套配额（D-09）**；daily 改原子预占+回滚、RPM advisory 锁迁 platform_user 键；schema 迁用户级、override 用户级优先、退款=仅成功计费扣、reservation 与入站幂等同事务、崩溃 reservation TTL 回收（D-09 精化 2026-07-19）。
6. App scope 是否纳入定时 dreaming 扫描（§6.6）。
7. AI 动态首版是否只做文字及生成频率；mailbox 触发/冷却/过期/待处理上限。
8. departure 证据窗口、cooldown、危机 freeze 与后台纠错 SOP。
9. B 同时可持有多少好友世界 visit；邀请码兑换是否需 A 二次确认；真人历史保留/删除期限。
10. **【已冻结】L3 共享范围 = 关于用户的沉淀记忆全量共享、无字段级白名单；但不共享聊天原文与检索（D-05，边界精化 2026-07-19）**。共享=对用户的沉淀认知（USER/MEMORY 事实段、派生画像）；不共享=各居民私聊逐字稿（`messages`）与 `tool_evidence_replay` 检索。产品意象：居民“八卦”对用户的认知，读不到彼此私聊。用户对 L3 的可见/编辑/清空为产品交互层、另做。
11. **【已冻结 = A】App 主动消息投递面 = 拉取式通知/收件箱（D-13、§11.3）**；细分待定：通知收件箱与世界 Feed 的入口是否合并、未读/红点/清理策略（产品交互）。
12. 真人级 proactive 的**发声人（resident）选择**策略（默认：该 universe 内最近互动的 active 居民；见 §11.4）——按什么选、能否用户指定"谁来找我"。
13. 真人级触达在 App-only 用户上的**首版是否启用**（默认 fail-closed，先只微信 legacy 居民；见 §11.6 安全阀）。

---

## 11. 主动消息（proactive）子系统改造

> Review 结论：proactive 从未按多形态/多居民 review 过。`app/proactive/` 整层按 `account_id` 键，全目录零引用 `platform_user`/`account_owner_bindings`/`account_user_meta`/`relationship_state`；真人↔account 映射（`account_owner_bindings` `_core.py:464`）存在但从不被读。故一个真人 N 居民 = N 条独立管线 + N 份独立预算。类别登记表 `app/proactive/contract/categories.py:17-127`；唯一出口 `app/proactive/delivery/outbound.py`；策略/预算 `app/proactive/delivery/policy.py:145`；调度 `scripts/run_proactive_scheduler.py` + `app/proactive/orchestration/scheduler.py`。

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
- **落点**：唯一出口 `dispatch_proactive_text`（`outbound.py:362`）新增一条投递分支——解析到 App 目标时写一行通知记录（新表，人级键 `platform_user_id` + 发声 `resident_id` + `category` + `status(unread/read)` + payload + `idempotency_key` 复用现有唯一约束思路），而非返回 `channel_not_proactive` 取消；`_select_route`（`common.py:40`）相应放行 App 目标到该分支。
- **客户端**：新增 `GET /api/v1/notifications`（未读/游标分页）+ 已读回执，与 `/worlds/home/feed` 分离。
- **覆盖范围**：收件箱同时承接 App 用户的 per-resident（reminder/commitment，轴一 L2）与真人级（拉活/邀请）消息——投递面与归层正交，两类都可能落 App 用户、都进收件箱。

### 11.4 真人级触达上提产品域层：三接缝 + 发声人

真人级类别从 Runtime 侧上提到 `companion_world` 域层：
1. **预算聚合**：`evaluate_outbound_policy`（`policy.py:145`）的计数（`get_outbound_daily_usage` `:270`、`count_total_...` `:309`、avoidance `:360-390`）对真人级类别跨 `account_owner_bindings` 扇出到 `platform_user`——**与 D-09 同源、同批解**。per-resident 类别保持 per-account。
2. **扫描单位**：真人级 due 队列（`list_due_reactivation_candidate_accounts` `proactive.py:2213`、`list_due_proactive_account_states` `:2183`、候选存 `proactive_account_state.metadata_json`）改按 universe 聚合，每人每窗口 ≤1。per-resident due 队列（`reminders`/`proactive_commitments`）不动。
3. **发声人选择（新职责）**：真人级消息须由某居民说出。默认策略 = 该 universe 内**最近互动的 active 居民**（备选：关系阶段最高 / 用户指定"谁来找我"）；作为待冻结产品项（§10.12）。

per-resident 义务（reminder/commitment）仍留在现有 Runtime 侧管线，不上提。

### 11.5 活跃判断跨渠道聚合（已存在 bug，形态 B 放大）

拉活资格用 `get_account_last_inbound_at(channel=WEIXIN)` + 微信专属 touch-state（`touch_state.py:42`，仅微信入站刷新）→ **App 活跃用户在这套里看似沉默**，会误触发拉活且投递不到。真人级"是否活跃"须跨该真人**所有居民 + 所有渠道（含 App inbound）**聚合判断。

### 11.6 分期与防 N× 安全阀

- **R0（随冻结立即）安全阀**：真正聚合前先加人级闸——真人级类别仅对 **legacy/primary 居民**触发，其余居民跳过（改 `_select_route`/dispatch 前置一处，成本极小，先堵 N 倍打扰）。App-only 用户真人级 proactive 首版可 fail-closed（沿用 `reactivation_dispatch_enabled=False` `config.py:169` 保守姿势）。
- **R2（P1 随建号解耦）**：预算与活跃判断跨 `account_owner_bindings` 聚合到 `platform_user`（与 D-09 同批）。
- **R3+（随 Feed/域层落地）**：真人级 proactive 整体上提 `companion_world` 域层 + App 通知收件箱投递面 + 发声人选择；per-resident reminder/commitment 留 Runtime 管线。

### 11.7 新增不变量与测试点

- 一个真人 N 居民：真人级类别（拉活/邀请）每人每窗口 **≤1 次**，非 N 次（PG 并发下亦然）。
- per-resident（reminder/commitment）保持每居民独立，不被人级闸误杀。
- App 目标不再命中 `channel_not_proactive` 取消，而是落通知收件箱一行；微信 legacy 仍走 `send_weixin_text`。
- App 活跃用户不被判为沉默（跨渠道 last-active）。
- 通知收件箱与世界 Feed 数据面分离：通知不进 Feed、Feed 不进通知。

### 11.8 R0 / R3 具体任务项

> 规范：schema 改动**新增迁移函数追加 `_MIGRATIONS`**（勿用启动期 `_ensure_column`，见 `_core.py`）；新 router 需在 `tests/conftest.py` 的 `fresh_db`/`client` 两 fixture 各补一行 `patch("app.routers.<模块>.settings", ...)`；并发正确性以 PG 用例为证、SQLite 只验功能（D-12）。真人级类别 = `NEW_USER_REACTIVATION` + `CONTENT_INVITATION` + `COMPANION_FOLLOWUP/account_check`；per-resident 类别 = `USER_REMINDER` + `COMPANION_FOLLOWUP/commitment` + `CONTENT_INVITATION_RESPONSE` + `TASK_RESULT`。

**R0 — 防 N× 安全阀（最小改动，随冻结即时上；不动 per-resident 类别）**

- [ ] **T0-1 primary 判定 helper**：新增 `is_primary_resident_account(account_id) -> bool`——经 `account_owner_bindings`（`_core.py:464`）解析 `platform_user_id`，与 `get_first_active_account_for_user`（最早 active binding，同 D-08 legacy 映射口径）比较。**单 account 用户恒 True**（形态 A/微信零回归：闸对其为 no-op）。放 `app/proactive/contract/common.py` 或 platform helper，proactive 侧只调用。
- [ ] **T0-2 人级闸（生成侧，省 LLM）**：真人级候选生成前置 `is_primary_resident_account` 过滤——非 primary 居民不生成候选。落点：reactivation 资格 `_new_user_reactivation_eligibility`（`planning.py:140`）、account-check 扫描 `scan_due_proactive_account_checks` 的 `due_accounts` 循环（`planning.py:458`）、content-invitation 生成（`recall/content_invitation.py`）。
- [ ] **T0-3 人级闸（投递侧，防御纵深）**：真人级 dispatch 入口再校验一次——`dispatch_reactivation_candidate`（`dispatch.py:70`）、`execute_account_check_decision`（`account_check.py`）。非 primary 直接 skip（非 cancelled 计费）。
- [ ] **T0-4 App-only fail-closed**：真人级生成前若该真人**无 proactive-capable 渠道**（无微信 binding，仅 App）则跳过——避免为投不出去的消息烧 LLM。沿用 `reactivation_dispatch_enabled=False`（`config.py:169`）保守默认。
- [ ] **T0-5 characterization + 多居民回归测试**：先钉现状（单 account 微信用户：reminder 触发、reactivation 每窗口一次、budget/quiet 生效）；再加多居民用例（一人 2 居民：真人级仅 primary 触发一次、per-resident reminder 两居民各自触发、人级闸不误杀 per-resident）。

**R3 — App 拉取式通知收件箱 + 真人级触达上提域层**

- [ ] **T3-1 新表 `app_notifications`（迁移函数追加 `_MIGRATIONS`）**：人级键 `platform_user_id`(FK) + `resident_id`/`runtime_account_id`(发声) + `universe_id` + `category` + `payload_json`(title/body) + `status`(unread/read) + `idempotency_key` UNIQUE + `created_at`/`read_at`；索引 `(platform_user_id, status, created_at)`。与 `universe_posts`（世界 Feed）**分表**。
- [ ] **T3-2 投递分支（经 delivery adapter，遵 §7.3 方向）**：`dispatch_proactive_text`（`outbound.py:362`）解析到 App 目标时，**发 typed proactive intent 给 `AppInboxAdapter`，由 adapter 写 `app_notifications`**（幂等键复用现有唯一约束思路），而非返回 `channel_not_proactive`；`_select_route`（`common.py:40`）放行 App 目标到该分支。**Runtime/proactive 核心不直接 import/写 `app_notifications`**（§7.3 接缝③修依赖反转，`universe_id`/`resident_id` 由 adapter 填）。**微信 legacy 走 `WeixinAdapter=send_weixin_text`（`outbound.py:288`）不变。**
- [ ] **T3-3 DB helpers**（新 `app/db/notifications.py` 或扩 `proactive.py`）：`insert_notification`(幂等)、`list_unread_notifications(platform_user_id, cursor)`、`mark_read`。
- [ ] **T3-4 API router `app/routers/app_notifications.py`**：`GET /api/v1/notifications`（未读/游标分页，从 session 解析 `platform_user`，**不接受 `account_id`**）+ `POST /api/v1/notifications/{id}/read`；与 `/worlds/home/feed` 分离；`Cache-Control: no-store` + 稳定 code/request_id/server_time。**conftest 双 fixture 补 settings patch**。
- [ ] **T3-5 真人级上提 `companion_world` 域层**：universe 级 due 队列（重键或后聚合 `list_due_reactivation_candidate_accounts` `proactive.py:2213` / `list_due_proactive_account_states` `:2183` / 候选 `proactive_account_state.metadata_json`）；预算跨 `account_owner_bindings` 聚合到 `platform_user`（`evaluate_outbound_policy` 的计数 `policy.py:270/:309/:360-390`，**与 D-09 同批**）。
- [ ] **T3-6 发声人选择 helper**：默认该 universe 内**最近互动的 active 居民**（策略待冻结 §10.12）；输出 `resident_id` 供 T3-1 通知行。
- [ ] **T3-7 跨渠道活跃聚合**：修 §11.5——真人级"是否沉默"跨该真人所有居民 + 所有渠道（含 App inbound）判断，替换 `get_account_last_inbound_at(channel=WEIXIN)` + 微信专属 touch-state（`touch_state.py:42`）单渠道口径。
- [ ] **T3-8 正交 + 并发测试（PG）**：App reminder(per-resident)→收件箱；微信 legacy 拉活(真人级)→`send_weixin_text`；真人级→App 用户每人每窗口**收件箱一行、非 N 行**；通知/Feed 数据面互不串；feature-flag 关闭可退回（App 真人级 fail-closed、微信不受影响）。

---

## 12. 开发计划（里程碑）

> 本节把 §3 冻结决策 + §8 重构顺序落成可交付里程碑。**关键重排（2026-07-19）**：原 R2「建号一并解耦」中的计费/配额上迁抽为**独立先行里程碑 R1a/M1**，趁 fork 最浅（多居民未上线）先于领域层单独上线；多钱包老用户走预检+自动合并（理由见 R1a、D-14）。

**里程碑 ↔ §8 R 映射**

| 里程碑 | 对应 R | 目标 | 产品冻结门槛 |
|---|---|---|---|
| **M0** 冻结·安全阀·脚手架 | R0 | 保护现状 + 防 N× 打扰 + 分层门禁 | 无，可立即开工 |
| **M1** 计费/配额锚点上迁 | R1a | 钱包/配额/RPM 锚 `platform_user`，零迁移 | 无，可立即开工 |
| **M2** Runtime facade + P1 多居民 | R1b+R2 | universe/resident/conversation + L3 首刀 | §10.1/.2/.6 |
| **M3** Feed + 通知收件箱 + 真人级 proactive 上提 | R3 | outbox + App 收件箱 + 去 N× | §10.7/.11/.12/.13 |
| **M4** Lifecycle + Mailbox | R4 | offline+farewell 原子事务、信箱 | §10.3/.4/.8 |
| **M5** Visit + Human Chat | R5 | 三 slot/高熵 code/ACL、真人分表 | §10.9 |

M2–M5 不并行，每阶段无下一阶段仍是完整可回滚体验。**M0/M1 无产品门槛、code-ready；M2+ 对应 §10 冻结项未定不进编码。**

### M0 — 冻结·脚手架·现状 characterization（可立即开工）

> 修订（Codex review 2026-07-19）：①PG lane + 阻塞式 CI **已存在**（`Makefile:31`/`tests.yml:35`），删除「转阻塞式」任务。②分层边界用 **stdlib AST 测试**,不引入 import-linter（CLAUDE.md 禁擅自加依赖；如需 import-linter 另行批准）。③**防 N× 安全阀移到 M2**——它必须 World-aware 判 primary（needs `universe_residents`）；用「最早 active account」判 primary 会误伤今天的多 account 用户、并复用文档要消除的默认账号假设。M0 无 resident 建号路径 → 此刻不存在 N× 风险。

> **交付状态（2026-07-19）：M0 全部完成。** 见 `tests/test_layer_boundaries.py`（脚手架存在性 + AST 边界门）与 `tests/test_characterization_baseline.py`（M1 安全网）。普查确认接缝 1/2/4/7/8/9 已被现有测试充分钉住，M0-1 只补三处 M1 会翻转的确定性现状（消息幂等、一人多号钱包/赠权、一人多号 daily 计数）；M0-5 现状由 `test_reactivation`/`test_proactive_*`/`test_reminders*` 等既有 115 用例承载，不造冗余测试。

| 交付 | 落点 | 出口标准 | 状态 |
|---|---|---|---|
| characterization 测试钉现状 | 微信/App 单 Agent：prompt 组装、session 轮转、持久化、moderation、钱包扣款、daily/RPM 计数 | 确定性接缝全绿（模型输出不钉） | ✅ 既有覆盖 + `test_characterization_baseline.py` 补 3 处 M1 缺口 |
| 分层边界测试（stdlib AST，不加依赖） | 禁 `app.domains.companion_world.*` import `app.db.*`/`app.turn_service`，纳入 CI | CI 阻塞（D-12），零新依赖 | ✅ `test_layer_boundaries.py`（负向探针验证门可失败） |
| 目录脚手架 | `app/domains/companion_world/`、`app/agent_runtime/`、`app/platform/` 空骨架 | AST 边界测试能识别层 | ✅ 四个空骨架包就位 |
| 现状 proactive characterization | 单 account 微信用户：reminder 触发、reactivation 每窗口一次、budget/quiet 生效 | 钉住现状（防 N× 安全阀连同多居民回归下沉 M2） | ✅ 既有 proactive 套件（115 用例）即现状钉板 |

### M1 — 计费/配额锚点上迁（可立即开工，零迁移窗口；依赖 M0 钱包/配额 characterization）

范围锁：`billing.py` + `rate_limiter.py` + `turn_service.py` daily 路径 + 1 迁移函数。

| 交付 | 落点 | 出口标准（PG 必过） |
|---|---|---|
| **D-14** 钱包唯一键 `account_id → platform_user_id` | 迁移函数追加 `_MIGRATIONS`（SQLite 表重建 / PG `ALTER CONSTRAINT`，分 expand/contract）；`billing.py` ~10 处 `WHERE account_id` 改内部 account→user→wallet 解析 | 多钱包用户预检+自动合并、单钱包直迁；对外 API 保 `account_id` 入参，读侧 `turn_service:1924`/`relationship_state:130,182`/web/admin 不动 |
| **D-14** 赠权按真人一次 + no-grant 建号 | `create_ai4all_account_for_user` 拆无赠权路径 | 居民 2…N 不重复赠贝壳、不拆余额 |
| **D-09** daily 原子预占 + 回滚 | `turn_service.py:1065/1185` 读—处理—+1 → 原子预占 | 消除 TOCTOU；PG 并发不双记 |
| **D-09** RPM 锁 key 迁 `platform_user_id` | `rate_limiter.py:52` advisory 锁键 | 多居民共享一套 RPM |
| **D-07** 容量脱建号计数 | `billing.py:2090` `COUNT(*) binding >= 10` 与容量解耦 | 满 10 后仍可 offline 补新（历史 binding 不占位） |
| PG 并发测试 | 跨居民同时扣款不双扣、`idempotency_key` 唯一守住 | §9 硬门禁 |

### M2 — Runtime facade + P1 多居民（待冻 §10.1/.2/.6；**前置 M2-0 端口契约**）

**前置 M2-0（编码前必做）**：定稿 §7.3 四接缝签名（初稿已写入「方法签名初稿」2026-07-19，均核过现有入口类型）+ 产出**正式 P1 后端规范**。规范已定稿 [`companion_world_p1_backend_spec.md`](./companion_world_p1_backend_spec.md)（`fact_type` 枚举全集 + 路由矩阵、5 张 P1 表 DDL/索引/唯一约束、DTO + 安全契约、稳定错误码表、**锁顺序细则 §2.7 + 老用户 backfill 分步伪码 §2.8**）；**M2-0 前置门清零**（剩余 offline 原子事务/visit 双世界锁随 M4/M5）。客户端 gap 文档只作输入、不替代后端规范。

本体：`AgentRuntimePort`（R1b）+ universe/template/resident/ai_conversation 表 + 幂等 bootstrap + 1–10 确认事务（world row lock，容量真相 = `status='active'`）+ resident 建号走 M1-5 的 no-grant/no-cap 内部路径（world lock 保护，容量此处校验）+ 显式 conversation history/turn（不接受客户端 `account_id`）+ **L3 首刀**（`universe_storage.py` + `read_universe_context()` + `prompt_builder.extra_blocks` 加性注入；八字段后端改指 universe 存储，§6.1/6.5，append-only §6.4）+ 老用户 backfill（D-08）+ **防 N× 安全阀（从 M0 下沉）**：World-aware 判 primary/发声人（凭 `universe_residents`，**不用「最早 account」**），仅对明确属于 Companion World 的 resident 生效，形态 A/多 account 老用户不受影响。出口：跨 resident 不串线、L3 同世界可见他人世界不可见、真人级每人每窗口 ≤1、关 flag 退回 legacy。

### M3–M5（各待对应 §10 冻结）

- **M3**：`universe_posts` + 事务 outbox（领域状态与 outbox 同事务、worker 幂等）；App 通知收件箱 **T3-1…T3-8**；真人级 proactive 上提域层（预算跨 binding 聚合到 platform_user、universe 级 due 队列、发声人选择、跨渠道活跃聚合修 §11.5）。
- **M4**：cooldown/audit/safety freeze、last-resident 保护、offline+farewell+read-only 原子事务、letters 接受事务（active `<8` 投递 / `<10` 接受）。
- **M5**：三 slot + 高熵 code + 绝对过期 + visit ACL；`human_conversations`/`human_messages` 分表（D-11）、到期只读、举报/拉黑。

### 关键风险与门槛

1. **产品冻结项**是 M2+ 硬前置：§10 未定不进对应里程碑编码；M0/M1 无此门槛。
2. **客户端口径冲突**：gap-analysis §4.2「默认隔离/白名单」与 D-05「全量共享沉淀记忆」相反，M2 落 L3 前需镜像对齐（本轮不动客户端仓库）。
3. **money 路径**：M1 是唯一动扣款的里程碑，PG 并发测试是发布闸，SQLite 绿不作数（§9）。
4. **App scope 漏扫**（§6.6）：`__app_active__` 不在 `DEFAULT_ACTIVE_SESSION_KEYS`，多居民后每居民首条卡顿——M2 需决策是否纳入定时扫描。

---

## 13. 参考

- 工作底稿（完整讨论与逐条代码核查）：`docs/tmp/companion_app_backend_refactor_handoff.md`
- P1 后端实现规范（fact_type / schema / DTO / 错误码，M2-0 交付物）：[`companion_world_p1_backend_spec.md`](./companion_world_p1_backend_spec.md)
- 客户端数据/API 初稿：[`private_world_backend_gap_analysis.md`](../../../ai4all-companion-app-rn/docs/tech_design/private_world_backend_gap_analysis.md)（§4.2 共享上下文口径需按 D-05 镜像更新）
- 客户端 PRD 与客户端架构：`ai_companion_universe_prd.md`、`mobile_client_architecture.md`
- 现存实现依据（核对基线 `main@abe6e2b`）：`app/prompt_builder.py`、`app/user_profiles.py`、`app/profile_storage.py`、`app/db/_core.py`、`app/agent_self_state.py`、`app/dreaming.py`、`app/memory_writer.py`、`app/rate_limiter.py`、`app/db/billing.py`、`app/channels.py`、`app/routers/app_api.py`
