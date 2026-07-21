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

## 四、进度与剩余工作（截至 2026-07-21，决策 B 后）

> **分支 `feat/companion-world-m0-m1-wallet`：已并入 origin/main #42，决策 B 后发布闸已清零；PR #44 → main 已开并更新。本简报不再内嵌易失效的 HEAD，接手时以 `git rev-parse HEAD` / PR 最新提交为准。**

**已交付（均已 commit）：**
- **M0** 全部：四个骨架包 + AST 分层门禁（含 Runtime→域层反向门）+ characterization 安全网。
- **M1** 全套：D-14 钱包上迁真人键 + 一真人一次赠权、D-09 daily/RPM 配额上迁 + 原子预占/回滚/TTL、D-07 容量脱建号计数、#11 PG 并发硬闸。
- **M2-A**：数据基座（5 张 P1 表迁移 m0025/m0026）+ Agent Runtime 四接缝端口**形状**（未接线）。
- **M2-B1**：L3「读注入」接缝（`read_universe_context` + `prompt_builder` 加性 `extra_blocks`），**form-A 微信零 live 行为变更**。
- **八字无工具化**：八字降级为与 weather 同形态的无工具 skill，替代原「八字段改指 universe 存储」的 B2。
- **codex 审查 5 findings 修复**（`1821ffd`）：M1 真人级上迁的冷路径漏扫——wipe 误删共享钱包、无 binding 孤儿行 daily 迁移、L3 跨 universe 写入隔离、Runtime→域层依赖反转、`grant_shells` 预览余额。
- **并入 main #42**（`4a3cae1`）：main 已合并的「App/账号收敛 + 渠道化人设」（一手机号×一 App=一 active 账号、`ux_owner_binding_active_user_app`、`CHANNEL_APP 'app'→'native'`、迁移 22/23/24）。分支 M1 迁移先改号 22–26→25–29（`c2ba253`）让号，冲突仅 `_core.py`（registry 取并集 1–29）。
- **✅ 账号模型对齐决策 B 实现**（`329ce59`）：见下节。
- **✅ 决策 B 后 PG 并发硬闸补齐**：旧「一真人第二账号」测试夹具改走真实 form-B 居民路径；新增「微信 binding 账号 + 世界居民账号」同真人并发共享钱包用例，覆盖两条 owner 解析链汇聚且不丢更新。

**发布闸（2026-07-21）：unit 562 passed；SQLite 全量 1379 passed / 6 skipped；PG 聚焦并发 4 passed；PG 全量 1381 passed / 4 skipped。全 29 迁移与双后端发布闸均绿。**

### ✅ 账号模型对齐 —— 决策 B（已定 + 已实现，替代原「合并阻塞」）

原「PR #44 合并阻塞」的根因是 **`account` 一词两义**（用户 App 账号 vs 居民 runtime 容器），**非产品矛盾**。已冻结为 **决策 B**（决策记录：[`tech_design/companion_world_account_model_reconciliation.md`](tech_design/companion_world_account_model_reconciliation.md)）：

- **口径**：`owner_binding` 是**微信接入（形态 A）独有**的产物、不是通用「用户账号」机制；朝夕相伴居民**不发 binding**，纯朝夕相伴用户**零 binding**。
- **解析链**（`accounts.resolve_owner_platform_user_id`）：`account →（1）owner_binding →〔无则〕（2）universe_residents.runtime_account_id → universes.owner_platform_user_id →〔无则〕（3）account_id 兜底`。居民从不建 active binding → #42 的唯一索引永不触发，**B 不破 #42**。
- **落地**：B-① 解析函数收口钱包/配额/wipe 四处冷热路径；B-② `billing.create_resident_runtime_account`（M1-5 内部建号原语：建 account + `universe_residents` 映射，不发 binding / 不赠权 / 不占容量）；B-③ 测试夹具 `make_resident_account` + 13 个红夹具改走内部路径；B-④ 命名消歧注释 + ADR D-02/D-07 落地说明 + `tests/test_resident_runtime_account.py`。

**剩余工作：**

*A. PR #44 合并门：*
- **发布闸已清零**：PG 双档验证与混合 owner 解析并发用例均已完成，分支已更新到 PR #44。
- **剩余唯一动作 = 合并到 main**；继续等用户明确点头，本轮只推分支、不合并（已承诺）。

*B. 卡产品冻结门 §10（非编码任务，是产品决策）：*
- **M2-C（居民接入）** — universe/resident bootstrap·confirm·candidates、老用户补居民 backfill、App 端点、App turn 适配器填 `extra_blocks`。卡 §10.1/.2/.6（预设居民数量/版本、老用户补居民映射、App 是否纳入 dreaming 扫描）。
- **M1-5 内部建号接线** — 原语已就绪（B-②），随 M2 建居民真正调用。
- **seam② after-turn typed sink** — 随 form-B 居民真正产出 typed fact 再落。
- **M3 / M4 / M5** — 各待对应 §10 冻结项。

> ⚠️ **本地 dev PG 污染提示**：本地 dev PG 曾被旧改号（22–26）污染，手动脚本直连 ambient `DATABASE_URL` 会报 `column "app_id" does not exist`——那是 **dev 机 artifact、非代码 bug**（fresh 测试库/生产只见 #42 的 22/23/24，按序应用 1–29 正确）。`make test-pg` 用隔离临时库，不受影响。

---

## 五、如何从本文档接手

1. **切到分支并取最新提交**：`git checkout feat/companion-world-m0-m1-wallet`；以 `git status` / `git rev-parse HEAD` 为准，不依赖文档里的静态 commit 号。
2. **当前发布基线**：`make test-unit` = 562 passed；`make test` = 1379 passed / 6 skipped；`make test-pg` = 1381 passed / 4 skipped（全 29 迁移 + 并发不变量）。后续改动按风险复跑对应档。
3. **PR #44 状态**：发布闸已清零；**合并动作等用户明确点头**。
4. **权威口径**：ADR `tech_design/companion_world_3_0_refactor_design.md`（§12 里程碑 / §7.3 端口 / D-01…D-14）、P1 规范 `..._p1_backend_spec.md`、账号模型 `..._account_model_reconciliation.md`。
5. **主干下一步唯一钥匙**：推动 §10 产品冻结（§10.1 预设居民数量/版本、§10.2/D-08 老用户补居民映射、§10.6 App dreaming 扫描）——产品口径一冻结，M2-C 即 code-ready。
