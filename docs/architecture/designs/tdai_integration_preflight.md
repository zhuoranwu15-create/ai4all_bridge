# TDAI 接入前确认与准备清单

> 状态：接入前确认稿  
> 日期：2026-06-28  
> 适用范围：AI4ALL 微信 Bot 接入本地 fork 的 TDAI Gateway 多租户版本。  
> 关联文档：`docs/architecture/designs/tdai_multitenant_design.md`、`/Users/suchong/workspace/TencentDB-Agent-Memory/docs/tdai_gateway_integration.md`

## 1. 已确认的实施边界

### 1.1 第一阶段范围

第一阶段只做三件事：

1. turn 前被动 `/recall`。
2. turn 后 after-turn `/capture`。
3. 解绑/清除账号时联动 `/namespace/wipe`。

第一阶段暂不接模型主动记忆搜索工具：

- 不接 `tdai_memory_search`。
- 不接 `tdai_conversation_search`。
- 不改 tool registry / tool handler / 每轮工具调用计数。

原因：被动 recall + capture 是最小闭环，能先验证记忆质量和稳定性；主动工具会触及工具注册、handler、TurnContext 状态、调用次数限制和更多测试，放到被动链路稳定后再做。

### 1.2 灰度策略

采用：

- `capture`：全量打开。
- `recall`：只对测试账号 allowlist 打开。

这里的“全量 capture”只覆盖 onboarding 完成后的正常对话轮次。这样可以让 TDAI 在后台先积累 L0/L1/L2/L3，后续扩大 recall 灰度时已有可召回材料。

建议 AI4ALL 配置：

```env
TDAI_ENABLED=false
TDAI_GATEWAY_URL=http://127.0.0.1:8420
TDAI_GATEWAY_API_KEY=
TDAI_CAPTURE_ENABLED=true
TDAI_RECALL_ENABLED=true
TDAI_RECALL_ACCOUNT_ALLOWLIST=aid_956326343,aid_806382741   # 逗号分隔 account_id；空=recall 对所有账号关闭（生产灰度名单）
TDAI_RECALL_TIMEOUT_SECONDS=0.5               # 热路径严格超时（秒），超时降级不阻回复；实测见下
TDAI_CAPTURE_TIMEOUT_SECONDS=2.0
TDAI_RECALL_MAX_CHARS=2500
```

`TDAI_ENABLED=false` 为默认值；生产启用时显式改为 `true`。

注意：`TDAI_RECALL_ACCOUNT_ALLOWLIST` 只控制 recall，capture 不受此控制（全量）。因此配置名不是 `TDAI_ACCOUNT_ALLOWLIST`。

recall 超时实测（本地开发机，DashScope embedding + hybrid 检索）：warm 召回 avg ~170 ms、max ~313 ms，约 30% 请求 >200 ms。初版 0.2s 会让约三成 warm 召回被误降级，故放宽到 0.5s（覆盖 warm 长尾 + 余量，热路径同步等待仍可接受）。账号首次 recall 会触发 persona 现场编译（DeepSeek 调用，秒级），无论超时多少都会降级，warm 后恢复——属预期冷命中（见 §3.2）。

allowlist 里的 `account_id` 必须用运行时真实主键：经 binding 解析的真实微信账号是 DB 短 id（如 `aid_806382741`），而 `send_mock_turn.py --sender X` 在无 binding 时 account_id 回落为 `openclaw-weixin:local:X`，两者是**不同账号**。本地用 mock 脚本验证 recall 时，allowlist 必须配 `openclaw-weixin:local:X` 形态，否则永远不命中。

### 1.3 Onboarding 期间关闭 TDAI

Onboarding 期间 TDAI 完全关闭：

- 不调用 `/recall`。
- 不调用 `/capture`。

原因：onboarding 的目标是采集用户称呼、AI 名字、Soul/persona 等基础设定。旧记忆召回或未确认材料不应影响这个流程；onboarding 完成后再开始 capture 和 recall。

### 1.4 Capture 边界

第一阶段 capture 只记录最终用户可见且业务确认有效的正常对话：

会 capture：

- 普通文本轮次。
- 工具调用后的最终可见回复。
- 图片理解成功后的文本描述轮次，`user_content` 使用已经写入 messages 的描述文本。

不会 capture：

