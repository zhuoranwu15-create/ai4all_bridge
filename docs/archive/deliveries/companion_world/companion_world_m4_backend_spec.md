# Companion World M4 Lifecycle + Mailbox 后端实现规范

> 状态：**M4-0 已冻结；M4-1…M4-6 已完成并归档（2026-07-23）。**
>
> 历史交付基线：PR #46 已合并为当时的 `origin/main@e230844`，M4-0 文档提交为 `4ff932b`。M4 PR #47 后续已合并，M5 再追加 m0035，m0036 修复历史编号碰撞；当前整体交付状态以总 ADR 为准。
>
> 权威上位决策：[`../../../architecture/designs/companion_world_3_0_refactor_design.md`](../../../architecture/designs/companion_world_3_0_refactor_design.md) D-02/D-06/D-08/D-12、§9、§10.3/.4/.8、§12 M4。

## 0. 目标与边界

M4 在 M2 的 resident/conversation 基座与 M3 的 Feed/outbox 上交付两个可独立关闭的闭环：

1. **Lifecycle**：服务端生成离开候选，经过证据窗口、冷静期、危机冻结与人工审核后，以单事务完成 resident `offline`、conversation `read_only`、唯一 farewell post 和 outbox。
2. **Mailbox**：运营版本化角色目录向符合资格的世界低频投递私人来信；用户明确接受后，以单事务创建 resident runtime、active resident 与 conversation。

M4 不包含：

- 用户主动移除已确认的 active resident；客户端不新增 departure/remove API。
- `legacy` resident 离开；D-08 豁免保持永久有效。
- 自动生成陌生角色、公开角色市场、排行榜或推荐流。
- Push/APNs/FCM；来信不写 M3 `app_notifications`，客户端只拉 mailbox 未读数。
- Feed 审核通用化、媒体 farewell、访客/邀请码/真人聊天（M5）。
- `offline → active`、召回、复活或复用同一关系实例。

## 1. 冻结产品口径

### 1.1 用户主动移除

- 首次确认前可以把 `candidate → dismissed`；确认后用户不能把 `active → offline`。
- `active → offline` 只允许由内部 lifecycle 流程提交，App 只读取最终状态与 farewell。
- `origin='legacy'` 永不进入 lifecycle 候选、人工强制或最后居民例外路径。

### 1.2 Lifecycle 数值与安全策略

| 项目 | 冻结值 |
| --- | --- |
| 长期未互动 | 连续 **60 天**没有 owner 向目标 resident 的用户入站 |
| 价值观不相容证据 | 滚动 **30 天**内至少 **3** 次独立结构化证据，首末跨度至少 **14 天** |
| cooldown | 达标后 **7 天**；期间恢复正常即取消候选 |
| crisis/vulnerability freeze | 最近一次危机/自伤/高度脆弱信号后 **30 天**内阻断普通离开 |
| 首版提交主体 | 自动流程只生成候选；所有不可逆提交均需 full admin 人工批准 |
| 最后一位保护 | 普通原因永久阻断；严重攻击/明确威胁/仇恨骚扰同等级例外仍需 full admin 批准 |

精确边界：`now >= threshold_at/cooldown_until` 才算到期；`now < crisis_freeze_until` 仍冻结。所有 DB 时间继续使用北京 naive 字符串，公开时间统一序列化为带 `+08:00` 的 ISO 8601。

### 1.3 纠错与 farewell

- cooldown/review 阶段可取消或驳回；不产生用户可见记录。
- 提交后禁止直接改库恢复 resident 或 conversation。纠错只追加审计动作，可由 full admin 隐藏不当 farewell；是否给予新关系补偿须另行产品批准。
- farewell 文本必须在事务外生成，在人工审核时确定安全终稿；事务内不调用 LLM/审核服务。
- farewell 不披露内部原因、证据、冷静期、风险分类，不指责/羞辱/威胁用户，不包含付费或保活 CTA。

### 1.4 Mailbox

