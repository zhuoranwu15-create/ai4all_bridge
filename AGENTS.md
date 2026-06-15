# AI4ALL 微信 Bot - Codex 工作说明

AI4ALL 微信 Bot 是一个微信个人 AI 陪伴项目。每个微信账号都有完全隔离的 Soul、对话状态和记忆。它不是客服机器人。

## Codex 开发规范（替代 Superpowers）

### 核心工作流（强制执行）

1. 接到需求后先做澄清：边界不明确、缺少参数或存在高风险歧义时，先提问确认，不盲目编码。
2. 动手前先输出开发计划：明确要修改的文件路径、核心逻辑和影响范围。
3. 编码遵循最小改动原则：不做无关重构，不删除已有业务逻辑，不顺手改动无关文件。
4. 代码完成后必须自审：检查语法错误、边界情况、账号隔离、依赖兼容性和潜在回归。
5. 交付前给出测试方案：明确验证步骤、预期结果；能运行的聚焦测试应优先运行。

### 代码规范

- 严格遵循项目现有技术栈和代码风格，不擅自引入新依赖。
- 公共方法必须加注释；复杂逻辑补充简洁行内说明，避免无意义注释。
- 禁止硬编码密钥、凭证和敏感配置。
- 修改代码必须标注主要改动点，并在交付说明中给出 diff 级别的变更摘要。

### 输出要求

- 输出聚焦核心改动、验证结果和必要风险，不写冗余说明。
- 用户要求代码时，优先给出关键代码或文件引用；避免大段无关解释。
- 如果未能运行测试或存在未覆盖风险，必须明确说明。

## 运行

```bash
.venv/bin/uvicorn app.main:app --reload --port 8180
```

本地地址：`http://localhost:8180`

标准数据库：`data/ai4all.sqlite3`

## 鉴权

| 调用方 | Header | 说明 |
|---|---|---|
| OpenClaw bridge | `Authorization: Bearer dev-secret` | `AI4ALL_BRIDGE_SECRET` |
| Admin / debug | `Authorization: Bearer dev-admin-token` | `ADMIN_TOKEN`，完全权限 |
| Staff | `Authorization: Bearer <ADMIN_STAFF_TOKEN>` | 运营权限，约等于 admin；默认空=禁用 |
| Reviewer | `Authorization: Bearer <ADMIN_REVIEWER_TOKEN>` | 仅 moderation 队列/详情；默认空=禁用 |

## 测试

```bash
.venv/bin/pytest tests/ -v
.venv/bin/pytest tests/test_turn_service.py -v
```

测试不需要启动服务。测试使用内存 SQLite。

测试选择策略：

- 窄范围代码改动，优先运行直接覆盖被改模块或行为的聚焦测试。
- 只有当改动触及共享基础设施、请求路由、持久化/schema、计费、prompt/tool 执行、跨模块契约，或准备提交较大改动时，才运行全量测试。
- 如果用户明确要求完整回归，运行全量测试。

## 开发脚本

```bash
# 直接注入一轮消息，绕过 OpenClaw；默认账号为 "local"。
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "hello"

# 查看某个账号组装后的 system prompt。
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account 86f866663cf9-im-bot
```

这两个脚本默认使用端口 `8000`；本地测试时必须显式传入 `--url`。

主要测试账号：`86f866663cf9-im-bot`，该账号在 `data/ai4all.sqlite3` 中历史最多。

完整调试参考：[`docs/guides/debugging.md`](docs/guides/debugging.md)。

## 模块地图

| 模块 | 职责 |
|---|---|
| `turn_service.py` | 每条入站消息的入口 |
| `prompt_builder.py` | 从所有来源组装 LLM 上下文 |
| `user_profiles.py` | 账号级上下文文件：SOUL、IDENTITY、USER |
| `session_lifecycle.py` | 对话 session 轮转 |
| `onboarding.py` | 新用户 onboarding 流程 |
| `dreaming.py` 和 `dreaming_scheduler.py` | Dreaming 记忆压缩与调度 |
| `app/proactive/*` | 主动消息：提醒、commitment、内容邀请、reactivation 拉活、账号主动检查 |
| `memory_writer.py` | turn 后记忆更新 |
| `rate_limiter.py` | 每日和 RPM 配额控制 |
| `openclaw_gateway.py` | 回调 OpenClaw 的 outbound 能力 |

## 开发约定

按账号隔离是核心不变量。任何未按 `account_id` 约束的 DB 查询或文件写入都是 bug。

`contacts` 表是历史模型，当前 DB 初始化会迁移到账号级 schema 并删除该表。Admin 账号管理使用 `/admin/accounts/*`；不要再新增 `/admin/contacts/*` API 或依赖 contacts 表。

主动调度器推荐作为独立进程运行，通过 `scripts/run_proactive_scheduler.py` 启动；只有在单 worker 部署时才可设置 `PROACTIVE_SCHEDULER_ENABLED=true`。Dreaming scheduler 可通过 FastAPI in-process 开关或 admin run-once 调试，避免多实例重复扫描。

标准数据库是 `data/ai4all.sqlite3`。忽略仓库根目录和 `data/` 下空的 `ai4all.db` 文件。

所有配置变量都在 `.env.example` 中用行内注释说明。

## Spec Kit

如需了解要使用的技术、项目结构、shell 命令和其他重要信息，阅读当前 plan。
