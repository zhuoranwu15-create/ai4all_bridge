# aliyun1 / aliyun2 部署差异梳理

> 目的：你期望两机「系统层 + 业务层」配置尽量一致，但因角色不同、历史安装方式不同，实际存在若干差异。本文把差异**集中列清**，标注哪些是**必然差异**（角色决定，不该消除）、哪些是**历史/环境差异**（可收敛或需对齐）。
>
> 最后更新：2026-06-21（**登机实测核对，非纸面推断**）
>
> ## 核实到的真实现状（2026-06-21）
>
> **P5 厚节点 + PG 迁移其实已经上线，两机都已切换完成**（runbook 的「阶段一/阶段二」实际已执行）：
>
> | 维度 | aliyun1 | aliyun2 |
> |---|---|---|
> | 角色 `AI4ALL_ROLE` | `central,node` | `node` |
> | DB | **本机 PostgreSQL**（`:5432` 监听 `172.24.16.141`+localhost）；**已无 SQLite 文件** | **直连 aliyun1 PG** `…@172.24.16.141:5432/ai4all`（厚节点，无本地库） |
> | turn 处理 | 本机处理自身 61 个账号 | **本机处理**自身 14 个账号（inbound 已切本地 :8180） |
> | 账号数（中心 PG `accounts`） | `assigned_node_id=aliyun1`：**61** | `assigned_node_id=aliyun2`：**14**（共 75） |
> | inbound（openclaw-bridge `backendUrl`） | `http://127.0.0.1:8180` | `http://127.0.0.1:8180`（已本地化，**不再转发中心**） |
>
> 两机共用 **aliyun1 上的一份中心 PG**；`access_nodes` 表两节点均在册（aliyun1→`http://127.0.0.1:8180`，aliyun2→`http://172.24.18.88:8190`）。HA 主备（standby）尚未部署，PG 为单实例。

---

## 0. 角色定位（差异的根源）

| | aliyun1 | aliyun2 |
|---|---|---|
| 公网/内网 | 中心，公网入口（nginx :80） | `39.96.70.112` / 内网 `172.24.18.88` |
| 业务角色 | `central,node`（中心控制面 + 自身也挂微信号跑 turn） | `node`（厚节点，跑 turn 但不持控制面/不持库） |
| DB | 持有中心 PG（唯一写者来源） | 直连中心 PG，**不持有本地库** |
| 控制面（/web /admin） | 挂载（`has_central_role=true`） | 不挂载（`has_central_role=false`） |
| 调度器 | proactive（独立进程）+ dreaming（进程内） | **不跑任何调度器** |

> 铁律：DDL 迁移只在中心（`init_db`）跑，节点 `AI4ALL_ROLE=node` 跳过建表；中心 PG 是唯一写者。两机「业务层不一致」的绝大部分由角色差异推导而来，属**必然差异**。

---

## 1. 系统层差异

