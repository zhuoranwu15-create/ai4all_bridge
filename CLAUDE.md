# AI4ALL 微信 Bot — Claude 开发说明

微信个人 AI 陪伴项目。每个微信账号都有完全隔离的 Soul、对话状态和记忆。不是客服机器人。

## 答复语言（强制执行）

优先使用中文答复；其次英文；不许使用中文、英文之外的任何其他语言。

## 开发规范

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

### 数据库后端（双后端，按 `DATABASE_URL` 二选一）

代码同时支持 SQLite 与 PostgreSQL，由 `.env` 的 `DATABASE_URL` 决定：

- **留空（默认）→ SQLite**：本地开发用 `data/ai4all.sqlite3`，测试用内存 SQLite。本文档下文提到的「标准数据库 `data/ai4all.sqlite3`」均指此本地/测试默认。
- **非空（`postgresql://…`）→ PostgreSQL**：**生产（aliyun1 + aliyun2 厚节点）自 2026-06-21 起已全量切到 PG，这是线上真实后端**。aliyun1 本地 PG，aliyun2 直连中心 PG。回滚只需重新注释 `DATABASE_URL` 并重启服务即回 SQLite。

因此 SQLite 代码路径是刻意保留的（dev/test 默认 + 回滚通道），并非生产形态。下文 `app/db/*` 等描述同时覆盖两后端。

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

### Marker 与快档

marker 由 `tests/conftest.py` 的 `pytest_collection_modifyitems` **按每个用例请求的 fixture 自动派生**（互斥三档，新增测试免手写 marker）：`integration`（用 `client`，FastAPI 全栈）、`db`（用 `fresh_db` 等 DB fixture）、`unit`（都不用，纯函数级）。另有 `slow` 按 `--durations` 实测显式标注（少数重用例）。全部注册在根目录 `pytest.ini`（`--strict-markers`，拼错即报错）。

```bash
make test-unit   # -m unit：纯 unit 快档（不碰 DB/FastAPI，秒级）
make test-fast   # -m "not integration"：跳过全栈用例（约 2x）
make test        # SQLite 全量；make test-pg 为 PG 全量
```

测试选择策略：

- 窄范围代码改动，优先运行直接覆盖被改模块或行为的聚焦测试；纯逻辑改动可先跑 `make test-unit` 拿秒级反馈。
- 只有当改动触及共享基础设施、请求路由、持久化/schema、计费、prompt/tool 执行、跨模块契约，或准备提交较大改动时，才运行全量测试。
- 如果用户明确要求完整回归，运行全量测试。

## 开发脚本

```bash
# 直接注入一轮消息，绕过 OpenClaw；默认账号为 "local"。
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "你好"

# 查看某个账号组装后的 system prompt。
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account aid_806382741
```

这两个脚本默认使用端口 `8000`；本地测试时必须显式传入 `--url`。

主要测试账号：`aid_806382741`。旧 `im-bot` 形态账号逐步淘汰，不再作为默认示例。

完整调试参考：[`docs/guides/debugging.md`](docs/guides/debugging.md)

## 模块地图

