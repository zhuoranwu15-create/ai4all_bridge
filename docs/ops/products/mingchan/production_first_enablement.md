# 鸣蝉生产首次启用 Runbook

更新时间：2026-08-05

状态：**开发机验证完成，线上步骤待发布确认。** 本文是朝夕相伴（微信端）与鸣蝉 App 拆分后的首次
生产执行单。执行对象是 aliyun1 central 与 aliyun2 厚节点；任何写操作必须在维护窗口内逐项确认。

## 1. 发布目标与硬约束

- 朝夕相伴继续使用 `app_id=zhaoxi`，保留 Web/H5、扫码、OpenClaw/微信和主动消息链路。
- 鸣蝉是全新产品，使用 `app_id=mingchan` 与 `/api/v1/products/mingchan/*`；不迁移旧 App token、
  session、World、居民、消息、权益或客户端缓存。
- `platform_users` 是跨产品真人身份。朝夕 legacy World、居民、runtime account、微信 binding、
  消息、记忆、账务和媒体全部原地保留；鸣蝉依靠 `app_id` 隔离创建自己的 World。
- 生产真实数据库是 PostgreSQL。禁止清空 `DATABASE_URL` 回落旧 SQLite 快照，禁止操作
  `data/ai4all.sqlite3`，禁止用只支持 SQLite 的 `scripts/restore_data.py` 恢复生产。
- 当前 `PRODUCTION_PRODUCT_REGISTRY` 中鸣蝉是代码级 `enabled=False`，**不是环境变量开关**。首次发布
  必须先暗部署当前禁用版本，完成 migration、保留型 precheck 和朝夕回归；之后另做经过开发机验证的启用提交。
  禁止直接在生产工作区把 `False` 改成 `True`。
- 鸣蝉 scheduler 只允许在 aliyun1 central 单实例运行；aliyun2 不安装、不启动。新旧 World worker
  不能并行。
- 现有 nginx 已原样透传 `/api/v1/`，并单独放行 `/v1/media/`；客户端使用 canonical namespace 时
  无需新增 location。先运行 `nginx -t`，不要手改 `/etc/nginx/conf.d/`。
- 不需要重启 OpenClaw gateway。除非独立确认 bridge 也有变更，否则禁止使用
  `scripts/restart_runtime.sh --restart-openclaw`，避免所有微信账号同时重连。

任一执行人不清楚目标 commit、备份标识、数据库主库、客户端版本或回滚负责人时，停止发布。

## 2. 发布记录单

开始前在发布工单记录以下内容；命令输出不得包含数据库口令、短信密钥或 token。

| 项目 | 发布时填写 |
| --- | --- |
| 维护窗口 |  |
| 暗部署 commit |  |
| 鸣蝉启用 commit |  |
| 前一稳定 commit |  |
| aliyun1 / aliyun2 当前 commit |  |
| PostgreSQL 主库与 schema version |  |
| 备份目录、dump 文件及完整性检查结果 |  |
| legacy 保留型 precheck 审核人 |  |
| 发布执行人 / 回滚负责人 |  |
| 鸣蝉测试手机号 |  |
| Native App 测试版本 / 最低支持版本 |  |
| 开始、结束、观察截止时间 |  |

开发机交付结果应同时附上：SQLite/PG 全量测试、聚焦门禁、OpenAPI snapshot、双产品 World 隔离测试、
代码变更清单和配置差异。当前基线见
[拆分计划](../../../plans/shared/zhaoxi_mingchan_product_split_plan.md)。

## 3. 发布前准备

### 3.1 准备两个不可混用的 commit

1. **暗部署 commit**：即本次拆分提交，`app/bootstrap/product_registry.py` 中鸣蝉保持
   `enabled=False`。它包含 schema、隔离 precheck、API、代码拆分和 systemd 单元。
2. **启用 commit**：仅在暗部署线上核验通过后，从暗部署 commit 创建；把生产注册表中的鸣蝉切为
   `enabled=True`，同步修改旁边的禁用说明，并在开发机重跑产品注册、manifest、API 和隔离聚焦测试。