| # | 维度 | aliyun1 | aliyun2 | 类型 | 备注 / 收敛建议 |
|---|---|---|---|---|---|
| S1 | 系统 Python | 满足项目要求 | 系统仅 **py3.6**（fastapi/pydantic 装不上）→ 用 **uv 装 3.11 venv** | 环境差异 | aliyun2 走 `ghfast.top` 拉 uv + tsinghua 镜像装包，详见 memory `aliyun2-python-env`。两机最终 venv 都是 3.11，运行时一致 |
| S2 | PostgreSQL | **本机装 PG**，`:5432` 监听 `172.24.16.141`+localhost（供 aliyun2 内网直连） | 不装 PG，仅 psycopg 客户端连中心 | 必然差异（角色） | 容量：`max_connections` ≥ Σ(各节点 `db_pool_max_size`)+中心+余量 |
| S3 | OpenClaw 安装方式 | 官方安装器，自带 pinned **node v22.22**，dist 在 `~/.openclaw/tools/node-v22.../.../openclaw/dist/`；CLI `~/.openclaw/bin/openclaw` | `npm install -g openclaw` 到 `~/.npm-global`，跑系统 **node v24.16**；CLI `~/.npm-global/bin/openclaw` | 历史差异 | 安装根、node 版本均不同；部署脚本已路径无关化（自动发现 bundle/node） |
| S4 | OpenClaw 版本 | **v2026.5.28** | **v2026.6.5** | 历史差异 | 版本不一致引出 S5/S6。**aliyun1 升 6.x 时需补齐 aliyun2 的补丁** |
| S5 | OpenClaw core/插件补丁 | 5.28 暂不需要 | 已打 **4 个补丁**：QR 登录、解绑登出、图片理解 media、**accountId hook**（6.5 漏 bot AccountId 致 `no_binding`） | 必然差异（因 S4） | 已耐久化 `scripts/patch_openclaw_accountid.sh` + deploy 脚本步骤；aliyun1 升级时随脚本一并打 |
| S6 | OpenClaw 6.5 运行时闸 | 无 | 需 `plugins.entries.*.hooks.allowConversationAccess=true`（已设）；weixin 插件须配置 channel 实例才被加载 | 必然差异（因 S4） | 见 memory `openclaw-install-patch-state` |
| S7 | 进程托管 | **系统级** `sudo systemctl`：`ai4all-weixin-backend`（uvicorn :8180）+ `ai4all-weixin-proactive-scheduler`（独立进程） | **用户级** `systemctl --user`：`ai4all-weixin-backend`（厚节点 :8180）+ `ai4all-weixin-node`（access node :8190） | 必然差异（角色） | `restart_runtime.sh` 已按角色自动分流（工作区未提交） |
| S8 | nginx 入口 | 有，公网 :80；base64 图须 `client_max_body_size 12m`（默认 1m→413） | 无 nginx | 必然差异（角色） | node 经内网直连 |
| S9 | 备份 / 监控 timer | 有系统级 backup/monitor timer（PG `pg_dump`） | 无 | 必然差异（角色） | 备份只在中心 |
| S10 | 微信号登录 | 挂自身真实微信号（61 账号归属） | 挂 2 个真实 bot（14 账号归属）；另有 1 空壳 `default` channel（无害，插件不支持 `--delete`，清理延后） | 业务数据差异 | — |

---

## 2. 业务层差异（`.env` / 应用配置；已登机核对）

> 下列变量在 `.env.example` 均有行内注释。密码等敏感值本文一律 `<redacted>`。

| # | 变量 | aliyun1（实测） | aliyun2（实测） | 类型 |
|---|---|---|---|---|
| B1 | `AI4ALL_ROLE` | `central,node` | `node` | 必然差异 |
| B2 | `NODE_ID` | `aliyun1` | `aliyun2` | 必然差异（全局唯一） |
| B3 | `DEFAULT_NODE_ID` | `aliyun1` | `aliyun2` | 角色相关 |
| B4 | `CENTRAL_URL` | `http://aliyun1` | `http://aliyun1` | **一致** |
| B5 | `NODE_BASE_URL` | （本机 node 自解析→`127.0.0.1:8180`） | `http://172.24.18.88:8190` | 必然差异 |
| B6 | `LOCAL_NODE_INLINE_DISPATCH` | `true`（同机 node 出站即时发） | （node 无意义，未用） | 必然差异 |
| B7 | `DATABASE_URL` | `postgresql://ai4all:<redacted>@localhost:5432/ai4all` | `postgresql://ai4all:<redacted>@172.24.16.141:5432/ai4all` | 必然差异（中心本地 vs 节点内网直连） |
| B8 | `DB_POOL_MIN/MAX_SIZE` | （中心，建议偏大） | `1` / `8` | 角色相关 |
| B9 | `PROACTIVE_SCHEDULER_ENABLED` | `false`（**改由独立进程跑**，非进程内） | `false`（不跑） | 见 §3 |
| B10 | `DREAMING_SCHEDULER_ENABLED` | **`true`**（进程内开） | `false` | 见 §3 |
| B11 | 端口 | backend :8180（+ PG :5432、nginx :80） | backend :8180 + access-node :8190 | 必然差异 |
| B12 | openclaw-bridge `backendUrl`（**openclaw 插件配置**） | `http://127.0.0.1:8180` | `http://127.0.0.1:8180` | **一致**（均本地处理 inbound） |
| B13 | `BRIDGE_SECRET` | 64 字符 | 与 aliyun1 逐字符对齐 | 必须一致 |
| B14 | `OPENCLAW_CLI_PATH` | 默认 | `/home/.../.npm-global/bin/openclaw`（绝对路径，因 S3） | 历史差异 |
| B15 | `NO_PROXY` | — | 须含 `aliyun1` / 内网段（机器有 clash 代理） | 环境差异 |

