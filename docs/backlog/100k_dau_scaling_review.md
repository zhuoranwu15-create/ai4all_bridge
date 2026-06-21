# 10 万 DAU 架构评估与演进建议

更新时间：2026-06-07

状态：技术评估稿。结论基于对当前代码的核对，可作为容量规划与改造排期的输入。

配套阅读：[`100k_dau_scaling_discussion_draft.md`](../archive/backlog/100k_dau_scaling_discussion_draft.md)（更早的讨论稿，已归档）、[`production_stability_prd.md`](../tech_design/production_stability_prd.md)。

---

## 1. 一句话结论

当前架构是**单机外测形态**（FastAPI 同步链路 + 单文件 SQLite + 本地账号文件 + 单进程 scheduler + 子进程 outbound），**不能直接支撑 10 万 DAU**。

但好消息是：10 万 DAU ≠ 10 万 QPS。基准估算下峰值约 **110 RPS**，真正的硬约束集中在少数几个点。只要按本文路线分阶段改造，单集群完全可以承载。**最大的近期变量不是机器数量，而是同步链路天花板**——这一项决定了你是用 6 台机器还是 30 台机器。

---

## 2. 当前架构事实核对（代码级）

以下结论均来自对当前代码的直接核对，不是推测。

| 维度 | 现状 | 代码位置 | 规模化影响 |
|---|---|---|---|
| turn 入口 | `/openclaw/turn` 是**同步 `def`** | `app/main.py:4177` | 落入 Starlette 线程池（默认 40 线程），单进程并发被钉死在 ~40 |
| LLM 调用 | **同步阻塞** `httpx.Client`，每次新建 client | `app/llm.py:67,151` | 每个 turn 全程独占一个线程；无 keepalive 连接复用 |
| 工具轮次 | `max_tool_rounds` 最高 8 | `app/llm.py:311` | 单轮墙钟可从 6s 膨胀到 30s+，进一步压低单进程吞吐 |
| 数据库连接 | 每次 `sqlite3.connect()`→commit→close，**无连接池** | `app/db.py:102` | 无池化短连接，迁 PG 后必须配 PgBouncer |
| SQLite 并发配置 | 只设了 `foreign_keys`，**无 WAL、无 busy_timeout** | `app/db.py:107` | 默认 rollback journal，写锁阻塞全库；撞锁直接抛 `database is locked` |
| 限流 | 进程内 `threading.Lock` + `deque` | `app/rate_limiter.py` | 多实例后每台独立计数，无法做账号级全局限流 |
| outbound | `subprocess.run` 调 `openclaw gateway call` | `app/openclaw_gateway.py:36,147` | 每次发送 fork 进程 + 解析 stdout JSON，高 QPS 开销大、难限速 |
| 账号上下文 | 写本地 `data/user_profiles` 文件 | `app/user_profiles.py` | 多机器无法天然共享，并发 append 有冲突风险 |
| scheduler | 单进程独立运行，无 leader election | `app/dreaming_scheduler.py`、`app/proactive/scheduler.py` | 文档明确要求不可多实例并跑 |

### 2.1 被低估的两个风险（重点）

**A. 单进程吞吐天花板 ≈ 40 并发**

同步端点 + 同步 LLM 调用的组合，意味着每个 turn 从进入到返回都**独占线程池里的一个线程**，整段时间在等 LLM。单进程吞吐上限 = `线程池大小 / 单轮墙钟秒数`：

| 单轮耗时 | 单进程上限 | 撑 110 RPS 峰值需要的进程数 |
|---|---:|---:|
| 6s（无工具） | ~6.7 turn/s | **~17** |
| 12s（一轮工具） | ~3.3 turn/s | **~33** |
| 30s（多轮工具） | ~1.3 turn/s | **80+** |

> 这是"FastAPI 不是瓶颈"这个常见判断的反例：在**当前同步写法下**，API 进程层就是第一个撞墙的地方。把 LLM 调用与端点改为 async（或显式调大线程池）后，单进程并发可从 40 抬到数百，机器数直接降一个数量级——**这是整份容量估算里弹性最大的单一变量**。

**B. SQLite 没开 WAL、没设 busy_timeout**

当前 `db.py` 全文只有 `PRAGMA foreign_keys`。后果：

