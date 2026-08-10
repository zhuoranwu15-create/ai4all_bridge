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

### Plum 本地联调（Coding Agent 默认）

当任务上下文是 Plum，用户说“启动服务”“启动项目”或“本地联调”时，Codex、Claude 等
Coding Agent 必须优先使用：

```bash
make plum-local-start
```

该命令只面向本地测试：它会启动本地 PostgreSQL，幂等初始化隔离的 `ai4all_plum_dev` 和
固定测试身份，开启 Plum 流式聊天，
并关闭若干与联调无关的后台调度器。不得用于生产或共享环境。

这些选项是当前测试约定，并非永久不变。若任务需要不同数据库、鉴权模式、端口或后台任务，
先查看 `Makefile` 中 `plum-local-init`、`plum-local-run` 和 `plum-local-start` 的具体封装；
确认不适用时应修改 Make 封装及本说明，不要长期绕开入口复制临时启动命令。

### 通用后端开发（非 Plum）

```bash
make pg-local-up
make pg-local-init
make run
```

本地地址：`http://localhost:8180`

### 数据库后端（主应用迁移到 PostgreSQL）

主应用本地开发与生产统一使用 PostgreSQL。本地 PostgreSQL 16 由 `compose.dev.yml` 提供。
`ai4all_dev` 供朝夕/鸣蝉本地开发共用，`ai4all_plum_dev` 供 Plum 联调隔离数据。生产
（aliyun1 + aliyun2 厚节点）自 2026-06-21 起已全量切到 PG：aliyun1 本地 PG，aliyun2
直连中心 PG。

主应用运行时和主 pytest 均固定使用 PostgreSQL；`DATABASE_URL` 为空或不是 PostgreSQL
连接串时，首次数据库连接会直接失败，不再回落或创建 SQLite 主库。nearline SQLite 分析库、
PG→SQLite 快照和 TDAI 自身 SQLite 继续保留，不属于主应用后端清理范围。

**「注释掉 `DATABASE_URL` 回落 SQLite」自 2026-07-26 起不再是生产退路**：切 PG 后一个多月的新数据不会同步回 `data/ai4all.sqlite3`，回落等于回到切换当天的快照。生产遇险走 PG 自身的备份/主备，不走后端回落。

主应用 SQLite 不是生产退路。历史 SQLite→PG 导入工具只处理离线旧快照，不是运行时后端。

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

测试不需要启动服务。主测试固定使用 PostgreSQL：由 `pytest-postgresql` 起临时实例，session
内只迁移一次模板库，每个 DB/integration 测试从模板克隆独立数据库。只需 PATH 上有
`pg_ctl`/`initdb`（RPM 系装
`postgresql-server` 即可，**不需要** `pg_config` / `*-devel`）；多版本共存时用
`AI4ALL_TEST_PG_CTL` 指定绝对路径。

测试选择策略：

- 窄范围代码改动，优先运行直接覆盖被改模块或行为的聚焦测试。
- 只有当改动触及共享基础设施、请求路由、持久化/schema、计费、prompt/tool 执行、跨模块契约，或准备提交较大改动时，才运行全量测试。
- 如果用户明确要求完整回归，运行全量测试。

## 开发脚本

```bash
# 直接注入一轮消息，绕过 OpenClaw；默认账号为 "local"。
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "hello"

# 查看某个账号组装后的 system prompt。
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account aid_806382741
```

这两个脚本默认使用端口 `8000`；本地测试时必须显式传入 `--url`。

主要测试账号：`aid_806382741`。旧 `im-bot` 形态账号逐步淘汰，不再作为默认示例。

完整调试参考：[`docs/ops/products/zhaoxi/debugging.md`](docs/ops/products/zhaoxi/debugging.md)。

## 模块地图

| 模块 | 职责 |
|---|---|
| `app/agent_runtime/turns/service.py` | 跨产品 turn engine；产品差异通过 `ProductTurnServices` 注入 |
| `app/products/zhaoxi/application/turn_services.py` | 朝夕 session、上下文、onboarding 与 after-turn hooks |
| `app/agent_runtime/context/prompt_builder.py` | 形态无关的 LLM 上下文组装与安全资产 owner |
| `app/bootstrap/application.py`、`app/products/zhaoxi/manifest.py` | ASGI composition root 与朝夕路由/lifecycle manifest |
| `app/products/zhaoxi/api/app.py` | 朝夕 App API；同时挂旧 `/v1/*` 与固定产品 namespace |
| `app/products/zhaoxi/infrastructure/profiles.py` | 朝夕账号级上下文文件：SOUL、IDENTITY、USER |
| `app/products/zhaoxi/application/memory/session_lifecycle.py` | 朝夕对话 session 轮转 |
| `app/products/zhaoxi/application/onboarding.py` | 朝夕新用户 onboarding 流程 |
| `app/products/zhaoxi/application/memory/dreaming.py` 和 `app/products/zhaoxi/jobs/dreaming/scheduler.py` | Dreaming 记忆压缩与调度 |
| `app/products/zhaoxi/proactive/*` | 朝夕主动消息：提醒、commitment、内容邀请、reactivation 拉活、账号主动检查 |
| `app/products/zhaoxi/proactive/obligations/reminder_schedule.py` | 朝夕提醒周期与下次触发时间算法 |
| `app/products/zhaoxi/tools/*` | 朝夕专属 LLM 工具 handler：使命、提醒、承诺、内容邀请、主动偏好 |
| `app/products/zhaoxi/application/memory/writer.py` | 朝夕 turn 后记忆更新 |
| `app/platform/moderation/*` | 跨产品内容审核规则、provider、worker 与持久化 |
| `app/platform/quota/rate_limiter.py` | 产品级 RPM 配额控制 |
| `app/platform/gateways/openclaw.py` | 回调 OpenClaw 的 outbound 能力 |

根 `app/turn_service.py`、`app/prompt_builder.py`、`app/reminder_utils.py` 仅保留旧 Python import
兼容，不再放业务实现。新增真实产品按 [`docs/guides/adding-product.md`](docs/guides/adding-product.md)
接入，不复制朝夕 legacy 路由。

## 开发约定

按账号隔离是核心不变量。任何未按 `account_id` 约束的 DB 查询或文件写入都是 bug。

`contacts` 表是历史模型，当前 DB 初始化会迁移到账号级 schema 并删除该表。Admin 账号管理使用 `/admin/accounts/*`；不要再新增 `/admin/contacts/*` API 或依赖 contacts 表。

主动调度器推荐作为独立进程运行，通过 `scripts/run_proactive_scheduler.py` 启动；只有在单 worker 部署时才可设置 `PROACTIVE_SCHEDULER_ENABLED=true`。Dreaming scheduler 可通过 FastAPI in-process 开关或 admin run-once 调试，避免多实例重复扫描。

主应用标准数据库是 PostgreSQL。本地 Compose 默认端口 `55432`；nearline/TDAI 的 SQLite
文件按各自文档管理。忽略仓库根目录和 `data/` 下空的 `ai4all.db` 文件。

所有配置变量都在 `.env.example` 中用行内注释说明。

## Spec Kit

如需了解要使用的技术、项目结构、shell 命令和其他重要信息，先看
[`docs/README.md`](docs/README.md) 与 [`docs/plans/README.md`](docs/plans/README.md)，再阅读对应当前 plan。
