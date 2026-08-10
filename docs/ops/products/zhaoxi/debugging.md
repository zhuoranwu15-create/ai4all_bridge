# 调试指南

本地开发调试参考。适用于开发者和团队成员了解各环节的调试方式。

**Base URL:** `http://localhost:8180`  
**所有 Admin/Debug 端点均需:** `Authorization: Bearer $ADMIN_TOKEN` 或 `Authorization: Bearer $ADMIN_STAFF_TOKEN`  
**后台总入口:** `http://localhost:8180/ui/index.html`  
**Interactive API 文档:** `http://localhost:8180/docs`（Swagger UI，Authorize 填 `Bearer dev-admin-token`）

---

## 技术 Debug 后台规范

技术 Debug 后台服务研发复现、状态操控和链路验证，不承载稳定运营流程。运营后台页面规划见 [运营后台页面规划](../../../products/zhaoxi/capabilities/admin_ops_views.md)。

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
| Proactive Debug | 触发 scheduler、账号主动检查、account check draft 和内容邀请生成 | `/ui/proactive_debug.html` |
| Web Search Debug | 查看 `web_search` schema、模拟 tool invocation 和 provider run trace | `/ui/web_search_debug.html` |
| Prompt Lab Debug | 查看账号 context files、构建真实 LLM input envelope、观测 blocks/history/tools/carryover、编辑后无副作用重放 | `/ui/prompt_debug.html` |
| Swagger UI | 直接调用 Admin/Debug API | `/docs` |

### 开发期明文策略

生产和共享环境默认脱敏，明文查看走 `/admin/plaintext/*` 和 `/admin/plaintext-grants`。开发阶段为了排查效率，可以在开发机 `.env` 中开启明文 Debug；这只适用于测试数据。

当前支持以下配置：

```bash
# 仅本地开发机使用。true 时 Admin/Debug 默认可直接看明文，便于调试测试数据。
ADMIN_DEBUG_PLAINTEXT_ENABLED=true

# 脱敏重新打开后，仍允许这些测试账号绕过 Admin/Debug 默认脱敏。
ADMIN_DEBUG_PLAINTEXT_ACCOUNT_ALLOWLIST=aid_806382741,aid_123456789
```

上线前或推到共享开发机/准生产环境时应切换为：

```bash
ADMIN_DEBUG_PLAINTEXT_ENABLED=false
ADMIN_DEBUG_PLAINTEXT_ACCOUNT_ALLOWLIST=aid_806382741,aid_123456789
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

默认账号 `local`，用 `--sender <account_id>` 切换。脚本接受 base URL 或完整 `/openclaw/turn` endpoint。
> 脚本默认端口 8000，本地务必传 `--url`。

---

## 查看 Prompt 组装结果

```bash
curl -s -X POST "http://localhost:8180/debug/prompt-lab/accounts/aid_806382741/build" \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"include_tool_instructions": true}' | jq '{prompt_blocks, tooling, history_metadata, carryover}'
```

返回本轮统一 LLM input envelope 的观测信息：prompt blocks、可用/禁用工具、历史消息来源和 carryover 注入状态。`/debug/accounts/{account_id}/prompt-preview` 仍保留为兼容 wrapper，但不再单独组装 prompt。

也可以用脚本查看，`--token` 默认读取环境变量 `ADMIN_TOKEN`，未设置时使用本地默认 `dev-admin-token`：

```bash
.venv/bin/python scripts/check_prompt.py \
  --url http://127.0.0.1:8180 \
  --account aid_806382741
```

需要编辑 prompt 后对比输出时，使用 `http://localhost:8180/ui/prompt_debug.html`。Prompt Lab 的 replay 只调用 LLM，不写正常消息、不发微信、不更新记忆、不触发工具。

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
curl -s "http://localhost:8180/debug/accounts/aid_806382741/user-profile" \
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

