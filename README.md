# AI4ALL Backend

AI4ALL Backend 是面向多个 AI 聊天产品的模块化后端。当前唯一启用产品是朝夕相伴
（`app_id=zhaoxi`），已同时服务微信私聊、Web/H5 和朝夕 Native App；未来产品通过独立
`app_id`、产品领域和固定 API 命名空间接入。

底层核心是形态无关的 Agent Runtime，负责对话、Prompt、工具、上下文和记忆；身份、鉴权、
配额、钱包、审核、媒体和观测属于共享 Platform；Companion World 等产品语义留在朝夕领域。
OpenClaw / `openclaw-weixin` 只是微信通道基础设施。

## 文档

- [文档导航](docs/README.md)
- [项目启动文档](start.md)
- [产品需求文档](docs/products/zhaoxi/prd.md)
- [产品专题 PRD](docs/products/zhaoxi/README.md)
- [朝夕微信端使用说明](docs/products/zhaoxi/experiences/wechat.md)
- [朝夕 App 客户端接入](docs/products/zhaoxi/integrations/README.md)
- [后台管理说明](docs/ops/products/zhaoxi/admin_guide.md)
- [后续规划](docs/roadmap.md)
- [总体架构 / 框架设计](docs/architecture/overview.md)
- [项目现状与近期方向](docs/STATUS.md)
- [详细技术设计 / 技术平面](docs/architecture/system_design.md)
- [OpenClaw Bridge 技术设计](docs/architecture/shared/access/openclaw_bridge_design.md)
- [主动消息与提醒设计](docs/architecture/products/zhaoxi/proactive_messaging_design.md)

## Local Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
make pg-local-up
make pg-local-init
make run
```

本地 PostgreSQL 由 Docker Compose 提供，默认监听 `127.0.0.1:55432`。初始化命令会幂等
创建并迁移 `ai4all_dev`（朝夕/鸣蝉）和 `ai4all_plum_dev`（Plum 联调）两个开发库；迁移
opt-in 只注入该初始化进程，不写入 `.env`。日常可用 `make pg-local-status` 查看状态，
`make pg-local-stop` 停止并保留数据。需要清空本地开发数据时必须显式执行
`make pg-local-reset CONFIRM=1`。

Health check:

```bash
curl http://127.0.0.1:8180/health
```

### Plum Chat 本地联调

Plum 联调使用同一本地 PostgreSQL 实例中的独立 `ai4all_plum_dev` 数据库，避免污染
朝夕/鸣蝉开发数据；模型密钥仍从 `.env` 读取。先初始化本地 PG，再初始化固定测试账号、
演示角色和 1000 金币，随后启动 SSE 流式后端：

```bash
make pg-local-up
make pg-local-init
make plum-local-init
make plum-local-run
```

`plum-local-init` 和 `plum-local-run` 都强制注入本地 Plum PG DSN，不读取 `.env` 中可能存在
的其他 `DATABASE_URL`。初始化脚本只允许 loopback 地址和固定开发库名，拒绝远端或生产库。

Web onboarding:

```text
http://127.0.0.1:8180/ui/onboarding.html
```

Registration now requires a mainland China mobile number, Aliyun Captcha, SMS
OTP verification, and a one-time `otp_token`. The current onboarding page calls
`/web/register-and-binding-intent` after OTP verification, so the backend creates
or reuses the platform user and default AI4ALL Account, then returns the WeChat
QR login intent directly. In `APP_ENV=local` or `APP_ENV=test`, empty Aliyun
SMS/Captcha credentials enable mock mode; in non-local environments, missing
Aliyun credentials fail closed.

For real SMS/Captcha, fill the `ALIYUN_*` block in `.env`. The static onboarding
pages read public Aliyun Captcha `scene_id` and `prefix` from `/web/config`;
server-side AccessKey and SMS template credentials must stay in `.env`.

Mock OpenClaw turn:

```bash
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "你好"
```

Fixed message id for dedupe testing:

```bash
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "你好" --message-id fixed-1
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "你好" --message-id fixed-1
```

## LLM

The backend uses an OpenAI-compatible `/chat/completions` API when `LLM_API_KEY`
is set. If `LLM_API_KEY` is empty, it keeps the local mock fallback.

```bash
cp .env.example .env
# DeepSeek example:
# LLM_BASE_URL=https://api.deepseek.com
# LLM_ACTIVE_FAMILY=deepseek
# LLM_API_KEY=...
```

## Storage

主应用本地开发与生产均使用 PostgreSQL；本地入口见上面的 `make pg-local-*`。本阶段代码仍按
`DATABASE_URL` 保留 SQLite 兼容路径，主 pytest 默认档也暂时仍使用内存 SQLite，后续阶段再
删除。nearline 分析库、PG→SQLite 快照和 TDAI 自身 SQLite 不属于主应用后端，继续保留。

迁移期 SQLite 默认路径是：

```text
data/ai4all.sqlite3
```

生产 PostgreSQL 连接串只写入部署环境的 `.env`，不要提交真实凭证；仓库模板中的 DSN
仅用于 loopback Compose。部署和切换步骤见
[生产运行手册](docs/ops/production_runbook.md)。

Backend 按 AI4ALL 业务账号隔离上下文。当前代码里的 `account_id` 是历史命名，语义上应理解为 `ai4all_account_id`；未绑定 legacy 入站可 fallback 为 OpenClaw `session_key`，Web onboarding 绑定完成后会路由到 Backend 预创建的 `aid_...` 账号（当前生成规则为 `aid_` + 9 位数字）。不要把它等同于 OpenClaw payload 原生 `account_id`。身份与架构边界见 [总体架构 / 框架设计](docs/architecture/overview.md)、[详细技术设计](docs/architecture/system_design.md) 和 [身份模型与微信绑定](docs/architecture/shared/access/identity_model_and_wechat_binding.md)。

## OpenClaw Bridge

Install or update the local Bridge plugin:

```bash
openclaw plugins install ./openclaw-bridge --force
openclaw gateway restart
```

Make sure the Bridge backend URL matches the FastAPI port you are using. The
current local and deployment docs use `8180`:

```bash
openclaw config set plugins.entries.ai4all-openclaw-bridge.config.backendUrl http://127.0.0.1:8180
openclaw gateway restart
```

Check plugin and channel status:

```bash
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

