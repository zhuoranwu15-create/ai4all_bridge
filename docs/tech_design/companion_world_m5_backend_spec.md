# Companion World M5 Visit + Human Chat 后端实现规范

> 状态：**M5-0…M5-4 已完成（2026-07-23）；M5-5 待收口。**
>
> 分支基线：`feat/companion-world-m5` 堆叠于已完成且全量测试通过的 M4 提交 `af03382`；Draft PR #47 尚未合并，M5 PR 在其合并前不得转 Ready。
>
> 权威上位决策：[`companion_world_3_0_refactor_design.md`](./companion_world_3_0_refactor_design.md) D-02/D-06/D-11/D-12、§9、§10.9、§12 M5。

## 0. 目标与边界

M5 以最小闭环交付两项能力：

1. **Visit**：A 生成高熵一次性世界邀请码，B 登录后兑换为 `pending`；A 明确接受后，B 获得 30 天 active visit，只读 A 世界已发布 Feed。
2. **Human Chat**：A 接受 visit 时同时建立独立真人会话；active visit 内双方可发文字，到期、撤销、离开或拉黑后立即只读。

M5 不包含：

- 通讯录、用户搜索、好友关系图、关注/粉丝、公开世界、转邀、多人群聊或访客间聊天。
- 访客评论/点赞/发 Feed、访问 AI 私聊、resident runtime、L1/L2/L3、mailbox、通知内部数据或草稿。
- 图片/语音/文件、消息撤回、逐条删除、端到端加密、Push 或复杂内容审核工作流。
- 自动续期、续访、邀请传播激励、推荐/排行榜或增长漏斗优化。
- 把真人消息写入 AI `messages`、prompt、Soul、Dreaming、AI Memory、AI moderation prompt 或 proactive。

## 1. 冻结产品口径

### 1.1 双侧容量

- **访客 B 容量**：B 名下 `pending + active` visit 合计最多 **3** 个，跨不同好友世界聚合；B 可取消 pending 立即释放自己的名额。
- **主人 A 世界容量**：一个 universe 的未兑换有效 invite、pending visit、active visit 合计最多占 **3** 个固定 slot；invite 兑换只把同一 slot 的 occupant 从 invite 切为 visit，不新增占位。
- `expired/rejected/cancelled/left/revoked/blocked` 均不占容量；终态不可恢复。
- 第 3/4 个名额竞争必须由 PostgreSQL 锁与 DB 约束证明，不能依赖先查后写。

### 1.2 邀请、pending 与 active

| 项目 | 冻结值 |
| --- | --- |
| 邀请码 | 32-byte CSPRNG 的 URL-safe、大小写敏感、一次性 token；DB 只存 SHA-256 hash |
| invite TTL | 固定 **24 小时**，A 可在兑换前撤销 |
| 兑换结果 | 只创建 `pending` visit，不授予 Feed/chat ACL |
| pending TTL | 兑换后 **7 天**；A 可接受/拒绝，B 可取消 |
| active visit | A 接受时开始，绝对有效 **30 天** |
| 续期 | 首版不支持；聊天或查看 Feed 不延长任何期限 |
| 精确边界 | `now >= expires_at/pending_expires_at` 即到期 |

- B 不能兑换自己的 invite；同一 B 对同一 universe 最多一个 `pending/active` visit。
- invite 成功兑换即永久 consumed；即使 pending 被拒绝/取消/到期，也不能再次兑换。
- A 接受必须在同一事务把 visit 置 active、设置绝对 `expires_at`、创建真人会话；任一步失败整体回滚。
- A 拒绝、B 取消 pending、A 撤销 active、B 主动离开均为终态。重新来访必须使用新 invite 和新 visit。

### 1.3 访问与拉黑

- active visitor 只可通过自己的 `visit_id` 读取目标世界 `status='published'` 的 Feed；API 不接受任意 `universe_id`。
- 每次 Feed 读取和真人消息发送都实时校验 visit 状态、绝对 expiry、owner/visitor 与 block；客户端倒计时或旧缓存不构成授权。
- 到期、撤销、离开或拉黑后，visitor 立即失去目标 Feed 读取权，真人会话立即只读。
- 任一方拉黑另一方后，双方之间所有 pending/active visit 终止，未来互相兑换/接受被拒绝；拉黑关系本身不公开给对方。
- 拉黑不删除历史。解除拉黑不恢复旧 visit 或写权限，只允许未来重新邀请。