- **默认 rollback journal**：任一写事务持有数据库级写锁，阻塞所有其他读写。
- **没有 busy_timeout**：并发写撞锁会**立刻抛 `database is locked`**，而不是等待重试。

这意味着即使在单机、几十并发下，压测就可能出现 locked 报错。加 `WAL` + `busy_timeout` 是几行改动，能显著推后这个问题——属于阶段 0 就该补的稳定性项。

---

## 3. 真实访问压力估算

粗估用于容量讨论，上线前必须用真实埋点校准。

### 3.1 入站 turn 与 RPS

| 场景 | 人均日入站 | 日入站 turn | 平均 RPS | 峰值小时(占 20%) | 瞬时峰值(~2x) |
|---|---:|---:|---:|---:|---:|
| 轻量 | 5 | 50 万 | 5.8 | 28 | **55** |
| 基准 | 10 | 100 万 | 11.6 | 56 | **110** |
| 重度 | 30 | 300 万 | 34.7 | 167 | **330** |

关键认知：压力来自**峰值集中 + LLM 等待时长 + 上下文 token 规模**，而非裸 QPS。

### 3.2 LLM token 压力（基准场景）

单轮假设：input 3,000 / output 300 / 合计 3,300 tokens。

- 日 token：约 **33 亿 tokens/day**
- 峰值 110 RPS：约 **36 万 tokens/s** ≈ 2,180 万 tokens/min
- 若上下文膨胀到 5,000 tokens/轮，峰值接近 **3,300 万 tokens/min**

必须提前与 LLM provider 锁定：TPM/RPM 配额、并发连接上限、高峰价格、降级模型。注意当前**实际生效模型是 DeepSeek**（`.env`：`LLM_MODEL=deepseek-chat`、`LLM_BASE_URL=https://api.deepseek.com`；`config.py:23` 的 `gpt-4o-mini` 只是 env 未设置时的代码 fallback），`llm.py:14-33` 的 DSML 兼容代码也正是为 DeepSeek 的 tool call 格式而写。`llm_max_retries=1`（`config.py:26`）——配额不足触发 429 后只重试一次就 fail。规模化时需按 DeepSeek 的 TPM/RPM 配额与并发上限做容量规划。

### 3.3 LLM 并发需求

并发 ≈ 峰值 RPS × LLM p95（秒）。按 p95=6s：

| 场景 | 瞬时峰值 RPS | 需要 LLM 并发 |
|---|---:|---:|
| 轻量 | 55 | 330 |
| 基准 | 110 | **660** |
| 重度 | 330 | 1,980 |

若每轮额外跑一次 commitment 抽取，LLM 调用量近乎翻倍——必须异步队列化 + 规则预筛 + 采样。

### 3.4 数据库写压力

每个正常 turn 至少产生：inbound 1 + outbound 1 + daily_usage 1 + session 1 +（cost_events/ledger 各 1）+（工具/搜索记录视调用而定）+ memory 写入。

基准峰值估 **500–1,000 writes/s**，重度 1,500–3,000 writes/s。SQLite 不适合此量级，必须迁 PostgreSQL。

---

## 4. 目标架构

```text
WeChat / OpenClaw
      |
      v
 Load Balancer / Ingress
      |
      v
 Stateless FastAPI API Pods  (async 端点 + async LLM client)
      |
      +--> Redis        : 全局限流 / 幂等 / 同账号锁 / 短期状态 / leader election
      |
      +--> PostgreSQL   : 业务真源 (PgBouncer 连接池)
      |
      +--> Queue        : chat / memory / commitment / proactive / outbound / retry / DLQ
                |
                v
          Worker Pools  (按 account_id 分区串行)
                |
                +--> LLM Providers      (多路由 + 熔断 + 降级)
                +--> OpenClaw Gateway    (按微信账号/channel 分片，独立容量池)
                +--> Object Storage / 共享上下文存储
```

不变量（任何阶段都不能破坏）：

- **账号隔离**：所有查询/写入必须带 `account_id` 约束。
- **同账号顺序**：同一账号的 turn 必须串行（Redis 账号锁 / 队列按 account_id 分区）。
- **幂等**：入站按 `account_id + message_id`，outbound 按 `idempotency_key`，scheduler 任务用原子 claim。