**模拟带营销活码进入（活码强制人设/AI名字/使命/引导语分支）：** 新建测试用户时可在下拉框选一个已配置的活码（来自 `/ui/campaign_codes_admin.html`），建号时会与生产注册共用 `apply_campaign_code_attribution` 写入归因快照并应用强制 SOUL 人设 / 强制 AI 名字，从而复现"用户扫这个活码进来"之后的 onboarding 分支（跳过人设菜单/跳过问 AI 名字、专属引导语、强制身份首次开口）。活码同时强制人设与 AI 名字时，onboarding 会在收到用户称呼后**直接完成**（少问一轮）。调试流量不计入活码转化统计（`increment_usage=False`）。不选活码则走默认 onboarding 流程。带活码账号点 Reset 会按注册快照重新落地强制 AI 名字 + 人设，不会退回空白模板。

> **前置开关**：这个面板通过合成 `session_key` 打到真实 `/openclaw/turn` 管线，依赖服务端 `OPENCLAW_INBOUND_REQUIRE_BINDING=false` 才能命中调试账号（未绑定入站不被收口）。它是 dev 专用工具，开发机 `.env`（`APP_ENV=local`）默认已置 false。若指向一台按生产默认值（`true`）配置的服务器，面板会静默显示"（无回复）"。

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
curl -s "http://localhost:8180/debug/reminders/aid_806382741" \
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

**推荐用 UI：** `http://localhost:8180/ui/proactive_debug.html`

Proactive Debug 可完成以下手动验证，不需要直接写 curl：

- 选择账号并开启 proactive state。
- 模拟入站消息，给测试账号补充聊天上下文。
- 将第一条 pending reminder 调整为到期。
- 运行一次 scheduler，观察 reminder / commitment / account check / content invitation 结果。
- 生成、提升或清理 account check draft。
- 单账号执行 `Run Proactive Check`，查看是否生成内容邀请；未生成时页面展示 reason/detail，例如 `llm_no_content_invitation`。

```bash
# 手动触发一次 dreaming（生成 memory 更新候选）
curl -s -X POST "http://localhost:8180/admin/accounts/aid_806382741/dreaming" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看 dreaming 历史和 memory items
curl -s "http://localhost:8180/admin/accounts/aid_806382741/dreaming" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看账号的 proactive 状态（账号主动检查候选、quiet hours 等）
curl -s "http://localhost:8180/admin/accounts/aid_806382741/proactive-state" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 手动触发一次 proactive scheduler（不等待定时器）
curl -s -X POST "http://localhost:8180/admin/proactive/scheduler/run-once" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看 commitments（从对话中提取的承诺/待办）
curl -s "http://localhost:8180/admin/accounts/aid_806382741/commitments" \
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
curl -s "http://localhost:8180/admin/accounts/aid_806382741" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 查看用量（今日 + 近 7 日消息数）
curl -s "http://localhost:8180/admin/accounts/aid_806382741/usage" \
  -H "Authorization: Bearer dev-admin-token" | jq .

# 禁用 / 启用账号
curl -s -X POST "http://localhost:8180/admin/accounts/aid_806382741/disable" \
  -H "Authorization: Bearer dev-admin-token" | jq .
curl -s -X POST "http://localhost:8180/admin/accounts/aid_806382741/enable" \
  -H "Authorization: Bearer dev-admin-token" | jq .
```

---

## Debug Traces（完整 Prompt 追踪）

在 `.env` 中开启指定账号的全量追踪：

```
DEBUG_TRACE_ACCOUNT_IDS=aid_806382741
```

```bash
# 列出 traces（默认 redacted）
curl -s "http://localhost:8180/debug/traces?account_id=aid_806382741" \
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

# 数据库消息（DATABASE_URL 从本机 .env 注入，不要把连接串贴进共享记录）
psql "$DATABASE_URL" -c "SELECT content FROM messages ORDER BY id DESC LIMIT 5;"
```

默认安全模式下，API 明文访问需要管理员通过 `/admin/plaintext-grants` 审批授权。开发机临时明文开关只用于测试数据；共享环境和真实用户问题仍按授权流程处理。

---

## 数据库直接查询

```bash
psql "$DATABASE_URL" -c "SELECT id, display_name, status FROM accounts;"
psql "$DATABASE_URL" -c "SELECT id, account_id, created_at FROM sessions ORDER BY id DESC LIMIT 10;"
psql "$DATABASE_URL" -c "SELECT id, session_id, substr(content,1,80) FROM messages ORDER BY id DESC LIMIT 10;"
```
