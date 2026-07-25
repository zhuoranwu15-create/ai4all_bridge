# 多产品模块化单体实施计划与工单拆分

更新时间：2026-07-25

状态：**MP-01～MP-06 已完成开发、双后端验收与生产发布；MP-07A 四批目录迁移已完成开发与聚焦回归，待分批评审合并。Fatetell 业务接入仍等待 PRD。**

决策基线：[`multi_product_modular_monolith_design.md`](../architecture/designs/multi_product_modular_monolith_design.md)（MP-01…MP-10、O-1…O-7）。Phase 1 合并基线为 `f4baa3b`；当前最大版本为 `m0046`。

## 1. 本版调整

上一版把 expand、切读和 contract 各拆成独立工单，虽然发布边界安全，但工单过碎；同时提前排入了没有产品需求输入的 Fatetell router、领域骨架、MemorySink 和主动消息开发。

本版改为：

- 当前保留 **6 张 Phase 1 工单**：五张主体工单，加一张最终审查后确认需要在冻结前完成的遗留硬化。
- expand/contract 仍作为工单内不可省略的发布检查点；**工单数量减少不代表可以把多次生产发布合成一次**。
- 不创建 `app/products/fatetell/`，不挂 Fatetell 路由，不修改 Runtime/主动消息契约来猜测 Fatetell 需求。
- 跨产品隔离测试使用可注入的 `test_product`，生产注册表暂只启用 zhaoxi。
- 产品命名空间、Runtime 接入、MemorySink、ProactiveDeliveryAdapter 和 Fatetell 端到端测试统一移入 §5 延期范围，等 PRD 后重新拆工单。

2026-07-25 生产发布完成后调整实施顺序：提前执行 **MP-07A 目录边界实体化**，只移动
归属明确的朝夕模块、收口 composition root 和强化 AST 门禁；不创建 Fatetell 产品骨架，
不修改 Runtime/Memory/Proactive 契约。该调整不改变上述 Phase 1 发布记录。

## 2. 共同执行纪律

- 不改写 m0001–m0036；新 migration 暂从 m0037 顺延。若开工前主干新增 migration，只顺延编号。
- SQLite 与 PostgreSQL 同时保留；schema、唯一约束、事务和并发测试必须覆盖双后端。
- 所有产品资产查询和写入以 `account_id` 或 `(platform_user_id, app_id)` 为隔离锚，禁止裸 `platform_user_id` 跨产品聚合。
- `app_id` 只能来自服务端产品注册表、固定 legacy 路由或已验证 account/session，不接受任意 Header。
- 当前生产注册表只启用 zhaoxi；第二产品仅在测试中通过依赖注入出现，不产生生产入口。
- 生产 PG 是多节点共享 writer。涉及旧唯一索引删除或旧 `ON CONFLICT` arbiter 失效的 contract 检查点，必须先 drain 全部旧 writer，再统一升级，不做混版本滚动。
- 每张工单完成后先跑聚焦测试；涉及钱、配额、referral、唯一约束或事务时必须跑对应 PG 测试。全量 SQLite/PG 回归放在 MP-05。
- MP-01～MP-06 遵循最小改动且不搬迁 Companion World；上线后的 MP-07A 只做行为零变更的物理归位，不借机修改业务规则。

## 3. 工单总览

| 工单 | 目标 | 依赖 | migration / 发布检查点 |
|---|---|---|---|
| MP-01 | 身份隔离基座：预检、membership、SessionPrincipal、入口账号 | 无 | m0037–m0038，均为加性 |
| MP-02 | 计费隔离：subscription、wallet、ledger、cost、wipe | MP-01 | m0039 expand → m0040 contract |
| MP-03 | 配额隔离：daily、reservation、RPM、override | MP-01 | m0041 expand → m0042 contract |
| MP-04 | 邀请隔离：产品级新客、referral、review、reward | MP-01、MP-02 | m0043 expand → m0044 contract |
| MP-05 | Phase 1 收紧：最终约束、AST 门禁、双产品测试与发布手册 | MP-02…MP-04 | m0045 contract / 总发布闸 |
| MP-06 | 遗留硬化：billing 复合幂等、membership turn gate、并发缺口 | MP-05 | m0046 billing idempotency contract |

