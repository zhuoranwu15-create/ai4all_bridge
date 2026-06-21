# 10 万 DAU 架构支撑讨论稿

更新时间：2026-06-07

状态：临时讨论稿，用于后续方案对齐；不是最终技术设计。

## 1. 结论

当前架构不能直接支撑 10 万 DAU。它适合单机或少量 ECS 外测：FastAPI + SQLite + 本地账号文件 + 独立 scheduler。

要支撑 10 万 DAU，需要先演进为：

- 无状态 API 服务。
- PostgreSQL 作为主业务数据库。
- Redis 作为全局限流、幂等、锁和短期状态层。
- 队列和 worker 承接 LLM、memory、commitment、scheduler、outbound retry 等异步任务。
- OpenClaw / 微信通道按账号或 channel 分片。

10 万 DAU 时，FastAPI 本身不是最大瓶颈。主要瓶颈会是：

- LLM 配额、吞吐、并发和尾延迟。
- 微信 / OpenClaw 通道容量和风控。
- SQLite 写锁与单机数据库能力。
- 本地账号文件无法跨机器共享。
- 同账号消息顺序一致性。
- scheduler 和 outbound 的单进程扫描/发送能力。

## 2. 当前架构瓶颈

### 2.1 同步 turn 链路过长

`/openclaw/turn` 当前同步调用 `handle_openclaw_turn()`。一轮消息会同步完成：

1. 身份解析。
2. account/session 获取或创建。
3. channel binding upsert。
4. 账号文件和上下文读取。
5. prompt 构造。
6. LLM 调用。
7. assistant reply 落库。
8. usage / cost 记录。
9. onboarding 状态推进。
10. after-turn memory / commitment 后台调度。

这意味着用户请求线程会长期等待 LLM。只要 LLM p95 上升，API 并发占用就会快速放大。

### 2.2 SQLite 不支持多实例横向写入

当前 `app/db.py` 每次打开 SQLite 连接并提交。SQLite 适合单机、小规模外测，不适合作为 10 万 DAU 的多实例共享业务数据库。

已有部署文档也明确要求不要多个实例同时写同一个 SQLite 文件。

主要风险：

- 写锁竞争。
- 单文件损坏风险。
- 无法跨机器共享。
- 缺少连接池、读写分离、分区、慢查询治理等生产能力。

### 2.3 进程内限流不能横向扩容

当前 RPM 限流是进程内内存结构。多台 API 实例后，每台机器都会独立计算限流，无法保证账号级全局限流。

需要迁移到 Redis，并按 `account_id` 做全局窗口限流。

### 2.4 本地账号上下文文件不能天然跨机器共享

账号 context、daily memory 等当前写入本地 `data/user_profiles`。多实例部署后会遇到：

- 不同机器读写不同副本。
- 文件同步延迟。
- 并发 append 冲突。
- 备份和恢复复杂。

中长期应迁到 PostgreSQL / OSS / 共享存储，并配合 Redis 或应用内缓存。

### 2.5 Scheduler 当前不能多实例运行

proactive scheduler 目前没有跨进程 leader election。文档要求不要同时运行多个 proactive scheduler，也不要在多 worker FastAPI 内启用 in-process scheduler。

10 万 DAU 下，需要：

- scheduler leader election 或任务分片。
- 原子 claim。
- 分队列 worker。
- 明确的 heartbeat、lag、重试和死信队列。

### 2.6 OpenClaw outbound 通过 CLI subprocess

当前 outbound 调用通过 `openclaw gateway call` 子进程执行。高 QPS 下会有明显开销：

- 进程启动成本。
- stdout/stderr JSON 解析成本。
- 并发控制困难。
- 发送失败重试与通道级限速困难。

大规模时应考虑直接 RPC/HTTP 接入 OpenClaw Gateway，或为 outbound 单独做 worker 池和通道限速。

## 3. 真实访问压力估算

以下为粗估，用于容量讨论。实际上线前必须用真实数据校准。

### 3.1 入站 turn 数

