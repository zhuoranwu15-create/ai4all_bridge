# 技术设计：Companion World M3 后端实现规范

> 状态：**M3-0…M3-6 全部完成（2026-07-22）；Feed、AI world-content scheduler、App 通知收件箱与真人级 proactive 均已 default-off 闭环。**
>
> 性质：实现级规范（buildable spec），冻结 M3 migration、状态机、API、幂等、锁与 rollout 边界。上位产品决策以 [`../../../architecture/designs/companion_world_3_0_refactor_design.md`](../../../architecture/products/zhaoxi/companion_world_3_0_refactor_design.md) D-10/D-13/D-15 为准。
>
> 实施计划：[`companion_world_m3_implementation_plan.md`](companion_world_m3_implementation_plan.md)。
>
> 历史交付基线：PR #45 merge commit `363500ea8364dcccd9e7c6e3c7c5eb5ce7ed9392`；M3 交付时 migration max = m0033。其后 M4/M5 已顺序追加 m0034/m0035，m0036 再修复历史编号碰撞；当前整体交付状态以总 ADR 为准。

## 0. 范围、术语与不变量

M3 首版只交付三个数据闭环：文字世界 Feed、App 拉取式通知收件箱、真人级 proactive 上提。新增三张表：

1. `universe_posts`：用户文字动态、AI 文字动态及 AI slot claim。
2. `companion_world_outbox`：Feed 领域事件事务 outbox。
3. `app_notifications`：App 收件箱、隐藏投递 reservation 与真人级 24 小时 claim。

不新增 Push token/APNs/FCM，不修改 `CHANNEL_APP.supports_proactive=false`，不把 Feed 伪装成 proactive，不纳入图片/音视频/评论/点赞/信箱/离场/访客/真人聊天。

贯穿不变量：

- L1/L2、私聊、reminder/commitment 仍以 `account_id` 隔离；World owner 数据以 `platform_user_id/universe_id` 隔离。
- 客户端不提交 `account_id`、`runtime_account_id` 或 `universe_id`；home world 一律由 Bearer session 的 `platform_user_id` 服务端解析。
- Feed 与通知分表、分 API、分 cursor、分红点；Feed 拉取不改变通知 read 状态。
- AI Feed 按 universe + 北京自然日 + slot 聚合，不按 resident 扇出；App-only 真人级 proactive 按 platform user 聚合，不按 runtime account 扇出。
- `app.domains.companion_world.*` 不 import `app.db.*`、FastAPI、`turn_service`；Runtime 不认识 Feed/通知/universe due。proactive core 只产出 typed delivery intent，不直接写 `app_notifications`。
- 所有新时间列沿用仓库约定：DB 存北京 naive `TEXT`（`YYYY-MM-DD HH:MM:SS`），API 序列化为带 `+08:00` 的 ISO 8601。滚动窗口按真实 24 小时计算，北京日界只用于 Feed slot。

## 1. Schema 与迁移

M3 从 m0032 后追加 migration；不修改历史 migration，不用启动期 `_ensure_column` 代替新表。下面是仓库 canonical SQLite 方言，现有 `_backend` 负责 PG 占位符、北京时间和整数类型翻译。

### 1.1 `universe_posts`

```sql
CREATE TABLE IF NOT EXISTS universe_posts (
    id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL,
    author_type TEXT NOT NULL,                 -- human | resident
    author_platform_user_id TEXT,              -- human 必填；resident 为空
    author_resident_id TEXT,                   -- resident 必填；human 为空
    source_type TEXT NOT NULL,                 -- user_post | ai_feed
    content_type TEXT NOT NULL DEFAULT 'text', -- M3 恒为 text
    text TEXT,                                 -- 1..2000 code points；claim 阶段可空
    status TEXT NOT NULL,                      -- generating | published | skipped | deleted
    client_request_id TEXT,                    -- user_post 幂等键原文，AI 为空
    request_fingerprint TEXT,                  -- sha256(canonical request)
    ai_local_date TEXT,                        -- YYYY-MM-DD，北京日；user_post 为空
    ai_slot TEXT,                              -- morning | evening；user_post 为空
    slot_window_end_at TEXT,                   -- AI 过此时刻不得再发布
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    claim_token TEXT,
    next_attempt_at TEXT,
    terminal_reason TEXT,
    published_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(author_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(author_resident_id) REFERENCES universe_residents(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_user_request
    ON universe_posts(universe_id, author_platform_user_id, client_request_id)
    WHERE source_type = 'user_post';
CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_ai_slot
    ON universe_posts(universe_id, ai_local_date, ai_slot)
    WHERE source_type = 'ai_feed';
CREATE INDEX IF NOT EXISTS ix_universe_posts_feed
    ON universe_posts(universe_id, status, published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_universe_posts_ai_claim
    ON universe_posts(source_type, status, next_attempt_at, claimed_at);
```

