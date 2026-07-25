# 隐私与后台访问控制技术设计

更新时间：2026-05-28

本文承接 [运营与后台 PRD](../../product/admin_ops_prd.md)，定义 Phase 1 内测所需的最小隐私控制、后台权限分级、明文查看和操作日志机制。

设计目标是尽可能简单：默认脱敏，少数高权限管理员可以看明文；普通后台用户如确需查看，申请 2 小时临时明文权限，由管理员审批。

## 1. 设计原则

- 默认所有 Admin/Debug 视图都返回元数据、统计、状态、摘要或脱敏内容。
- 明文查看是例外，不是普通后台能力。
- 管理员是最高权限角色，可以查看明文，用于必要 debug、事故排查和用户投诉处理。
- 普通后台用户不能直接查看明文；如需查看，必须申请临时明文权限。
- 临时明文权限有效期默认 2 小时，到期自动失效。
- 所有明文查看都必须记录操作日志。
- Phase 1 不引入复杂安全委员会、外部审批系统或多级审批流。

## 2. 敏感内容范围

以下内容默认视为正文类敏感信息：

| 类型 | 示例 |
| --- | --- |
| 用户聊天正文 | 用户发给 AI 的文本、语音转写文本、图片识别描述 |
| AI 回复正文 | 已发送给用户的模型回复 |
| raw payload 正文 | OpenClaw / Weixin payload 中的用户消息正文、媒体识别正文 |
| prompt/messages | system prompt、LLM messages、tool 输入中含正文的部分 |
| daily notes 正文 | 按业务日保存的原始文字化聊天材料 |
| debug trace 明文 | prompt、messages、reply、raw request/response 中的正文内容 |

默认可见内容：

- account id、platform user id、binding 状态、channel route。
- message id、时间、类型、状态、耗时、错误码、token、成本。
- session id、turn count、开始/结束时间、close reason。
- task/reminder/outbound 状态、provider、失败原因、重试次数。
- Context Files metadata、daily notes metadata、Dreaming 任务状态、diff 摘要。

## 3. 角色模型

Phase 1 使用简单后台角色：

| 角色 | 能力 |
| --- | --- |
| `support` | 查看账号、绑定、会话、消息元数据、用量、任务状态；不能看明文 |
| `operator` | `support` 能力 + 账号禁用/恢复、额度补发、提醒/主动消息状态处理；不能看明文 |
| `developer` | `support` 能力 + debug metadata、provider 错误、trace metadata；不能默认看明文 |
| `admin` | 最高权限；可管理后台用户、审批临时明文权限、必要时查看明文 |

实现上可以先只落地两个层级：

- `admin`：最高权限。
- `staff`：普通后台用户，默认只能看脱敏视图。

如果内测后台用户很少，先实现 `admin/staff` 即可；`support/operator/developer` 可以作为后续细分。

## 4. 明文访问模型

明文访问有两种来源：

1. 管理员最高权限：`role=admin` 的后台用户可以查看明文。
2. 临时明文权限：普通后台用户申请后，由管理员审批，有效期 2 小时。

临时权限必须限制范围：

- 账号范围：单个或少量 `ai4all_account_id`。
- 时间范围：例如某天、某个 session、某条消息前后。
- 资源类型：messages、raw payload、debug trace、daily notes、prompt/messages。
- 原因：用户投诉、事故排查、误扣排查、安全事件等。
- 过期时间：默认 `approved_at + 2h`。

普通后台用户没有有效临时权限时，任何明文接口都返回 403。

## 5. 最小数据模型

### 5.1 admin_users

```text
admin_users
- id
- email
- display_name
- role: admin | staff
- status: active | disabled
- created_at
- updated_at
```

Phase 1 如果暂时仍使用 `admin_token`，也应在技术债中记录：进入多人后台前必须迁移到具名后台用户，否则无法可靠记录操作人。

### 5.2 admin_plaintext_grants

```text
admin_plaintext_grants
- id
- requester_admin_user_id
- approver_admin_user_id
- status: pending | approved | rejected | expired | revoked
- reason
- account_scope_json
- resource_scope_json
- time_scope_start
- time_scope_end
- approved_at
- expires_at
- revoked_at
- created_at
- updated_at
```

约束：

