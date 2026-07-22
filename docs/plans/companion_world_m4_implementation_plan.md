# Companion World M4 实施计划

> 状态：**M4-0 已完成（2026-07-23）；M4-1 待开始。**
>
> 决策冻结：ADR §10.3/.4/.8 与 [`companion_world_m4_backend_spec.md`](../tech_design/companion_world_m4_backend_spec.md)。
>
> 分支基线：`feat/companion-world-m4` 暂从 M3 已验证提交 `01552c9` 切出。PR #46 尚未合并，本分支是依赖 M3 的堆叠分支；M4 runtime 提交前须确认 M3 已合并并校准 main。

## 1. 目标

在不改变 Runtime 形态边界、微信 legacy 行为和 M3 Feed/通知分面的前提下，完成：

1. 可审计、默认 shadow、人工批准的 resident 离开流程。
2. `offline + farewell + conversation read_only + outbox` 原子事务。
3. 私密 mailbox catalog、低频投递、显式处理和接受事务。
4. default-off 灰度、central scheduler、heartbeat、对账 SQL 与双后端门禁。

## 2. 已冻结口径

- 初次确认后用户不能主动移除 active resident；M4 不提供客户端 departure API。
- legacy resident 永不离开；所有 offline 首版均需 full admin 人工批准。
- inactivity=60 天；value mismatch=30 天内 3 次、跨度 14 天；cooldown=7 天；crisis freeze=30 天。
- 普通最后居民离开阻断；severe-abuse 例外仍需 admin 显式批准。
- offline 不可恢复；post-commit 纠错只追加审计、可隐藏 farewell，不直接复活。
- mailbox active `<8` 投递、`<10` 接受；open=1；30 天投递间隔与 TTL；defer 不续期；同角色不重投。
- mailbox 只用运营版本化 catalog，不 Push、不写 M3 app inbox。

## 3. 范围边界

### 包含

- m0034、lifecycle audit/action、mailbox catalog/letter、farewell post 扩展。
- 纯领域 lifecycle/mailbox 状态机与 repository ports。
- central scheduler 的 candidate/cooldown/freeze 与 mailbox delivery/expiry。
- admin review/catalog contract、owner mailbox API。
- offline 与 letter accept 两个强事务。
- SQLite 功能、PG 并发、分层、账号隔离、运行手册与对账。

### 不包含

- 用户删除 active resident、legacy 离开、offline 恢复。
- 自动生成角色、Push、访客/真人聊天、通用 Feed moderation。
- 客户端仓库改动、正式 catalog 内容、生产开 flag。

## 4. 分批实施

每批独立提交，先聚焦测试再进入下一批；不修改历史 migration，不引入新依赖。

### M4-0：产品门与实现级规范

**状态：已完成（2026-07-23）。**

- 冻结 §10.3/.4/.8 与 mailbox 数值/处理语义。
- 定稿 m0034、状态机、owner/admin API、错误码、锁序、flags、scheduler 和测试矩阵。
- 新增 M4 backend spec/实施计划，并同步 ADR/简报。

出口：M4-1 不再需要在编码时猜测不可逆语义、唯一键或容量锁。

### M4-1：m0034 schema、DTO/ports 与迁移门禁

主要文件：

- `app/db/_core.py`：只追加 m0034。
- `app/db/companion_world.py` 或新增 `app/db/lifecycle.py`、`app/db/mailbox.py`：底层原语。
- `app/domains/companion_world/{contracts,lifecycle,mailbox}.py`：纯 DTO/状态机/ports。
- `tests/test_companion_world_schema.py`、新 storage/边界测试。

交付：

- 扩展 farewell post，新增 lifecycle event/action、catalog、letter 四表及全部唯一/claim/list 索引。
- owner-scoped 读、event/action append、catalog create/retire、letter 状态 CAS 原语。
- 三个 default-off flag 与有界策略配置进入 `app/config.py`、`.env.example`、`tests/conftest.py`。
- AST 分层门禁止领域层依赖 DB/FastAPI/Runtime。

出口：SQLite/PG 迁移顺序和幂等均通过；旧 M3 post/Feed 行为不变；owner 查询没有无锚读取。

### M4-2：Lifecycle evidence、cooldown、freeze 与 admin review queue

主要文件：

- `app/domains/companion_world/lifecycle.py`。
- `app/platform/companion_world_lifecycle.py`。
- `app/world_lifecycle/{scheduler,evidence}.py`、`scripts/run_world_lifecycle_scheduler.py`。
- `app/routers/admin_companion_world.py`。

交付：

- inactivity 确定性扫描；value mismatch 结构化 evaluator；moderation-backed crisis/severe-abuse evidence adapter。
- open event 幂等、7 天 cooldown、恢复取消、30 天 crisis freeze、last-resident/legacy skip。
- staff/admin 脱敏 review queue；full admin approve 暂由 commit flag 拦截，先支持 shadow/reject/cancel。
- scheduler heartbeat 与分页游标，central 单例 + DB 唯一约束防重复。

