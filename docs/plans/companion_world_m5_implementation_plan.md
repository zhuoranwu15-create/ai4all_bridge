# Companion World M5 实施计划

> 状态：**M5-0…M5-5 已完成并归档（2026-07-23）。**
>
> 决策冻结：ADR §10.9 与 [`companion_world_m5_backend_spec.md`](../tech_design/companion_world_m5_backend_spec.md)。
>
> 分支基线：M4 PR #47 已合并；`feat/companion-world-m5` 已同步 `origin/main@d41f26f`。M5 Draft PR #48 现以 `main` 为 base 并重跑 CI，尚未转 Ready 或合并。

## 1. 目标与精简原则

完成认证后一次性邀请、pending owner 确认、限时只读 Feed 和一对一真人文字聊天。优先完成重构闭环，不增加通用社交图、群聊、媒体、Push、自动续访、复杂审核编排或新依赖。

## 2. 已冻结口径

- B 跨好友世界 `pending + active <= 3`，可取消 pending。
- A 世界有效 invite + pending + active 共用三个固定 slot。
- B 兑换只进入 pending，A 接受后才建立 ACL。
- invite 24h、pending 7d、accept 后 visit 30d，均绝对到期且不因互动续期。
- visitor 只读 published Feed，永不访问 L1/L2/L3/runtime/AI 私聊。
- 真人聊天独立分表；visit 终止立即只读，历史默认保留，手动删除仅 self-hide。
- 任一方 block 立即终止双方 visit/chat 写权限；举报 evidence 独立保留。

## 3. 分批实施

每批独立提交；先聚焦 SQLite，再跑 PG 并发。历史 migration 不改写，flags 全部默认关闭。

### M5-0：产品门与实现级规范

**状态：已完成（2026-07-23）。**

- 冻结 §10.9 三项及 invite/pending/visit/block 派生参数。
- 定稿 m0035 七表、状态机、API、错误码、锁序、flags、scheduler 与测试矩阵。
- 同步总设计、重构简报，明确客户端旧 TTL 口径是生产阻断项。

出口：M5-1 不再猜测容量主体、owner approval、终态历史或锁序。

### M5-1：m0035、DTO/状态机、DB 原语与 flags

**状态：已完成（2026-07-23）。**

主要文件：

- `app/db/_core.py`：只追加 m0035。
- `app/db/companion_world_visits.py`、`app/db/companion_world_human_chat.py`。
- `app/domains/companion_world/visits.py`、`app/domains/companion_world/human_chat.py`。
- `app/config.py`、`.env.example`、`tests/conftest.py`。
- `tests/test_companion_world_m5_schema.py`、分层测试。

交付：七张加性表/索引、owner/visitor-scoped 读写原语、纯状态机、两个 default-off flag；未接 API/scheduler，现有行为不变。最大 migration 已更新为 m0035，历史 m0033 版本断言同步修正。

出口：SQLite/PG migration 幂等；旧 M2–M4 测试不变；领域层不依赖 DB/FastAPI/Runtime。

验证：M5/schema/边界 SQLite `23 passed / 1 skipped`、PostgreSQL `24 passed`；Companion World SQLite 联合 `106 passed / 16 skipped`；unit `570 passed / 955 deselected`；compileall/diff check 通过。

### M5-2：Invite、pending approval 与 active visit

**状态：已完成（2026-07-23）。**

主要文件：

- `app/platform/companion_world_visits.py`。
- `app/routers/companion_world_visits.py`、`app/main.py`。
- visit DB/领域模块与聚焦/PG 并发测试。

交付：create/list/revoke、redeem、owner accept/reject、visitor cancel/leave、owner revoke；A 三 slot 与 B 三 visit 双侧容量；accept 同事务创建 human conversation。

出口：同码双花、B 第 3/4、A 第 3/4、accept 竞态均由 PG 门禁守住；pending 无任何 Feed/chat ACL。

实现结果：一次性 code 只存 SHA-256，redeem 只创建 pending；accept 同事务写 active/30 天 expiry/human conversation，所有终态释放 slot 并将既有会话只读。公开 DTO 不返回 hash、内部 user/world/runtime id，visit flag 仍默认关闭。