应用层在写入前强制下列组合约束：

- `human/user_post`：`author_platform_user_id`、`client_request_id`、`request_fingerprint` 非空；AI slot 列与 resident author 为空。
- `resident/ai_feed`：`author_resident_id`、`ai_local_date`、`ai_slot`、`slot_window_end_at` 非空；human author/client request 为空。
- `published`：`text`、`published_at` 非空；Feed API 只读 `status='published'`。
- `skipped/deleted`：`terminal_reason` 非空；`deleted` 只由已发布动态进入。

AI 每日上限不另存计数器：只有 `morning/evening` 两个合法 slot，`ux_universe_posts_ai_slot` 直接证明同 universe 同北京日最多两次 slot、每窗最多一次。用户动态不带 slot，不占 AI 名额。

### 1.2 `companion_world_outbox`

```sql
CREATE TABLE IF NOT EXISTS companion_world_outbox (
    id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    event_type TEXT NOT NULL,                   -- universe_post.published.v1 | universe_post.deleted.v1
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | processing | delivered | dead
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    claimed_at TEXT,
    claim_token TEXT,
    last_error TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(post_id) REFERENCES universe_posts(id)
);

CREATE INDEX IF NOT EXISTS ix_companion_world_outbox_claim
    ON companion_world_outbox(status, available_at, claimed_at, id);
CREATE INDEX IF NOT EXISTS ix_companion_world_outbox_post
    ON companion_world_outbox(post_id, event_type);
```

发布/删除 post 与对应 outbox INSERT 必须同一事务。outbox 提供 at-least-once，不宣称无法实现的 exactly-once；worker 重放安全来自稳定 `idempotency_key` 和消费端幂等。M3 不删除 delivered 行，物理保留期由后续统一审计策略决定，不把未冻结天数写死。

### 1.3 `app_notifications`

```sql
CREATE TABLE IF NOT EXISTS app_notifications (
    id TEXT PRIMARY KEY,
    platform_user_id TEXT NOT NULL,
    universe_id TEXT NOT NULL,
    resident_id TEXT,
    scope TEXT NOT NULL,                        -- resident | human
    category TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    delivery_status TEXT NOT NULL,              -- reserved | visible | cancelled
    title TEXT,
    body_text TEXT,
    target_type TEXT NOT NULL DEFAULT 'none',   -- none | conversation | feed
    target_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    claim_token TEXT,
    claim_expires_at TEXT,
    delivered_at TEXT,
    read_at TEXT,
    expires_at TEXT,
    cancelled_at TEXT,
    terminal_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(resident_id) REFERENCES universe_residents(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_app_notifications_user_idempotency
    ON app_notifications(platform_user_id, idempotency_key);
CREATE INDEX IF NOT EXISTS ix_app_notifications_list
    ON app_notifications(platform_user_id, delivery_status, delivered_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_app_notifications_unread
    ON app_notifications(platform_user_id, delivery_status, read_at, expires_at);
CREATE INDEX IF NOT EXISTS ix_app_notifications_human_window
    ON app_notifications(platform_user_id, scope, delivery_status, delivered_at, claim_expires_at);
CREATE INDEX IF NOT EXISTS ix_app_notifications_cleanup
    ON app_notifications(delivery_status, expires_at, claim_expires_at, id);
```