出口：evaluation flag off 零候选；on/commit off 只写内部审计，不产生任何用户可见变化；event 不存聊天原文。

### M4-3：Offline + farewell + read-only 原子事务

主要文件：

- `app/platform/companion_world_lifecycle.py`：transaction-bound L1→L2。
- `app/db/companion_world.py`：CAS/locked helpers、farewell+outbox。
- `app/routers/admin_companion_world.py`：approve/correct。
- `app/domains/companion_world/feed.py`、公开 serializers：farewell projection。

交付：

- full admin approve 在同事务写 event/resident/conversation/post/outbox/action。
- Runtime account 与历史保留；offline resident 不再 turn、普通 Feed author 或 proactive speaker。
- correction 追加审计并可隐藏 farewell，禁止 offline→active。
- 重用 `conv:` advisory lock；PG 证明与 turn 并发不撕裂。

出口：所有故障注入全回滚；approve 重放恰好一个 farewell/outbox；legacy/last-resident/crisis 在事务内重校验。

### M4-4：Mailbox catalog、投递、expiry 与 owner 读取

主要文件：

- `app/domains/companion_world/mailbox.py`。
- `app/platform/companion_world_mailbox.py`。
- `app/world_lifecycle/scheduler.py`：mailbox maintenance 独立步骤。
- `app/routers/companion_world_mailbox.py`、`app/routers/admin_companion_world.py`。
- `scripts/import_companion_world_mailbox_catalog.py`。

交付：

- catalog create/list/retire 与 signed manifest dry-run/import。
- confirmed + active `<8` + open=0 + 30 天 cooldown 的 deterministic delivery。
- owner list/detail/unread/read/defer/decline；列表不自动已读。
- request-time + scheduler expiry，精确 30 天边界；无 Push/app_notifications。

出口：同 world 并发 scheduler 最多一封 open letter；同 character 不重投；所有 API owner 隔离、防枚举、no-store。

### M4-5：Letter accept 强事务

主要文件：

- `app/domains/companion_world/mailbox.py`。
- `app/platform/companion_world_mailbox.py`、`companion_world_repository.py`。
- `app/db/{mailbox,companion_world,billing}.py` 的既有 no-grant UoW 接缝。
- owner accept API 与 PG concurrency tests。

交付：

- world lock 下实时 expiry/template/active `<10` 复核。
- runtime(no binding/no grant)+resident(origin mailbox)+conversation+letter accepted 同事务。
- 重放返回同一 resident；满员、retire、故障不留孤儿数据。

出口：PG 双 accept 单 winner、第 10/11 位竞争不超限；不新增钱包、赠权或 owner binding。

### M4-6：全量门禁、运行手册与交付

- heartbeat 低基数指标、admin health、SQLite/PG 对账 SQL。
- 三 flag 灰度/回滚矩阵与 central 单例部署说明。
- 同步 ADR、M4 spec/计划、简报、Admin guide、`.env.example`。
- 运行 unit、SQLite 全量、PG 全量、compileall、diff check。
- PR 保持 Draft；Ready/合并仍需用户明确授权。

## 5. 测试与验收

### 聚焦

- lifecycle：阈值、cooldown、recovery、crisis、last resident、legacy、review 权限与纠错。
- offline：原子性、幂等、唯一 farewell、history read-only、Feed/proactive 排除。
- mailbox：catalog、eligibility、open cap、cooldown/expiry、owner ACL、显式 read/defer/decline。
- accept：容量、模板状态、no-grant/no-binding、失败回滚与重放。

### PG 硬门

- offline vs turn；double approve；last resident vs concurrent accept/create。
- double delivery；double accept；10/11 capacity；accept vs expire/retire/create。

### 合并前命令

```bash
make test-unit
make test
make test-pg
.venv/bin/python -m compileall app scripts tests
git diff --check
```

## 6. 发布与回滚

1. m0034/default-off 先部署，不改既有行为。
2. lifecycle evaluation shadow → 人工核验 → commit 小流量。
3. mailbox catalog dry-run/import → 只读 API → central delivery → accept。
4. 回滚关 flags/停 scheduler；不反向迁移、不复活 offline、不撤销已接受 resident。
5. M4 生产开量仍依赖 P1/M3 客户端、模板、backfill、迁移和现场对账门。

## 7. M4-1 开工门

- [x] §10.3/.4/.8 与 mailbox 决策已冻结。
- [x] backend spec 已覆盖 schema/state/API/lock/flag/test/rollout。
- [ ] PR #46 合并并同步最新 main；或明确授权继续以堆叠分支开发 runtime。
- [ ] 确认 M4-1 仅落 schema/ports，不在同批接 live scheduler/API。