- 入站审核拦截的用户消息。
- 出站同步红线拦截后的安全兜底回复。
- LLM 生成失败兜底回复。
- `#重置会话`、`#状态` 等特殊命令。
- 图片理解失败或未开启时的“没看清”兜底。
- onboarding 期间的任何轮次。
- TDAI recall context、system prompt、tool result、debug trace。

capture 的 payload 使用：

```json
{
  "session_key": "ai4all:<account_id>",
  "session_id": "<AI4ALL sessions.id>",
  "user_content": "<用户可见文本>",
  "assistant_content": "<最终发送给用户的回复>"
}
```

`session_key` 只能由 AI4ALL account_id 构造，不能使用 OpenClaw 原始 `session_key`、`channel_account_id` 或 sender/chat id。

### 1.5 Wipe / Unbind 语义

确认：用户解绑账号时，清除 TDAI 里的该账号记忆。

具体语义：

- `keep_memories=false`：AI4ALL DB/profile 记忆清除；TDAI namespace 也清除。
- `keep_memories=true`：AI4ALL 现有 DB/profile 记忆按当前逻辑保留；TDAI 作为派生缓存（派生自 AI4ALL `messages` 表），仍调用 `/namespace/wipe` 清除。

TDAI wipe 即使在 `keep_memories=true` 场景下也安全，原因：TDAI 是 AI4ALL `messages` 表的派生缓存，权威数据仍在中心 PostgreSQL；wipe 后可通过重新 capture 或未来的 per-account seed 脚本从 `messages` 重建，不存在数据单点风险。

实现上不要把 TDAI HTTP 调用放进 AI4ALL DB 事务里。建议 DB 事务提交后再 best-effort 调用 `/namespace/wipe`；失败要记录日志/告警，并保留可重试入口。

已确认实现：`web.py` 中解绑的两个分支（`keep_memories=true` 和 `keep_memories=false`）均在 DB 事务提交后派发 `namespace_wipe` 到 `background_loop`。

### 1.6 Seed 是什么，为什么 HTTP `/seed` 不能直接用

Seed 指“历史记忆回灌”：把 AI4ALL 现有 `messages` 表里的历史对话导入 TDAI，让 TDAI 不必只从接入后的新消息开始积累。

例子：账号 `aid_806382741` 过去已经聊了几千条。不开 seed 的话，TDAI 只能记住接入之后的新对话；做 seed 的话，可以把历史 user/assistant 对话批量喂给 TDAI，生成 L0/L1/L2/L3。

TDAI 文档说多租户下 HTTP `/seed` 不可用，是因为当前 Gateway 的多租户隔离是结构式：

```text
TDAI_DATA_DIR/
  ai4all_alice.<hash>/
    memory.sqlite
    persona.md
  ai4all_bob.<hash>/
    memory.sqlite
    persona.md
```

普通 `/capture`、`/recall` 会根据 `session_key` 路由到某个账号自己的目录。但 HTTP `/seed` 是旧的单租户/快照导入接口，它写到共享的 `baseDir/seed-<timestamp>/`，不是 `session_key` 对应的账号目录；因此导入成功后，正常 `/recall` 也看不到这些数据。

当前 fork 已在多租户模式下拒绝 HTTP `/seed`，避免“导入成功但召回不可见”的误用。

第一阶段结论：

- 不做历史 seed。
- 先从新 turn 的全量 capture 开始积累。
- 后续如需历史回灌，新增 AI4ALL 脚本 `scripts/seed_tdai_memory.py`：按账号从中心 PG/SQLite 读取 `messages`，再直接导入到该账号的 TDAI per-account dataDir，而不是调用 HTTP `/seed`。

### 1.7 两台节点部署

确认：两台节点各自运行一个 TDAI sidecar。

拓扑：

```text
节点 A: AI4ALL app + TDAI Gateway + 本机 TDAI_DATA_DIR
节点 B: AI4ALL app + TDAI Gateway + 本机 TDAI_DATA_DIR
```

约束：

- Gateway 绑定 `127.0.0.1:8420`。
- AI4ALL 只调用同机 `http://127.0.0.1:8420`。
- 每台机器的 `TDAI_DATA_DIR` 独立。
- 禁止两个 TDAI Gateway 进程共享同一个 dataDir。
- 用户仍按现有微信节点绑定处理；用户在哪台节点服务，TDAI 记忆就落在哪台节点。

### 1.8 隐私确认