| 场景 | 每人每日入站 | 日入站 turn | 平均 RPS | 峰值小时占 20% | 瞬时峰值约 2x |
| --- | ---: | ---: | ---: | ---: | ---: |
| 轻量 | 5 | 50 万 | 5.8 | 27.8 RPS | 55 RPS |
| 基准 | 10 | 100 万 | 11.6 | 55.6 RPS | 110 RPS |
| 重度 | 30 | 300 万 | 34.7 | 166.7 RPS | 330 RPS |

10 万 DAU 并不等于 10 万 QPS。真实压力主要来自峰值集中、LLM 等待时间和上下文 token 规模。

### 3.2 LLM token 压力

假设一轮平均：

- input tokens：3,000
- output tokens：300
- total：3,300 tokens/turn

基准场景 100 万 turn/day：

- 约 33 亿 tokens/day。
- 峰值 110 RPS 时约 36 万 tokens/s。
- 约 2,178 万 tokens/min。

如果上下文膨胀到 5,000 tokens/turn，峰值会接近 3,300 万 tokens/min。

这意味着需要提前确认 LLM provider 的：

- TPM 配额。
- RPM 配额。
- 并发连接限制。
- 失败重试策略。
- 高峰价格和成本。
- 降级模型策略。

### 3.3 LLM 并发

并发估算公式：

```text
LLM 并发 = 峰值 RPS * LLM p95 latency seconds
```

假设 LLM p95 为 6 秒：

| 场景 | 瞬时峰值 RPS | LLM p95 | 需要 LLM 并发 |
| --- | ---: | ---: | ---: |
| 轻量 | 55 | 6s | 330 |
| 基准 | 110 | 6s | 660 |
| 重度 | 330 | 6s | 1,980 |

如果启用 hidden commitment extraction，并且每轮都额外打一轮 LLM，LLM 调用量可能接近翻倍。因此 commitment extraction 需要异步队列、规则预筛、采样或低频触发。

### 3.4 数据库写入压力

每个正常 turn 至少会产生：

- inbound message 1 条。
- outbound assistant message 1 条。
- daily_usage 更新 1 次。
- session turn_count 更新 1 次。
- cost_events / ledger 可能各 1 条。
- tool_invocations / search_provider_runs 视工具调用而定。
- memory 文件或未来 memory 表写入。

基准 110 RPS 峰值时，数据库写操作可能是 500-1,000 writes/s 级别。重度场景可能达到 1,500-3,000 writes/s。

SQLite 不适合这个量级。PostgreSQL 需要：

- 连接池。
- 合理索引。
- messages 分区或归档。
- 热点更新治理，例如 daily_usage、wallet。
- 慢查询监控。

## 4. 目标架构

```text
WeChat / OpenClaw
      |
      v
Load Balancer / Ingress
      |
      v
Stateless FastAPI API Pods
      |
      +--> Redis: auth cache / rate limit / idempotency / locks
      |
      +--> PostgreSQL: source of truth
      |
      +--> Queue: chat jobs / memory jobs / proactive jobs / outbound jobs
                |
                v
          Worker Pools
                |
                +--> LLM Providers
                +--> OpenClaw Gateway Cluster
                +--> Object Storage / shared context storage
```

### 4.1 API 服务

API 服务应尽量无状态：

- 校验 Authorization。
- 做幂等判断。
- 做全局限流。
- 轻量入库。
- 将长耗时任务交给 worker。

对于微信用户体验，普通聊天仍需要尽量同步返回。但内部可以把工作拆成：

- API 接收请求。
- 同账号串行锁。
- chat worker 处理 LLM。
- API 等待 worker 结果，或由 bridge 支持异步回调。

如果 OpenClaw bridge 必须同步返回，则至少要把 FastAPI 和 LLM client 做 async 化，并控制并发。

### 4.2 PostgreSQL

替代 SQLite，承接：

- accounts
- sessions
- messages
- channel_bindings
- outbound_messages
- reminders
- proactive_commitments
- content_invitations
- cost_events
- entitlement_wallets / ledger
- debug traces
- scheduler heartbeats

建议：

- 使用 RDS PostgreSQL。
- 使用 PgBouncer。
- messages 按时间分区，必要时再按 hash/account 分区。
- 所有查询必须保留 `account_id` 过滤。
- 对 `status, due_at`、`account_id, created_at/id`、`account_id, date` 建核心索引。