启用 commit 必须走正常代码评审和部署，不能通过 SSH 临时编辑。关闭鸣蝉时也使用反向提交恢复
`enabled=False`，而不是在线上留未提交改动。

### 3.2 Native App 与配置评审

- App 清除旧 `zhaoxi` token/缓存，以新账号请求 `/api/v1/products/mingchan/*`。
- App 不再请求旧朝夕 `/v1/*` App API，也不能通过 Header 动态选择产品。
- 先使用受控测试构建验证，真机通过后再扩大客户端发布范围。
- `.env` 只使用 `MINGCHAN_*` 新变量。第一阶段建议只开启核心 World：
  `MINGCHAN_P1_ENABLED=true`；Feed、App inbox、lifecycle commit、mailbox、visit、human chat 继续为
  `false`，验证后逐项开启。
- 若开启媒体，必须先配置 `MEDIA_URL_SIGNING_SECRET`、存储目录、资源 origin，并验证返回是图片/音频
  而非 nginx 的 HTML fallback。
- 若开启 Feed，四个北京时区窗口必须填写且不重叠/不跨日；否则 world-content scheduler 不得启动。
- 若开启 mailbox，先配置 secret manager 中的 `MINGCHAN_MAILBOX_MANIFEST_HMAC_SECRET`。密钥不得写入
  文档、命令历史或 Git。

### 3.3 服务与数据库只读核对

两台机器分别进入实际工作区并确认没有本地修改：

```bash
cd /opt/workspace/ai4all_bridge
git status --short
git rev-parse HEAD
```

`git status --short` 必须为空。aliyun1 再执行：

```bash
.venv/bin/python -c "from app.db._backend import is_postgres; assert is_postgres(), 'production must use PostgreSQL'; print('database_backend=postgres')"
.venv/bin/python -c "from app.config import settings; print('role=', settings.ai4all_role); print('central=', settings.has_central_role)"
sudo systemctl status ai4all-weixin-backend ai4all-weixin-proactive-scheduler --no-pager -n 30
sudo systemctl list-unit-files 'ai4all-*world*'
curl -fsS http://127.0.0.1:8180/health/ready
```

必须确认 aliyun1 是 central-capable、主库可写、aliyun2 流复制/异机冷备状态正常，并记录发布前朝夕
账号、微信 binding、messages、wallet、主动任务及 legacy World 计数。可使用已批准的只读 SQL/运营
查询；输出只保留聚合计数，不导出手机号、消息正文或 token。

## 4. 第一阶段：备份与暗部署

### 4.1 创建可恢复备份

在 aliyun1 当前稳定 commit 上执行：

```bash
cd /opt/workspace/ai4all_bridge
.venv/bin/python scripts/backup_data.py --dry-run
.venv/bin/python scripts/backup_data.py
```

必须记录备份目录和 PG dump 文件，确认脚本的 `pg_restore --list` 完整性检查成功；同时确认 aliyun2
异机 `pg_dump` 和流复制没有异常。仅“生成了文件”不等于可恢复备份。

### 4.2 停止旧 World worker

如果以下旧单元存在，先停止并禁用；不存在时记录 `not-found` 即可：

```bash
sudo systemctl disable --now ai4all-weixin-world-content-scheduler.service
sudo systemctl disable --now ai4all-weixin-world-lifecycle-scheduler.service
```

不要停止朝夕主动消息 scheduler。确认没有遗留脚本进程或其他节点在运行 World worker。

### 4.3 部署暗部署 commit

按现有标准发布流程把两台机器都切到已评审的暗部署 commit，禁止 `git reset --hard`。如果
`requirements.txt` 有变化先安装依赖；无变化不要无意义重装。

aliyun1 在重启服务前显式执行 migration：

```bash
cd /opt/workspace/ai4all_bridge
AI4ALL_ALLOW_AUTO_MIGRATE=1 .venv/bin/python -c "from app.db import init_db; init_db()"
.venv/bin/python - <<'PY'
from app.db import connect

with connect() as conn:
    row = conn.execute(
        "SELECT MAX(version) AS version FROM schema_migrations"
    ).fetchone()
    print("schema_version=", row["version"])
PY
scripts/restart_runtime.sh
```

