# 系统 3.0 重构简报 — Agent Runtime 分层 + Companion World 产品领域层

> 面向：项目干系人 / 新加入者的快速通读。权威口径以 ADR
> [`tech_design/companion_world_3_0_refactor_design.md`](tech_design/companion_world_3_0_refactor_design.md)
> 与 P1 规范 [`tech_design/companion_world_p1_backend_spec.md`](tech_design/companion_world_p1_backend_spec.md)、M3 规范 [`tech_design/companion_world_m3_backend_spec.md`](tech_design/companion_world_m3_backend_spec.md)、M4 规范 [`tech_design/companion_world_m4_backend_spec.md`](tech_design/companion_world_m4_backend_spec.md) 为准；本文只做浓缩。
> 更新日期：2026-07-23
> 后续开发统一从上位 ADR 的“接手说明”开始；当前执行计划为 [`plans/companion_world_m4_implementation_plan.md`](plans/companion_world_m4_implementation_plan.md)。

---

## 一、背景：为什么要做这次重构

现有系统（1.0 微信单 Agent 陪伴 → 2.0 平台化底座）的核心业务模型始终是 **1:1**：
一个真人 → 一个默认 account → 一份 Soul/Memory/session/turn。

新产品「**朝夕相伴** App」把这个前提推翻了——它要求 **一个真人拥有一个私人世界，世界里有 1–10 位平等的 AI 居民**（无主角色），每位居民有独立的关系、会话、人设与记忆。系统从 **1:1 变成多对多**。

矛盾的根在于：今天的 `account` 一个概念同时背着 **五重身份**——AI 关系运行时 / 产品默认账号 / Profile-Memory owner / 计费钱包锚点 / 渠道绑定对象。在单 Agent 下它们恰好是同一件事；一旦一个真人有多位居民，这五件事就不再重合，处处出 bug（App API 依赖「第一个 account」、建居民会重复赠权+拆散余额、满 10 个 binding 后永久无法补新居民……）。

---

## 二、核心思路

**一句话：不 fork、不重写，在稳定的 Agent Runtime 之上加一层产品领域层。**

- **分层，而非分叉**（ADR D-01/D-06）：把「通用 Agent 运行能力」沉为 **Agent Runtime（形态无关 form-agnostic）**；把「这个产品的实体 / 权限 ACL / 共享范围 / 生命周期」上提为 **Companion World 产品领域层**。Runtime 不认识「世界」「居民」，只认识「一个隔离的关系运行容器」。
- **关键判断（已核对代码，决定了改造面）**：三层 context 在现有实现里**物理上早已分散在不同载体**——L1 人设/使命、L2 关系状态**天然按 account 独立**、无需为多对多改造；真正要新建的**只有 L3（关于用户的沉淀记忆）的「世界级共享」承载 + 一个加性注入块**。所以「多对多」听起来很大，实际内核改动远比想象小。
- **形态显式化**（D-03）：把「共享/隔离范围」变成一个显式维度——**A 微信**（1 真人↔1 Agent，无世界）、**B 朝夕相伴**（1 真人↔1 世界↔N 居民，共享 L3、独立 L1/L2）、**C 未来**（不提前设计，只要求内核不写死形态假设）。

依赖方向严格单向：`API 适配层 → 产品域层 → AgentRuntimePort → 现有实现`，域层旁挂平台服务；**Runtime 绝不反向依赖 Companion World**（由 CI 分层门禁强制）。

---

## 三、整体方案

### 3 层 context 模型（D-04）
| 层 | 内容 | 基数 | 3.0 处理 |
|---|---|---|---|
| **L1** | 人设 / 使命 | 1 模板 → N 关系 | 已 per-account 独立，白拿 |
| **L2** | 关系状态 | 1 per 用户×Agent | 已 per-account 独立，白拿 |
| **L3** | 关于用户的沉淀记忆 | **1 per 世界，居民共享** | **唯一真正新建**：append-only typed fact + 单 writer compact；共享「对用户的认知」，不共享各居民私聊逐字稿 |

### 里程碑（M0→M5，串行、每阶段独立可回滚）
| 里程碑 | 目标 | 产品冻结门槛 |
|---|---|---|
| **M0** 冻结·脚手架·护栏 | 保护现状 + 分层门禁 | ✅ 已交付 |
| **M1** 计费/配额锚点上迁 | 钱包/配额/RPM 锚到 `platform_user`（趁多居民未上线先独立发布） | ✅ 已交付；m0031 收口 override/TTL |
| **M2** Runtime facade + 多居民 | universe/resident/conversation + L3 首刀 | ✅ 代码已交付，default-off |
| **M3** Feed + 通知收件箱 + 真人级 proactive 上提 | outbox + App 收件箱 + 去 N× 打扰 | ✅ M3-0…M3-6 已完成，default-off |
| **M4** 生命周期 + 信箱 | offline+farewell 原子事务、信箱 | ✅ M4-0…M4-6 已完成 |
| **M5** 访客 + 真人聊天 | slot/高熵邀请码/ACL、真人分表 | M5-0…M5-3 完成，default-off |