确认接受第一阶段隐私边界：

- TDAI extraction/persona LLM 固定使用 DeepSeek。
- TDAI embedding 使用 DashScope `text-embedding-v3`。
- 被 capture 的记忆文本会发往上述第三方服务。

AI4ALL 侧仍保留 capture 前的保守边界：审核拦截、红线替换、生成失败、特殊命令和 onboarding 均不进入 TDAI。

### 1.9 TDAI LLM 配置

确认：TDAI 固定使用 DeepSeek，不跟随 AI4ALL runtime active provider 动态切换。

建议 TDAI `.env`：

```env
TDAI_MULTI_TENANT=true
TDAI_DATA_DIR=/var/lib/ai4all/tdai
TDAI_GATEWAY_HOST=127.0.0.1
TDAI_GATEWAY_PORT=8420
TDAI_GATEWAY_API_KEY=<long-random-secret>
TDAI_LLM_BASE_URL=https://api.deepseek.com/v1
TDAI_LLM_API_KEY=<deepseek-key>
TDAI_LLM_MODEL=deepseek-chat
DASHSCOPE_API_KEY=<dashscope-key>
```

`tdai-gateway.yaml` 只放 env 不能覆盖的 embedding 配置，使用 DashScope `text-embedding-v3` 和 `memory.recall.strategy: hybrid`。

## 2. 成本与容量参数

### 2.1 参数解释

#### `TDAI_MAX_CONCURRENT_EXTRACTIONS`

含义：限制一个 Gateway 内所有账号后台 L1/L2/L3 提纯任务的总并发。

它限制的是后台 extraction，不是 `/recall` 请求并发，也不是 `/capture` 的 L0 写入。数值越高，记忆从 L0 变成 L1/L2/L3 越快，但 DeepSeek 并发、token 成本和失败风险也越高。

观察指标：

- `/health.extraction.active`
- `/health.extraction.waiting`
- 记忆形成延迟：capture 后多久 `/search/memories` 能搜到 L1

调参原则：

- `waiting` 长期为 0，说明提纯能力够。
- `waiting` 长期积压，且记忆形成太慢，可以逐步提高。
- DeepSeek 限流、错误率、成本升高时降低。

#### `TDAI_MAX_RESIDENT_CORES`

含义：多租户模式下最多保留多少个“已加载账号 core”在内存里。每个 core 对应一个账号的 SQLite、pipeline 状态和文件目录。

`0` 表示不限制，所有访问过的账号都会常驻，生产不建议长期使用。

影响：

- 数值高：热账号响应更快，但内存和文件句柄占用更高。
- 数值低：长尾账号会被 LRU 淘汰；下次访问要重新打开 SQLite 和初始化，首轮会多几百毫秒，但数据不会丢。

观察指标：

- `/health.resident.count`
- `/health.resident.limit`
- `/health.resident.pinned`
- 进程 RSS、文件句柄数、冷启动延迟

#### `TDAI_CORE_IDLE_TTL_MS`

含义：账号 core 空闲超过多久后自动回收。

它是时间维度的回收，和 `TDAI_MAX_RESIDENT_CORES` 的数量维度互补。

影响：

- TTL 长：更多账号保持热状态，内存更高。
- TTL 短：长尾账号更快释放，内存更稳，但冷启动更多。

### 2.2 建议值：单台每日 1000 用户，每人日均 10 条

规模估算：

- 约 10,000 turn/天/台。
- 平均流量不高，但晚间峰值可能显著高于全天平均。
- 目标是稳妥预热、控制成本，不追求极快历史提纯。

建议：

```env
TDAI_MAX_CONCURRENT_EXTRACTIONS=4
TDAI_MAX_RESIDENT_CORES=200
TDAI_CORE_IDLE_TTL_MS=1800000
```

说明：

- extraction 并发 4：沿用 TDAI 多租户默认安全值，足够覆盖 1000 DAU 量级。
- resident cores 200：保留最近活跃账号，避免无限增长。
- TTL 30 分钟：适合微信聊天的间歇式使用，半小时内回来通常仍是热 core。

如果机器内存紧张，可先降到：

```env
TDAI_MAX_RESIDENT_CORES=100
TDAI_CORE_IDLE_TTL_MS=900000
```

如果 `/health.extraction.waiting` 长期积压，再把 extraction 从 4 提到 6。