### 1.4 真人聊天保留/删除

- visit 到期或终止只把会话变为 `read_only`，历史默认长期保留，不自动 30 天删除。
- “手动删除”首版定义为**仅对当前用户隐藏会话入口与历史**；不删除对方副本、不物理删除消息、不撤销举报证据。新消息不会让已终止会话恢复。
- M5 不提供逐条消息删除/撤回。双方都隐藏后也不自动物理清除；数据主体删除请求另走账号合规删除流程。
- 举报时把必要消息证据复制到独立 immutable evidence snapshot；普通会话隐藏/账号常规清理不能删除该快照。
- 举报证据按后续合规配置清理；M5 首版在期限未由法务/运营配置前 fail-safe 保留，不擅自自动删除。

## 2. m0035 加性数据模型

历史 migration 不改写。M5-1 已在 `_MIGRATIONS` 末尾追加 `m0035_companion_world_visit_human_chat`。

### 2.1 `universe_visit_slots`

每个 confirmed universe 固定创建 slot 1–3；旧世界按需在 world lock 内幂等补齐。

```sql
CREATE TABLE universe_visit_slots (
    universe_id TEXT NOT NULL,
    slot_no INTEGER NOT NULL,
    occupant_type TEXT,                 -- invite | visit | NULL
    occupant_id TEXT,
    occupied_at TEXT,
    PRIMARY KEY (universe_id, slot_no),
    UNIQUE (occupant_type, occupant_id),
    FOREIGN KEY(universe_id) REFERENCES universes(id)
);
```

应用层与测试约束 `slot_no IN (1,2,3)`，`occupant_type/occupant_id` 同空同非空。分配取最小空 slot，确保确定性。

### 2.2 `universe_invites`

```sql
CREATE TABLE universe_invites (
    id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL,
    owner_platform_user_id TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE,
    code_prefix TEXT NOT NULL,
    status TEXT NOT NULL,               -- active | redeemed | revoked | expired
    expires_at TEXT NOT NULL,
    redeemed_by_platform_user_id TEXT,
    redeemed_visit_id TEXT,
    redeemed_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(redeemed_by_platform_user_id) REFERENCES platform_users(id)
);
CREATE INDEX ix_universe_invites_owner
    ON universe_invites(owner_platform_user_id, created_at DESC, id DESC);
CREATE INDEX ix_universe_invites_expiry
    ON universe_invites(status, expires_at, id);
```

明文 token 只在 create 响应返回一次，不进 DB、日志、trace、埋点或错误。`code_prefix` 仅供 owner 列表辨认，不用于兑换查询。

### 2.3 `universe_visits`

```sql
CREATE TABLE universe_visits (
    id TEXT PRIMARY KEY,
    invite_id TEXT NOT NULL UNIQUE,
    universe_id TEXT NOT NULL,
    owner_platform_user_id TEXT NOT NULL,
    visitor_platform_user_id TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending | active | expired | rejected | cancelled | left | revoked | blocked
    pending_expires_at TEXT NOT NULL,
    accepted_at TEXT,
    expires_at TEXT,
    terminal_at TEXT,
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(invite_id) REFERENCES universe_invites(id),
    FOREIGN KEY(universe_id) REFERENCES universes(id),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(visitor_platform_user_id) REFERENCES platform_users(id)
);
CREATE UNIQUE INDEX ux_universe_visits_open_pair
    ON universe_visits(universe_id, visitor_platform_user_id)
    WHERE status IN ('pending', 'active');
CREATE INDEX ix_universe_visits_visitor
    ON universe_visits(visitor_platform_user_id, status, created_at DESC, id DESC);
CREATE INDEX ix_universe_visits_owner
    ON universe_visits(owner_platform_user_id, status, created_at DESC, id DESC);
CREATE INDEX ix_universe_visits_expiry
    ON universe_visits(status, pending_expires_at, expires_at, id);
```