---

## 5. OpenClaw Gateway 独立化与分片（专章）

这是整个规模化里**唯一真·有状态、且无法像无状态服务那样自动 failover 的层**，单独成章。

### 5.0 先纠正一个常见误解

OpenClaw Gateway **本来就是独立组件**，Backend 只是它的客户端——你们不 fork OpenClaw、不碰微信协议（见 `openclaw_bridge_design.md`）。`app/openclaw_gateway.py` 只是个薄封装，通过 `subprocess.run` 调本地 `openclaw` CLI。所以"独立化"不是从 0 拆，而是**解开隐性本地耦合 + 做分片和 HA**。

### 5.1 当前真实链路（代码核对）

**入站（用户发消息）：**
```text
微信用户 → openclaw-weixin 插件(握微信登录态) → OpenClaw Gateway
        → ai4all-bridge 插件 → HTTP POST /openclaw/turn → Backend
        → Backend 同步返回 reply 文本 → bridge → OpenClaw → 微信
```
**出站（主动消息/提醒）：**
```text
Backend → subprocess.run("openclaw gateway call send ...") → OpenClaw Gateway → 微信
```

三个关键事实（均来自代码）：

1. **真正有状态的是微信登录活会话，它只活在 openclaw-weixin + Gateway 里，Backend 不持有**（`app/openclaw_gateway.py`）。这正是 Gateway 必须独立并按账号分片的根因。
2. **入站回复当前是同步的**：bridge POST 过来后等 HTTP 响应里的 reply 文本再发回微信（`app/schemas.py` `OpenClawTurnResponse`、`app/main.py:4177`）。即 §9 开放问题里的"同步 synthetic reply"——现状就是同步。
3. **出站走本地 subprocess**（`app/openclaw_gateway.py:36`）：假设 Backend 本机能调到 Gateway。**这是当前最大的隐性耦合**，一旦 Backend 与 Gateway 不在同机即失效。

身份字段：`channel_account_id` = 微信账号（provider 侧），`account_id` = AI4ALL 业务账号；一个 OpenClaw 实例可挂多个微信账号（Phase 1 设定）。

### 5.2 独立化要做的事

| # | 改造 | 说明 |
|---|---|---|
| G1 | **物理拆分** | Gateway 节点（openclaw-weixin + OpenClaw + bridge 插件）独立机器/容器组，不与无状态 Backend 混部；微信会话只在 gateway 节点上 |
| G2 | **出站改网络 RPC** | 把 `app/openclaw_gateway.py` 的 `subprocess.run` 换成 HTTP/gRPC，打到"持有该 `channel_account_id` 的 gateway 节点"；改动收敛在 `_run_gateway_call` 一层，业务不动（即 M6） |
| G3 | **账号→节点注册表** | 维护 `channel_account_id → gateway_node → 连接状态` 映射（PG/Redis）；分片的核心 |
| G4 | **显式分配分片** | 微信会话不可 rehash（迁节点=重新扫码），故用显式分配表而非一致性哈希；新账号绑定时挑"未满"节点写入 |
| G5 | **连接健康监控 + 重登流程** | gateway 的"HA"靠监控 + 重连 + 重扫，而非自动 failover（见 §5.4） |

### 5.3 路由模型：入站不路由，出站查表路由

```text
                 ┌───────────────── 无状态 Backend 集群 ─────────────────┐
   入站(任意Backend)│  API / Worker (PG + Redis 共享态)                    │
        ▲          └───────┬───────────────────────────────────┬─────────┘
        │ POST turn        │ 出站: 查 channel_account_id→node    │
        │                  ▼                                    ▼
 ┌──────┴───────┐   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
 │ Gateway 节点A │   │ Gateway 节点A │  │ Gateway 节点B │  │ Gateway 节点C │
 │ 微信号 1..N   │   │ (send 给1..N) │  │ 微信号 N+1.. │  │ 微信号 ...    │
 └──────────────┘   └──────────────┘  └──────────────┘  └──────────────┘
        每个 gateway 节点 = 一组登录态微信账号(物理绑定，不可随意迁移)
```