---

## 3. 主动消息调度

### 3.1 Proactive 主动消息（决策：**保持方案 A**）

**当前实测运转：**

- 只有 **aliyun1** 跑主动调度：`ai4all-weixin-proactive-scheduler.service`（ExecStart=`run_proactive_scheduler.py`，**不传 node_id → 扫全部 75 个账号**）。
- 对 aliyun1 自身 61 个账号：`LOCAL_NODE_INLINE_DISPATCH=true` + 账号归属本机 → **inline 本机直发**。
- 对 aliyun2 的 14 个账号：远程 → `dispatch_proactive_text` 只 `enqueue` pending → **aliyun2 access-node(:8190) pull 循环 claim + 本机发出**。
- **aliyun2 不跑任何调度器**（`PROACTIVE_SCHEDULER_ENABLED=false`）。

**方案对比（2026-06-21 已讨论定）：**

| 维度 | 方案 A（现状，**采纳**） | 方案 B（aliyun2 自扫自发） |
|---|---|---|
| 出站延迟 | enqueue→pull（`outbound_pull_interval_seconds` 间隔，均值 +½间隔）→send | 调度即直发，省 pull 往返 |
| 复杂度 | 单调度器，认知简单 | 须给中心调度加「只扫 `NULL`+自身」域语义，否则**重复扫描→双发** |
| 故障域 | aliyun2 调度无关；消息已生成不丢，pull 恢复即补发 | aliyun2 调度挂 = 它的账号当时无主动消息 |
| 最终发送方 | aliyun2 本机 openclaw（微信号在此登录） | 同左 |

**决策：保持方案 A。** 主动消息（提醒/commitment/拉活）对延迟不敏感，方案 B 的降延迟收益小，却引入「扫描域切割」这一易出双发 bug 的复杂度，风险/收益不划算。

- 若嫌 pull 延迟略大，**零风险折中**是调小 `outbound_pull_interval_seconds`（2.0→1.0/0.5），不动方案 A。
- **重切方案 B 的触发条件**：aliyun2 承载账号数大幅增长（约 >100~150），中心扫全量 + 海量 enqueue/pull 成为瓶颈时再议。当前 14 个远未到。

### 3.2 Dreaming 记忆压缩（与 proactive **不对称**，注意覆盖盲区）

Dreaming 有两条触发路径，机制与 proactive 不同：

1. **turn 内惰性 dreaming**（`get_or_create_account_active_session_with_dreaming`）：session 轮转时就地触发，**跑在处理该 turn 的机器上**。aliyun2 账号的 turn 在 aliyun2 本地处理 → dreaming 也在 aliyun2 跑（读写 aliyun1 PG）。dreaming 只压缩记忆、**不经 openclaw 发送**，故任何有 PG 访问的机器都能为任意账号 dream，无「归属节点」约束。
2. **每日定时 dreaming 扫描**（`DreamingScheduler` → `run_daily_dreaming_scan`）：**仅 aliyun1 进程内开**（`DREAMING_SCHEDULER_ENABLED=true`），且 `main.py` 用 `node_id=settings.node_id`（=aliyun1）启动 → **只扫 `assigned_node_id=aliyun1` 的 61 个**。

> ✅ **已修（O3，2026-06-21）**：`startup_dreaming_scheduler` 改为「具备中心能力的机器扫全量 `node_id=None`、纯 node 才按 P4 分片只扫自身」。aliyun1 现每日扫全 75（含 aliyun2 的 14），盲区消除。dreaming 节点无关（不发 openclaw），中心代扫合法。
>
> ⚠️ **竞态说明（已评估，可接受）**：中心每日扫描 + aliyun2 turn 内惰性 dreaming 可能跨机并发触发同一 session（`close_session` 无 `status='active'` CAS 守卫）。但此竞态**本就存在于 aliyun1 自身 61 账号**（每日扫描 task 与请求处理并发，同样无锁），靠「凌晨扫描窗口窄 + 低流量」容忍，生产未见问题；扩到 aliyun2 的 14 个属同类、非新增风险。**各节点不要再单开 dreaming 调度器**（会与中心全量扫重复）。