| 项目 | 冻结值 |
| --- | --- |
| 投递容量门 | 投递事务重查 active `< 8` |
| 接受容量门 | 接受事务重查 active `< 10` |
| open 上限 | 每个 universe 恰好最多 **1** 封；`unread/read/deferred` 均算 open |
| 最短投递间隔 | 同一 universe 两次投递至少 **30 天** |
| 有效期 | 投递后 **30 天**；不向用户展示倒计时 |
| defer | 不延长 `expires_at`，仍占 open 名额 |
| 重复角色 | 同一 `character_key` 不再次投递给同一 universe |
| 角色来源 | 仅运营版本化 catalog；M4 不自动生成角色 |

投递后世界即使增至 8/9 位，letter 仍可保留并在 `<10` 时接受；满 10 时只禁用接受，不替换居民。`now >= expires_at` 即 expired，请求路径与 scheduler 都必须执行该判定。

## 2. m0034 加性数据模型

历史 migration 不改写。M4-1 已在当时的 `_MIGRATIONS` 末尾追加 `m0034_companion_world_lifecycle_mailbox`；M4 交付时最大版本为 m0034，后续 M5 顺序追加 m0035，m0036 再以前向迁移修复历史编号碰撞。

### 2.1 扩展 `universe_posts`

```sql
ALTER TABLE universe_posts ADD COLUMN post_type TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE universe_posts ADD COLUMN departure_event_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_posts_departure_event
    ON universe_posts(departure_event_id)
    WHERE departure_event_id IS NOT NULL;
```

- `post_type`: `normal | farewell`；现有 M3 行迁移后均为 `normal`。
- farewell 固定组合：`author_type='resident'`、`source_type='lifecycle_farewell'`、`post_type='farewell'`、`status='published'`、`departure_event_id` 非空。
- 唯一索引 + resident 的 `departure_event_id` 共同保证一次 departure 恰好一条 farewell。
- 继续复用 `companion_world_outbox`，事件类型为 `universe_post.published.v1`；幂等键 `departure-farewell:v1:{event_id}`。

### 2.2 `resident_lifecycle_events`

```sql
CREATE TABLE IF NOT EXISTS resident_lifecycle_events (
    id TEXT PRIMARY KEY,
    owner_platform_user_id TEXT NOT NULL,
    universe_id TEXT NOT NULL,
    resident_id TEXT NOT NULL,
    event_type TEXT NOT NULL,             -- inactivity | value_misalignment | severe_abuse
    status TEXT NOT NULL,                 -- cooling_down | review_pending | committed | cancelled | rejected
    policy_version TEXT NOT NULL,
    evidence_window_start TEXT NOT NULL,
    evidence_window_end TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    cooldown_until TEXT,
    crisis_freeze_until TEXT,
    last_resident_exception_requested INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    farewell_text TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    terminal_reason TEXT,
    committed_at TEXT,
    farewell_post_id TEXT,
    correction_status TEXT NOT NULL DEFAULT 'none', -- none | acknowledged | farewell_hidden
    corrected_by TEXT,
    corrected_at TEXT,
    correction_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(resident_id) REFERENCES universe_residents(id),
    FOREIGN KEY(farewell_post_id) REFERENCES universe_posts(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_resident_open
    ON resident_lifecycle_events(resident_id)
    WHERE status IN ('cooling_down', 'review_pending');
CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_resident_committed
    ON resident_lifecycle_events(resident_id)
    WHERE status = 'committed';
CREATE INDEX IF NOT EXISTS ix_lifecycle_review_queue
    ON resident_lifecycle_events(status, cooldown_until, created_at, id);
CREATE INDEX IF NOT EXISTS ix_lifecycle_owner
    ON resident_lifecycle_events(owner_platform_user_id, created_at DESC, id DESC);
```

`evidence_refs_json` 只存目标 runtime 内的 opaque message/task ID、分类、置信度和时间，不复制聊天原文。内部详情只向 admin 返回脱敏结构，绝不进入 owner API、Feed、prompt、L3 或日志正文。

### 2.3 `resident_lifecycle_event_actions`

```sql
CREATE TABLE IF NOT EXISTS resident_lifecycle_event_actions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_type TEXT NOT NULL,              -- scheduler | admin | system
    actor_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(event_id) REFERENCES resident_lifecycle_events(id)
);
CREATE INDEX IF NOT EXISTS ix_lifecycle_actions_event
    ON resident_lifecycle_event_actions(event_id, created_at, id);
```