依赖顺序：`MP-01 → MP-02 → MP-03 → MP-04 → MP-05 → MP-06`。MP-02 与 MP-03 技术上可在 MP-01 后并行，但为降低 schema/发布认知负担，默认仍串行开发。

## 4. 当前执行工单

### MP-01：身份隔离基座

状态：**已生产发布。** 发布前生产只读预检结论为 PASS；其中 40 条历史 orphan account quota fallback 在 MP-03 contract 前完成治理。SQLite 全量回归与 PostgreSQL migration/auth/并发聚焦回归已通过。

目标：先建立可信的 `(platform_user_id, app_id)` membership、session audience 和入口账号解析，使后续计费/配额/referral 都有同一个产品归属锚。

主要文件：

- `scripts/precheck_multi_product_phase1.py`（新增，只读）。
- `app/db/_core.py`：追加 m0037–m0038。
- `app/db/product_memberships.py`、`app/db/__init__.py`。
- `app/bootstrap/product_registry.py`：生产仅注册并启用 zhaoxi；测试允许注入 `test_product`。
- `app/db/accounts.py`、`app/db/billing.py`。
- `app/routers/deps.py`、`app/products/zhaoxi/api/companion_world.py`、`app/routers/web.py`、`app/routers/app_api.py`（MP-07A 后物理路径）。
- `tests/test_precheck_multi_product_phase1.py`、`tests/test_product_memberships.py`、`tests/test_session_principal.py`、`tests/test_multi_product_account_resolution.py`（新增）。

交付：

1. 只读预检：输出不含手机号/正文的 PASS/BLOCK 报告，检查 account/binding app drift、重复 active subscription、钱包/ledger 勾稽、quota owner fallback、referral 孤儿引用和 session 存量。
2. m0037 新建 `product_memberships(platform_user_id, app_id, status, daily_limit, rpm_limit, settings_json, created_at, updated_at)`，唯一键 `(platform_user_id, app_id)`；存量真人回填 `zhaoxi/active`，配额 override 同步复制。
3. membership 原语只接受注册表 app_id；无行=未加入，状态只允许 `active|disabled`。user upsert 与 zhaoxi membership ensure 同事务，内部返回 `is_new_membership`。
4. m0038 为 `platform_user_sessions` 增加 app_id 并回填 zhaoxi；引入 `SessionPrincipal(session_id, platform_user_id, app_id, expires_at)`，resolver 同时校验 session、注册表和 active membership。
5. 旧 `/web/*`、`/v1/*` 固定 zhaoxi audience；收口 `_require_session`、`_require_world_session` 和 token resolver 直接调用点。
6. 新增 `get_active_bound_account_for_user_in_app(platform_user_id, app_id)`；建号强制 active membership。裸 resolver 仅留临时 zhaoxi compatibility wrapper。

验收：

- 生产预检 PASS 后才执行 m0037。
- backfill 不改变 account/onboarding/session 数量和现有 zhaoxi API 行为。
- disabled/missing membership 不能创建 session/account 或继续使用旧 token。
- zhaoxi token 可访问 legacy API；`test_product` token 不能访问 `/web/*`、`/v1/*`。
- 同一真人在两个测试产品的入口账号互不返回；每产品最多一个 active binding。

验证：上述新增测试、现有 App/Web/Companion World auth 与 onboarding 测试、migration 幂等、PG membership/建号并发测试。

回滚：m0037–m0038 均为加性；代码可回到只读 zhaoxi 路径并保留新增表/列，不删除 membership/session 数据。

### MP-02：计费与订阅隔离

状态：**已生产发布。** SQLite 全量回归通过；PostgreSQL migration、Web、计费及并发聚焦回归通过。发布前生产只读预检为 PASS，m0039 前可检查的计费存量阻断项均为 0。

目标：把 subscription、wallet、ledger、cost 和账号清理完整切到 `(platform_user_id, app_id)`，同时保持 zhaoxi 存量余额、历史订阅和新客赠权不变。

主要文件：

- `app/db/_core.py`：追加 m0039–m0040。
- `app/db/billing.py`、`app/db/lifecycle.py`。
- `app/products/zhaoxi/api/admin_accounts.py`、`app/routers/web.py`。
- `scripts/precheck_multi_product_phase1.py` 追加计费 reconcile。
- `tests/test_multi_product_billing.py`、`tests/test_multi_product_lifecycle.py`（新增）及现有 billing/PG concurrency tests。

