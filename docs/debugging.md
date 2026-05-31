# 调试指南

本地开发调试参考。适用于开发者和团队成员了解各环节的调试方式。

**Base URL:** `http://localhost:8180`  
**所有 Admin/Debug 端点均需:** `Authorization: Bearer $ADMIN_TOKEN` 或 `Authorization: Bearer $ADMIN_STAFF_TOKEN`  
**后台总入口:** `http://localhost:8180/ui/index.html`  
**Interactive API 文档:** `http://localhost:8180/docs`（Swagger UI，Authorize 填 `Bearer dev-admin-token`）

---

## 技术 Debug 后台规范

技术 Debug 后台服务研发复现、状态操控和链路验证，不承载稳定运营流程。运营后台页面规划见 [运营后台页面规划](product/admin_ops_views.md)。

### 页面和 API 约定

- Debug 页面统一放在 `/ui/*_debug.html`，例如 `/ui/onboarding_debug.html`、`/ui/reminder_debug.html`、`/ui/web_search_debug.html`。
- Debug API 统一放在 `/debug/*`，全部需要 Admin 鉴权，不能暴露到公网。
- Debug 页面可以提供 reset、跳状态、模拟消息、编辑测试数据等强操作，但页面要标明 DEV ONLY。
- 新增 Debug 能力时，优先补充本文件，不单独新增 PRD。
- 如果一个页面主要用于客服和运营处理真实用户问题，应进入 Admin/运营后台，而不是 Debug 后台。

### Admin / Staff Token

Token 来自后端环境变量，示例见仓库根目录 `.env.example`：

```bash
ADMIN_TOKEN=dev-admin-token
ADMIN_STAFF_TOKEN=
```

- `ADMIN_TOKEN`：最高权限后台用户，当前映射为 `role=admin`。
- `ADMIN_STAFF_TOKEN`：普通后台用户，当前映射为 `role=staff`。留空时不启用 staff token。
- Swagger UI 的 Authorize 输入 `Bearer dev-admin-token`；后台静态页首次访问会提示输入 Admin / Staff Token，并存入浏览器 `localStorage["admin_token"]`。

### 当前 Debug 页面

| 页面 | 用途 | 主要入口 |
| --- | --- | --- |
| Onboarding Debug | 测试 onboarding 状态、reset、跳步骤、prompt preview | `/ui/onboarding_debug.html` |
| Reminder Debug | 通过模拟聊天创建提醒，查看/编辑/取消测试提醒 | `/ui/reminder_debug.html` |
| Web Search Debug | 查看 `web_search` schema、模拟 tool invocation 和 provider run trace | `/ui/web_search_debug.html` |
| Swagger UI | 直接调用 Admin/Debug API | `/docs` |

### 开发期明文策略

生产和共享环境默认脱敏，明文查看走 `/admin/plaintext/*` 和 `/admin/plaintext-grants`。开发阶段为了排查效率，可以在开发机 `.env` 中开启明文 Debug；这只适用于测试数据。

当前支持以下配置：

```bash
# 仅本地开发机使用。true 时 Admin/Debug 默认可直接看明文，便于调试测试数据。
ADMIN_DEBUG_PLAINTEXT_ENABLED=true

# 脱敏重新打开后，仍允许这些测试账号绕过 Admin/Debug 默认脱敏。
ADMIN_DEBUG_PLAINTEXT_ACCOUNT_ALLOWLIST=86f866663cf9-im-bot,acct_test_1
```

上线前或推到共享开发机/准生产环境时应切换为：

```bash
ADMIN_DEBUG_PLAINTEXT_ENABLED=false
ADMIN_DEBUG_PLAINTEXT_ACCOUNT_ALLOWLIST=86f866663cf9-im-bot,acct_test_1
```

约束：

- `ADMIN_DEBUG_PLAINTEXT_ENABLED=true` 不得用于生产环境。
- 白名单只放测试账号，不放真实用户账号。
- 白名单绕过是为了稳定复现 Debug，不等于取消明文访问审计；真实用户问题仍按授权流程处理。
- 全局明文开关只在 `APP_ENV=local`、`development` 或 `test` 生效；生产环境即使误配也不会全局放开。

新增 Debug 页面时，需要在本文件补充：

- 页面入口。
- 解决什么排障问题。
- 依赖的 API。
- 高风险动作。
- 是否可能展示正文，以及对应脱敏/白名单规则。

---

## 发送 Mock Turn（无需 OpenClaw / WeChat）

测试完整对话链路的最快方式：

```bash
.venv/bin/python scripts/send_mock_turn.py \
  --url http://127.0.0.1:8180 \
  --text "你好，帮我设个提醒"
```

默认账号 `local`，用 `--sender <account_id>` 切换。  
> 脚本默认端口 8000，本地务必传 `--url`。

---

## 查看 Prompt 组装结果

```bash
curl -s "http://localhost:8180/debug/accounts/86f866663cf9-im-bot/prompt-preview" \
  -H "Authorization: Bearer dev-admin-token" | jq .blocks
```

返回各 block 的字符数（soul / identity / user prefs / memory / daily notes）。

> `scripts/check_prompt.py` 未传 Auth header，调用会 401，暂用上面的 curl。

---

## 消息 & 会话

