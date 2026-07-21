# 系统 3.0 重构简报 — Agent Runtime 分层 + Companion World 产品领域层

> 面向：项目干系人 / 新加入者的快速通读。权威口径以 ADR
> [`tech_design/companion_world_3_0_refactor_design.md`](tech_design/companion_world_3_0_refactor_design.md)
> 与 P1 规范 [`tech_design/companion_world_p1_backend_spec.md`](tech_design/companion_world_p1_backend_spec.md) 为准；本文只做浓缩。
> 更新日期：2026-07-21

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
| **M0** 冻结·脚手架·护栏 | 保护现状 + 分层门禁 | 无，可立即开工 |
| **M1** 计费/配额锚点上迁 | 钱包/配额/RPM 锚到 `platform_user`（趁多居民未上线先独立发布） | 无，可立即开工 |
| **M2** Runtime facade + 多居民 | universe/resident/conversation + L3 首刀 | §10.1/.2/.6 |
| **M3** Feed + 通知收件箱 + 真人级 proactive 上提 | outbox + App 收件箱 + 去 N× 打扰 | §10.7/.11/.12/.13 |
| **M4** 生命周期 + 信箱 | offline+farewell 原子事务、信箱 | §10.3/.4/.8 |
| **M5** 访客 + 真人聊天 | slot/高熵邀请码/ACL、真人分表 | §10.9 |

> **关键重排**：把「计费/配额上迁」从建号解耦里抽成 **独立先行的 M1**——趁 fork 最浅（多居民还没上线）先把钱包/配额从 account 迁到「真人」维度，零迁移窗口；少量多钱包老用户走**预检 + 自动合并**。

### 贯穿全程的工程手法
- **characterization test 先钉现状**，再逐步抽边界（避免重构悄悄改行为）。
- **分层边界用 stdlib-AST 门禁**（不引入 import-linter 依赖）纳入 CI 阻塞。
- **schema 改动一律追加迁移函数**到 `_MIGRATIONS`（`PRAGMA user_version` 跟踪），不用启动期补丁。
- **双后端零分叉技巧（"Option A"）**：钱包/配额上迁保留旧 `UNIQUE(account_id)` 约束、只**加局部唯一索引**锚真人键——避开 SQLite 12 步表重建 / PG DROP CONSTRAINT 的后端分叉。
- **并发正确性以 PostgreSQL 用例为证**（SQLite 单写者不作数）；L3 用 append-only + 单 writer 规避多居民并发写覆盖。

---

## 四、进度与剩余工作（截至 2026-07-21）

**已交付（打包在分支 `feat/companion-world-m0-m1-wallet`，PR #44 → main）：**
- **M0** 全部：四个骨架包 + AST 分层门禁 + characterization 安全网。
- **M1** 全套：D-14 钱包上迁真人键 + 一真人一次赠权、D-09 daily/RPM 配额上迁 + 原子预占/回滚/TTL、D-07 容量脱建号计数、#11 PG 并发硬闸。
- **M2-A**：数据基座（5 张 P1 表迁移）+ Agent Runtime 四接缝端口**形状**（未接线）。
- **M2-B1**：L3「读注入」接缝（`read_universe_context` + `prompt_builder` 加性 `extra_blocks`），**form-A 微信零 live 行为变更**。
- **八字无工具化**：八字降级为与 weather 同形态的无工具 skill，替代原「八字段改指 universe 存储」的 B2。

发布闸：SQLite 1368 / PG 1369 双档全绿。

**剩余工作：**
- **M2-C（居民接入）** — universe/resident bootstrap·confirm·candidates、老用户补居民 backfill、App 端点、App turn 适配器填 `extra_blocks`。**卡在产品冻结门 §10.1/.2/.6**（预设居民数量与版本、老用户补居民映射、App 是否纳入 dreaming 扫描）——**非编码任务，是产品决策**。
- **M1-5** no-cap/no-grant 内部建号路径（随 M2 建居民接线）。
- **seam② after-turn typed sink**（随 form-B 居民真正产出 typed fact 再落）。
- **M3 / M4 / M5** — 各待对应 §10 冻结项。

**⚠️ 当前合并阻塞（一个窄技术决定，非产品矛盾）：** PR #44 与 main 已合并的 **#42「App/账号收敛」在用户面并不矛盾**——「一真人一手机号、一 App 一个用户账号、一个钱包」是二者共同的目标终态（#42 保证一 App 一 active 用户账号；M1 把钱包/配额锚到真人 = 一个钱包）。真正卡点是 **`account` 一词两义**：#42 眼里 account = 用户 App 账号（一个），朝夕相伴眼里每个 AI 居民各要一行 runtime `account`（1–10 个），共用同一张表。M1 靠 `account_owner_bindings` 把「某 account 属哪个真人（花哪个钱包）」解析出来，而 #42 的唯一索引限「一 (真人,App) 一条 active binding」→ 第 2…N 个居民账号解析不到钱包。**唯一待拍板** = 居民 runtime account 怎么挂到真人钱包：A. 居民也发 binding 但索引区分主账号/居民；**B（倾向）. 居民不发 binding，改用 `universes.owner_platform_user_id` 解析**。此项**与 §10 产品冻结无关**，是可单独定的内部接线。

---

**下一步唯一的钥匙 = 推动 §10 产品冻结**（§10.1 预设居民数量/版本、§10.2/D-08 老用户补居民映射、§10.6 App dreaming 扫描）+ 定账号模型口径。产品口径一冻结，M2-C 即 code-ready。