预期 schema version 为 `63`。如果 migration 失败，不得反复重跑或手改 migration 表；保留日志并中止。

随后部署 aliyun2，同步相同 commit，并用 `scripts/restart_runtime.sh` 按 node-only 路径重启。两机都要
通过 ready 检查。暗部署阶段鸣蝉仍应返回 `503`，且不得创建 membership/session：

```bash
curl -i https://ai4company.top/api/v1/products/mingchan/app/config
```

同时完成朝夕最小回归：Web 注册页/API、现有微信账号被动回复、一个只读 Admin 查询。不要为了本次
发布全局重启 OpenClaw。

## 5. 保留 legacy 朝夕 World 的隔离验收

生产已有 legacy World 与微信老用户绑定，因此本次采用**原地保留**方案，不执行删除型 cleanup。
验收只在 aliyun1、暗部署版本、PostgreSQL 后端执行；不要把生产连接串复制到命令行。

### 5.1 只读 precheck

```bash
cd /opt/workspace/ai4all_bridge
.venv/bin/python scripts/precheck_mingchan_clean_start.py
```

人工核对输出：

- `mode=preserve_legacy_zhaoxi`、`schema_version=63`；
- `product_owner_unique=true`、`safe_to_enable=true`；
- `mingchan_counts` 中 World、模板、通知、membership、session、account 全为 `0`；
- `legacy_counts_retained` 中 World/居民/微信 binding/消息计数必须与发布前登记一致；
- retention 必须明确保留朝夕 World 子域、微信 binding、消息、记忆、账务、媒体和 runtime account。

任一鸣蝉计数非零、组合唯一契约缺失或朝夕计数异常时立即停止。不要为让检查通过而直接改表或删数据。

### 5.2 并存探针与朝夕基线复核

代码级 SQLite/PG 测试必须证明同一 `platform_user_id` 可同时拥有 `zhaoxi` 与 `mingchan` World，
且鸣蝉 owner 查询和 scheduler 只扫描 `app_id=mingchan`。生产暗部署阶段鸣蝉仍 disabled，因此不在
真实库创建探针 World；只复核 schema/index 与朝夕聚合基线。

```bash
.venv/bin/python scripts/precheck_mingchan_clean_start.py
```

验收要求：

- 两次 precheck 输出一致且完全只读；
- 朝夕账号、World、居民、微信 binding、messages、memory、wallet、主动任务计数无变化；
- PG 的 `universes(app_id, owner_platform_user_id)` 唯一索引存在，旧 owner 单列唯一约束已移除；
- `scripts/cleanup_legacy_app_test_data.py --apply` 本次严禁执行；其 blocker 是保护证据，不是待绕过错误。

## 6. 安装新 scheduler 单元（先不启动）

仅在 aliyun1 执行：

```bash
sudo install -m 0644 deploy/systemd/aliyun1-system/ai4all-mingchan-world-content-scheduler.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/aliyun1-system/ai4all-mingchan-world-lifecycle-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ai4all-mingchan-world-content-scheduler.service
sudo systemctl enable ai4all-mingchan-world-lifecycle-scheduler.service
sudo systemctl is-enabled ai4all-mingchan-world-content-scheduler.service
sudo systemctl is-enabled ai4all-mingchan-world-lifecycle-scheduler.service
```

此时不要 `start`。两个脚本在产品注册禁用时会正常退出，不能据此认定 worker 已上线。再次确认
aliyun2 没有这两个单元。

## 7. 第二阶段：启用鸣蝉并灰度验收

### 7.1 上线启用 commit

确认以下条件全部满足后，才能部署 §3.1 的启用 commit：

- 暗部署两机健康，schema version 为 63；
- legacy 保留型 precheck 和朝夕回归通过；
- Native App 测试构建已切 canonical namespace 并清除旧 token；
- 核心环境变量和密钥已由双人复核；
- 回滚负责人在线，观察窗口充足。