`reserved` 是服务端隐藏的真人级 claim，不进入列表、红点或 200 条上限；`visible` 才是用户通知。公开 read 状态由 `read_at` 派生，不与投递工作流状态混成一个枚举：

- `visible + read_at IS NULL` = `unread`；`expires_at = delivered_at + 30 days`。
- `visible + read_at IS NOT NULL` = `read`；标记时把 `expires_at` 改为 `read_at + 7 days`。
- `reserved` 必须有 `claim_token/claim_expires_at`，正文允许为空。
- `visible` 必须有 `body_text/delivered_at/expires_at`，`body_text` 为 1..1000 code points，`title` 为空或 1..80。
- `visible` 转换时必须写入当时 active 的 `resident_id`；`reserved` 可在最终选择 speaker 前暂为空。后续 resident 离场不删除历史通知，列表仍可用保留的 resident/template projection 展示当时发声人。
- `cancelled` 不展示、不占 24 小时成功触达额度；必须写 `terminal_reason/cancelled_at`，并设置 `expires_at=cancelled_at+7 days` 供 central cleanup。

首版 category 只允许现有 registry 值：`user_reminder`、`companion_followup`、`new_user_reactivation`、`content_invitation`、`content_invitation_response`、`task_result`。`scope='human'` 只允许 `new_user_reactivation`、`content_invitation`、`companion_followup` 且 source 必须区分 `account_check`；commitment 的 `companion_followup` 始终是 `scope='resident'`。

### 1.4 FK 与删除策略

M3 不做 cascade delete：动态、通知和 outbox 都是审计/幂等依据，父资源的业务下线使用状态字段。账号彻底擦除走现有显式 lifecycle UoW，顺序固定为：

1. `companion_world_outbox`
2. `app_notifications`
3. `universe_posts`
4. 既有 conversation/resident/universe 数据

仓库 PG 垫片当前会剥离 inline FK，因此 PG 上 repository owner predicate、删除顺序与隔离测试是硬保证；不得把“DDL 写了 FK”当作 PG 已强制。SQLite 继续开启现有 FK 校验。M3 不借机重构全仓 PG FK 策略。

## 2. 状态机与内容发布

### 2.1 用户文字动态

```text
request
  ├─ validation / owner fail → 无行，返回稳定错误
  └─ INSERT published + outbox(published)（同事务）

published ──未来内容治理确认需下架──> deleted + outbox(deleted)
```

- world 必须 `status='active' AND onboarding_state='confirmed'` 且存在 active resident；用户是 human author。
- API 接受 `client_request_id`（8..128）和非空 `text`（trim 后 1..2000）。同幂等键 + 同 fingerprint 返回原 post；同键不同正文返回 `idempotency_conflict`。
- M3 默认直接发布，不调用现有 `content_moderation_tasks`、同步 guard 或 machine moderation worker；发布 post 与 published outbox 必须同事务。
- 保留领域级 `delete_post(reason_code)` 下架接缝，但 M3 不新增审核路由、不自动执行下架。后续审核策略和测试单独设计后再接入。
- Feed 内容归属是 `platform_user_id/universe_id/post_id/source_type`，不得为了兼容现有审核表而挑选任意 resident runtime `account_id`。未来真人内容治理面向 post/world/platform user；AI 内容治理面向 post 与生成链路，默认不得处罚用户或 resident runtime account。

### 2.2 AI Feed slot

```text
不存在 slot 行 ──原子 INSERT──> generating
generating ──生成成功且仍在窗内、author active──> published + outbox
generating ──永久失败/窗口关闭──> skipped
published ──确认风险──> deleted + outbox
```

eligibility 必须同时满足：

- `universes.status='active'`、`onboarding_state='confirmed'`；
- 至少一位 `universe_residents.status='active'`；
- owner 在 `now - 7 days` 后有任一渠道 inbound。聚合 owner binding 与所有 resident runtime account 的 channel inbound，不能只查选中 resident；
- 当前北京时间落在已配置 morning/evening 窗口内。

claim 规则：

