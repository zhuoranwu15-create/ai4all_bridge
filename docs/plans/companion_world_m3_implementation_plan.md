# Companion World M3 实施计划

> 状态：**M3-0…M3-6 全部完成（2026-07-22）；本计划已归档。**
>
> 硬前置已满足：PR #45 已合并，M3-1+ 在基于 `origin/main` 的 `feat/companion-world-m3` 实施，不在 `feat/companion-world-m2c` 上叠加运行时代码。
>
> 决策冻结：ADR §10.7/.11/.12/.13，详见 D-15。
>
> 计划基线：PR #45 merge commit `363500ea8364dcccd9e7c6e3c7c5eb5ce7ed9392`；M3-1 后数据库最大迁移版本 m0033。

## 1. 目标

在 M2 多居民/L3 基座之上交付三个彼此正交、可独立关闭的闭环：

1. 文字世界 Feed：用户文字动态 + 按 universe 限频的 AI 文字动态，领域事件经事务 outbox 幂等发布。
2. App 拉取式通知收件箱：承接 per-resident 义务与真人级触达，显式已读、独立红点、确定性保留/清理。
3. 真人级 proactive 上提 Companion World 域层：预算、due、活跃和发声人不再按 N 个 runtime account 放大；App-only 可受控灰度。

M3 完成后必须保持：微信 form-A 原投递不回归、L1/L2/私聊仍按 `account_id` 隔离、World owner 数据按 `platform_user_id/universe_id` 隔离、Feed 与通知数据面完全分离。

## 2. 已冻结口径

- AI Feed 首版仅文字；每个 universe 每个北京自然日最多 2 条，上午/傍晚各最多 1 条。只扫描 confirmed、存在 active resident、真人最近 7 天任一渠道有入站的世界；允许跳过、不补发。
- 通知拉取不自动已读；支持单条/全部已读。read 保留 7 天、unread 保留 30 天、每真人最多 200 条；写入事务内先清最旧 read、再清最旧 unread，central scheduler 清到期行并对账。
- App 真人级发声人优先最近收到用户入站的 active resident，无历史时按最近会话活动、`joined_at DESC`、`resident_id ASC` 回退；投递前失活重选。微信只允许 legacy primary 真实发声。
- App-only 真人级首版只进收件箱；独立 flag 默认关闭后灰度，真人级合计每真人滚动 24 小时最多 1 条。per-resident reminder/commitment 不受该 flag 影响。
- Feed M3 默认直接发布，不接现有 account-scoped moderation；保留 post 下架扩展接缝，审核主体、策略和测试后续单独设计。不得为 Feed 随机选择 resident runtime account 作为处罚锚。
- `character_letters`/mailbox 属 M4，不进入本计划。

## 3. 范围边界

### 本轮包含

- `universe_posts`、事务 outbox、文字 Feed owner API、AI Feed scheduler/worker。
- `app_notifications`、App inbox adapter、通知 API、红点计数、7/30 天与 200 条 central cleanup。
- 真人级 due/预算/活跃聚合、确定性发声人选择、App-only 独立灰度 flag。
- Feed/通知/幂等/账号隔离、双后端与 PG 并发门禁，以及后续审核扩展的正确 owner 边界。

### 本轮不包含

- 图片、音视频、外链卡片、工具结果型动态；Feed 排序推荐与访客 Feed。
- APNs/FCM/system push；不得把 `CHANNEL_APP.supports_proactive` 改为 true。
- AI/用户评论、点赞等社交互动；不得在 M3-0 规范阶段顺手扩入。
- `character_letters`、resident offline/farewell、用户移除 active AI（M4）。
- visit/invite/human chat（M5）。
- 正式人物内容、客户端版本号或生产开关操作。

## 4. 依赖方向与进程边界

```text
App API → Companion World domain → repository/outbox ports → platform DB adapters
proactive core → typed delivery intent → AppInboxAdapter / WeixinAdapter
world-content scheduler → domain command → outbox worker → moderated universe post
```

- `app.domains.companion_world.*` 不 import `app.db.*`、FastAPI 或 `turn_service`。
- Agent Runtime 不认识 Feed、通知、universe due 或发声人策略。
- proactive core 不直接 import/write `app_notifications`；由 typed delivery intent 调 adapter。
- Feed 生成使用独立 world-content scheduler，不把世界内容伪装成 proactive message。通知清理可作为 central proactive scheduler 的独立维护步骤，但失败必须步骤隔离并写 heartbeat。
- scheduler 单例是部署约束，PG 同键 claim/唯一约束仍作并发防线，不能靠“正常只跑一个进程”证明正确。