工单内检查点 A——expand（m0039）：

- `subscriptions`、`entitlement_wallets`、`entitlement_ledger`、`cost_events` 增加 app_id，存量回填 zhaoxi。
- 新代码从 account/membership/wallet 推导并双写 app_id；不一致时 fail closed。
- subscription 保留正常 cancelled/expired 历史；重复 active 按 `updated_at DESC, id DESC` 留最新，其余标 `superseded`。
- 暂时保留钱包旧真人级 active 唯一索引和旧读 fallback，旧 writer 仍可运行，第二产品钱包仍不可创建。

工单内检查点 B——cutover/contract（m0040）：

- 钱包 active 唯一索引切为 `(platform_user_id, app_id) WHERE status='active'`。
- subscription 建局部唯一 `(platform_user_id, app_id) WHERE status='active'`，inactive 历史保留。
- wallet/ledger/cost/subscription 的读写、Admin 展示与幂等分支全部带 app_id。
- 新客赠权按 membership 每产品一次；zhaoxi 兼容识别既有 `new-user-grant-{platform_user_id}`，避免存量二次赠权。
- `wipe_account_data` 只判断和清理当前 app 的共享 wallet/daily，不碰另一产品资产。

验收与发布闸：

- contract 前 reconcile 必须为 0 NULL、0 drift、0 duplicate active，余额与 ledger 勾稽不变。
- m0040 会让旧钱包 `ON CONFLICT(platform_user_id)` 失效，执行前必须 drain 全部旧 API/scheduler writer。
- 同真人两个测试产品可各有一个 active wallet/active subscription；扣款、赠权、ledger list 和 wipe 互不影响。

验证：SQLite billing/lifecycle 聚焦全集；PG 跨产品并发扣款、同 key 幂等、active wallet/subscription 唯一测试。

交付：

1. m0039 为 `subscriptions`、`entitlement_wallets`、`entitlement_ledger`、`cost_events` 增加并回填 `app_id`，从 account/wallet 对齐冗余归属；重复 active subscription 按冻结规则仅把旧行标为 `superseded`。
2. m0040 内置聚合 reconcile，任一 NULL、scope drift、重复 active 或 wallet/ledger 勾稽异常均拒绝 contract；随后把 wallet/subscription active 唯一约束切到 `(platform_user_id, app_id)`。
3. subscription、wallet、ledger、cost 的创建、读取、幂等返回与 Admin 展示全部 app-scoped，并要求 active membership；同真人可在两个测试产品拥有独立钱包、订阅、赠权、扣款与流水。
4. zhaoxi 继续识别历史 `new-user-grant-{platform_user_id}`，测试产品使用含 app_id 的新键；referral schema/资格仍留 MP-04，MP-02 期间非 zhaoxi 账号不会触发旧 referral 奖励链。
5. `wipe_account_data` 仅在当前产品内判断 sibling 并清理 wallet/ledger/cost；另一产品资产不参与判断也不被删除。`daily_usage` 尚无 app_id，因此非 zhaoxi wipe 不碰 legacy daily，完整 daily 隔离留 MP-03。
6. `scripts/precheck_multi_product_phase1.py` 可同时运行在 m0039 前与 expand 后；expand 后复用 m0040 同源计费 reconcile。2026-07-24 生产只读结果 PASS，未执行 migration 或写数据。
7. 验证结果：SQLite 全量 `1546 passed, 33 skipped`；PostgreSQL 受影响面与并发聚焦 `98 passed`。

发布特别闸：常规 `init_db()` 已有代码级 interlock，既有 PostgreSQL 库即将跨越 m0040 时会拒绝启动，不能用启动新实例替代逐闸 runner。必须先排空全部旧 API/scheduler writer，再执行 m0039、复跑 expand 后 reconcile，确认全 0 后执行 m0040，最后启动新 writer。开发/测试空库一次建全不代表生产可以跳过该停写窗口。

回滚：expand 阶段可回旧代码；contract 后只能关闭第二产品能力并继续使用 app-aware 版本，禁止把两个产品钱包重新合并。