1. 计算 `ai_local_date` 与 `ai_slot`，尝试 INSERT `status='generating'`。唯一冲突表示本窗已被处理，直接 skip，不读后再写。
2. 生成在事务外执行；`claim_token` 防旧 worker 覆盖新 claim。stale claim 只可在同一窗口内 CAS 重领，重试退避由 `next_attempt_at` 控制。
3. 到 `slot_window_end_at` 后禁止新 claim、重领与 publish；遗留 generating/pending 行置 `skipped(window_closed)`，绝不跨窗、跨日补发。
4. AI author 在 claim 时从 active residents 中确定性选择：最近 App 会话活动优先，其次 `joined_at DESC, resident_id ASC`。发布前必须复核该 resident 仍 active；失活则本窗 skip，不把已按旧 resident 生成的内容冒充为另一 resident。
5. `published` 更新和 outbox INSERT 同事务；只有 published 行计入 Feed。M3 不在生成与发布之间插入审核等待状态。

窗口内的 transient retry 不是“补发”；窗口关闭后的任何 publish 才是被禁止的 catch-up。`attempt_count` 上限属于运维配置，达到上限提前 `skipped(retry_exhausted)`。

### 2.3 outbox worker

- PG claim：短事务内 `SELECT ... WHERE status='pending' AND available_at<=now ORDER BY available_at,id LIMIT ? FOR UPDATE SKIP LOCKED`，更新 `processing/claim_token/claimed_at/attempts` 后提交；handler 在事务外运行。
- SQLite：`BEGIN IMMEDIATE` + 条件 UPDATE，仅作功能回退；并发正确性由 PG 测试证明。
- 成功以 `WHERE id=? AND status='processing' AND claim_token=?` CAS 为 `delivered`；失败按退避回 pending，超过上限为 dead 并报警。
- processing lease 超时可重领；旧 worker token 失效。consumer 必须以 outbox `idempotency_key` 去重。
- v1 payload 只含公开 post DTO 所需 ID/类型/时间，不放 runtime account、persona、私聊原文或整段内部 prompt。

## 3. 通知、保留与真人级 claim

### 3.1 visible 写入与 200 条硬上限

所有通知写入先解析 server-side owner，并在单事务内按以下顺序执行：

1. PG `SELECT id FROM platform_users WHERE id=? FOR UPDATE`；SQLite 写事务。
2. 校验 universe owner、resident 归属与 active/read-only 语义；禁止调用方传另一真人 ID 覆盖解析结果。
3. 以 `(platform_user_id,idempotency_key)` INSERT/命中已有行；同键不同 fingerprint 为内部 `idempotency_conflict`，不得静默覆盖正文。
4. 变为 visible 后先删除本真人逻辑过期行，再维持 visible 总数 ≤200：
   - 先删 read，排序 `read_at ASC, delivered_at ASC, id ASC`；
   - 仍超限再删 unread，排序 `delivered_at ASC, id ASC`。
5. 提交后才返回成功。

`reserved/cancelled` 不计 200；新插入 visible 是最新行，因此满 200 unread 时淘汰最旧 unread，不会误删新通知。所有 DELETE 必须显式带 `platform_user_id=?`。

### 3.2 read 与逻辑过期

- list/count/read/read-all 都先用 `delivery_status='visible' AND expires_at>now` 逻辑过滤，不等待 scheduler。
- 单条 read：`WHERE id=? AND platform_user_id=?`；不存在、越权、已过期统一 `notification_not_found`。重复 read 幂等返回原 `read_at`，不延长 7 天。
- read-all：在 platform user 行锁下把“调用线性化时刻之前”的当前有效 unread 批量置同一 `read_at=now, expires_at=now+7d`；并发中在锁释放后插入的新通知保持 unread。
- unread_count 只数有效 visible unread；Feed 从不影响该数字。
- central scheduler 把 lease 过期 reservation CAS 为 `cancelled(claim_expired)` 并设置 7 天到期，再物理删除 `expires_at<=now` 的 visible/cancelled；随后按 platform user 锁对账 visible ≤200。单用户失败不阻断后续用户，结果写入 central heartbeat；node scheduler 不重复执行。

### 3.3 真人级 App-only proactive claim