### 4.3 Redis

Redis 用于：

- 全局 RPM 限流。
- 幂等缓存。
- 同账号 turn 串行锁。
- scheduler leader election。
- worker lease。
- 热点配置缓存。
- 队列或队列辅助。

限流 key 示例：

```text
rate:rpm:{account_id}
rate:daily:{account_id}:{date}
turn_lock:{account_id}
dedupe:message:{account_id}:{message_id}
```

### 4.4 队列和 Worker

建议拆分队列：

- `chat_turn`: 普通聊天 LLM。
- `memory_write`: daily notes / memory event。
- `commitment_extract`: hidden commitment 抽取。
- `dreaming`: 记忆压缩。
- `proactive_scan`: 账号主动检查。
- `outbound_send`: 微信发送。
- `retry`: 失败重试。
- `dead_letter`: 死信。

同账号聊天需要保证顺序，可以采用：

- Redis lock by `account_id`。
- Kafka partition by `account_id`。
- worker 内按 `account_id` 串行处理。

### 4.5 Scheduler

大规模 scheduler 不应是单进程每 30 秒扫 20 条。

需要：

- leader election。
- 多 worker 分片扫描。
- 原子 claim。
- 可观测 lag。
- 任务类型分离。
- 每类任务独立 batch size 和限速。

示例：

- reminder due scan：高优先级。
- commitment due scan：中优先级。
- reactivation / content invitation：低优先级。
- dreaming：离线低优先级。

### 4.6 OpenClaw / 微信通道

10 万 DAU 时，微信通道本身是核心风险。

需要单独评估：

- 单个微信账号可承载用户数。
- 单账号发送频率限制。
- 多账号池分片。
- 掉线重连策略。
- 账号风控。
- 是否迁移到公众号、服务号、企业微信或其他官方能力。

当前个人微信 bot 形态不能默认假设能服务 10 万 DAU。

## 5. 机器和资源估算

以下按基准场景估算：

- 10 万 DAU。
- 每人每日 10 条入站。
- 100 万 turn/day。
- 峰值 110 RPS。
- LLM p95 6 秒。

### 5.1 API 服务

建议起步：

- 6-10 台 8C16G ECS。
- 或 K8s 20-40 个 2C4G pod。

前提：

- API 无状态。
- 使用 async HTTP client。
- DB/Redis 有连接池。
- LLM 并发受控。

### 5.2 Chat Worker

建议起步：

- 8-16 台 8C16G。

实际瓶颈通常不是 CPU，而是：

- LLM provider 并发。
- LLM p95。
- prompt 构造耗时。
- DB 连接池。
- 队列 lag。

### 5.3 PostgreSQL

建议起步：

- RDS PostgreSQL 16C64G。
- PgBouncer。
- 自动备份。
- 慢查询日志。
- 存储按 messages 增长预留。

粗略存储估算：

- 100 万 turn/day = 200 万 messages/day。
- 如果单条 message 连 raw_json 平均 2 KB，则约 4 GB/day。
- 加索引、debug、cost、tool 等，可能 6-10 GB/day。
- 30 天热数据可能 180-300 GB。

需要尽早设计归档和冷热分层。

### 5.4 Redis

建议起步：

- 三节点高可用。
- 单节点 4C8G 起步。

如果 Redis 承接大量队列，需要按队列积压量扩容，并设置内存水位报警。

### 5.5 Scheduler / Outbound Worker

建议起步：

- 3-6 台 4C8G。

提醒和主动消息高峰需要能处理数千条/分钟，并且要按通道限速。

### 5.6 OpenClaw Gateway

需要独立容量池，不能和 API 服务混在一个容量估算里。

建议：

- 按微信账号或 channel_account_id 分片。
- 每个 gateway 节点管理一组微信账号。
- outbound 队列按 channel 分片限速。
- 对掉线、发送失败、二维码重登建立专门运维面板。

## 6. 稳定性保障

### 6.1 必须观测的指标

用户链路：

- inbound turn total。
- turn success rate。
- turn latency p50/p95/p99。
- duplicate rate。
- rate limited total。
- per-account error rate。