## 5. 分批实施

每批独立提交、先聚焦测试再进入下一批；所有新功能 flag 默认关闭。

### M3-0：实现级规范与 API/schema 冻结

**状态：已评审冻结（2026-07-22）。** 已新增 [`companion_world_m3_backend_spec.md`](../tech_design/companion_world_m3_backend_spec.md)，并在编码前冻结：

- `universe_posts`、outbox、`app_notifications` 的完整 DDL、索引、状态机、FK/删除策略与双后端 SQL。
- Feed/通知 DTO、游标、稳定错误码、直接发布语义、审核延期边界与幂等键格式。
- 北京日界/上午傍晚运营配置形状；具体窗口边界作为部署配置，不改变“每窗 1、每日 2、不补发”产品不变量。
- App-only 滚动 24 小时 claim、真人级 due 键和发声人重选的事务/锁顺序。
- 三个正交 rollout flag 的最终命名与边界：Feed、App inbox、App-only 真人级；均不得替代既有 P1/L3/safety flag。

出口：规范能够直接写 migration/API/PG 竞争测试，不留状态机或唯一键待编码时猜测。三个新 flag 冻结为 `COMPANION_WORLD_FEED_ENABLED`、`COMPANION_WORLD_APP_INBOX_ENABLED`、`COMPANION_WORLD_APP_ONLY_HUMAN_PROACTIVE_ENABLED`。

### M3-1：schema、repository 原语与分层门禁

**状态：已完成（2026-07-22）。** 已追加 m0033 三表 migration、Feed slot/post+outbox/outbox claim 原语、真人级 notification reservation/owner 读取原语和纯领域 DTO/ports；未接 API、scheduler 或 live proactive 路径。

主要文件：

- `app/db/_core.py`：从 m0032 后追加新 migration，不改历史 migration。
- `app/db/companion_world.py` 或拆分的新 World DB 模块：Feed/outbox 原语。
- `app/db/notifications.py`（新）：通知写入、查询、已读、计数与清理原语。
- `app/domains/companion_world/contracts.py`：新增纯领域 DTO/ports，不泄漏 SQL row。
- `tests/test_companion_world_schema.py`、新 schema/repository/分层测试。

出口：SQLite/PG 顺序迁移成功且幂等；所有 owner 查询强制锚 `platform_user_id`，Feed 强制锚 `universe_id`；同幂等键只产生一行。

验证：SQLite 全量 `1438 passed / 11 skipped`；PostgreSQL 全量 `1445 passed / 4 skipped`。M3 聚焦门禁另证同 slot 竞争仅一行、同真人并发仅一条 live reservation、outbox worker claim 不重叠。

### M3-2：用户文字 Feed + 事务 outbox

**状态：已完成（2026-07-22）。** 已交付纯领域 Feed service、owner-scoped SQL adapter、用户 post+published outbox 同事务、GET/POST Feed API、base64url tuple cursor、下架扩展接缝与 `COMPANION_WORLD_FEED_ENABLED=false`。未接 moderation、AI scheduler 或 outbox consumer。

主要文件：

- `app/domains/companion_world/feed.py`（新）：纯领域校验和发布状态机。
- `app/platform/companion_world_repository.py`：owner universe、active author、post/outbox 同事务 adapter。
- `app/routers/companion_world.py`：`GET /v1/worlds/home/feed` 与用户文字发布端点；外部 `/api/v1` 继续由现有映射兼容。
- 后续审核接缝：M3 不接现有 account-scoped moderation；领域保留下架命令，未来按 post/world/platform user 单独设计 adapter、策略与测试。

出口：客户端不传 `account_id/universe_id` 选世界；跨用户资源统一防枚举；文字长度、直接发布、幂等和游标稳定；提交 post 与 outbox 不出现一半成功。

验证：SQLite 全量 `1445 passed / 12 skipped`；PostgreSQL 全量 `1452 passed / 5 skipped`。PG 并发重放证明同一 user request 只生成 1 个 post + 1 个 outbox；SQLite 故障注入证明 outbox 插入失败时 post 同事务回滚。

### M3-3：AI Feed scheduler/outbox worker