### MP-03：配额隔离

状态：**已生产发布。** SQLite 全量回归与 PostgreSQL migration/quota/RPM/并发聚焦回归已通过。2026-07-24 生产 m0041 前只读预检为 PASS；40 条 `quota_owner_fallback` 已在 m0042 contract 前治理为 0。

目标：把 daily、reservation、RPM 和运营 override 切到 `(platform_user_id, app_id)`，保持同产品多 resident 共享、跨产品互不消耗。

主要文件：

- `app/db/_core.py`：追加 m0041–m0042。
- `app/db/accounts.py`、`app/db/lifecycle.py`、`app/rate_limiter.py`、`app/turn_service.py`。
- `app/products/zhaoxi/api/admin_accounts.py`。
- `scripts/precheck_multi_product_phase1.py` 追加配额 reconcile。
- `tests/test_multi_product_quota.py`（新增）及 quota/RPM/PG reservation tests。

工单内检查点 A——expand（m0041）：

- `daily_usage`、`daily_quota_reservations` 增加 app_id 并回填 zhaoxi，暂时保留旧 `(platform_user_id, date)` 唯一索引。
- 新 turn 从 account 推导并双写 app_id；reservation 与 usage app_id 必须一致。
- override 读取 membership 优先、旧 `platform_users/accounts` fallback；Admin 写 membership 并在迁移期兼容旧副本。
- 引入结构化 RPM subject helper，但 zhaoxi 暂保持旧 subject，避免迁移期重置窗口。

工单内检查点 B——cutover/contract（m0042）：

- daily 唯一索引切为 `(platform_user_id, app_id, date)`；usage/reservation 查询、confirm、rollback、TTL prune 全部带 app_id。
- RPM subject 与 PG advisory lock key 同时切为 `(platform_user_id, app_id)`；IP/campaign 等非产品 subject 不变。
- 去掉 `platform_users` override fallback，membership 成为 canonical；Admin 不再跨产品传播 override。

验收与发布闸：

- expand 期间 zhaoxi 配额连续、不重置、不放宽。
- contract 前 quota reconcile 0 NULL/0 drift；删除旧 conflict arbiter 前 drain 旧 writer。
- 同产品多 resident 继续共享额度；两个测试产品的 daily/RPM/override 互不影响。

验证：SQLite quota/RPM 全集；PG 同产品不超卖、跨产品互不阻塞、reservation confirm/rollback 测试。

交付：

1. m0041 为 `daily_usage`、`daily_quota_reservations` 增加并回填 `app_id`，保留旧真人级唯一 arbiter 供旧 zhaoxi writer 继续运行，同时建立产品维度查询索引。
2. m0042 内置 quota reconcile；任一空 app、owner fallback、account/owner/membership scope drift 或重复 daily 行都会拒绝 contract。通过后把 daily 唯一索引切到 `(platform_user_id, app_id, date)`。
3. daily 查询、原子增量、reservation reserve/confirm/rollback、prune 与 PG advisory lock 全部按产品作用域；confirm 会再次校验 reservation 与当前 account scope，漂移时 fail closed。
4. RPM 使用结构化产品 subject，m0042 在停写窗口内把尚在滑窗中的 zhaoxi 真人/孤儿账号旧 key 原位迁移到新 key；IP、campaign 等非产品 RateLimiter subject 不变。
5. override canonical 改为 `product_memberships.daily_limit/rpm_limit`；Admin 只传播到同产品 accounts。迁移兼容期仅 zhaoxi 镜像旧 `platform_users` 副本，新产品不会写真人全局 override。
6. `wipe_account_data` 只删除当前产品的 daily/reservation；同产品仍有 sibling 时保留共享 daily，只回收被清账号自己的在途 reservation，另一产品不参与判断也不受影响。
7. `scripts/precheck_multi_product_phase1.py` 可在 m0041 前继续给出 legacy WARN，也可在 expand 后复用 m0042 同源 reconcile 并把 fallback 升为 BLOCK。2026-07-24 生产只读结果为 PASS、`quota_owner_fallback=40`，未执行 migration 或写数据。
8. 验证结果：SQLite 全量 `1551 passed, 34 skipped`；PostgreSQL MP-03 migration/quota/RPM/并发聚焦 `67 passed`。