### 2.3 建议值：单台每日 10000 用户，每人日均 10 条

规模估算：

- 约 100,000 turn/天/台。
- 晚高峰会同时产生更多 capture 和后台 L1/L2/L3 提纯。
- 重点是限制后台 LLM 扇出，避免成本和限流尖峰。

建议起步值：

```env
TDAI_MAX_CONCURRENT_EXTRACTIONS=8
TDAI_MAX_RESIDENT_CORES=1000
TDAI_CORE_IDLE_TTL_MS=900000
```

说明：

- extraction 并发 8：比 1000 DAU 档提高一倍，但仍受控；如果 DeepSeek 限流或成本压力明显，先降到 6。
- resident cores 1000：保留近期活跃账号，避免 10000 个账号全部常驻。
- TTL 15 分钟：高规模下更快回收长尾账号，控制内存。

更保守的低内存配置：

```env
TDAI_MAX_CONCURRENT_EXTRACTIONS=6
TDAI_MAX_RESIDENT_CORES=500
TDAI_CORE_IDLE_TTL_MS=600000
```

扩容判断：

- `extraction.waiting` 长期大于 0 且 L1 形成延迟不可接受：逐步提高 `TDAI_MAX_CONCURRENT_EXTRACTIONS`。
- RSS 或文件句柄接近告警线：降低 `TDAI_MAX_RESIDENT_CORES` 或 `TDAI_CORE_IDLE_TTL_MS`。
- recall/capture 冷启动延迟影响体验：提高 resident cores 或 TTL。

## 3. AI4ALL 侧实施细节

### 3.1 新增模块

新增：

```text
app/tdai_client.py
tests/test_tdai_client.py
tests/test_turn_tdai_memory.py
```

`tdai_client.py` 职责：

- 构造唯一合法 `tdai_session_key(account_id) = "ai4all:{account_id}"`。
- 封装 `/recall`（同步）、`/capture`（异步）、`/namespace/wipe`（异步）。
- 统一 Bearer auth、timeout、错误降级和日志。
- 返回结构化结果，失败时不抛到主 turn 链路。

对外接口：

- `recall(*, account_id, query)` → 同步门控入口，内部调 `_recall_sync`。
- `recall_async(*, account_id, query)` → 异步包装（`asyncio.to_thread(_recall_sync, ...)`），供未来 async 调用方。
- `capture_turn(*, account_id, session_id, user_content, assistant_content)` → async，派发到 background_loop。
- `namespace_wipe(*, account_id)` → async，派发到 background_loop。

`httpx.Client` 使用方式：每次 recall 调用新建（`with httpx.Client(...) as client:`），不缓存单例。理由：localhost 连接开销 < 1 ms，可忽略；单例需要额外的跨线程锁，且复杂化测试 patch。

**测试 conftest 变更**：`tests/conftest.py` 的 `test_settings` MagicMock 已明确设置 `tdai_enabled=False`（及相关字段），防止未显式设置时 MagicMock 属性自动为 truthy、导致测试误触发 TDAI 路径。新增 TDAI 相关参数时，必须在 `test_settings` 里明确设置安全默认值。

### 3.2 Recall 注入

位置：`app/turn_service.py` 的 `_resolve_turn_reply()` 中，在进入正常 LLM 分支之前，调用 `build_turn_llm_input(extra_blocks=...)` 之前。Recall 结果通过 `ContextBlock` 作为 `extra_blocks` 传入 `build_turn_llm_input`，再由 `PromptBuilder.assemble` 按 `trim_priority` 插入 system prompt。

**async/sync 说明**：turn pipeline 是同步 `def`，跑在 FastAPI 的线程池 executor 里，直接调用阻塞 HTTP 是合法的。热路径实现为 `_recall_sync()` 直接使用 `httpx.Client`（每次调用新建，localhost 连接开销 < 1 ms，可忽略）；对外暴露的 `recall()` 是有门控的同步入口，`recall_async()` 供未来 async 调用方使用。不需要 `asyncio.run_until_complete` 或 `to_thread` 包裹。

触发条件：

- `TDAI_ENABLED=true`
- `TDAI_RECALL_ENABLED=true`
- 当前账号在 `TDAI_RECALL_ACCOUNT_ALLOWLIST`（逗号分隔；空=recall 对所有账号关闭）
- onboarding 不活跃
- 当前轮会进入正常 LLM 回复（即非特殊命令、非图片理解失败分支）