**状态：已完成（2026-07-22）。** 已交付独立中心 `world-content scheduler`、北京半开双窗口、confirmed/active/近 7 日任一渠道 inbound eligibility、确定性 resident 作者、slot claim + stale lease + 指数退避、作者失活/关窗 skip、事务 outbox worker、跨 batch 稳定游标及跨 slot 重置。生成器只接收 resident 名称/日期/slot，不读取私聊、L3 或 runtime account 内容；四项生产窗口继续留空等待部署评审。

主要文件：

- `app/world_content/`（新）：eligibility、slot claim、生成、outbox worker。
- `scripts/run_world_content_scheduler.py`（新）及 central composition root：独立单例进程；不挂到厚节点重复扫描。
- `app/config.py`、`.env.example`：Feed flag、两个运营窗口、batch/interval 配置与注释。
- 聚焦测试：时间窗/日界/7 日活跃、作者失活、skip/no-catch-up、outbox 重放。

出口：同 universe 同窗口并发最多发布 1 条、每日最多 2 条，resident 数量不改变生成次数；失败可重试但不重复发布，错过窗口不补发。

验证：SQLite 全量 `1454 passed / 13 skipped`；PostgreSQL 全量 `1462 passed / 5 skipped`；聚焦门禁覆盖窗口边界、7 日 eligibility、batch 游标、跨 slot 重置、作者失活、retry exhaustion、late close、同 slot stale reclaim 单 winner、outbox retry/dead/旧 token CAS；`compileall` 与 `git diff --check` 通过。

### M3-4：App 通知收件箱与清理

**状态：已完成（2026-07-22）。** 已交付 typed `AppInboxAdapter`、post-policy per-resident reminder/commitment App 路由、owner-scoped 通知 repository/API、opaque tuple cursor、独立未读数、幂等单条/全部已读、7/30 天逻辑过期、read→unread 的 200 条事务内淘汰，以及 central proactive scheduler 的 reservation/TTL/超限 cleanup 与 heartbeat。真实微信路由继续优先；App-only 真人级 source 在 M3-5 前 fail-closed；`CHANNEL_APP.supports_proactive=false` 未修改。

主要文件：

- `app/platform/app_inbox.py`（新）：实现 typed `AppInboxAdapter`，解析 `platform_user_id/universe_id/resident_id` 后写通知。
- `app/proactive/delivery/outbound.py` 与 route selection：App 目标发 typed intent，不直接写 DB；微信 adapter 保持不变。
- `app/routers/app_notifications.py`（新）、`app/main.py`：list(all/unread)、`unread_count`、单条已读、全部已读；`Cache-Control: no-store`。
- `app/proactive/orchestration/scheduler.py`：central-only cleanup 独立步骤，异常隔离并进入 heartbeat。
- `app/config.py`、`.env.example`：App inbox flag 与 cleanup batch 配置。

出口：拉取不改状态且即时排除逻辑过期行；单条/全部已读与清理不跨真人；read 7 天、unread 30 天、200 条顺序准确；App reminder/commitment 可入箱且不受 App-only 真人级 flag 误杀。

验证：SQLite 全量 `1459 passed / 14 skipped`；PostgreSQL 全量 `1468 passed / 5 skipped`；聚焦门禁覆盖 flag 隐藏、防 owner 注入/防枚举、cursor、拉取不已读、单条幂等/read-all、逻辑过期、read 优先淘汰、reservation cleanup、微信优先和 App 不触达微信网关；PG 并发证明同真人 visible 硬上限不超配。

### M3-5：真人级 proactive 上提与发声人

**状态：已完成（2026-07-22）。** 已交付纯领域真人级 scope/route/speaker 决策、owner bindings + 全 resident runtime accounts 的跨渠道活跃与预算聚合、按 platform user 折叠的 due 扫描、真实微信 legacy primary 优先、App-only 双 flag、滚动 24 小时 reservation、token-scoped finalize/cancel CAS，以及投递事务内的 speaker 重选/行锁复核。现有候选默认视为 resident 强绑定内容，speaker 失活即 cancel；只有生成方显式声明为真人级通用内容才允许重选。per-resident reminder/commitment 保持 M3-4 路径，不进入真人级桶。

验证：SQLite 全量 `1465 passed / 14 skipped`；PostgreSQL 全量 `1474 passed / 5 skipped`。门禁覆盖微信与 App 竞争时微信唯一胜出、双 flag off 零真人级通知、owner 跨 resident 活跃/预算、确定性 speaker、24 小时精确边界、旧 token 不得 finalize/cancel 新 lease、通用内容重选与强绑定内容取消；PG 并发继续证明同真人最多一条 live reservation。

主要文件：