> **关键重排**：把「计费/配额上迁」从建号解耦里抽成 **独立先行的 M1**——趁 fork 最浅（多居民还没上线）先把钱包/配额从 account 迁到「真人」维度，零迁移窗口；少量多钱包老用户走**预检 + 自动合并**。

### 贯穿全程的工程手法
- **characterization test 先钉现状**，再逐步抽边界（避免重构悄悄改行为）。
- **分层边界用 stdlib-AST 门禁**（不引入 import-linter 依赖）纳入 CI 阻塞。
- **schema 改动一律追加迁移函数**到 `_MIGRATIONS`（`PRAGMA user_version` 跟踪），不用启动期补丁。
- **双后端零分叉技巧（"Option A"）**：钱包/配额上迁保留旧 `UNIQUE(account_id)` 约束、只**加局部唯一索引**锚真人键——避开 SQLite 12 步表重建 / PG DROP CONSTRAINT 的后端分叉。
- **并发正确性以 PostgreSQL 用例为证**（SQLite 单写者不作数）；L3 用 append-only + 单 writer 规避多居民并发写覆盖。

---

## 四、进度与发布状态（截至 2026-07-23）

> PR #45、PR #46 均已合并。M4 分支 `feat/companion-world-m4` 已校准到 `origin/main@e230844`；M4-1=`0815740`、M4-2=`792d4fe`，M4-3…M4-6 已完成但尚未提交/推送。P1/M3/M4 feature flag 均默认关闭。

### 已交付

- **M0/M1/M2-A/B1**：分层门禁、真人级钱包/配额、P1 五表、Runtime 端口与 L3 加性读注入均已交付。
- **C0 `7471ca8`**：m0030 初始候选 rank/模板关系唯一约束；`__app_active__` 纳入每日 Dreaming。
- **C1 `2f419de`**：World 领域契约、SQL repository、runtime account + resident + conversation 同事务创建。
- **C2 `ec980ad`**：flag-default-off 的 world API、四模板导入器、legacy 全 binding backfill、新用户 `account:null` auth 分支。
- **C3 `f5fd3c5`**：conversation list/history/text turn、owner ACL、防枚举、PG 非阻塞 advisory single-flight、L3 同世界注入。
- **C4 `244d7a7`**：Dreaming `fact_type`、fail-closed typed sink、resident→universe L3 append、exact-normalized compact 与 central-only single writer。
- **C5 `a47d41e`**：真人级 proactive 防 N× 安全阀；仅 legacy primary 可触发，App-only fail-closed；reminder/commitment 仍 per-resident。
- **C6 `6000c0b`**：发布运行手册、测试结果和文档收尾。
- **M3-1**：m0033 三表、Feed/outbox/通知 reservation 存储原语与领域契约。
- **M3-2**：用户文字直接发布、post+outbox 同事务、owner Feed API、opaque cursor 与 default-off Feed flag。
- **M3-3**：独立中心 world-content scheduler、北京双窗口/7 日 eligibility、确定性 AI 作者、slot lease/retry/no-catch-up、outbox worker、heartbeat 与批量游标。
- **M3-4**：App 通知 owner API、typed AppInboxAdapter、per-resident reminder/commitment 入箱、显式已读、7/30 天及 200 条 central cleanup；微信继续优先，真人级 App-only 仍关闭。
- **M3-5**：真人级 due/预算/活跃按 owner 聚合，真实微信 legacy primary 优先，App-only 双 flag + 滚动 24 小时 reservation，投递前 speaker 重选/锁定。
- **M3-6**：三 flag 灰度/回滚运行手册、central 单例约束、Feed/outbox/通知/真人级 heartbeat、只读对账 SQL 与最终门禁。
- **M4-0**：已冻结用户不得移除 active resident、legacy 永久豁免、60/30/3/14/7/30 lifecycle 策略、人工不可逆提交、最后居民/crisis 保护，以及 mailbox `<8/<10`、open=1、30 天间隔/TTL；m0034/schema/API/锁序/flags/PG 门禁已形成实现级规范。
- **M4-1**：已追加 m0034 farewell/lifecycle/mailbox schema，交付 owner-scoped DB 原语、纯领域 DTO/ports 与三个 default-off flag；未接 scheduler、API 或不可逆 live 行为。
- **M4-2**：已交付 runtime-scoped inactivity/value mismatch/crisis/severe-abuse evidence、cooldown/recovery/last-resident 复核、脱敏 staff/admin review queue 与独立中心 scheduler heartbeat；证据不存原文，commit 保持关闭。
- **M4-3**：已交付 full-admin approve/correct、event policy snapshot 重校验、共用 `conv:` 锁，以及 `offline + farewell + read_only + outbox + action` 原子事务；重复 approve 恰好一条 farewell/outbox，纠错只能隐藏 farewell、不复活。
- **M4-4**：已交付 HMAC 签名 catalog、`<8`/open=1/30 天确定性投递、scheduler/request-time expiry、owner 私密 list/detail/unread/read/defer/decline 与 PG 双 scheduler 单 open letter。
- **M4-4 门禁**：M4 聚焦 SQLite `27 passed / 3 skipped`、PG `30 passed`；unit `568 passed`；SQLite 全量 `1492 passed / 17 skipped`；PG 全量 `1504 passed / 5 skipped`。
- **M4-5**：已交付 owner accept；world/letter/catalog/template 锁内实时复核 expiry、版本和 active `<10`，一次提交 no-binding/no-grant runtime + mailbox resident + conversation + accepted letter，重放返回同一 resident。
- **M4-5 门禁**：mailbox 聚焦 SQLite `15 passed / 4 skipped`、PG `19 passed`；unit `568 passed`；SQLite 全量 `1498 passed / 20 skipped`；PG 全量 `1513 passed / 5 skipped`。PG 覆盖 double accept、第 10 位与常规创建竞争、accept-vs-expiry。通知/欢迎 turn/补偿框架与 catalog-retire 专项竞态压测留后续收口。
- **M4-6**：复用既有 admin ops/heartbeat，补齐 M4 灰度、只读对账和回滚手册，以及 accept-vs-catalog-retire PG 竞态；没有新增产品行为、endpoint、配置或指标系统。
- **M4 最终门禁**：lifecycle+mailbox SQLite `25 passed / 7 skipped`、PG `32 passed`；unit `568 passed`；SQLite 全量 `1498 passed / 21 skipped`；PG 全量 `1514 passed / 5 skipped`；compileall/diff check 通过。