每次状态变化与 action append 必须同事务。action 只追加、不更新/删除；`metadata_json` 同样禁止存聊天原文。

### 2.4 `character_letter_catalog`

```sql
CREATE TABLE IF NOT EXISTS character_letter_catalog (
    id TEXT PRIMARY KEY,
    character_key TEXT NOT NULL,
    character_template_id TEXT NOT NULL,
    template_version TEXT NOT NULL,
    letter_body TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', -- active | retired
    available_from TEXT,
    available_until TEXT,
    created_by TEXT NOT NULL,
    retired_by TEXT,
    retired_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(character_template_id) REFERENCES character_templates(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_letter_catalog_character_version
    ON character_letter_catalog(character_key, template_version);
CREATE UNIQUE INDEX IF NOT EXISTS ux_letter_catalog_active_character
    ON character_letter_catalog(character_key)
    WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_letter_catalog_template
    ON character_letter_catalog(character_template_id);
CREATE INDEX IF NOT EXISTS ix_letter_catalog_selection
    ON character_letter_catalog(status, priority DESC, id);
```

catalog entry 发布后不可原地修改 template/body/version；更新须创建新版本并 retire 旧 entry。`character_key` 是跨版本稳定的运营角色身份，用于“同一角色不重复投递”。

### 2.5 `character_letters`

```sql
CREATE TABLE IF NOT EXISTS character_letters (
    id TEXT PRIMARY KEY,
    owner_platform_user_id TEXT NOT NULL,
    universe_id TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    character_key TEXT NOT NULL,
    character_template_id TEXT NOT NULL,
    template_version TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL,                  -- unread | read | deferred | accepted | declined | expired
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    eligibility_snapshot_json TEXT NOT NULL DEFAULT '{}',
    policy_version TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    read_at TEXT,
    deferred_at TEXT,
    handled_at TEXT,
    expires_at TEXT NOT NULL,
    accepted_resident_id TEXT,
    terminal_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(catalog_id) REFERENCES character_letter_catalog(id),
    FOREIGN KEY(character_template_id) REFERENCES character_templates(id),
    FOREIGN KEY(accepted_resident_id) REFERENCES universe_residents(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_character_letters_open_world
    ON character_letters(universe_id)
    WHERE status IN ('unread', 'read', 'deferred');
CREATE UNIQUE INDEX IF NOT EXISTS ux_character_letters_world_character
    ON character_letters(universe_id, character_key);
CREATE INDEX IF NOT EXISTS ix_character_letters_owner_list
    ON character_letters(owner_platform_user_id, delivered_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_character_letters_expiry
    ON character_letters(status, expires_at, id);
```

`body_text/template_version` 在投递时快照，运营 retire 不改历史展示。`eligibility_snapshot_json` 仅记录 active count、policy/catalog version 等非敏感判定，不存私聊、L3 或内部 persona。

## 3. 状态机

### 3.1 Lifecycle event

```text
cooling_down ──到期+复核通过──> review_pending ──admin approve+commit──> committed
      │                              │
      ├──恢复/危机/证据失效──> cancelled
      └──────────────────────────────┴──admin reject──────────────> rejected
```

- `severe_abuse` 可直接进入 `review_pending`，但不能自动 commit。
- `approved` 不作为可悬挂状态：approve 请求要么完成完整 offline 事务并变 `committed`，要么整体回滚保持 `review_pending`。
- `committed` 后仅追加 correction action/字段，不改变 lifecycle 真相。

### 3.2 Resident/conversation/post

```text
resident:     active ──> offline              （单向）
conversation: active ──> read_only            （单向）
farewell:     不存在 ──> published ──> deleted（仅 admin 纠错可隐藏）
```

不 disable/delete runtime account，不删除 Soul/Profile/session/message/memory。所有常规 Feed/proactive/speaker 查询继续以 resident `status='active'` 为准。

### 3.3 Letter

```text
unread ──read──> read ──defer──> deferred
   │               │                 │
   ├───────────────┴────accept───────┴──> accepted
   ├───────────────┴────decline──────┴──> declined
   └───────────────┴────到期─────────┴──> expired
```