Expected plugin output includes:

```text
Typed hooks:
before_agent_reply
```

Shadow trace stays disabled by default. Leave the Bridge-side account list
empty; add test accounts only when doing local orchestration comparison. Those
accounts will let OpenClaw run its native agent, rewrite the outbound content
back to AI4ALL's reply, and store the OpenClaw prompt trace in the backend:

```json5
{
  plugins: {
    entries: {
      "ai4all-openclaw-bridge": {
        hooks: {
          allowConversationAccess: true
        },
        config: {
          shadowTraceAccountIds: ""
        }
      }
    }
  }
}
```

Only set the backend side when enabling shadow trace:

```bash
# DEBUG_TRACE_ACCOUNT_IDS=aid_123456789
```

## WeChat E2E Check

1. Start the backend on the same port configured in the Bridge (`8180` in the current docs).
2. Install/update the Bridge plugin.
3. Restart OpenClaw Gateway.
4. Confirm `openclaw-weixin` is running.
5. Send a private WeChat message from the OpenClaw-connected WeChat account.
6. Confirm the WeChat reply comes from AI4ALL Backend.

## Debug APIs

These endpoints are intended for local development only.

```bash
curl http://127.0.0.1:8180/debug/sessions
curl 'http://127.0.0.1:8180/debug/messages?session_id=1'
curl 'http://127.0.0.1:8180/debug/messages/raw?limit=5'
curl 'http://127.0.0.1:8180/debug/traces?account_id=aid_123456789'
curl http://127.0.0.1:8180/debug/accounts/aid_123456789/user-profile
curl -X POST http://127.0.0.1:8180/debug/sessions/1/reset
curl http://127.0.0.1:8180/debug/sessions/1/profile
curl -X POST http://127.0.0.1:8180/debug/sessions/1/profile \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'
```

## Admin APIs

Admin endpoints require `Authorization: Bearer $ADMIN_TOKEN`.

```bash
export ADMIN_TOKEN=dev-admin-token

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts/aid_123456789

curl -X PATCH http://127.0.0.1:8180/admin/accounts/aid_123456789/profile \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'

curl -X POST http://127.0.0.1:8180/admin/accounts/aid_123456789/disable \
  -H "Authorization: Bearer $ADMIN_TOKEN"

curl -X POST http://127.0.0.1:8180/admin/accounts/aid_123456789/enable \
  -H "Authorization: Bearer $ADMIN_TOKEN"

curl -X POST http://127.0.0.1:8180/admin/sessions/1/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

The old `contacts` table and `/admin/contacts/*` route family have been migrated
away in current code. Account-level management is now under `/admin/accounts/*`.

## Troubleshooting

Backend logs are printed by uvicorn.

OpenClaw logs:

```bash
tail -120 ~/.openclaw/logs/gateway.log
tail -120 ~/.openclaw/logs/gateway.err.log
tail -120 ~/.openclaw/tmp/openclaw-501/openclaw-$(date +%F).log
```

Common checks:

```bash
curl http://127.0.0.1:8180/health
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

If WeChat replies with the default OpenClaw assistant instead of AI4ALL, check:

- Bridge plugin is installed and enabled.
- `before_agent_reply` appears in runtime inspect.
- Backend URL in the Bridge environment/config points to the reachable backend.
- Backend logs show `POST /openclaw/turn`.