- `expires_at` 默认不超过 `approved_at + 2h`。
- 只能由 `role=admin` 的用户审批。
- 过期权限无需后台任务强制改状态；权限检查时只要 `expires_at <= now` 即视为失效。

### 5.3 admin_access_events

```text
admin_access_events
- id
- admin_user_id
- action
- resource_type
- resource_id
- account_id
- plaintext: boolean
- grant_id
- reason
- request_path
- created_at
- metadata_json
```

Phase 1 最少记录明文查看事件；普通元数据查看可以先记录关键高风险操作，例如禁用账号、补发贝壳、取消提醒、修改 proactive state。

## 6. API 设计

默认接口只返回脱敏结果：

```text
GET /admin/accounts/{account_id}
GET /admin/accounts/{account_id}/sessions
GET /admin/messages
GET /admin/debug/traces
```

明文查看建议使用显式接口或显式参数，不与默认接口混用：

```text
GET /admin/plaintext/messages/{message_id}
GET /admin/plaintext/debug-traces/{trace_id}
GET /admin/plaintext/accounts/{account_id}/daily-notes?date=YYYY-MM-DD
```

权限申请：

```text
POST /admin/plaintext-grants
GET /admin/plaintext-grants
POST /admin/plaintext-grants/{grant_id}/approve
POST /admin/plaintext-grants/{grant_id}/reject
POST /admin/plaintext-grants/{grant_id}/revoke
```

明文接口必须执行：

1. 解析当前后台用户。
2. 如果 `role=admin`，允许。
3. 否则查找 active approved grant。
4. 校验 grant 未过期、账号范围匹配、资源类型匹配、时间范围匹配。
5. 写入 `admin_access_events`。
6. 返回明文。

## 7. 脱敏策略

默认视图中：

- `content`、`reply`、`prompt`、`messages`、`raw_payload.content` 返回空、摘要或 `redacted=true`。
- 可以展示字符数、消息类型、token 数、provider、耗时、错误码。
- debug trace 默认展示 prompt block metadata，不展示 system prompt 全文和 messages 全文。
- daily notes 默认展示日期、turn 数、字符数、写入状态，不展示正文。

建议统一返回结构：

```json
{
  "content_redacted": true,
  "content_chars": 128,
  "content_preview": null
}
```

Phase 1 可以不做复杂自动摘要；摘要如果无法安全生成，宁可只展示 metadata。

## 8. 当前代码差距

当前代码仍是单一 `admin_token`：

- 无具名后台用户。
- 无角色分级。
- `/admin/messages/*/raw`、`/debug/*`、`/admin/debug/traces/*` 可能返回明文。
- 无临时授权。
- 无明文查看操作日志。

因此进入内测前至少需要：

1. 将现有 Admin/Debug 明文接口改为默认脱敏。
2. 增加 `admin/staff` 两级角色，或在过渡期将 `admin_token` 仅视为 `role=admin`。
3. 增加临时明文权限表和审批接口。
4. 增加明文接口权限检查。
5. 增加明文查看日志。

## 9. 开发切分

建议按最小闭环推进：

1. Redaction helper：统一脱敏 messages、raw payload、debug trace、daily notes。
2. 默认脱敏：改造现有 Admin/Debug 列表和详情接口，默认不返回正文。
3. 角色上下文：先支持 `admin/staff` 两级角色；本地 `admin_token` 可临时映射为 `admin`。
4. 明文授权：新增 `admin_plaintext_grants` 和审批接口，默认有效期 2 小时。
5. 明文接口：新增 `/admin/plaintext/*`，只对 admin 或有有效 grant 的 staff 开放。
6. 明文日志：新增 `admin_access_events`，所有明文查看必写。
7. UI：普通视图显示“已脱敏”；staff 可发起申请；admin 可审批。

## 10. 验收点

- 普通后台用户默认不能在 Admin UI、Debug API 或日志平台看到正文。
- 普通后台用户访问明文接口时，没有有效授权会返回 403。
- 普通后台用户申请明文权限后，管理员可以审批。
- 临时明文权限默认 2 小时后失效。
- 临时明文权限只能查看被授权账号、资源类型和时间范围。
- 管理员可以查看明文，但每次明文查看都写入 `admin_access_events`。
- Debug trace 默认只展示 metadata；明文 prompt/messages/reply 只能通过明文权限查看。
- raw payload 默认脱敏；明文字段只能通过明文权限查看。