| 模块 | 职责 |
|---|---|
| `main.py` | FastAPI app 装配：中间件、静态挂载、startup/shutdown、scheduler 接线、`include_router`。路由本身已拆到 `app/routers/*`，此文件不再含 handler |
| `app/routers/*` | 按域拆分的 `APIRouter`：`bridge`(openclaw inbound)、`health`、`web`(/web/*)、`debug`(/debug/*)、`admin_{moderation,accounts,proactive,dreaming,ops,security,campaigns,llm}`(/admin/*)。多节点接入的 `/node/*` 端点在 `app/node_gateway.py`/`app/node_agent.py`（见下）。共享层：`deps`(鉴权依赖)、`serializers`(脱敏/视图 helper)、`models`(共享请求模型)。对外 URL 与拆分前一致。`app/app_runtime.py` 持有后台事件循环（startup 写入，router 请求时 `get_background_loop()` 读取） |
| `turn_service.py` | 每条入站消息的入口 |
| `prompt_builder.py` | 从所有来源组装 LLM 上下文 |
| `user_profiles.py` | 账号级上下文文件：SOUL、IDENTITY、USER、MEMORY。厚节点下这些文件已下沉到 DB（`profile_storage.py` + `account_profile_files` 表），`user_profiles.py` 为业务读写门面 |
| `session_lifecycle.py` | 对话 session 轮转 |
| `onboarding.py` | 新用户 onboarding 流程 |
| `dreaming.py` 和 `dreaming_scheduler.py` | Dreaming 记忆压缩与调度 |
| `app/proactive/*` | 主动消息：提醒、commitment、内容邀请、reactivation 拉活、账号主动检查 |
| `memory_writer.py` | turn 后记忆更新 |
| `rate_limiter.py` | 每日和 RPM 配额控制 |
| `openclaw_gateway.py` | 回调 OpenClaw 的 outbound 能力（`openclaw_gateway_ws.py` 为持久 WS 网关变体） |
| `llm.py` / `llm_providers.py` / `llm_adapters.py` | LLM 统一入口。选型为 **family×tier 两层**：`tier_for_task(task)` 把调用点映射到 pro/flash 档，`resolve_provider_for_tier` 按 active family × tier 解析 provider；`llm_adapters` 把各协议响应归一。运行时切换存 `app/db/llm_config.py`，后台 `admin_llm` 管理。详见 [LLM family×tier 设计](docs/tech_design/llm_family_tier_design.md) |
| `agent_self_state.py` / `relationship_state.py` / `mission_*.py` + `mission_templates/` | Agent 自我状态层与使命子系统：关系阶段 + 马斯洛需求 + 使命，确定性更新后注入 prompt block。设计见 [agent_self_prd](docs/product/agent_self_prd.md)、[使命与编排设计](docs/tech_design/agent_mission_and_orchestration_design.md) |
| `user_meta_scheduler.py` | 账号级派生画像（user_meta）的天级刷新调度器——**与 proactive、dreaming 并列的第三个调度器**。数据在 `app/db/user_meta.py` |
| `node_gateway.py` / `node_agent.py` | 多节点接入：中心侧按 `node_id` 派发 openclaw 登录/登出与 outbound（local 直调 / remote HTTP push），`node_agent` 为瘦接入节点（node-only 机，如 aliyun2，全程不碰本地 DB）。设计见 [multi_node_access_refactor](docs/tech_design/multi_node_access_refactor.md) |
| `tool_evidence_replay.py` / `context_window.py` / `context_summarizer.py` | 短期上下文运行时：工具证据跨 turn 回灌、历史 token 预算裁剪、session 内滚动摘要 |
| `app/db/*` | 数据访问层（SQLite/PostgreSQL 双后端，按 `DATABASE_URL` 二选一，见上文「数据库后端」）。按域拆分的包：`_core`(连接/`init_db`/`_MIGRATIONS` 迁移框架)、`_backend`(SQLite/PG 后端中立垫片)、`accounts`、`lifecycle`(session 轮转，无独立 sessions 模块)、`billing`、`moderation`、`proactive`、`admin`、`analytics`、`ops`、`campaign`(营销活码)、`llm_config`(运行时 family/tier 绑定)、`user_meta`(账号级派生画像)、`mission`(使命分配)。`from app.db import X` 接口不变，由 `__init__.py` 重导出 |

## 开发约定

按账号隔离是核心不变量。任何未按 `account_id` 约束的 DB 查询或文件写入都是 bug。

`contacts` 表是历史模型，早期 DB 已迁移到账号级 schema；当前代码不再包含该迁移、也无 contacts 表。Admin 账号管理使用 `/admin/accounts/*`；不要再新增 `/admin/contacts/*` API 或依赖 contacts 表。

数据库 schema 由 `PRAGMA user_version` 跟踪版本（`app/db/_core.py` 的 `_MIGRATIONS` 注册表，`init_db()` 顺序应用）。修改 schema 时**新增一个迁移函数并追加到 `_MIGRATIONS`**，不要再用启动期 `_ensure_column` 补丁。基线 m0001 是当前全量 schema 的幂等定义。

新增路由请加到 `app/routers/` 对应域的模块（而非 `main.py`）；`main.py` 只负责 app 装配与 `include_router`。新增 router 模块若 `from app.config import settings`，需在 `tests/conftest.py` 的 `fresh_db`/`client` 两个 fixture 各补一行 `patch("app.routers.<模块>.settings", ...)`，否则测试会落到生产配置（per-module patch 约定）。测试 patch router 内部对象时遵循 "patch where it's used"（patch `app.routers.<模块>.X`，不是 `app.main.X`）。

主动调度器推荐作为独立进程运行，通过 `scripts/run_proactive_scheduler.py` 启动；只有在单 worker 部署时才可设置 `PROACTIVE_SCHEDULER_ENABLED=true`。Dreaming scheduler 可通过 FastAPI in-process 开关（`DREAMING_SCHEDULER_ENABLED`）或 admin run-once 调试，避免多实例重复扫描。User meta scheduler（账号级派生画像天级刷新）同为 FastAPI in-process 开关（`USER_META_SCHEDULER_ENABLED`，默认关），是与前两者并列的第三个调度器。

标准数据库是 `data/ai4all.sqlite3`。忽略仓库根目录和 `data/` 下空的 `ai4all.db` 文件。

所有配置变量都在 `.env.example` 中用行内注释说明。

<!-- SPECKIT START -->
如需了解要使用的技术、项目结构、shell 命令和其他重要信息，阅读当前 plan。
<!-- SPECKIT END -->