真人级类别集合为：`new_user_reactivation`、`content_invitation`、`companion_followup/account_check`。scheduler 的扫描主体、due key、活跃与预算主体都是 `platform_user_id`，不是 N 个 runtime account。

claim 算法：

1. 先做无锁候选计算：跨 owner bindings + world resident accounts 聚合 last inbound/预算，计算稳定 `source_dedupe_key`。
2. 若存在 legacy primary 的真实微信主动路由，同一个 due key 继续走现有微信路径；不得同时创建 App-only reservation，也不得借 primary 通道冒充其他 resident。
3. 仅在 App inbox flag 与 App-only human flag 都开启时进入 App claim。
4. 事务内锁 `platform_users` 行，清理该真人过期 reservation；若存在：
   - `scope='human' AND delivery_status='visible' AND delivered_at > now-24h`，或
   - 未过期 `scope='human' AND delivery_status='reserved'`
   则 claim 失败。边界 `delivered_at <= now-24h` 可再次触达。
5. INSERT hidden `reserved`，幂等键为 §5 格式，设置短 lease；提交后在事务外生成、做用户开关/类别开关、跨渠道活跃、quiet hours、现有预算与 moderation。
6. 最终投递事务再次锁 platform user；按 §3.4 选择/重选 speaker，并锁对应 resident 行复核 active；随后把 reservation CAS 为 visible、设置正文/30 天到期，并执行 200 条上限淘汰。
7. policy/moderation/生成失败把本 reservation CAS 为 cancelled；crash 由 lease 回收。同 fingerprint 的同一 due key 在仍有效时允许把 cancelled 行 CAS 回 reserved，而不是插入第二行。同一真人任一时刻最多一条 live human reservation。

24 小时额度只以成功 `visible.delivered_at` 消耗；pending reservation 只作并发互斥。per-resident reminder/commitment 直接写 `scope='resident'`，不检查 App-only human flag，也不进入真人级 24 小时桶，但仍受 App inbox flag、既有义务语义和 200 条总上限。

### 3.4 App 发声人

候选必须属于当前 home universe 且 `status='active' AND runtime_account_id IS NOT NULL`。排序冻结为：

1. 最近收到真人 inbound 的 resident，按聚合后的 `last_inbound_at DESC`；
2. 若所有 resident 均无 inbound，按最近 App conversation activity `last_activity_at DESC`；
3. 再按 `joined_at DESC NULLS LAST`；双后端 SQL 写成 `CASE WHEN joined_at IS NULL THEN 1 ELSE 0 END, joined_at DESC`，不依赖方言默认 NULL 排序；
4. 最后 `resident_id ASC`。

时间并列继续走下一键，保证 SQLite/PG 结果稳定。首版不接收用户指定 speaker。计划阶段选中的 resident 只作 hint；最终 visible 事务必须重跑排序并锁最终 resident，防 resident 在计划与投递之间失活。

该策略只决定 App 通知展示的 resident，不改变内容所属的 per-resident L1/L2，不允许用 A 的人设生成后标成 B。若原内容与原 speaker 强绑定而原 speaker 失活，则 cancel，不重标；真人级通用内容才允许重选后再生成/投递。

## 4. API 与 DTO

沿用 P1 成功/失败信封、Bearer session、`Cache-Control: no-store` 与 `/v1` 直接路由；外部网关可映射 `/api/v1`。所有列表 `limit` 默认 20、范围 1..50，服务端取 `limit+1` 判断 `next_cursor`。

### 4.1 Feed

| 方法 | 路径 | 请求 | 响应/语义 |
|---|---|---|---|
| GET | `/v1/worlds/home/feed` | `cursor?`, `limit?` | 只列 owner home world 的 published posts；不改任何 read 状态 |
| POST | `/v1/worlds/home/feed/posts` | `{client_request_id,text}`，extra forbid | 首次发布 `201` 返回 published post；幂等重试 `200` 返回同一 post |

Feed item：

```jsonc
{
  "post_id": "post_...",
  "author": {
    "type": "human | resident",
    "resident_id": "res_... | null",
    "name": "展示名 | null",
    "avatar_ref": "... | null"
  },
  "content": {"type": "text", "text": "..."},
  "source": "user_post | ai_feed",
  "published_at": "2026-07-22T09:30:00+08:00"
}
```