### 2.4 `human_conversations`

```sql
CREATE TABLE human_conversations (
    id TEXT PRIMARY KEY,
    visit_id TEXT NOT NULL UNIQUE,
    owner_platform_user_id TEXT NOT NULL,
    visitor_platform_user_id TEXT NOT NULL,
    status TEXT NOT NULL,               -- active | read_only
    owner_hidden_at TEXT,
    visitor_hidden_at TEXT,
    owner_last_read_at TEXT,
    visitor_last_read_at TEXT,
    last_message_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(visit_id) REFERENCES universe_visits(id),
    FOREIGN KEY(owner_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(visitor_platform_user_id) REFERENCES platform_users(id)
);
CREATE INDEX ix_human_conversations_owner
    ON human_conversations(owner_platform_user_id, last_message_at DESC, id DESC);
CREATE INDEX ix_human_conversations_visitor
    ON human_conversations(visitor_platform_user_id, last_message_at DESC, id DESC);
```

两个 participant 字段是有意的精简一对一模型；M5 不为未知群聊抽象 participant 表。

### 2.5 `human_messages`

```sql
CREATE TABLE human_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_platform_user_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    body_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES human_conversations(id),
    FOREIGN KEY(sender_platform_user_id) REFERENCES platform_users(id),
    UNIQUE(conversation_id, sender_platform_user_id, client_message_id),
    UNIQUE(conversation_id, sequence_no)
);
CREATE INDEX ix_human_messages_list
    ON human_messages(conversation_id, sequence_no DESC);
```

正文首版只允许 UTF-8 文字并设置既有 API 同等级长度上限。sender 必须是 conversation 两方之一；服务端从 session 推导，客户端不可冒充。发送事务在 conversation row lock 下分配递增 `sequence_no`，列表/游标按 sequence，避免同秒消息被随机 ID 打乱。

### 2.6 `platform_user_blocks`

```sql
CREATE TABLE platform_user_blocks (
    blocker_platform_user_id TEXT NOT NULL,
    blocked_platform_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(blocker_platform_user_id, blocked_platform_user_id),
    FOREIGN KEY(blocker_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(blocked_platform_user_id) REFERENCES platform_users(id)
);
```

任一方向存在 block 即双方 contact ACL 关闭。解除只删 blocker 自己创建的行，不恢复旧 visit。

### 2.7 `human_chat_reports`

```sql
CREATE TABLE human_chat_reports (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reporter_platform_user_id TEXT NOT NULL,
    reported_platform_user_id TEXT NOT NULL,
    reported_message_id TEXT,
    reason_code TEXT NOT NULL,
    details_text TEXT,
    evidence_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open | reviewed | closed
    retained_until TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    FOREIGN KEY(conversation_id) REFERENCES human_conversations(id),
    FOREIGN KEY(reporter_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(reported_platform_user_id) REFERENCES platform_users(id),
    FOREIGN KEY(reported_message_id) REFERENCES human_messages(id)
);
CREATE INDEX ix_human_chat_reports_queue
    ON human_chat_reports(status, created_at, id);
```

snapshot 只含被举报消息和有限相邻上下文、双方 opaque user id 与时间；不含手机号、AI memory、其他会话或目标世界私有数据。

## 3. 状态机与原子事务

### 3.1 Invite / visit

```text
invite: active ──redeem──> redeemed
          ├──owner revoke──> revoked
          └──expiry────────> expired

visit: pending ──owner accept──> active ──expiry────────> expired
          │                       ├──owner revoke──> revoked
          ├──owner reject──> rejected
          ├──visitor cancel─> cancelled
          └──pending expiry─> expired
                                  active ──visitor leave──> left
                         pending/active ──either block────> blocked
```