先更新 `.env`，首阶段只打开评审通过的能力；运行 `.venv/bin/python -c "from app.config import settings; ..."`
核对布尔值时只打印开关，不打印 secret。然后按标准流程部署启用 commit 到 aliyun1 和 aliyun2，确认
两机 commit 一致、工作区干净。启用后：

```bash
curl -fsS https://ai4company.top/api/v1/products/mingchan/app/config
curl -fsS http://127.0.0.1:8180/health/ready
```

`app/config` 必须返回 `app_id=mingchan`，能力位必须与本轮开关完全一致；不能因客户端期望而临时把
所有能力一次性打开。

### 7.2 受控真机验收

使用全新测试手机号与测试构建，按顺序验证：

1. captcha、OTP 发送/校验、session 创建，token audience 为鸣蝉；日志不出现明文 OTP/token。
2. `/me`、资料更新、重新登录、退出；朝夕 token 访问鸣蝉返回 401，鸣蝉 token 不能访问朝夕。
3. World bootstrap、候选居民、确认居民；生成的 World 与 resident runtime account 均为 `mingchan`。
4. 居民 turn、会话历史、已读、L3/记忆隔离；居民 A 的原文/检索结果不能进入居民 B。
5. 按已开启能力验证 Feed、通知、信箱、许愿、访问、真人聊天和媒体；关闭项必须 fail closed 或在
   `app/config` 中显示 false。
6. 注销鸣蝉账号只清鸣蝉 membership/session/资产，不影响同一真人的朝夕账号、微信 binding、消息、
   钱包与主动任务。
7. 再验证一个朝夕现有账号的微信被动回复和近期提醒/主动消息。

验收期间持续查看 backend ERROR、503/5xx、PG 锁/连接、短信发送量、session/membership 新增数量和
跨产品拒绝日志。

### 7.3 启动 worker

只有对应能力和依赖已验证后才启动：

```bash
sudo systemctl start ai4all-mingchan-world-lifecycle-scheduler.service
sudo systemctl status ai4all-mingchan-world-lifecycle-scheduler.service --no-pager -n 50
sudo journalctl -u ai4all-mingchan-world-lifecycle-scheduler.service -n 100 --no-pager
```

lifecycle worker 即使业务开关关闭也会承担鸣蝉通知清理、孤儿媒体回收和媒体机审；启动前必须确认
这些 provider/目录配置正确。单个维护步骤失败应上报 `partial_error`，不能让整个进程退出。

仅当 `MINGCHAN_FEED_ENABLED=true` 且四个窗口已复核时启动 content worker：

```bash
sudo systemctl start ai4all-mingchan-world-content-scheduler.service
sudo systemctl status ai4all-mingchan-world-content-scheduler.service --no-pager -n 50
sudo journalctl -u ai4all-mingchan-world-content-scheduler.service -n 100 --no-pager
```

在 monitor 配置中加入 `world_lifecycle_scheduler`；Feed worker 启用时再加入
`world_content_scheduler`。验证 scheduler heartbeat 持续更新、metadata 只有聚合指标，不含用户或
resident 明细。确认旧 `ai4all-weixin-world-*` 单元仍是 inactive/disabled，aliyun2 无重复 worker。

## 8. 灰度开关建议顺序

每一级至少完成真机与观察后再进入下一级；出现异常先回退当前开关，不连带修改朝夕配置。

1. 注册表启用 + `MINGCHAN_P1_ENABLED=true`：身份、World、居民 turn、基础资料。
2. `MINGCHAN_APP_INBOX_ENABLED=true`：App 拉取式通知；不经过微信 outbound。
3. 媒体签名与三类媒体位：先 owner 读写，再访客授权和机审。
4. `MINGCHAN_FEED_ENABLED=true`：配置窗口后启动 content worker。
5. `MINGCHAN_MAILBOX_ENABLED=true`：信箱与异步许愿；先配置 manifest HMAC secret。
6. `MINGCHAN_VISITS_ENABLED=true`，再启用 `MINGCHAN_HUMAN_CHAT_ENABLED=true`。
7. `MINGCHAN_LIFECYCLE_EVALUATION_ENABLED=true` 只做候选评估；最后单独评审
   `MINGCHAN_LIFECYCLE_COMMIT_ENABLED=true`，因为它允许不可逆 lifecycle 提交。