- `app/domains/companion_world/proactive.py`（新）：真人级 due、滚动 24 小时 App-only claim、发声人选择纯策略。
- `app/platform/companion_world_repository.py`：跨居民/跨渠道 last inbound 聚合、候选 resident 与 route adapter。
- `app/proactive/orchestration/planning.py`、`delivery/policy.py`、`delivery/dispatch.py`、`delivery/account_check.py`：真人级调用域服务；per-resident obligations 保持原键。
- `app/config.py`、`.env.example`：App-only 真人级独立 flag，默认 false。

出口：App-only flag off 继续 fail-closed；on 时只进收件箱且滚动 24 小时合计 ≤1。微信 legacy 仍从真实 primary 路由发送；最近 App resident 不得借 primary 微信通道冒充发声。

### M3-6：全量门禁、运行手册与交付

**状态：已完成（2026-07-22）。** 已补齐两个 scheduler 的 M3 heartbeat 指标、outbox lag 只读快照、三 flag 灰度/回滚与单例部署手册、PG/SQLite 只读对账 SQL。最终门禁：unit `567 passed`；SQLite `1465 passed / 14 skipped`；PostgreSQL `1474 passed / 5 skipped`；`compileall` 与 `git diff --check` 通过。

- 更新 ADR、M3 backend spec、简报、Admin guide 与 `.env.example` 开关矩阵。
- 增加 Feed/outbox、通知、真人级聚合的 metrics/heartbeat 字段和只读对账 SQL。
- 完成聚焦、SQLite 全量、PG 全量、`compileall` 与 `git diff --check`。
- PR 保持 Draft，直到所有 M3 出口和双后端检查通过；Ready/合并需用户明确授权。

## 6. 测试与验收

### 聚焦测试

- Feed：owner ACL、文字直接发布、下架接缝、游标、同窗口/每日上限、7 日活跃、作者 active、outbox 重放。
- 通知：幂等写、all/unread、红点、显式已读、read-all、7/30 天、200 条淘汰顺序、per-resident 正交。
- proactive：跨居民/跨渠道活跃、App/微信发声人、App-only flag on/off、滚动 24 小时、预算与 quiet hours。
- 分层：Runtime/proactive domain 不直接依赖 World DB/通知表。

### PG 硬门禁

- 同 universe 同生成窗口并发 claim 恰好一条；每日第二/第三条竞争不超限。
- post + outbox 同事务；worker 并发/重放只发布一次。
- 同真人 N resident 并发真人级 due/投递：App-only 24 小时最多一条，不生成 N 行通知。
- 通知插入与 cleanup/read 并发不越过 200 上限、不删除其他 `platform_user` 数据。

### 合并前命令

```bash
make test-unit
make test
make test-pg
.venv/bin/python -m compileall app scripts tests
git diff --check
```

## 7. 发布与回滚边界

1. 只在 PR #45 已合并的新分支开发 M3；先部署 migration 和 default-off 代码。
2. Feed、App inbox、App-only 真人级三个开关分别灰度；不得用 `COMPANION_WORLD_P1_ENABLED` 代替。
3. App inbox 先开 per-resident 投递和读 API，再开 App-only 真人级，确保关闭真人级 flag 时 reminder/commitment 仍正常。
4. Feed 先只读/用户文字，再小量启用 AI scheduler，观察生成 skip、slot claim/outbox lag；审核策略未单独评审前不接入。
5. 回滚优先关对应 flag并停 world-content scheduler；保留 post/notification/outbox 加性数据，不物理逆迁移。通知清理可继续运行，除非确认其自身有缺陷。

## 8. M3 开工前剩余门槛

- [x] PR #45 已完成评审并合并（merge commit `363500ea8364dcccd9e7c6e3c7c5eb5ce7ed9392`）。
- [x] 已从合并后的 `origin/main` 创建 `feat/companion-world-m3`，未在 M2-C 分支叠加代码。
- [x] M3-0 backend spec 已评审冻结，覆盖 DDL、状态机、幂等键、锁序、审核延期边界与三个 flag。
- [x] 服务端契约已冻结通知 all/unread、单条/全部已读、独立红点及文字 Feed DTO；客户端仍需镜像实现/联调，但不再阻塞 M3-1 服务端编码，也不等于要求先提供正式角色人设。

四角色 manifest、客户端支持 `account:null` 的最低版本、生产模板/backfill/对账仍阻断 M2/P1 生产开量，但不授权本计划自行编造输入或改变生产状态。