resident author 的 name/avatar 来自 resident/template owner-scoped projection；human author 的 name 取 `platform_users.display_name`，缺失时为 null，avatar 首版为 null。客户端按 `author.type='human'` 渲染“我”，后端不硬编码本地化文案。

列表固定 `ORDER BY published_at DESC,id DESC`。cursor 是 opaque base64url JSON `{v:1,published_at,id}`；下一页 predicate 为 `(published_at < ?) OR (published_at = ? AND id < ?)`。无效版本/格式返回 `invalid_cursor`，cursor 不包含 owner/runtime account。

### 4.2 通知

| 方法 | 路径 | 请求 | 响应/语义 |
|---|---|---|---|
| GET | `/v1/notifications` | `status=all|unread`（默认 all）、`cursor?`,`limit?` | 拉取不自动 read，只返回未逻辑过期 visible |
| GET | `/v1/notifications/unread-count` | — | `{unread_count}`，独立红点 |
| POST | `/v1/notifications/{notification_id}/read` | 空体 | 幂等单条 read |
| POST | `/v1/notifications/read-all` | 空体 | `{marked_count,read_at}` |

Notification item：

```jsonc
{
  "notification_id": "ntf_...",
  "category": "companion_followup",
  "scope": "resident | human",
  "resident": {"resident_id": "res_...", "name": "...", "avatar_ref": "... | null"},
  "title": "... | null",
  "body": {"type": "text", "text": "..."},
  "target": {"type": "none | conversation | feed", "id": "... | null"},
  "status": "unread | read",
  "read_at": null,
  "created_at": "2026-07-22T18:00:00+08:00",
  "expires_at": "2026-08-21T18:00:00+08:00"
}
```

`created_at` 对外取 `delivered_at`，不暴露 reservation 创建时间。列表固定 `ORDER BY delivered_at DESC,id DESC`，cursor 为 `{v:1,delivered_at,id}`，predicate 与 Feed 同构。`status=unread` 只是 filter，不生成第二套 cursor 语义。

### 4.3 稳定错误码

| code | HTTP | 触发 |
|---|---:|---|
| `not_found` | 404 | 对应 rollout flag 关闭，隐藏整组新路由 |
| `unauthorized` | 401 | 无效 session |
| `invalid_request` | 422 | 字段类型、长度、extra 字段不合法 |
| `account_id_not_accepted` | 400 | 请求携带 account/runtime account/universe ID |
| `invalid_cursor` | 400 | cursor 格式、版本或字段非法 |
| `world_not_ready` | 409 | home world 未 confirmed 或无 active resident |
| `world_disabled` | 403 | universe disabled |
| `idempotency_conflict` | 409 | 同 client/idempotency key 对应不同 fingerprint |
| `notification_not_found` | 404 | 通知不存在、越权、非 visible 或已过期；防枚举统一 |

API 永不返回内部 `runtime_account_id`、claim token、fingerprint、terminal reason、outbox payload 或 metadata。

## 5. 幂等键与 fingerprint

键只使用服务端已验证 ID；任意用户输入先做长度/字符集约束再 canonicalize。fingerprint 使用 UTF-8 canonical JSON（key 排序、无多余空白）的 SHA-256 hex。

| 场景 | 唯一键格式 | fingerprint 输入 |
|---|---|---|
| 用户 Feed | DB unique `(universe_id,platform_user_id,client_request_id)` | `{v:1,text:trimmed_text}` |
| AI Feed slot | DB unique `(universe_id,ai_local_date,ai_slot)` | slot 本身，不另收客户端键 |
| post published outbox | `world-post-published:v1:{post_id}` | immutable event payload |
| post deleted outbox | `world-post-deleted:v1:{post_id}` | `{post_id,deleted_at,reason_code}` |
| per-resident obligation | `resident-obligation:v1:{source_type}:{source_id}` | owner/universe/resident/category/正文/target |
| 真人级 proactive | `human-proactive:v1:{category}:{source_dedupe_sha256}` | owner/category/due/source payload |