- redeem：锁 visitor → universe → slot/invite；重查 B 的 open visit `<3`、A slot、block、自邀、invite active/expiry；创建 pending 并原位转 slot occupant。
- accept：锁 visitor → universe → visit；重查 pending/expiry/block/双侧容量，写 active + 30 天 expiry + human conversation。
- terminal：锁双方 user（按 id 排序）→ universe（按 id 排序）→ visit/conversation；visit 终态、conversation read_only、释放 slot 同事务。
- 请求路径发现 expiry 时必须在锁内落终态并释放 slot；central scheduler 只是兜底清理，不是 ACL 真相来源。

### 3.2 Human conversation

```text
active ──visit terminal/block──> read_only
```

hide 是 participant projection，不改变 conversation 状态。发送锁 conversation，随后在同一事务重读 visit/block/expiry；消息 insert 与 `last_message_at` 一次提交。相同 `(conversation,sender,client_message_id)` 重放返回原消息，正文不同返回 `idempotency_conflict`。

## 4. Owner / visitor API

所有接口复用 World session，响应统一 request envelope、`Cache-Control: no-store`。客户端不得提交 `platform_user_id`、`owner_id`、`visitor_id`、`universe_id` 或 AI `account_id`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/world/invites` | A 创建 24h invite；明文 code 仅返回一次 |
| GET | `/v1/world/invites` | A 查看 slot/invite/pending/active 状态 |
| DELETE | `/v1/world/invites/{id}` | A 撤销未兑换 invite |
| POST | `/v1/visits/redeem` | B 兑换 code，返回 pending visit |
| GET | `/v1/visits` | B 查看 pending/active/历史；A 的待审批另分面返回 |
| POST | `/v1/visits/{id}/accept` | A 接受 pending |
| POST | `/v1/visits/{id}/reject` | A 拒绝 pending |
| POST | `/v1/visits/{id}/cancel` | B 取消 pending |
| POST | `/v1/visits/{id}/leave` | B 提前离开 active |
| POST | `/v1/visits/{id}/revoke` | A 提前结束 active |
| GET | `/v1/visits/{id}/feed` | B 只读 active visit 的 published Feed |
| GET | `/v1/human-conversations` | 当前用户会话列表，默认不含自己的 hidden entry |
| GET | `/v1/human-conversations/{id}/messages` | 双方分页读取，active/read_only 均可 |
| POST | `/v1/human-conversations/{id}/messages` | active visit 内发文字，要求 client_message_id |
| POST | `/v1/human-conversations/{id}/read` | 更新当前 participant read marker |
| DELETE | `/v1/human-conversations/{id}/entry` | 仅隐藏当前用户入口/历史 |
| POST | `/v1/human-conversations/{id}/report` | 创建独立证据 snapshot |
| POST | `/v1/human-conversations/{id}/block` | 举报可选同时拉黑，也支持单独拉黑 |

跨 participant 的资源 ID 统一返回对应 `*_not_found`，防枚举。列表使用 `(created_at,id)` 或 `(last_message_at,id)` tuple cursor；时间公开为带 `+08:00` 的 ISO 8601。

## 5. 稳定错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `invalid_invite_code` | token 形状错误 |
| 403 | `self_invite_not_allowed` | B 兑换自己的 invite |
| 403 | `visit_contact_blocked` | 任一方向已拉黑 |
| 404 | `invite_not_found` | hash 不存在 |
| 404 | `visit_not_found` | 非 owner/visitor 或不存在 |
| 404 | `human_conversation_not_found` | 非 participant 或不存在 |
| 409 | `invite_expired` | `now >= invite.expires_at` |
| 409 | `invite_unavailable` | 已兑换/撤销 |
| 409 | `visitor_visit_limit_reached` | B 的 pending+active 已达 3 |
| 409 | `world_visit_limit_reached` | A 世界三个 slot 已占满 |
| 409 | `visit_already_open` | 同 B/世界已有 pending/active |
| 409 | `visit_pending_expired` | pending 已到期 |
| 409 | `visit_not_pending` | accept/reject/cancel 状态错误 |
| 409 | `visit_not_active` | Feed/send/leave/revoke 状态错误 |
| 409 | `human_chat_read_only` | visit 终止或 chat write flag 关闭 |
| 409 | `idempotency_conflict` | 相同 client_message_id 不同正文 |

## 6. 分层、锁序与隔离

- 纯规则/DTO：`app/domains/companion_world/{visits,human_chat}.py`，不得 import `app.db`、FastAPI、Runtime。
- 事务编排：`app/platform/companion_world_visits.py`、`app/platform/companion_world_human_chat.py`。
- DB 原语：`app/db/companion_world_visits.py`、`app/db/companion_world_human_chat.py`。
- API：`app/routers/companion_world_visits.py`、`app/routers/companion_world_human_chat.py`。
- 复用 M3 Feed projection，但 visitor facade 只传服务端解析出的 `visit_id → universe_id`；领域/Runtime 不接受 visitor 指定 world。
- human tables 不引用 `accounts`、`ai_conversations` 或 AI `messages`，Runtime/turn/prompt/dreaming/proactive 不新增 human import。

PG 总锁序：

1. 涉及真人关系时按 `platform_user_id ASC` 锁 user；单 B 容量也先锁 B。
2. 涉及世界时按 `universe_id ASC` 锁 world。
3. slot → invite/visit → human conversation。
4. message/report append。

SQLite 复用进程锁做功能回退；PG `FOR UPDATE`/advisory lock 与唯一约束是并发权威。

## 7. Flags、scheduler 与回滚

新增两个 default-off flag：

- `COMPANION_WORLD_VISITS_ENABLED=false`：关闭 invite/create/redeem/accept 与 visitor Feed；owner revoke、visitor cancel/leave、block/report 等安全终止接口仍可用。
- `COMPANION_WORLD_HUMAN_CHAT_ENABLED=false`：禁止新消息；历史读取、hide、report、block 仍可用。

不新增进程。把 invite/pending/active expiry 作为现有 central world lifecycle scheduler 的独立有界步骤；每页固定 cursor、低基数 heartbeat。请求路径仍实时判 expiry。

回滚只关 flag/停 M5 expiry step，不删除 m0035 数据、不恢复终态 visit、不自动解除 block。已存在 chat 保持只读可导出/举报。

## 8. 安全与滥用防护

- redeem 仅对已登录用户开放，按 user + source IP 使用现有 API 限流接缝；失败日志不含完整 code。
- 文本发送做长度/空白/控制字符校验和现有真人内容安全入口；M5 不把内容送进 AI prompt moderation。
- 公开 DTO 不返回手机号、login identity、code hash、internal owner id、runtime account、L2/L3 或举报 evidence。
- report 后允许用户选择 block；服务端不因举报自动判罚，admin 只读 review queue 后续按现有 staff/full-admin 权限接入。
- Feed visitor 响应 no-store；客户端缓存清理由客户端 PR 阻断，后端不把缓存视作 ACL。

## 9. 测试门禁

### SQLite / 单元

- invite 明文只返回一次、hash 持久化、24h 精确 expiry、revoke/self/used/invalid。
- B `pending+active <=3`、A slot occupant 转换/释放、pending 7d、active 30d 不续期。
- pending 无 ACL；active 仅 published Feed；终态/跨 visitor/任意 world id 全拒绝。
- human message 幂等、participant/owner 隔离、read_only、self-hide、report snapshot/block。
- AST 证明 Runtime/AI message/prompt/dreaming/proactive 不依赖 human chat。

### PostgreSQL 硬门

- 同码双兑换仅一人成功；同 B 第 3/4 个 pending/active 竞争不超限。
- 同 A 世界第 3/4 slot 创建/兑换/接受竞争不超限。
- accept vs reject/cancel/pending-expiry；active expiry/revoke/leave/block vs Feed/send。
- 同 `client_message_id` 双发仅一条；block 与反向新 invite/redeem 并发 fail-closed。
- 双世界/双用户反向 block 不死锁；锁等待有界并返回稳定 retry 语义。

### 合并前

```bash
make test-unit
make test
make test-pg
.venv/bin/python -m compileall app scripts tests
git diff --check
```

## 10. 客户端与发布阻断项

当前客户端 PRD 仍写“邀请码默认 12h/最长 7d、兑换即获得 invite 剩余时长”。M5 新冻结口径已改为“24h invite → 7d pending 审批 → 接受后独立 30d visit”，必须在生产开 flag 前镜像更新。

生产启用还要求：

1. PR #47 合并，M5 branch 校准到最新 main；m0035 双后端迁移与只读对账通过。
2. 客户端实现 pending/owner accept、到期缓存清理、no-store、终态只读与隐藏语义。
3. 运营/法务确认举报 evidence retention 配置；未确认前不运行自动清理。
4. 两个 flag 按 schema → read/history → invite/pending → active Feed → human write 顺序小流量开启。

## 11. M5-1 落地记录

已交付 m0035 七表/索引、`visits.py`/`human_chat.py` 纯领域 DTO 与状态规则、两个 owner/visitor-scoped DB 原语模块，以及 `COMPANION_WORLD_VISITS_ENABLED` / `COMPANION_WORLD_HUMAN_CHAT_ENABLED` 两个 default-off flag。新模块仅由 `app.db` 重导出，没有注册路由、scheduler 或 Runtime 调用方。

验证：M5/schema/边界 SQLite `23 passed / 1 skipped`、PostgreSQL `24 passed`；Companion World SQLite 联合 `106 passed / 16 skipped`；unit `570 passed / 955 deselected`；compileall 与 diff check 通过。

## 12. M5-2 落地记录

已交付 owner invite create/list/revoke、visitor redeem、owner accept/reject、visitor cancel/leave、owner revoke。code 使用 `secrets.token_urlsafe(32)` 且只持久化 SHA-256；redeem 只产生 pending，accept 在同事务写 active 30 天 expiry 与独立 human conversation。A 三 slot 与 B `pending+active<=3` 分别以 owner/world 和 visitor user 锁保护，终态释放 slot 并把既有会话只读。路由与稳定错误信封已接入，但 visit flag 保持 default-off。

验证：M5 SQLite `16 passed / 4 skipped`、PostgreSQL `20 passed`；PG 覆盖同码双花、A/B 第 3/4 容量及 accept/cancel 竞态；Companion World SQLite 联合 `112 passed / 20 skipped`；unit `570 passed / 965 deselected`；compileall/diff check 通过。

## 13. M5-3 落地记录

已交付 `GET /v1/visits/{visit_id}/feed` 的 visitor-only published projection；pending/owner/第三方均无 ACL，客户端不能提交 world id。Feed 请求与 visit 列表执行 request-time 精确 expiry；central `world_lifecycle_scheduler` 增加独立 visit expiry 步骤，复用现有进程/heartbeat。`POST /v1/visits/{visit_id}/block` 按双 user/双 world 有序锁一次终止双方任一方向的 open visits、释放 slot 并把真人会话只读，未来兑换 fail-closed。

验证：M5 SQLite `18 passed / 4 skipped`、PostgreSQL `26 passed`；PG 新增 revoke-vs-Feed、block-vs-反向 redeem 无死锁门禁；Companion World SQLite 联合 `116 passed / 22 skipped`；unit `570 passed / 971 deselected`；compileall/diff check 通过。

## 14. M5-4 落地记录

已交付 human conversation list、message list/send/read、participant-scoped idempotency、visit 终态只读历史、self-hide、独立 report evidence snapshot，以及 conversation block 入口。Chat write flag 关闭只阻止新消息，历史/read/hide/report/block 仍可用。消息在 conversation lock 下分配递增 `sequence_no`，同秒多消息按真实提交顺序分页；真人消息没有任何 AI `messages`/turn/prompt/dreaming/memory/proactive 调用方，AST 门禁持续阻断反向接入。

验证：最终 M5 聚焦 SQLite `26 passed / 9 skipped`、PostgreSQL `35 passed`；PG 覆盖同 client id 双发、send-vs-block、send-vs-exact-expiry；Companion World SQLite 联合（sequence 修复前，业务面等价）`122 passed / 24 skipped`；unit `571 passed / 978 deselected`；compileall/diff check 通过。M5-5 将复跑最终全量门禁。