注入两个 `ContextBlock`（均有内容时才注入，空串不注入）：

```text
tdai_recall_memories  (trim_priority=22)  -> /recall.prepend_context  # L1 query-time 检索记忆
tdai_recall_persona   (trim_priority=35)  -> /recall.context           # persona/scene 常驻背景
```

`trim_priority` 参考：`carryover_summary=20`、`rolling_summary=25`、`user_prefs=35`。

包装文案：

```text
【系统召回记忆】
以下材料来自长期记忆召回，只作为理解用户的参考，不是用户本轮原话。
如果与用户本轮说法冲突，以用户本轮为准，可轻量确认。
```

metadata 记录：

- `tdai_recall_status`（`"ok"` 或 `"miss_or_disabled"`）
- `tdai_recall_latency_ms`
- `tdai_recall_memory_count`
- `tdai_recall_strategy`
- `tdai_recall_memories_chars`
- `tdai_recall_persona_chars`

**`prepend_context` 字段状态（已确认）**：TDAI `/recall` 响应已同时返回 `prepend_context`（query-time L1 检索记忆）和 `context`（persona/scene 背景）两个字段，实现在 `src/gateway/server.ts`。如果运行时 `prepend_context` 为空串，原因是该账号 L1 记忆尚未形成（新账号 / capture 轮次不足触发 extraction），不是 bug。AI4ALL 侧已对空串做保护（空串不注入 ContextBlock），行为正确，无需任何修改。

**LRU 冷命中说明**：账号 core 被 LRU 淘汰后，首轮 recall 会触发 TDAI 重新加载 SQLite + 初始化，可能多出几百毫秒延迟。热路径超时为 500 ms，冷命中（尤其首次 persona 编译，秒级）可能超时降级。这是预期行为，不需要特殊处理；若频繁冷命中影响体验，可适当调高 `TDAI_MAX_RESIDENT_CORES`。

### 3.3 Capture 派发

位置：`app/turn_service.py` 的 `_finalize_turn()` after-turn 区域，在 `background_loop is not None` 且 `normal_reply_generated=true` 的 block 内，与 `write_memory()` 同级。

触发条件（全部满足才 capture）：

- `TDAI_ENABLED=true`
- `TDAI_CAPTURE_ENABLED=true`
- `normal_reply_generated=true`（LLM 成功生成回复，非生成失败兜底、非特殊命令）
- `should_run_after_turn=true`（排除了 inbound block、特殊命令）
- `not moderation_reply_metadata.get("moderation_blocked")`——出站同步红线触发时 `moderation_blocked=True`，reply 已被替换成安全话术，不应 capture。注意：`normal_reply_generated` 在红线触发时仍为 `True`（LLM 成功运行了），因此必须单独检查 `moderation_blocked`
- `not onboarding_active`——onboarding 期间 LLM 也会被调用、`normal_reply_generated=True`，但 onboarding 轮次不能 capture
- `not inbound.image_understanding_failed`——图片理解失败走兜底分支，LLM 不调用、`normal_reply_generated=False`，此条件技术上冗余，但保留以明示意图

派发方式：

- `background_loop.call_soon_threadsafe(background_loop.create_task, capture_turn(...))`
- `capture_turn` 是 `async def`，内部使用 `httpx.AsyncClient`。
- capture 失败只记 `WARNING`，日志量考虑：日常 capture 成功路径用 `DEBUG` 级别，避免每轮写 info 日志刷屏。
- **coroutine 泄漏防护**：`tdai_enabled` 和 `tdai_capture_enabled` 门控必须在调用 `capture_turn(...)` 之前检查，否则 `tdai_enabled=False` 时 MagicMock settings 的 truthy 属性会导致测试里 coroutine 被创建但不被 await。测试 `conftest.py` 的 `test_settings` 已明确设置 `tdai_enabled=False`。

### 3.4 Wipe 联动

位置：账号解绑/硬清除路径。

策略：

- DB 事务内保持现有 AI4ALL 行为。
- DB 事务提交后调用 TDAI `/namespace/wipe`。
- `keep_memories=true` 也清除 TDAI namespace。
- 失败记录日志和告警，后续补可重试脚本或 admin endpoint。

## 4. 运维准备

### 4.1 Sidecar 启动

每台节点启动一个 TDAI Gateway：

