# Nooki 产品 Manifest

- `app_id`：`nooki`
- 状态：P0+P1 已实现，生产默认关闭（`NOOKI_PRODUCT_ENABLED=false`）
- 入口：微信小程序 App API
- 代码命名空间：`app/products/nooki/`
- 固定 API namespace：`/api/v1/products/nooki`
- 产品需求与状态机：[PRD](prd.md)

Nooki 与朝夕平级，复用共享 Agent Runtime；任务业务状态只由
`GoalBreakdownService` 在数据库事务中修改。Nooki 不使用独立 Runner、独立
PromptBuilder、JSON Actions、朝夕 Skills 或 after-turn 任务写入。

## P1 API

- `POST /auth/bind`：`login_code + phone_code` 首次绑定
- `POST /auth/session`：`login_code` 静默续期
- `GET /bootstrap`：恢复默认 Conversation、近期消息、Profile 与任务投影
- `GET /conversations/{conversation_id}/messages`：跨 Runtime Session 历史分页
- `GET /sync`：按消息游标增量同步消息与权威任务投影
- `POST /chat`：携带 `conversation_id + client_message_id` 发送消息，返回消息、游标、
  `state + cards + metadata`
- `GET /state`：读取权威 `state + cards`
- `GET/POST /later-items`：读取或幂等新增服务端稍后项
- `PATCH /later-items/{item_id}`：按 `expected_version` 修改稍后项
- `POST /later-items/{item_id}/archive`：按 `expected_version` 归档稍后项
- `POST /tasks/{task_id}/plans/{plan_id}/select`
- `POST /steps/{step_id}/start`
- `POST /steps/{step_id}/complete`
- `POST /tasks/{task_id}/abandon`

按钮写请求统一携带 `client_request_id` 和可选 `expected_version`；缩小行动需要
replacement 内容，继续通过聊天 Tool 执行。

## 对话存储

`nooki_conversations` 是用户看到的长期聊天窗口，一个用户 P1 只有一个 active
Conversation；它绑定一个 Nooki Runtime account，但 API 不向小程序暴露 account/session
标识。Runtime 继续使用共享 `sessions/messages/tool_invocations`，`__app_active__` Session
按北京时间业务日轮转，历史接口跨当前段与归档段读取完整 Conversation。

`nooki_later_items` 以 `platform_user_id` 隔离，`client_request_id` 保证创建重试不重复，
`version` 防止多设备用旧状态覆盖新状态。`bootstrap` 与 `sync` 都返回当前 inbox 投影。