发布特别闸：当前生产尚未执行 MP-01～MP-03 migration；常规 `init_db()` 已会在既有 PostgreSQL 库跨越 m0042 前拒绝启动。m0041 是加性 expand，旧 writer 可继续依靠默认 `app_id='zhaoxi'`；但 m0042 前必须先治理 40 条 fallback、排空全部旧 API/scheduler writer，复跑 expand 后 reconcile 并确认全 0，再通过逐闸 runner 执行 m0042、最后启动 app-aware writer。m0042 会删除旧 daily conflict arbiter 并迁移 RPM subject，不能做混版本滚动。

回滚：expand 阶段可回旧读；contract 后继续使用 app-aware 代码，不恢复真人全局 daily 唯一索引。

### MP-04：邀请、产品级新客与奖励隔离

状态：**已生产发布。** SQLite 全量回归与 PostgreSQL migration/Web/App/referral/并发聚焦回归已通过。2026-07-24 生产 m0043 前只读预检为 PASS，`referral_orphan_reference=0`、现有 referral relationship 为 0。

目标：让 referral code、relationship、meaningful review、奖励释放和“新客”统一按 product membership 工作。

主要文件：

- `app/db/_core.py`：追加 m0043–m0044。
- `app/db/billing.py`、`app/routers/web.py`、`app/routers/app_api.py`、Admin referral 调用方。
- `tests/test_multi_product_referrals.py`（新增）及 PG 并发测试。

工单内检查点 A——expand（m0043）：

- `referral_codes`、`referral_relationships`、`meaningful_message_reviews` 增加 app_id，存量回填 zhaoxi。
- 个人码唯一索引增加 app_id；code 字符串继续全局唯一，但 validate/preview 必须带 expected app_id。
- 新写路径双写 app_id；奖励 ledger/wallet 必须同产品。旧 invitee 全局唯一约束暂保留，旧 writer 仍可运行。

工单内检查点 B——cutover/contract（m0044）：

- referral relationship 唯一性改为 `(invitee_platform_user_id, app_id)`；SQLite 重建表，PG 删除旧约束并建组合唯一。
- OTP 消费、platform user upsert、membership 首建、relationship 建立和 code `used_count` 消费同事务。
- 仅 `is_new_membership=true` 时消费本产品邀请码；已有 platform user 首入测试产品仍是新成员，重复进入不重复消费。
- meaningful message 计数、AI/manual review、retry/release、奖励 ledger 和后台查询全链 app-scoped。

验收与发布闸：

- SQLite 重建保留全部行、FK 与索引；PG constraint 变更前预检并 drain 旧 writer。
- 同一真人可分别成为两个测试产品 invitee；同产品只能一次。
- 并发首入只创建一次 membership/relationship、只消费一次 code、只发一次奖励。

验证：SQLite referral 全集；PG 并发 membership/referral/code consumption/reward release 测试。

交付：

1. m0043 为 `referral_codes`、`referral_relationships`、`meaningful_message_reviews` 增加并回填 `app_id`；personal code 唯一索引切为 `(platform_user_id, app_id, code_type)`，code 字符串继续保持全局唯一。
2. m0044 内置 referral reconcile；空 app、重复 personal code/产品内 invitee、code/membership/reward ledger scope drift 或 review/account scope drift 都会拒绝 contract。通过后 relationship 唯一性切为 `(invitee_platform_user_id, app_id)`。
3. SQLite contract 同步重建 relationship 与其 review 子表，避免带存量 child FK 时直接 drop parent 失败；全部字段、行、外键和索引均由测试验证保留。PostgreSQL 删除旧列级唯一 constraint 后创建组合唯一索引。
4. personal code 创建、validate 和 preview 必须带 expected app；邀请码不能跨产品使用。Admin code 仍可无 inviter，个人码要求 inviter 的同产品 membership active。
5. `register_platform_user_with_referral` 把 OTP 消费、真人 upsert、membership 首建、relationship 建立与 code `used_count` 更新放在同一事务；仅 `is_new_membership=true` 时校验和消费邀请码。已有真人首次加入第二测试产品属于产品新客，重复进入不消费也不重复建关系。
6. relationship bind、有效消息计数、soft review、延迟释放、retry、Admin 列表与奖励钱包全部按 app scope。奖励 ledger 继承 relationship app，并要求同产品 inviter account/wallet。
7. PostgreSQL 奖励发放新增 relationship 级事务 advisory lock，修复两个节点同时看到 `reward_ledger_id IS NULL` 后撞 ledger 幂等唯一键的竞态；并发首入和并发奖励均只落一次。
8. `scripts/precheck_multi_product_phase1.py` 可在 m0043 前使用 legacy orphan 检查，也可在 expand 后复用 m0044 同源 reconcile。2026-07-24 生产只读结果为 PASS、referral 存量阻断项 0，未执行 migration 或写数据。
9. 验证结果：SQLite 全量 `1556 passed, 35 skipped`；PostgreSQL MP-04 受影响面与并发聚焦 `103 passed, 1 skipped`。