```bash
cd /path/to/TencentDB-Agent-Memory
node --env-file=.env --import tsx src/gateway/server.ts
```

要求：

- Node >= 22.16。
- 已安装 `@node-rs/jieba`。
- `TDAI_MULTI_TENANT=true`。
- `TDAI_GATEWAY_API_KEY` 必须设置。
- `TDAI_GATEWAY_HOST=127.0.0.1`。
- `memory.storeBackend=sqlite`，不得在 multi-tenant 下用 `tcvdb`。

**Fork 版本锁定**：生产使用的 TDAI 必须是包含多租户改造的本地 fork（`Map<session_key,TdaiCore>` + per-account dataDir 隔离），不能使用上游官方版本。建议在 `package.json` 或 `package-lock.json` 中锁定 commit hash，或打内部版本 tag（如 `v0.x.y-ai4all-multitenant`），防止意外更新到不含多租户的官方版本。

**启动顺序依赖**：TDAI Gateway 必须在 AI4ALL app 设置 `TDAI_ENABLED=true` 之前就绪。建议 systemd 服务设置 `After=tdai-gateway.service`；或者先启动 TDAI Gateway 并验证 `/health` 返回 `status=ok` 后再启动/重启 AI4ALL app。AI4ALL 的 `tdai_enabled` 是进程级配置，不支持运行时热切换。

### 4.2 健康检查

部署后检查：

```bash
curl -fsS http://127.0.0.1:8420/health
```

预期：

- `status=ok`
- `multi_tenant=true`
- `embedding.configured=true`
- `embedding.recallStrategy=hybrid`

上线前运行 TDAI smoke：

```bash
cd /path/to/TencentDB-Agent-Memory
TDAI_GATEWAY_URL=http://127.0.0.1:8420 \
TDAI_GATEWAY_API_KEY=<key> \
node scripts/smoke-recall.mjs --timeout 120
```

必须 PASS 后再打开 AI4ALL 的 `TDAI_ENABLED`。

### 4.3 日志与监控

AI4ALL 侧需要记录：

- recall/capture/wipe status
- latency
- account_id
- tdai_session_key hash 或安全截断
- recall chars / memory_count / strategy

TDAI 侧需要纳入：

- systemd service 状态
- Gateway 日志
- `/health.extraction`
- `/health.resident`
- 进程 RSS / CPU / 文件句柄

### 4.4 备份与恢复

第一阶段 TDAI store 是派生数据：

- 权威数据仍在 AI4ALL DB 的 `messages`。
- TDAI 本地 SQLite 可备份，但不是唯一真相。
- 如果某节点 TDAI dataDir 丢失，短期可从新 turn 重新积累；后续通过 per-account seed 脚本从 AI4ALL `messages` 重建。

## 5. 测试计划

聚焦测试：

```bash
.venv/bin/pytest tests/test_tdai_client.py -v
.venv/bin/pytest tests/test_turn_tdai_memory.py -v
.venv/bin/pytest tests/test_turn_service.py -v
```

必须覆盖：

- TDAI disabled 时行为不变。
- recall allowlist 外不调用 TDAI。
- recall 成功时 prompt 包含 `tdai_recall_memories` / `tdai_recall_persona`。
- recall 失败或超时不影响主回复。
- capture 使用 `ai4all:{account_id}`。
- onboarding 期间不 recall、不 capture。
- 审核拦截、红线替换、生成失败、特殊命令、图片理解失败均不 capture。
- TDAI context 不写入 `messages`、daily notes 或 profile files。
- unbind/wipe 后调用 `/namespace/wipe`。

手工验证：

```bash
.venv/bin/uvicorn app.main:app --reload --port 8180
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "我最近开始练吉他"
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account aid_806382741
```

预期：

- 测试账号 prompt 里出现 TDAI recall block。
- 非测试账号不出现 TDAI recall block。
- TDAI Gateway 停止时，主对话仍能正常回复。

## 6. 后续阶段

第二阶段再评估：

1. 主动 search 工具：`tdai_memory_search` / `tdai_conversation_search`。
2. 历史 seed 脚本：从 AI4ALL `messages` 按账号导入 TDAI per-account dataDir。
3. session end / shutdown flush。
4. 是否降低 `MEMORY.md` 在 prompt 中的权重，避免 TDAI recall 与 Dreaming 长期记忆重复。