同键同 fingerprint 返回已有结果；同键异 fingerprint fail-closed。禁止用随机 UUID 代替业务幂等键，也禁止把原始用户文本拼进 key。

## 6. 锁顺序与 PG 并发证明

M3 新增锁顺序：

| 序 | 锁/约束 | 用途 |
|---|---|---|
| M0 | 唯一约束 | request/slot/outbox/notification 幂等，逻辑最先 |
| M1 | `platform_users FOR UPDATE` | 真人级 claim、通知写入/read-all/上限清理 |
| M2 | `universe_residents FOR UPDATE` | 最终 speaker active 复核；只在已持 M1 的真人级投递中使用 |
| M3 | notification 行条件 UPDATE | reservation CAS、单条 read |

- AI slot 只靠 unique INSERT/CAS，不锁 universe；LLM/handler 网络调用绝不持 DB 锁。
- outbox worker 只锁 outbox rows 且 `SKIP LOCKED`，不在 claim 事务内锁 post/user/resident。
- 通知路径若需 resident 锁必须 M1→M2；不得先锁 resident 再锁 platform user。
- M3 路径不同时获取 P1 的 universe/conversation/quota/wallet 锁。未来 M4 若一个事务同时需要 platform user 与 universe，必须按 `platform_user → universe → resident → conversation → quota` 扩展全序；钱包仍独立事务。

PG 硬门禁至少证明：

1. 同 universe 同 slot N worker 只生成一行 claim，morning/evening 合计最多两行；关闭窗口后无 late publish。
2. post publish + outbox 同事务，worker 并发/lease 重领不丢事件，consumer 同 key 只产生一次副作用。
3. 同 platform user N resident 并发真人级 due 只产生一条 live reservation/visible 通知，滚动 24 小时边界准确。
4. 通知 insert、read/read-all、cleanup 并发时 visible ≤200，淘汰顺序正确且不删除其他 platform user 行。
5. speaker 在计划后失活时不会冒充；可重选的通用内容按确定性顺序重选，强绑定内容 cancel。

SQLite 只验证状态机、DTO、幂等和清理功能，不作为上述竞争结论证据。

## 7. Rollout 配置

三个新开关最终命名与边界冻结如下，代码默认均为 `false`：

| 环境变量 | 门控 | 不门控 |
|---|---|---|
| `COMPANION_WORLD_FEED_ENABLED` | Feed 两个 API、AI world-content scheduler、Feed outbox consumer | P1 API/L3/proactive/通知 |
| `COMPANION_WORLD_APP_INBOX_ENABLED` | 通知 API、AppInboxAdapter visible 写入 | 微信 outbound；App-only human 候选生成决策；既有通知 cleanup |
| `COMPANION_WORLD_APP_ONLY_HUMAN_PROACTIVE_ENABLED` | 无真实微信路由时的真人级 App reservation/投递 | per-resident reminder/commitment；通知读 API；微信 legacy 路径 |

依赖关系不是开关合并：World API 仍受既有 `COMPANION_WORLD_P1_ENABLED` 隐藏门控；App-only 投递有效值为 `APP_INBOX && APP_ONLY_HUMAN_PROACTIVE`。新开关不得替代 `COMPANION_WORLD_L3_BACKGROUND_ENABLED`、`COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED` 或现有 moderation/safety flags。

Feed 窗口配置形状：

```dotenv
# 均为北京本地 HH:MM；默认留空。FEED_ENABLED=true 时四项必须全部有效。
COMPANION_WORLD_FEED_MORNING_START=
COMPANION_WORLD_FEED_MORNING_END=
COMPANION_WORLD_FEED_EVENING_START=
COMPANION_WORLD_FEED_EVENING_END=
```

- timezone 固定 `Asia/Shanghai`，不做可配置项；产品不变量是北京自然日。
- 采用半开区间 `[start,end)`；必须满足 `00:00 <= morning_start < morning_end <= evening_start < evening_end <= 24:00`，禁止重叠/跨日。
- flag=false 时允许四项为空；flag=true 且缺失/非法则进程启动 fail-fast。本文不编造生产窗口值，由部署配置评审填入。
- scheduler interval、batch、claim lease、retry max 是运维参数，不改变“每窗 1、每日 2、不补发”；其默认值在 M3-3 代码评审时按现有 scheduler 约定给出。