### 当前发布闸

- `make test-unit`：**568 passed / 926 deselected**。
- `make test`（SQLite）：**1479 passed / 15 skipped**。
- `make test-pg`（PostgreSQL）：**1489 passed / 5 skipped**。
- `git diff --check`：通过。PG 继续作为容量竞争、single-flight、配额和 L3 并发的权威。
- 非阻断告警：既有 Pydantic/FastAPI deprecated warning；`test_image_turn` mock 有一次 `asyncio.to_thread` 未 await RuntimeWarning，无失败。

### 生产状态与阻断项

- 开发分支代码迁移已到 **m0034**（新增 lifecycle/mailbox 四表并扩展 farewell post），但本仓库没有生产执行证据；不能把“代码已就绪”写成“生产已迁移”。
- `COMPANION_WORLD_P1_ENABLED=false` 仍是默认值；尚未授权开启。
- API/auth、L3 后台、proactive safety 已拆成三个正交开关；发布期可在保留防 N× safety 的同时独立停止 L3，回滚不再需要临时发版。
- 正式四位首发角色 manifest（名称、头像、简介、三个标签、persona、版本）尚未提供，代码没有编造默认人设。
- 支持 `account:null` + world bootstrap 的客户端最低版本/上线窗口尚未提供。
- 客户端文档仍需同步“L3 共享沉淀记忆”和“legacy resident 离开豁免”两项冻结口径。
- D-06 composition 已移出 Runtime；D-09 override 已上迁 `platform_user`，reservation TTL 已接 central scheduler。开 flag 前仍须完成遗留 override 对账。
- 生产模板导入、带固定 cutoff 的 backfill dry-run/实跑、数据对账尚待按 [后台管理说明](guides/admin_guide.md#companion-world-p1-发布运行手册) 现场执行。

因此当前结论是：**M2-C 与 M3 代码闭环均已完成、默认关闭，尚未达到生产开 flag 条件**。回滚始终是先关对应 flag；不删除 world/resident/conversation/L3、Feed/outbox/notification 加性数据。

### M3 产品项已冻结（2026-07-22）

- Feed 首版只做文字，按 universe 每个北京自然日最多 2 条、上午/傍晚各 1 条；只为已确认、存在 active resident、真人最近 7 天有任一渠道入站的世界生成，允许跳过、不补发。
- Feed 与通知分入口、分红点；通知拉取不自动已读，支持单条/全部已读。已读保留 7 天、未读保留 30 天、每真人最多 200 条；写入事务内先清最旧已读、再清最旧未读，central scheduler 清到期行并对账。
- App 真人级消息由最近收到用户入站的 active resident 发声，无历史时确定性回退；微信只允许 legacy primary 使用真实路由发声，首版不支持用户指定。
- App-only 真人级首版启用但只进拉取式收件箱；独立 flag 默认关闭后灰度，开启后真人级合计每真人滚动 24 小时最多 1 条。per-resident reminder/commitment 不受该 flag 影响。
- `character_letters`/mailbox 属 M4；其策略已在 M4-0 冻结，不再是开放产品问题。
- M3-0 已评审冻结；Feed 默认直接发布，不接现有 account-scoped moderation，未来审核按 post/world/platform user 单独设计，AI 内容不得误处罚 resident account。
- M3-1 已完成 m0033、repository 原语、纯领域契约及双后端/PG 并发门禁。
- M3-2 已完成用户文字直接发布、事务 outbox、owner Feed API、opaque cursor 与独立 default-off Feed flag。
- M3-3 已完成独立中心 AI Feed scheduler/outbox worker；生成器不读取私聊/L3，PG 已证明同 slot stale reclaim 单 winner、outbox claim 不重叠及旧 token CAS。生产 Feed 窗口仍未填写。
- M3-4 已完成 default-off App 收件箱闭环：拉取不自动已读、owner 防枚举、单条/read-all、独立红点、7/30 天和 200 条清理均已接线；App per-resident reminder/commitment 不调用微信网关。
- M3-5 已完成真人级 proactive 上提：真实微信 primary 优先；App-only 需 inbox + human 双 flag，按真人滚动 24 小时最多一条 visible；活跃与预算跨 owner 全账号聚合，due 扫描按真人折叠，最终 speaker 在投递事务重选并锁定。现有候选默认强绑定原 resident，失活 cancel；显式通用内容才允许重选。SQLite `1465 passed / 14 skipped`，PG `1474 passed / 5 skipped`。
- M3-6 已完成运行手册、三 flag 灰度/回滚矩阵、central 单例约束、Feed/outbox/通知/真人级 heartbeat 与只读对账 SQL。最终门禁：unit `567 passed`，SQLite `1465 passed / 14 skipped`，PG `1474 passed / 5 skipped`，静态检查通过。

### M4 产品项已冻结（2026-07-23）

- 首次确认后用户不能主动移除 active resident；App 不提供 departure/remove API。legacy resident 永不离开。
- 自动流程首版只产生 departure candidate；所有 offline 由 full admin 人工批准。inactivity=60 天，value mismatch=30 天内 3 次且跨度 14 天，cooldown=7 天，crisis freeze=30 天。
- 普通最后居民离开永久阻断；severe-abuse 例外仍需审批。offline 不可恢复，提交后纠错只追加审计并可隐藏 farewell。
- mailbox 仅运营版本化目录；active `<8` 投递、`<10` 接受，每世界 open=1，投递间隔与 TTL 均为 30 天，defer 不续期、同角色不重投；不做 Push、不写 M3 notification。
- M4-0 backend spec 与 M4-1…M4-6 实施计划均已完成归档；lifecycle 候选/审批/offline 原子事务与 mailbox catalog/投递/读取/接受闭环已完成。三 flag 仍默认关闭。

---

## 五、下一步

1. 取得运营签字的四模板 manifest、客户端最低版本，并同步客户端两项冻结口径。
2. P1/M3/M4 生产发布仍在 flag=false 下完成模板导入、固定 cutoff backfill、override/M3/M4 数据对账和全量只读核验；不得把开发分支 m0034 等同于已部署。
3. 发布前复跑最终双后端门禁，按 Admin guide 顺序小流量开启 App inbox、用户 Feed、AI scheduler、App-only human，并观察 heartbeat。
4. M3 后续严格保持 Feed/通知分面、Runtime 不依赖 World DB、三个 flag default-off，并继续以 PG 并发测试作为权威门禁。
5. M4 已整理到 Draft PR #47，Ready/合并仍由用户明确决定。M5 已完成产品门、visit 审批闭环、visitor Feed/expiry/block；真人消息/self-hide/report 待 M5-4，M5 分支暂堆叠于 M4，#47 合并前不得转 Ready。
6. 客户端仍须镜像 D-05 L3 全量共享与 D-08 legacy 离开豁免；M4 backend 冻结不替代客户端文档修订。
7. 客户端还须把旧“邀请码默认 12h/兑换即生效”改为 M5 冻结口径：24h invite、兑换后 pending、A 接受后独立 30d visit；这是生产开 M5 flag 的阻断项。
