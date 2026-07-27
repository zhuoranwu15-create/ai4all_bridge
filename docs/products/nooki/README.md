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
- `POST /chat`：自然语言对话，返回 `reply + state + cards + metadata`
- `GET /state`：读取权威 `state + cards`
- `POST /tasks/{task_id}/plans/{plan_id}/select`
- `POST /steps/{step_id}/start`
- `POST /steps/{step_id}/complete`
- `POST /tasks/{task_id}/abandon`

按钮写请求统一携带 `client_request_id` 和可选 `expected_version`；缩小行动需要
replacement 内容，继续通过聊天 Tool 执行。