## 8. 进程与发布/回滚

- `world-content scheduler` 是独立中心单例，只做 eligibility/slot claim/generation；不得挂到每个厚节点 proactive 扫描。
- outbox worker 可多实例，靠 PG `SKIP LOCKED` + lease；通知 TTL/上限 cleanup 是 central proactive scheduler 的独立维护步骤，失败隔离并写 heartbeat。
- 部署顺序：migration → 全部 default-off 代码 → 开 App inbox API/per-resident adapter → 开用户 Feed → 开 AI Feed scheduler → 最后灰度 App-only human proactive。
- 回滚先关对应新 flag并停 world-content scheduler；保留三表加性数据，不逆迁移。关 App-only human flag不得影响已存在通知读取和 per-resident obligations。
- PR #45 已合并；M3-1 运行时代码基于合并后的 `origin/main` 新分支实施。

## 9. M3-0 出口检查

- [x] 三表 DDL、索引、FK/删除策略已冻结。
- [x] Feed/outbox/notification 状态机、幂等键与“默认直接发布、审核后续单独设计”边界已冻结。
- [x] Feed/通知 DTO、cursor、错误码和 owner ACL 已冻结。
- [x] AI slot、outbox、真人级 24 小时 claim 与通知上限的 PG 锁/竞争证明已定义。
- [x] 三个正交 rollout flag 与北京窗口配置形状已冻结。
- [x] `character_letters`/mailbox、Push、媒体/互动继续留在 M4/后续，不进入 M3。

M3-1 已交付 m0033 与存储原语；M3-2 已交付用户文字直接发布、post+outbox 同事务、owner Feed API、opaque cursor、下架接缝和独立 default-off Feed flag；M3-3 已交付独立中心 `world-content scheduler`、北京双窗口/7 日 eligibility、确定性 resident 作者、slot claim/retry/no-catch-up、事务 outbox worker、跨 batch/slot 游标及 heartbeat。PG 门禁证明同 slot stale reclaim 只有一个 winner、outbox worker claim 不重叠且旧 token 不能覆盖新 lease。

M3-3 验证：SQLite 全量 `1454 passed / 13 skipped`；PostgreSQL 全量 `1462 passed / 5 skipped`。M3-4 已交付 typed `AppInboxAdapter`、owner-scoped 通知 API、显式单条/全部已读、7/30 天逻辑过期、read→unread 的 200 条淘汰及 central-only cleanup/heartbeat；`CHANNEL_APP.supports_proactive` 仍为 false，inbox flag 只放行 per-resident reminder/commitment，真人级 App-only 继续 fail-closed 等待 M3-5。

M3-4 验证：SQLite 全量 `1459 passed / 14 skipped`；PostgreSQL 全量 `1468 passed / 5 skipped`；PG 门禁证明同真人并发 visible 写入仍维持硬上限，`compileall` 与 `git diff --check` 通过。

M3-5 已交付真人级纯领域契约、owner-scoped due/活跃/预算聚合、真实微信 legacy primary 优先、App-only 双 flag、滚动 24 小时 reservation、token CAS 与投递前 speaker 重选/锁定。现有候选默认强绑定原 resident，失活即 cancel；显式通用内容才允许重选。精确 24h 边界、旧 token、跨 resident 活跃/预算、微信/App 竞争和 PG 同真人并发均通过。

M3-6 已交付 Feed claim/skip/retry/outbox lag、通知 cleanup、真人级 claim/24h/speaker 的 heartbeat 字段，以及三 flag 灰度/回滚、central 单例部署和只读对账 SQL。最终门禁：unit `567 passed`；SQLite `1465 passed / 14 skipped`；PostgreSQL `1474 passed / 5 skipped`；`compileall` 与 `git diff --check` 通过。M3 至此完成，继续保持三个新 flag default-off；生产开量仍受 P1 模板、backfill、客户端版本与现场对账门槛约束。