- **入站无需路由**：gateway 节点收到微信消息时本就知道是哪个账号，直接 POST 给任意无状态 Backend（带 `channel_account_id`）。
- **出站需查表路由**：Backend 发消息前先查注册表定位 gateway 节点，再把 `send` 打过去。**这就是"按用户分流"的唯一发生点——只在出站找 gateway，不在 Backend 处理层。**

注册表 schema 草案：
```text
gateway_account_routes
  channel_account_id   TEXT PRIMARY KEY   -- 微信账号(provider 侧)
  gateway_node_id      TEXT NOT NULL      -- 物理 gateway 节点
  gateway_endpoint     TEXT NOT NULL      -- 出站 RPC 地址
  status               TEXT               -- connected / disconnected / relogin_required
  last_heartbeat_at    TEXT
  assigned_at          TEXT
  metadata_json        TEXT
索引: (gateway_node_id, status)
```

### 5.4 关键且反直觉：Gateway 的 HA 不等于无状态 HA

**没法自动 failover 一个微信会话**：

- gateway 节点挂 → 其上微信号**全部掉线**。
- 不能"自动迁到另一台"——个人微信要**重新扫码**才能在新节点登录，而扫码需**真人**操作。
- 你们已有的 `web.login.start/wait` + `binding_intents` 表 + 二维码流程，本质就是 gateway 的恢复路径。

因此 gateway 这层要的是**运维面板 + 监控**（即 L5），不是自动 HA：

- 每账号连接健康（connected / 掉线 / 待重登）。
- 掉线自动重连；重连失败 → 触发重扫 → 通知用户/运营。
- **单节点别挂太多账号**（控制爆炸半径），宁可多节点、每节点少账号。

### 5.5 同步回复 → 决定 Backend 能否队列化

现状入站同步（§5.1 事实 2），对 gateway 独立化有连带影响：

- **保持同步**：bridge POST 要挂着等 reply，Backend 即便内部用 worker 也得"同步等结果再返回"，超时/背压受 bridge HTTP 超时约束。
- **改异步**：Backend 收到即返回 202，worker 处理完用出站 `send` RPC 主动把回复推回去（出站通道已存在，主动消息就是这么发的），回复延迟与 HTTP 请求解耦，Backend 才能真正队列化（L1）。

**出站 `send` 已可用，异步回复技术上通；要确认的是 bridge 插件能否接受"先不同步回复、稍后用 send 补发"模式**（§9 第 1 条）。

### 5.6 压在最前面的硬约束

> 一个 openclaw-weixin / OpenClaw 实例能稳定维持多少个登录态个人微信账号？

- 若"一账号一用户"，2 万 DAU ≈ 2 万个登录态微信号，个人微信不可能这么堆。
- 这要么逼你改形态（公众号/服务号/企业微信，§9 第 4 条），要么靠"单节点账号数 × 节点数"覆盖，且每号有发送频率/风控上限。

**这个数没量出来之前，gateway 容量与节点数都是猜测——它是 2 万规模真正的硬约束，优先级高于 Backend 部署。**

### 5.7 上线 checklist

- [ ] 量出单 gateway 节点稳定承载的微信账号数（§5.6 硬约束，最高优先级）。
- [ ] 确认 bridge 插件是否支持异步回复（§5.5）。
- [ ] Gateway 与 Backend 物理拆分部署（G1）。
- [ ] 出站从 subprocess 改网络 RPC（G2 / M6）。
- [ ] 建 `gateway_account_routes` 注册表 + 出站查表路由（G3）。
- [ ] 新账号绑定时按"未满节点"显式分配（G4）。
- [ ] 每账号连接健康监控 + 掉线重连/重扫运维面板（G5 / L5）。
- [ ] 出站按账号/通道限速，避免触发微信风控。

---

## 6. 架构改进建议

> 拆成**短期**（不改业务逻辑、最小改动、当前阶段就能做）和**中长期**（结构性演进、需要排期与迁移）。短期项是中长期演进的前置铺垫，不是临时补丁。

### 6.1 短期建议（阶段 0–1，单机/主备，最小改动）

目标：**在不改业务逻辑的前提下，把单机稳定性和单进程吞吐拉满，并补齐度量**。