LLM：

- llm call total。
- llm error total。
- llm latency p50/p95/p99。
- llm timeout total。
- model/provider 维度成功率。
- token usage。
- queue lag。

DB：

- QPS / TPS。
- connection pool usage。
- slow query。
- lock wait。
- replication lag，如果有只读副本。
- table/index size。

Scheduler / Worker：

- heartbeat。
- queue depth。
- queue lag。
- claim success/fail。
- retry count。
- dead letter count。

OpenClaw：

- channel connected。
- outbound send success/error。
- gateway latency。
- per-account send rate。
- dropped / failed messages。

资源：

- CPU。
- memory。
- disk。
- network。
- process up。

### 6.2 幂等和一致性

必须坚持：

- 入站 message 以 `account_id + message_id` 幂等。
- outbound 以 `idempotency_key` 幂等。
- scheduler 任务用原子 claim。
- 同账号聊天 turn 串行。
- 所有查询和写入必须带 `account_id` 约束。

### 6.3 降级策略

高峰或故障时：

- 降级到更便宜/更快模型。
- 缩短上下文窗口。
- 暂停 commitment extraction。
- 暂停 content invitation / reactivation。
- 保留用户明确 reminder。
- LLM timeout 后返回短 fallback。
- 对低优先级 worker 限速。

### 6.4 成本控制

10 万 DAU 的主要成本是 LLM tokens。

需要：

- prompt token 预算。
- 最近上下文裁剪。
- memory 检索而不是全量注入。
- 按场景选择模型。
- 对后台 LLM 任务采样或延迟。
- per-account daily token budget。
- provider 费用监控。

## 7. 阶段路线

### 阶段 0：当前小规模外测

目标：稳定单机。

- 保持 SQLite。
- 保持独立 proactive scheduler。
- 完善 health、ready、scheduler heartbeat。
- 做真实用户链路日志和报警。
- 不做多实例写 SQLite。

### 阶段 1：千 DAU

目标：验证产品和真实行为。

- 继续单机或主备。
- 增加压测脚本。
- 统计真实每人每日 turn、峰值小时、LLM latency、token。
- 优化 prompt token。
- 对 commitment extraction 做预筛或采样。

### 阶段 2：万 DAU 前

目标：完成横向扩容基础。

- SQLite 迁 PostgreSQL。
- 进程内限流迁 Redis。
- context 文件迁共享存储或结构化表。
- API 多副本。
- scheduler 加 leader election。
- outbound worker 化。
- 完整指标和报警。

### 阶段 3：十万 DAU 前

目标：大规模生产架构。

- chat turn worker 队列化。
- 同账号分区串行。
- PostgreSQL 分区和归档。
- LLM provider 多路由和熔断。
- OpenClaw gateway 分片。
- 大规模压测。
- 队列 lag 和 DLQ 治理。
- 故障演练和容量预案。

## 8. 待确认问题

1. 10 万 DAU 是否仍坚持个人微信 bot 形态，还是会迁移到公众号/服务号/企业微信。
2. OpenClaw 单账号和单 gateway 的真实承载能力是多少。
3. 微信侧是否允许异步回复，还是必须同步 synthetic reply。
4. 目标平均每人每日消息数是多少。
5. 目标 LLM 模型、价格、TPM/RPM 配额是多少。
6. 是否接受高峰降级回复。
7. 用户主动消息策略在 10 万 DAU 下是否仍开启。
8. 聊天历史热数据保留多久，冷数据如何归档。
9. Admin/debug 明文访问和隐私审计在规模化后的合规要求。

## 9. 优先级建议

近期最值得先做的不是直接上机器，而是补充真实度量：

1. 记录 turn latency、LLM latency、token 估算、每账号每日 turn。
2. 统计真实峰值小时分布。
3. 跑一次 50/100/300 RPS 的 synthetic 压测，验证 SQLite 和 LLM 同步链路瓶颈。
4. 设计 PostgreSQL schema migration 方案。
5. 设计 Redis 全局限流和同账号串行锁。
6. 明确 OpenClaw / 微信通道容量边界。