发布特别闸：生产必须先完成 MP-01～MP-03 的逐闸发布并治理 `quota_owner_fallback=40`；常规 `init_db()` 已会在既有 PostgreSQL 库跨越 m0044 前拒绝启动。m0043 是加性 expand，旧 zhaoxi writer 可继续依靠默认 app；m0044 前必须排空全部旧 API/scheduler writer，复跑 expand 后 referral reconcile 并确认全 0，再通过逐闸 runner 执行 m0044、最后启动 app-aware writer。m0044 移除 invitee 全局唯一约束，不能做混版本滚动。

回滚：expand 阶段可回旧 zhaoxi 路径；contract 后继续使用 app-aware 代码，不恢复 invitee 全局唯一。

### MP-05：Phase 1 收紧与总验收

状态：**开发、SQLite/PostgreSQL 全量验收与生产发布完成。** Phase 1 migration 已按受控检查点完成上线。

目标：清掉迁移期 fallback，固化依赖红线和跨产品隔离测试，并形成可执行的生产发布/回滚手册。

主要文件：

- `app/db/_core.py`：按实际 expand 结果追加 m0045，只收紧 NOT NULL/最终索引，不重写业务数据。
- `tests/test_layer_boundaries.py`。
- `tests/test_multi_product_isolation.py`（新增，使用注入的 `test_product`）。
- `scripts/precheck_multi_product_phase1.py` 最终 reconcile 模式。
- `scripts/migrate_multi_product_phase1.py` 受控检查点迁移。
- `docs/guides/` 下 Phase 1 发布与回滚说明。

交付：

- 所有冗余 app_id 收紧为最终约束；删除旧 fallback 和裸 resolver 的内部调用。
- AST 泛化：Runtime 与共享 Platform 不依赖具体产品、产品互不 import、domain 不碰 FastAPI/SQL/turn_service、composition root 例外、Runtime 禁产品字符串分支。Phase 1 不搬目录，现有朝夕 Platform adapter 以显式冻结名单管理，不允许向共享模块扩散。
- 双产品测试覆盖 token/account/messages/onboarding/quota/RPM/wallet/subscription/referral/wipe；memory/proactive 的真实第二产品接入延期，不在此虚构实现。
- 输出生产 precheck/reconcile、节点 drain 顺序、迁移检查点和 contract 后不可逆回滚限制。

验收：

- 最终 reconcile 0 NULL、0 drift、0 duplicate active。
- zhaoxi 微信 + Native App + Web 行为零回归。
- 注入的 `test_product` 证明数据隔离成立，但生产无第二产品路由、manifest 或 scheduler。
- SQLite 全量、PG 全量、PG 并发门禁、compileall、diff check 全绿。

回滚：不回并已经产品化的钱包/配额/referral；关闭未来第二产品入口并保持 app-aware zhaoxi 代码运行。

完成记录：