```bash
# 列出会话
curl -s "http://localhost:8180/debug/sessions" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看某会话的消息（默认 redacted；开发机明文开关接入后可按配置放开）
curl -s "http://localhost:8180/debug/messages?session_id=<id>" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 清空一个会话的消息（重置上下文）
curl -s -X POST "http://localhost:8180/debug/sessions/<session_id>/reset" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

默认 redacted 是生产安全基线。开发机可以按[开发期明文策略](#开发期明文策略)临时放开。

---

## 用户 Profile / 上下文文件

```bash
# 查看 profile 元信息
curl -s "http://localhost:8180/debug/accounts/86f866663cf9-im-bot/user-profile" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

文件实体在磁盘，直接读最方便：

```bash
cat data/user_profiles/<account_id>/SOUL.md
cat data/user_profiles/<account_id>/IDENTITY.md
cat data/user_profiles/<account_id>/USER.md
cat data/user_profiles/<account_id>/MEMORY.md
```

文件加载顺序：`SOUL.md → IDENTITY.md → USER.md → MEMORY.md`

---

## Onboarding 调试

**推荐用 UI：** `http://localhost:8180/ui/onboarding_debug.html`  
（支持创建测试用户、模拟对话、一键 Reset、跳步骤、实时 prompt 预览）

**API：**

```bash
# 查看状态
curl -s "http://localhost:8180/debug/accounts/{account_id}/onboarding" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# Reset（清空 SOUL/IDENTITY/USER 文件）
curl -s -X POST "http://localhost:8180/debug/accounts/{account_id}/onboarding/reset" \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"clear_context_files": true}' | jq .

# 跳到指定步骤
curl -s -X PATCH "http://localhost:8180/debug/accounts/{account_id}/onboarding/state" \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"state": "step2_sent"}' | jq .
```

有效状态：`pending` / `step1_sent` / `step2_sent` / `step3_sent` / `complete` / `timed_out`

---

## 提醒（Reminders）

```bash
# 列出某账号的提醒
curl -s "http://localhost:8180/debug/reminders/86f866663cf9-im-bot" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 修改提醒（如重新调度）
curl -s -X PATCH "http://localhost:8180/debug/reminders/<reminder_id>" \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"due_at": "2026-05-31T09:00:00"}' | jq .

# 删除提醒
curl -s -X DELETE "http://localhost:8180/debug/reminders/<reminder_id>" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

---

## Proactive / Dreaming

```bash
# 手动触发一次 dreaming（生成 memory 更新候选）
curl -s -X POST "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/dreaming" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看 dreaming 历史和 memory items
curl -s "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/dreaming" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看账号的 proactive 状态（heartbeat 候选、quiet hours 等）
curl -s "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/proactive-state" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 手动触发一次 proactive scheduler（不等待定时器）
curl -s -X POST "http://localhost:8180/admin/proactive/scheduler/run-once" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看 commitments（从对话中提取的承诺/待办）
curl -s "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/commitments" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

持续运行调度器（多进程部署时）：
```bash
.venv/bin/python scripts/run_proactive_scheduler.py
```

---

## 账号管理

**推荐用 UI：** `http://localhost:8180/ui/index.html`（运营后台）  
账号列表页，可点击进入单账号详情（`account.html`），支持查看基本信息、channel bindings、编辑显示名称/备注、禁用/启用账号。

**API（供 Claude / 脚本调用）：**

```bash
# 列出所有账号
curl -s "http://localhost:8180/admin/accounts" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看账号详情（含会话、channel bindings）
curl -s "http://localhost:8180/admin/accounts/86f866663cf9-im-bot" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看用量（今日 + 近 7 日消息数）
curl -s "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/usage" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 禁用 / 启用账号
curl -s -X POST "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/disable" \
  -H "Authorization: Bearer dev-admin-token" | jq .
curl -s -X POST "http://localhost:8180/admin/accounts/86f866663cf9-im-bot/enable" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

---

## Debug Traces（完整 Prompt 追踪）

在 `.env` 中开启指定账号的全量追踪：

```
DEBUG_TRACE_ACCOUNT_IDS=86f866663cf9-im-bot
```

```bash
# 列出 traces（默认 redacted）
curl -s "http://localhost:8180/debug/traces?account_id=86f866663cf9-im-bot" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 获取单条 trace
curl -s "http://localhost:8180/debug/traces/<trace_id>" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

明文内容见下节。

---

## 明文访问

消息内容、user profile、debug traces 默认 redacted。本地开发可通过 `ADMIN_DEBUG_PLAINTEXT_ENABLED=true` 或测试账号白名单让 Admin/Debug 默认视图返回明文。也可以直接读磁盘或数据库：

```bash
# 上下文文件
cat data/user_profiles/<account_id>/USER.md

# 数据库消息
sqlite3 data/ai4all.sqlite3 "SELECT content FROM messages ORDER BY id DESC LIMIT 5;"
```

默认安全模式下，API 明文访问需要管理员通过 `/admin/plaintext-grants` 审批授权。开发机临时明文开关只用于测试数据；共享环境和真实用户问题仍按授权流程处理。

---

## 数据库直接查询

```bash
sqlite3 data/ai4all.sqlite3 "SELECT id, display_name, status FROM accounts;"
sqlite3 data/ai4all.sqlite3 "SELECT id, account_id, created_at FROM sessions ORDER BY id DESC LIMIT 10;"
sqlite3 data/ai4all.sqlite3 "SELECT id, session_id, substr(content,1,80) FROM messages ORDER BY id DESC LIMIT 10;"
```