`MINGCHAN_APP_ONLY_HUMAN_PROACTIVE_ENABLED` 只有在 App inbox 已开启并完成策略验收后才可打开。

## 9. 观察与完成标准

至少覆盖一个完整业务观察窗口，并记录：

- 两台 backend、朝夕 proactive scheduler、鸣蝉已启用 worker 均健康；
- 鸣蝉 API 2xx/4xx/5xx、OTP、登录、World bootstrap、turn、通知/Feed/outbox 的聚合指标正常；
- 没有 `zhaoxi`/`mingchan` membership、session、account、wallet、World 或通知串域；
- 朝夕注册、微信被动回复和主动消息指标与发布前基线一致；
- PG 无长事务、异常锁等待、连接耗尽或 schema error；
- nginx `/api/v1/products/mingchan/*` 返回正确 JSON，媒体 URL 不返回 HTML；
- 日志和 heartbeat 不含手机号、消息正文、OTP、token、HMAC secret。

只有以上全部满足，才能扩大 Native App 灰度，并在拆分计划中登记生产完成。Runbook 不因一次成功
执行而删除；保留实际 commit、备份、计数和验收结果。

## 10. 中止与回滚

### 10.1 未启用时

- migration、健康检查、朝夕回归或 precheck 任一失败：停止发布，鸣蝉保持 disabled。
- additive schema 可以保留；不要删除 migration 记录。确需回退代码时部署“前一稳定 commit”，不要
  `git reset --hard`，并确认旧 App 写入口是否会重新出现。
- 本次没有 destructive cleanup；可以按标准代码回滚，但不得恢复旧朝夕 App 写入口。

### 10.2 启用后的业务异常

1. 先部署禁用提交恢复 `mingchan enabled=False`。
2. 停止鸣蝉两个 worker：

   ```bash
   sudo systemctl stop ai4all-mingchan-world-content-scheduler.service
   sudo systemctl stop ai4all-mingchan-world-lifecycle-scheduler.service
   ```

3. 保持朝夕 backend、proactive scheduler 和微信链路运行；不要恢复旧 App namespace，不要重启
   OpenClaw。
4. 如果只是单能力异常，优先关闭对应 `MINGCHAN_*` 开关并重启 backend/相关 worker。

### 10.3 数据异常或需要恢复 PG

- 立即停止 aliyun1/aliyun2 所有 backend、access node 和所有 scheduler，暂停客户端流量，先保存当前
  故障现场 dump。
- 由数据库负责人按 PostgreSQL 备份/主备流程恢复并核对 schema、朝夕关键计数和账本；禁止使用
  `scripts/restore_data.py`，禁止回落 SQLite。
- 恢复后先以鸣蝉 disabled 启动 backend，完成朝夕与数据 reconcile，再决定是否重新启用。

详细备份/恢复与双机差异分别见[生产运行手册](../../production_runbook.md)、
[PG 备份与切换跟踪](../../pg_backup_failover_tracking.md)和
[aliyun1/aliyun2 部署差异](../../platform/aliyun1_aliyun2_deployment_diff.md)。

## 11. 严禁事项速查

- 禁止线上手改 `product_registry.py` 或留下未提交文件。
- 禁止执行 legacy cleanup `--apply`、绕过保护计数、手工拼 DELETE 或删除朝夕 World/居民/微信资产/账务。
- 禁止把 `--database-url sqlite:///...` 当临时库；生产只允许 PostgreSQL。
- 禁止在 aliyun2 启动鸣蝉 scheduler，禁止新旧 World worker 并行。
- 禁止一次性打开全部高风险能力，尤其 lifecycle commit。
- 禁止为本次发布全局重启 OpenClaw，禁止把 secret/token/手机号/正文贴进发布记录。
- 禁止回切旧朝夕 App API，禁止把旧 SQLite 快照当生产退路。