1. m0045 聚合身份/session/message、billing、quota、referral 的最终 reconcile；任一 NULL、scope drift、重复 active/产品唯一关系都会拒绝 contract。PG 显式收紧 `SET NOT NULL`，SQLite 验证已有 NOT NULL 后只固化最终索引，不重写业务数据。
2. `scripts/precheck_multi_product_phase1.py` 在 m0037/m0038 后即启用身份 reconcile，并在全部 expand 后复用 m0045 完整同源计数；新增受控 `--through 38…46` migration runner，在 advisory lock 内校验起始版本，支持维护窗逐闸执行。常规 `init_db()` 对既有 PostgreSQL 库增加 contract interlock，不能替代 runner。
3. 移除裸 `get_first_active_account_for_user`、`get_platform_user_by_session_token` compatibility resolver；内部调用全部改为显式 app resolver/`SessionPrincipal`。quota override 停止写真人全局迁移副本，空 app 参数改为 fail closed。
4. AST 门禁已泛化到未来 `app/products/*`：Runtime/产品互导/domain 框架与 SQL 越界/共享 Platform 新依赖/Runtime 产品字符串分支均为 CI 阻断。
5. `tests/test_multi_product_isolation.py` 覆盖 token、入口 account、runtime messages、onboarding、daily/RPM、wallet、subscription、referral、wipe 及 m0045/checkpoint runner。
6. 发布/回滚手册见 [`multi_product_phase1_release_runbook.md`](../guides/multi_product_phase1_release_runbook.md)，明确全 writer drain、逐版本检查点和 contract 后只能前向修复。
7. 最终验证：SQLite 全量 `1565 passed, 35 skipped`；PostgreSQL 全量 `1594 passed, 6 skipped`；PG 并发聚焦 `9 passed`；`compileall`、`git diff --check` 通过。

### MP-06：Phase 1 遗留硬化

状态：**开发、SQLite/PostgreSQL 全量验收与生产发布完成。**

目标：收掉最终 diff 审查发现的产品级幂等和运营拒绝缺口，并为现有并发路径补权威回归；不提前决定第二产品 scheduler 的业务编排。

交付：

- m0046 将 `entitlement_ledger`、`cost_events` 的全局 `idempotency_key` 唯一性切为 `(app_id, idempotency_key)`；PG 删除旧列级约束后建组合索引，SQLite 在保护 referral 子表和外键的前提下重建目标表。
- 常规 PG `init_db()` 同步拦截 m0046；受控 runner、precheck 和发布手册覆盖新检查点。
- `platform_users` 注册改为 `INSERT ... ON CONFLICT(phone) DO NOTHING` 后读取规范真人，消除同手机号完整注册的 SELECT→INSERT 竞态。
- referral 生产消息路径从候选消息 read-modify-write 前获取 relationship advisory lock，避免多节点覆盖计数；SQLite 继续使用单 writer 降级并验证重复发奖幂等。
- 所有渠道共享的 turn 入口在创建 session、写 profile/message 和配额解析前检查 owner 的产品 membership；disabled/missing 明确返回业务拒绝并记录 warning。
- 新增跨产品相同裸 billing key、同手机号全新注册并发、referral 主路径并发及 SQLite lock 降级测试。

验证：SQLite 全量 `1571 passed, 37 skipped`；PostgreSQL 全量 `1601 passed, 7 skipped`；`compileall`、`git diff --check` 通过。仅保留既有 FastAPI/Pydantic 弃用及异步资源 warning，无测试失败。

明确延期：全局 referral release job 在第二产品接入时按真实部署选择“每产品独立 job”或“遍历启用产品”；当前入口已支持显式 `app_id`，生产仅有 zhaoxi，本轮不改变默认行为。

## 5. 延期范围：等待 Fatetell PRD 后重新拆工单

以下内容不属于 MP-01…MP-06，本轮不开发：

- `app/products/fatetell/` 目录、manifest、领域模型、repository 和 jobs。
- `/v1/products/fatetell/*` 路由、Fatetell OTP/session/account bootstrap 与客户端契约。
- Fatetell persona、context blocks、ToolPolicy、命盘/命格等领域数据。
- 为 Fatetell 修改 `AgentRuntimePort` 或新增候选端口。
- `ProactiveIntent` 去 Companion World 化、Fatetell MemorySink、ProactiveDeliveryAdapter、通知存储和 scheduler。
- Fatetell 端到端、开量、监控与运营后台。
- 第二产品的 referral 定时释放编排；接入时必须显式指定或遍历产品，不能无意识依赖默认 zhaoxi。

重新开工前，Fatetell PRD 至少需要明确：