| # | 改造项 | 改动范围 | 收益 | 风险 |
|---|---|---|---|---|
| S1 | SQLite 开 `WAL` + `busy_timeout=5000` | `db.py` connect() 加两行 PRAGMA | 读写不再互相全阻塞，撞锁改为等待而非报错 | 极低 |
| S2 | LLM 端点 + client async 化 | `/openclaw/turn` 改 `async def`，`llm.py` 换 `httpx.AsyncClient` 并复用单例 | 单进程并发从 ~40 → 数百，机器数降一个数量级 | 中（需回归 turn 链路与工具调用） |
| S3 | 全链路埋点 | turn latency、LLM latency/p95、token 估算、每账号每日 turn、峰值小时分布、`database is locked` 计数 | 让后续容量估算建立在真实数据上 | 低 |
| S4 | commitment 抽取预筛/采样 | 在触发前加规则过滤或低频采样 | LLM 调用量不翻倍 | 低 |
| S5 | prompt token 预算与裁剪 | `prompt_builder.py` 加最近上下文裁剪 | 直接压低 token 成本与尾延迟 | 低 |
| S6 | 健康检查与 scheduler heartbeat 报警 | health/ready + heartbeat lag 告警 | 单机故障可观测、可报警 | 低 |
| S7 | 一次性 synthetic 压测 | 50/100/300 RPS 脚本 | 实测 SQLite 与同步链路真实瓶颈，验证 S1/S2 收益 | 低 |

**S1、S2 是性价比最高的两项**：S1 用几行换来单机抗压能力，S2 解开整份容量估算里最大的变量。两者都不触碰业务逻辑。

### 6.2 中长期建议（阶段 2–3，横向扩容与大规模生产）

目标：**从单机演进为无状态多副本 + 队列 + 分片的生产架构**。按依赖顺序排列。

**阶段 2（万 DAU 前）— 打好横向扩容地基**

| # | 改造项 | 说明 |
|---|---|---|
| M1 | **SQLite → PostgreSQL** | RDS PG + PgBouncer；messages 按时间分区，预留 hash/account 二级分区；核心索引：`(status, due_at)`、`(account_id, created_at)`、`(account_id, date)`；治理 daily_usage/wallet 热点更新 |
| M2 | **进程内限流 → Redis** | 按 `account_id` 做全局窗口限流：`rate:rpm:{account_id}`、`rate:daily:{account_id}:{date}` |
| M3 | **本地账号文件 → 共享存储/结构化表** | context、daily memory 迁 PG 或 OSS，配应用内/Redis 缓存 |
| M4 | **API 多副本 + 同账号串行锁** | API 无状态化；同账号 turn 用 Redis 锁 `turn_lock:{account_id}` 串行 |
| M5 | **scheduler leader election** | 去掉"不可多实例"限制：leader 选举 + 原子 claim + heartbeat/lag 可观测 |
| M6 | **outbound worker 化** | 从 `subprocess.run` 改为直连 OpenClaw Gateway 的 RPC/HTTP，或独立 outbound worker 池 + 通道级限速 |

**阶段 3（十万 DAU 前）— 大规模生产架构**

| # | 改造项 | 说明 |
|---|---|---|
| L1 | **chat turn 队列化** | API 只做接收/幂等/限流/轻量入库，LLM 处理交给 chat worker；队列按 `account_id` 分区保证顺序 |
| L2 | **队列分类 + DLQ** | `chat_turn` / `memory_write` / `commitment_extract` / `dreaming` / `proactive_scan` / `outbound_send` / `retry` / `dead_letter`，各自独立 batch size 与限速 |
| L3 | **PostgreSQL 分区与归档** | messages 冷热分层；30 天热数据预估 180–300 GB（含索引/debug/cost），尽早设计归档 |
| L4 | **LLM 多路由 + 熔断 + 降级** | 多 provider 路由、超时熔断、按场景选模型、高峰降级到更快/更便宜模型 |
| L5 | **OpenClaw Gateway 分片** | 按微信账号/`channel_account_id` 分片，每节点管一组账号；掉线重连/二维码重登运维面板（详见 §5 专章） |
| L6 | **故障演练与容量预案** | 大规模压测、队列 lag 与 DLQ 治理、降级开关演练 |

### 6.3 降级策略（高峰/故障时按优先级生效）