---

## 4. 一句话总结

- **应当一致、且已一致**：venv 3.11、`BRIDGE_SECRET`、`CENTRAL_URL`、bridge `backendUrl`、代码版本。
- **必然不同、不该对齐**：角色（B1–B3/B5–B7）、控制面/PG/nginx/timer/调度（S2/S7–S9、B9–B11）。
- **历史/环境不同、可收敛或需留意**：OpenClaw 安装方式与版本（S3/S4 → 引出 S5/S6 补丁差异）、Python 来源（S1）、代理/CLI 路径（B14/B15）。
- **遗留待办**：见 §5。

---

## 5. 优化 / 修补 backlog（2026-06-21 梳理）

> 按优先级。P0 为线上正确性问题，建议尽快处理。

| # | 优先级 | 问题 | 现状证据 | 建议 |
|---|---|---|---|---|
| O1 | **P0** | **aliyun1 的「账号检查」主动消息对 aliyun2 账号发不出** | aliyun1 跑已提交 HEAD，`account_checks.py` 仍用 `send_proactive_text`（旧）；扫到 aliyun2 账号时在 aliyun1 本机直发，但微信号在 aliyun2 → 失败且抢占 pending 行，aliyun2 pull 不到 | 已有修复（`account_checks.py` send→`dispatch_proactive_text`，在 aliyun2 工作区未提交）→ **提交并部署到 aliyun1**。reminders/commitment/reactivation 已是 dispatch 版，不受影响 |
| O2 | **P0** | **连接池改动未提交、仅在 aliyun2 工作区** | aliyun1 HEAD 无池代码；aliyun2 重启后池已生效但**未提交**，任何 `git pull`/reset 会丢 | **提交** `_backend.py`+`_core.py`+`main.py`（含 shutdown 关池）；aliyun1 也部署（本地 socket 收益小但保持一致+幂等耐久） |
| O3 | ✅ **已修** | **每日 dreaming 扫描漏掉 aliyun2 的 14 账号** | （原）`main.py` 用 `node_id=settings.node_id`(aliyun1) 起 DreamingScheduler，只扫 61 | **已修**：`startup_dreaming_scheduler` 改为「中心机扫全量 `node_id=None`、纯 node 才分片」，aliyun1 现每日扫全 75。dreaming 节点无关（不发 openclaw）故中心代扫合法。竞态见 §3.2 注 |
| O4 | P2 | aliyun2 出站绕远：已直连 PG 却仍走 HTTP pull（:8190→中心 `/node/outbound/claim`） | 瘦节点时代产物 | 可让 aliyun2 直接从 PG `claim_pending_outbound_by_node` 认领，去掉对中心 HTTP 的出站依赖（降耦合/延迟）。非紧急 |
| O5 | P2 | 主动消息 pull 延迟 | `outbound_pull_interval_seconds=2.0` | 嫌慢可调 1.0/0.5，零风险 |
| O6 | P2 | PG 单点无 HA | 单实例，无 standby | 参考 runbook §10 配流复制热备 + `pg_dump` PITR |
| O7 | P3 | PG `max_connections` 余量核对 | 现 100，活动连接仅 2 | 加池后 Σ(各节点 `db_pool_max`)+中心+timer 需 ≤100，账号扩容前复核 |
| O8 | P3 | OpenClaw 版本/补丁漂移 | aliyun1 v5.28 / aliyun2 v6.5 + 4 补丁 | aliyun1 升 6.x 时随 `patch_openclaw_accountid.sh` 一并补（见 S4/S5） |
| O9 | P3 | `restart_runtime.sh` 按角色分流改动未提交 | aliyun2 工作区 `M` | 与 O1/O2 一并提交 |