1. 用户入口、渠道与 API/客户端边界。
2. 一个 membership 下的入口 account 与 Runtime account 数量/所有权模型。
3. onboarding、persona、context、工具与记忆层级需求。
4. 订阅/钱包/配额/邀请规则及新客权益。
5. 主动消息的触发、投递渠道、频控、通知存储与退订策略。
6. 命理领域实体、隐私/合规、数据保留与删除要求。

PRD 冻结后再按真实调用点拆三类工单：产品 API/composition、Runtime/Memory/Proactive 接入、双产品端到端发布。届时允许对通用端口做一次有真实调用方支撑的泛化，不提前造抽象。

## 6. 下一步

Phase 1 已生产发布。MP-07A 按顺序提交分四批推进：

1. Companion World 垂直切片归入 `app/products/zhaoxi/`，路由组合收口到 `app/bootstrap/`（已合入 PR #53）。
2. 对根目录中归属明确的共享能力做 platform/agent_runtime 归位（已完成开发与聚焦回归）：
   - 平台层按 `auth`、`quota`、`media`、`search`、`gateways`、`observability` 归组；
   - Runtime 按 `llm`、`context`、`persistence` 归组；
   - `app.main:app`、`scripts/run_*.py` 入口保持不变。
3. 对 onboarding、memory、mission、relationship 等朝夕模块归位（已完成开发与聚焦回归）：
   - application 按 `memory/`、`missions/`、`prompts/` 归组；领域使命模板和 SOUL 模板随 owner 一并迁移；
   - dreaming 与 user-meta scheduler 归入朝夕 `jobs/`；
   - `app/products/zhaoxi/application/__init__.py` 保留兼容导出，但改为懒加载以避免 package 初始化环。
4. 收口剩余归属明确的业务包与文档体系（已完成开发与聚焦回归）：
   - `proactive` 整体归入朝夕产品；moderation 归入共享 Platform；
   - proactive/notification/mission/user-meta/campaign persistence 与产品 routes/tools 随 owner
     归位，`app.db` 通过懒加载 façade 继续兼容旧公共导入；
   - 接入节点登记/账号路由 persistence 归入 `platform/gateways`，Platform 禁止通过
     `app.db` façade 隐式加载产品；
   - 文档统一为 `architecture / product / plans / ops / archive` 生命周期，已完成或被取代的
     计划归档，重复的 Companion World 简报删除，并新增本地 Markdown 链接门禁。

完成四批后，`app/` 根目录只保留 8 个 Python 文件：入口/包文件 `main.py`、`__init__.py`，
跨层稳定原语 `config.py`、`schemas.py`、`time_utils.py`，以及仍待真实第二产品调用点后再泛化的
`turn_service.py`、`prompt_builder.py`、`reminder_utils.py`。其中前五个不是业务平铺；后三个是
有意保留的过渡模块，不能在 Fatetell 契约未冻结时强塞入 Runtime 或朝夕目录。

MP-07A 不创建 `app/products/fatetell/`，不修改数据库或外部 API。Fatetell 的产品命名空间、
Runtime/Memory/Proactive 接入和端到端发布仍按 §5 等待 PRD 后拆单。

### MP-07A 后仍有意保留的混合边界

- `routers/web.py`、`routers/app_api.py` 同时承载共享身份 bootstrap 与朝夕 legacy API；等第二产品
  路由冻结后再拆，避免仅换目录却继续混责。
- `products/zhaoxi/api/admin_accounts.py` 已归产品所有，但内部仍组合平台账号/资产 base view 与
  朝夕扩展字段；第二产品接入时应抽平台 base query，由各产品追加 projection。
- `routers/admin_ops.py` 与 `serializers.py` 仍含少量朝夕 scheduler/proactive 展示逻辑；应按
  endpoint/helper 拆分，不能把整个共享运维面搬进产品。
- `tools/definitions.py` / `registry.py` / `executor.py` 仍组合通用工具和朝夕 tool schema；Fatetell
  接入时由真实工具差异催生 `ToolPolicy`，不提前创建空抽象。
- `db/__init__.py` 与 `_core.py` 仍是 schema/migration 兼容入口；产品 persistence 已物理归位，
  但 migration registry 和共享事务边界不在本轮拆分。
- `turn_service.py`、`prompt_builder.py`、`reminder_utils.py` 仍是显式过渡模块，处理条件同上。