1. 降级到更快/更便宜模型；2. 缩短上下文窗口；3. 暂停 commitment 抽取；4. 暂停 content invitation / reactivation；5. **保留用户明确设置的 reminder**；6. LLM 超时返回短 fallback；7. 低优先级 worker 限速。

---

## 7. 机器与资源估算（基准场景，前提：已完成 S2 async 化）

> 强调前提：以下 API 进程数假设已 async 化。若仍是同步链路，API 层需按 §2.1 的 17–33+ 进程估。

| 组件 | 起步规格 | 说明 |
|---|---|---|
| API 服务 | 6–10 台 8C16G，或 K8s 20–40 个 2C4G pod | async + 连接池 + LLM 并发受控为前提 |
| Chat Worker | 8–16 台 8C16G | 瓶颈通常是 LLM 并发/p95、prompt 构造、队列 lag，而非 CPU |
| PostgreSQL | RDS 16C64G + PgBouncer | 自动备份、慢查询日志，按 messages 增长预留存储 |
| Redis | 三节点高可用，单节点 4C8G 起 | 承接队列时按积压扩容，设内存水位报警 |
| Scheduler/Outbound Worker | 3–6 台 4C8G | 提醒/主动消息高峰需数千条/分钟，按通道限速 |
| OpenClaw Gateway | **独立容量池** | 按微信账号/channel 分片，不与 API 混算；节点数取决于 §5.6 硬约束，详见 §5 专章 |

存储粗估：100 万 turn/day = 200 万 messages/day；单条含 raw_json 约 2 KB → ~4 GB/day，加索引/debug/cost/tool 约 6–10 GB/day；30 天热数据 180–300 GB。

---

## 8. 稳定性必备观测指标

- **用户链路**：inbound turn total、success rate、latency p50/p95/p99、duplicate rate、rate-limited total、per-account error rate。
- **LLM**：call/error/timeout total、latency p50/p95/p99、model/provider 成功率、token usage、queue lag。
- **DB**：QPS/TPS、连接池使用率、slow query、lock wait、`database is locked` 计数、副本延迟、表/索引大小。
- **Scheduler/Worker**：heartbeat、queue depth/lag、claim 成功率、retry、dead letter count。
- **OpenClaw**：channel connected、send success/error、gateway latency、per-account send rate、dropped/failed。
- **资源**：CPU/mem/disk/network/process-up。

---

## 9. 必须先拉通的前提问题（会颠覆容量模型）

按影响排序，前三条会直接改变架构方案空间：

1. **微信侧是否允许异步回复，还是必须同步 synthetic reply？** —— 若必须同步，则 §6 的"chat 队列化 + worker 异步处理"方案空间被大幅压缩，需要改为"API 同步等 worker 结果"模式，重新设计超时与背压（详见 §5.5）。
2. **OpenClaw 单账号 / 单 gateway 真实承载多少用户、单账号发送频率上限？** —— 决定 §6.2 L5 的分片粒度与 gateway 节点数（详见 §5.6，这是 2 万规模的硬约束）。
3. **目标 LLM 模型、价格、TPM/RPM 配额？** —— 决定 §3.3 的 660 并发能否落地，以及成本量级。
4. 10 万 DAU 是否仍坚持个人微信 bot 形态，还是迁公众号/服务号/企业微信？
5. 目标人均日消息数？高峰是否接受降级回复？
6. 主动消息策略在 10 万 DAU 下是否仍全量开启？
7. 聊天历史热数据保留多久、冷数据如何归档？
8. Admin/debug 明文访问在规模化后的合规与审计要求？

---

## 10. 推荐的执行顺序

1. **先做 S1 + S3 + S7**：开 WAL/busy_timeout，补埋点，跑一次压测——用真实数据替换本文的所有粗估。
2. **再做 S2**：async 化，解开单进程并发天花板，这是机器数估算里最大的变量。
3. **并行拉通 §9 的前 3 个前提问题**——它们决定中长期方案走向，越早越好；其中 gateway 承载数（§5.6）优先级最高。
4. 数据与前提清晰后，按 §6.2 阶段 2 → 阶段 3 推进结构性改造。

近期最值得投入的不是直接上机器，而是 **S1/S2/S3 这三项低成本改造 + 真实度量 + 前提对齐**。