验证：M5 SQLite `16 passed / 4 skipped`、PostgreSQL `20 passed`；Companion World SQLite 联合 `112 passed / 20 skipped`；unit `570 passed / 965 deselected`；compileall/diff check 通过。

### M5-3：Visitor Feed、expiry 与 block

**状态：已完成（2026-07-23）。**

主要文件：

- visit platform facade/router。
- `app/world_lifecycle/scheduler.py` 的独立 expiry step。
- M3 Feed projection 只读复用与 block 原语。

交付：`visit_id` capability、published-only Feed、request-time 精确 expiry、central 兜底、终态释放 slot、双向 contact block。

出口：到期/撤销/离开/block 与 Feed 并发 fail-closed；没有任意 world id、L2/L3/runtime 读取。

实现结果：visitor Feed 只接受 session+B 自己的 active visit id 并复用 M3 公开投影；list/Feed request-time expiry 与 existing central scheduler 兜底均已接通。任一方 block 以双方 user/涉及 worlds 排序锁终结所有双向 open visits并把 chat 只读，未来兑换拒绝。

验证：M5 SQLite `18 passed / 4 skipped`、PostgreSQL `26 passed`；Companion World SQLite 联合 `116 passed / 22 skipped`；unit `570 passed / 971 deselected`；compileall/diff check 通过。

### M5-4：独立 Human Chat 与举报

**状态：已完成（2026-07-23）。**

主要文件：

- `app/platform/companion_world_human_chat.py`。
- `app/routers/companion_world_human_chat.py`。
- human DB/领域模块与隔离/PG 测试。

交付：conversation list、message list/send/read、client idempotency、visit 终态只读、self-hide、report evidence snapshot、block。

出口：真人消息永不进入 AI tables/prompt/dreaming/proactive；跨 participant 防枚举；send vs expiry/block PG 竞态通过。

实现结果：独立 router/platform/DB 链路已完成；chat flag 只门控新 send，历史与安全动作不关闭。self-hide 只写当前 participant 字段，report 复制必要 immutable snapshot。发送在 conversation lock 下分配 `sequence_no`，同秒消息顺序不依赖 UUID。AST 门禁新增 AI paths 禁止 human chat storage/platform/domain import。

验证：M5 SQLite `26 passed / 9 skipped`、PostgreSQL `35 passed`；Companion World SQLite 联合 `122 passed / 24 skipped`；unit `571 passed / 978 deselected`；compileall/diff check 通过。最终全量结果见 M5-5。

### M5-5：全量门禁、运行手册与交付

**状态：已完成（2026-07-23）。**

- Admin guide 增加 flags、scheduler、只读对账、回滚与 evidence retention 未决闸。
- 同步 ADR/spec/计划/简报/`.env.example`。
- 运行 unit、SQLite 全量、PG 全量、compileall、diff check。
- 整理提交、推送并创建 Draft PR；PR #47 合并后 retarget `main` 并重跑 CI，未经用户明确授权不转 Ready。

实现结果：Admin guide 已补双 flag 真实边界、单 central scheduler、heartbeat、只读对账 SQL、evidence retention 闸与不可逆终态回滚；redeem 增加真人 10 RPM + IP 30 RPM 的 DB-backed 限流；admin ops 暴露 world lifecycle 配置。Draft PR #48 已创建并在 M4 PR #47 合并后 retarget `main`；仍未执行生产 migration/开量/Ready/合并。

最终验证：unit `571 passed / 978 deselected`；SQLite 全量 `1521 passed / 30 skipped`；PostgreSQL 全量 `1546 passed / 5 skipped`；compileall/diff check 通过。既存 deprecation/async mock warnings 不阻断。

## 4. 测试与发布

聚焦测试覆盖 invite/visit 状态机、双侧容量、ACL、human chat、self-hide、report/block；PG 权威覆盖并发双花、3/4 容量、accept/terminal、send/terminal 与反向 block 锁序。

```bash
make test-unit
make test
make test-pg
.venv/bin/python -m compileall app scripts tests
git diff --check
```

发布顺序：m0035/default-off → read/history → invite/pending → active visitor Feed → human write。回滚关 flags/停 expiry step，不删数据、不恢复终态、不解除 block。M5 实施计划至此归档，后续生产发布以 Admin guide 为准。