- read/defer/decline 幂等；accept 重放返回同一 `accepted_resident_id`。
- `accepted/declined/expired` 为终态。
- 接受与到期在精确边界竞争时，world lock 下以 `now >= expires_at` 为 expired。

## 4. Owner API 契约

所有端点复用 World session；客户端不得提交 `account_id`、`runtime_account_id`、`universe_id`、`platform_user_id`。跨 owner 的 letter ID 统一返回 `letter_not_found`，防枚举。响应统一 request envelope、`Cache-Control: no-store`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/mailbox/letters?status=&cursor=&limit=` | tuple cursor `(delivered_at,id)`，默认含全部历史 |
| GET | `/v1/mailbox/unread-count` | 仅数未过期 `status='unread'` |
| GET | `/v1/mailbox/letters/{id}` | 公开角色资料、正文、状态和公开时间 |
| POST | `/v1/mailbox/letters/{id}/read` | 显式已读；列表拉取不改状态 |
| POST | `/v1/mailbox/letters/{id}/defer` | 置 deferred，不延长 expiry |
| POST | `/v1/mailbox/letters/{id}/decline` | 终态拒绝 |
| POST | `/v1/mailbox/letters/{id}/accept` | world lock 下接受并创建 resident/runtime/conversation |

公开 DTO 不返回 catalog id、owner id、persona seed、eligibility snapshot、policy version、内部错误或 runtime account。接受成功返回既有 resident DTO；重复接受同一封返回 200 和同一 resident。

M4 不新增 resident delete/departure owner API。

## 5. Admin contract

### 5.1 Lifecycle

- staff/admin：list/detail review queue、查看脱敏 evidence refs、reject/cancel。
- **只有 `ADMIN_TOKEN` full admin**：approve+commit、最后居民 severe-abuse exception、post-commit correction/hide farewell。
- reviewer token 仍只处理 moderation，不自动获得 lifecycle 权限。

建议端点：

| 方法 | 路径 |
| --- | --- |
| GET | `/admin/companion-world/lifecycle-events` |
| GET | `/admin/companion-world/lifecycle-events/{id}` |
| POST | `/admin/companion-world/lifecycle-events/{id}/reject` |
| POST | `/admin/companion-world/lifecycle-events/{id}/cancel` |
| POST | `/admin/companion-world/lifecycle-events/{id}/approve` |
| POST | `/admin/companion-world/lifecycle-events/{id}/correct` |

approve payload 必含最终 `farewell_text` 与 operator reason；最后居民例外还须显式 `allow_last_resident_exception=true`。服务端仍重新校验 event type、active count、crisis freeze、legacy 和 evidence，不信任 UI。

### 5.2 Mailbox catalog

staff/admin 可 list/create/retire catalog；published entry 不提供覆盖更新。正式 catalog 也支持签名 manifest import + dry-run，沿用四预设模板导入器的失败即停、不可编造内容原则。

## 6. 领域与依赖边界

建议新增：

```text
app/domains/companion_world/lifecycle.py      # 纯状态机/策略/DTO
app/domains/companion_world/mailbox.py        # 纯 eligibility/letter 状态机
app/platform/companion_world_lifecycle.py     # DB + moderation/evidence adapter
app/platform/companion_world_mailbox.py       # DB/catalog/runtime adapter
app/world_lifecycle/scheduler.py              # central lifecycle + mailbox maintenance
scripts/run_world_lifecycle_scheduler.py      # 独立中心进程
app/routers/admin_companion_world.py           # admin review/catalog
app/routers/companion_world_mailbox.py         # owner mailbox API
```

领域层不得 import `app.db`、FastAPI、`turn_service` 或具体 LLM provider。Runtime 不认识 lifecycle/mailbox；offline 组合事务由 platform repository 持有。证据 provider 只读取目标 resident runtime 的数据：

- inactivity：目标 runtime 的 App 用户入站最后时间；不把同 owner 其他 resident 的聊天当作该关系互动。
- value misalignment：独立 evaluator 输出结构化 observation；event 只存 opaque refs/score/version。
- crisis freeze / severe abuse：复用目标 account 的 moderation task/category；不得把其他 resident 或 owner 的无关文本混入。

## 7. 事务与锁序（PG 权威）

延续 P1/M3 全序：`platform_user（需要时） → universe → letter/event/resident row → conversation advisory → quota`。M4 路径不持钱包锁。

### 7.1 Offline 原子事务

approve 请求：

1. 开事务并锁 lifecycle event；确认 `review_pending`。
2. 按 `universe_id` 获取 L1 world row lock。
3. 重读 resident/conversation/event；拒绝 legacy、非 active、已 committed。
4. 重算 active count、普通事件的 crisis freeze 与最后居民规则；只有显式审批的 `severe_abuse` 可走最后居民例外。
5. 在**同一 DB 事务连接**获取 `conv:{conversation_id}` L2 advisory lock；拿不到返回 `conversation_busy`，不部分提交。
6. 锁内再次读 `ai_conversations.state='active'`。
7. 插入唯一 farewell post + outbox。
8. CAS 更新 resident `active→offline`，写 `offline_at/departure_event_id`。
9. CAS 更新 conversation `active→read_only`。
10. event `review_pending→committed`，写 review/farewell/commit 字段并 append action。
11. 一次 commit；任一步失败整体回滚。

现有 turn 继续持相同 `conv:` 锁并在锁内重读 state，因此 offline 与新 turn 严格串行。SQLite 复用同一进程锁只验功能；并发正确性必须由 PG 测试证明。

### 7.2 Letter 投递

1. 锁 world；确认 active/confirmed。
2. 重算 active `<8`、30 天 cooldown、无 open letter。
3. 选择 `priority DESC,id ASC` 的 active catalog，排除已投递 `character_key`、已有任何 non-legacy resident template、过期 catalog。
4. 插入快照 letter；partial unique/index 与幂等键作第二道防线。

### 7.3 Letter 接受

1. 由 session user 解析 home world，锁 world 后锁 owner-scoped letter。
2. 请求时先处理 `now >= expires_at`；非 open 且非已 accepted 时拒绝。
3. 已 accepted 返回同一 resident；否则重查 template/catalog 快照可用、active `<10`。
4. 在同一 UoW 中插 runtime account（no binding/no grant）→ active `origin='mailbox'` resident → AI conversation → letter accepted。
5. 任一步失败回滚，不留孤儿 account/resident；不触发赠权或新钱包。

## 8. Feature flags 与配置

全部默认 false：

- `COMPANION_WORLD_LIFECYCLE_EVALUATION_ENABLED`：允许 central scheduler 生成/推进候选；关闭不影响既有 offline/read-only 真相。
- `COMPANION_WORLD_LIFECYCLE_COMMIT_ENABLED`：允许 full admin approve 执行不可逆事务；shadow 阶段保持 false。
- `COMPANION_WORLD_MAILBOX_ENABLED`：控制 mailbox owner API、投递、过期和接受；关闭不撤销已接受居民。

固定产品不变量（投递 `<8`、接受 `<10`、open=1）不做可漂移配置。60/30/14/7/30 天数写入 `.env.example` 为有界配置，并在 event/letter 记录 policy snapshot；修改只影响新候选/新 letter，不重写历史。

world lifecycle scheduler 必须运行在 central-capable 独立进程。请求时实时门禁始终存在，不能把正确性建立在 scheduler 单例上。

## 9. 稳定错误码

| code | HTTP | 场景 |
| --- | ---: | --- |
| `letter_not_found` | 404 | 不存在或非 owner |
| `letter_not_open` | 409 | 非 open 状态动作 |
| `letter_expired` | 409 | 接受边界已过期 |
| `letter_template_unavailable` | 409 | catalog/template 已不可接受 |
| `resident_capacity_exceeded` | 409 | 接受时 active 已满 10 |
| `mailbox_catalog_not_found` | 404 | admin catalog 不存在 |
| `mailbox_catalog_invalid` | 422 | catalog 内容、模板版本或时间窗非法/冲突 |
| `lifecycle_event_not_found` | 404 | admin event 不存在 |
| `lifecycle_event_not_reviewable` | 409 | event 非 review_pending |
| `lifecycle_commit_disabled` | 503 | commit flag 关闭 |
| `legacy_resident_departure_forbidden` | 409 | D-08 豁免 |
| `last_resident_protected` | 409 | 普通离开撞最后居民保护 |
| `crisis_freeze_active` | 409 | 普通离开仍在 freeze |
| `lifecycle_evidence_invalid` | 409 | 事务时证据已恢复或不再达标 |
| `lifecycle_policy_invalid` | 409 | event 的冻结 policy version 无法安全恢复 |
| `conversation_busy` | 409 | offline 未取得 L2 锁 |
| `lifecycle_commit_conflict` | 409 | 组合事务唯一键/CAS 冲突并已整体回滚 |
| `lifecycle_event_not_correctable` | 409 | 非 committed event 请求 post-commit 纠错 |
| `farewell_invalid` | 422 | 最终文案为空/超长/未通过安全校验 |

flag 关闭的 owner mailbox API 与现有 World 规则一致，对外表现为 404；不得借错误码泄漏未开放能力。

## 10. 可观测、对账与审计

`world_lifecycle_scheduler` heartbeat 使用低基数字段：

- lifecycle：scanned、candidate_created、cooldown_advanced、recovery_cancelled、crisis_frozen、last_resident_blocked、legacy_skipped、review_pending、commit_success/failure。
- mailbox scheduler：world_scanned、eligible、delivered、blocked_open、blocked_cooldown、blocked_capacity、catalog_empty、expired。首版不为 owner accept 另建内存指标系统；接受结果由稳定 HTTP code 与下方只读事实对账覆盖。

发布前只读对账至少证明：

1. offline resident 必须有 committed event、read_only conversation、恰好一条 farewell。
2. committed event 的 resident/event/post 三方 ID 一致，outbox 不存在长期 pending/dead。
3. 不存在 offline legacy resident；不存在 active resident 绑定 committed departure。
4. 每世界 open letter ≤1；accepted letter 必须指向同世界 `origin='mailbox'` resident。
5. active resident ≤10；letter 投递时 eligibility snapshot `<8`。
6. lifecycle action 链存在 create/revalidate/review/commit 或明确终态动作。

审计与 terminal letter 首版不物理删除；保留期/账号注销联动另行合规评审，不能在 cleanup 中顺手猜测。

## 11. 测试门禁

### SQLite 功能

- m0034 顺序迁移、重复 init 幂等、旧 post 默认 normal。
- lifecycle 状态机、数值精确边界、recovery/crisis/legacy/最后居民保护。
- offline 故障注入：post/outbox/resident/conversation/event 任一步失败均全回滚。
- owner letter list/detail/read/defer/decline/accept、防枚举、cursor、不自动已读。
- 30 天投递/expiry 精确边界、open=1、同 character 不重投、retired template。
- 接受失败不留 runtime account、不赠权、不新建 owner binding/钱包。
- offline 后 history 可读、turn 拒绝、Feed/proactive 不再选择该 resident。

### PostgreSQL 硬门禁

- offline 与新 turn 竞争：要么 turn 完成后 offline，要么 offline 后 turn 见 read_only；无撕裂。
- 两次 approve 同一 event：恰好一次 committed、一个 farewell、一个 outbox。
- 普通最后居民离开与并发 mailbox accept/create resident 的 active count 重算正确。
- 两个 scheduler 同时投递：同世界最多一封 open letter。
- 两次接受同一 letter：只创建一个 runtime/resident/conversation。
- 第 10/11 位 resident 竞争：active 永不超过 10。
- accept 与 expire、catalog retire、另一路 create resident 竞争保持状态一致。

账号/世界隔离、锁/事务、容量相关逻辑只有 PG 绿才算完成。

## 12. Rollout / rollback

1. 合并 M3 后从最新 main 校准 M4 分支；部署 m0034 与 default-off 代码。
2. 导入并签字确认 mailbox catalog，但保持 mailbox false。
3. 只开 lifecycle evaluation，commit false，至少观察一个完整证据/cooldown 周期或使用受控时间加速 staging；核验误报与 crisis/last-resident block。
4. 小量开放 admin commit，逐条人工批准并核对原子事务/Feed/outbox/read-only。
5. 先开 mailbox 只读/API，再启用 central delivery；观察 open/cooldown/expiry/accept 指标。
6. 回滚先关 evaluation/commit/mailbox 并停 scheduler。已经 committed 的 offline/read-only/farewell 与已接受 resident 不回滚、不物理删除。

M4 default-off 部署不等于生产启用；P1/M3 的模板、backfill、客户端最低版本和现场对账门仍独立有效。

## 13. M4-0 出口

- [x] §10.3：确认后不允许用户主动移除 active resident。
- [x] §10.4：legacy resident 永久离开豁免保持。
- [x] §10.8：60/30/3/14/7/30 数值、安全冻结、人工审批和纠错 SOP 已冻结。
- [x] Mailbox：`<8` 投递、`<10` 接受、open=1、30 天间隔/TTL、运营目录已冻结。
- [x] m0034 schema、状态机、API、错误码、锁序、flags、scheduler 和 PG 门禁可直接进入实现。
- [x] PR #46 已合并，M4 分支已校准到 `origin/main@e230844`；M4 提交未进入 PR #46。

## 14. M4-2 实现对齐（2026-07-23）

- lifecycle 扫描以 `resident.runtime_account_id` 为证据隔离锚；inactivity 只读目标 runtime 的 owner 入站，value mismatch 只消费 moderation task 中显式 `confirmed` 的结构化 observation。
- crisis/self-harm/high-vulnerability 与 severe-abuse/credible-threat/hate-harassment 复用目标 runtime moderation category；事件和 admin DTO 仅保留 opaque source id、category、confidence、policy version、observed time 白名单，不落聊天原文。
- central scheduler 已接 resident cursor、低基数 heartbeat、7 天精确 cooldown、恢复/证据失效取消、30 天 crisis freeze、legacy/普通最后居民保护；PG 并发门证明同 resident 最多一个 open event。
- staff/admin 可 list/detail/reject/cancel；reviewer 无权限。full admin approve 契约已预留，但 commit flag 默认 false，M4-3 原子事务接入前即使误开也 fail-closed。
- 验证：M4 聚焦 SQLite `27 passed / 2 skipped`、PG `29 passed`；unit `568 passed / 926 deselected`；SQLite 全量 `1479 passed / 15 skipped`；PG 全量 `1489 passed / 5 skipped`。

## 15. M4-3 实现对齐（2026-07-23）

- M4-1、M4-2 已分别提交为 `0815740`、`792d4fe`；M4-3/M4-4/M4-5 当前在 `feat/companion-world-m4` 工作树，尚未提交/推送。
- full-admin approve 已接 `COMPANION_WORLD_LIFECYCLE_COMMIT_ENABLED`：事务内按 event→world→resident→`conv:` 锁序重读 owner/runtime/conversation，按 event policy version 恢复阈值并重校验 evidence、crisis、legacy、active count 与最后居民例外。
- 同一事务写唯一 farewell post/published outbox、resident `active→offline`、conversation `active→read_only`、event `review_pending→committed` 与 append-only action；任一唯一键/CAS 冲突整体回滚，重复 approve 返回既有 committed event/post。
- turn 与 offline 共用同一个 `conv:{conversation_id}` try-lock；offline resident 仍可读历史，但不能再 turn、成为普通 Feed author 或 proactive speaker。最后居民 severe-abuse 例外后，即使 active=0，仍允许只读已发布 farewell；不因此开放普通 Feed 写入。
- full-admin correct 只追加纠错审计，可把 farewell `published→deleted` 并写 deleted outbox；resident/conversation 保持 offline/read-only，不存在恢复入口。
- farewell 在事务外由 full admin 确定安全终稿；后端只做非空、2000 codepoint 与 NUL 结构校验，事务内不调用 LLM/远程审核服务。
- 验证：聚焦 SQLite `34 passed / 2 skipped`、PostgreSQL `36 passed`；unit `568 passed / 931 deselected`；SQLite 全量 `1483 passed / 16 skipped`；PostgreSQL 全量 `1494 passed / 5 skipped`。覆盖 outbox 冲突全回滚、同键 turn lock、double approve 单 farewell/outbox、policy snapshot、公开 farewell projection 与 correction。

## 16. M4-4 实现对齐（2026-07-23）

- `CompanionWorldMailboxService` 已接 confirmed world cursor、world row lock、active `<8`、open=0、30 天 delivery cooldown/TTL 与确定性 `priority DESC,id ASC` catalog 选择；排除历史已投递 `character_key` 和世界中已有 non-legacy template。
- scheduler 与 owner request 均先执行 `expires_at <= now` 的 CAS expiry；精确 30 天边界可在同一轮释放 open 名额并投递下一封。mailbox maintenance 与 lifecycle evaluation 使用独立 flag/cursor，任一关闭不借用另一方开关。
- owner API 已交付 list/detail/unread/read/defer/decline、opaque tuple cursor、no-store 与跨 owner 防枚举；列表不自动已读，defer 不延长 TTL，公开 DTO 不返回 owner/catalog/policy/eligibility/runtime/persona。
- staff/admin 已可 create/list/retire 不可变 catalog；reviewer 无权限。签名 manifest import 使用标准库 HMAC-SHA256，密钥来自 `COMPANION_WORLD_MAILBOX_MANIFEST_HMAC_SECRET`，dry-run/apply 均先验签，报告不输出正文、签名或密钥。
- PG world lock 与 partial unique index 已证明同 world 两个 scheduler 最多一封 open letter；runtime/resident/conversation 接受组合已在 M4-5 接通。
- 验证：M4 聚焦 SQLite `27 passed / 3 skipped`、PostgreSQL `30 passed`；unit `568 passed / 941 deselected`；SQLite 全量 `1492 passed / 17 skipped`；PostgreSQL 全量 `1504 passed / 5 skipped`。

## 17. M4-5 实现对齐（2026-07-23）

- owner accept 只提供 `POST /v1/mailbox/letters/{id}/accept`；不新增通知、欢迎消息、自动 turn、补偿 workflow、配置项或通用 UoW 抽象。
- 单事务按 world→owner-scoped letter/catalog/template 加锁并复核精确 expiry、open/accepted、catalog/template active+版本+来源、同模板 non-legacy 关系和 active `<10`。`now >= expires_at` 会先提交 expired，再返回 `letter_expired`。
- 创建链复用既有 no-binding/no-grant runtime 原语，随后写 `origin='mailbox'` active resident、AI conversation 和 accepted letter，一次提交；失败不留 account/profile/persona/resident/conversation 孤儿。
- accepted 重放返回同一 resident/conversation；公开响应只含 letter/resident 白名单，不下发 runtime、owner、catalog、policy、persona 或 eligibility。
- mailbox 聚焦 SQLite `15 passed / 4 skipped`、PG `19 passed`；lifecycle+mailbox 联合 SQLite `25 passed / 6 skipped`、PG `31 passed`；unit `568 passed / 950 deselected`；SQLite 全量 `1498 passed / 20 skipped`；PG 全量 `1513 passed / 5 skipped`。PG 已证明 double accept、与常规建居民争抢第 10 位、accept-vs-expiry 不撕裂。catalog-retire 专项并发压测由 M4-6 补齐。

## 18. M4-6 最终实现对齐（2026-07-23）

- 精简收口复用既有 `GET /admin/ops/status` 和 `world_lifecycle_scheduler` heartbeat；未新增 health endpoint、accept 指标系统、配置、通知或产品行为。
- Admin guide 已补 lifecycle/mailbox 独立开关、central 单例、灰度顺序、聚合 heartbeat、SQLite/PG 只读对账与不可逆回滚说明。
- PG accept-vs-catalog-retire 竞态证明：结果只能是 accepted 后 catalog retired，或 retire 先完成导致 `letter_template_unavailable`；两种结果均无孤儿 runtime/resident/conversation。
- 最终门禁：mailbox PG `20 passed`；lifecycle+mailbox 联合 SQLite `25 passed / 7 skipped`、PG `32 passed`；unit `568 passed / 951 deselected`；SQLite 全量 `1498 passed / 21 skipped`；PG 全量 `1514 passed / 5 skipped`；`compileall` 与 `git diff --check` 通过。
- M4 全部 flag 保持默认关闭；PR #47 已合并，但本归档不代表生产已执行 m0034、导入 catalog、启动 scheduler 或开量。
