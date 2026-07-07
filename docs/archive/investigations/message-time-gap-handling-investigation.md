# 长间隔消息的历史处理：现状梳理与建议

> 临时文档。背景：参考 OpenClaw 对"隔很久再发消息"的处理机制，评估 AI4ALL 是否有同类问题、
> 现有基础设施覆盖到什么程度、真正缺的是什么。本文只做现状梳理 + 建议，未开始实现。

日期：2026-07-05。

---

## 1. 问题

用户隔了很久（几小时到跨天）再发消息时，如果历史轮次原样喂给 LLM 且不带时间信息，模型容易把
"很久以前的对话"当成"刚刚发生的连续上下文"来理解，导致语气/话题衔接失真。

---

## 2. AI4ALL 现状（按源码逐条核实）

### 2.1 Session 生命周期：有跨天硬切分，无同日 idle 切分

- `app/session_lifecycle.py:20-31` `business_day_for()`：以本地时间 4 点为界计算"业务日"。
- `app/session_lifecycle.py:34-44` `_close_reason_for()`：当前 session 的 `business_day` 与
  当前请求的业务日不一致时，返回 `daily_dreaming` 关闭原因。
- `app/session_lifecycle.py:108-152` `get_or_create_account_active_session_with_dreaming()`：
  命中 `daily_dreaming` 时，先跑 `_rotate_session_with_dreaming()` 生成
  `session_summary` / `carryover_summary`（经 `app/dreaming.py:run_dreaming`），再开新 session，
  新 session 只带 `carryover_summary`，不带旧 session 的 raw history。

  **这一层和 OpenClaw 的 daily reset 思路一致**：跨业务日 = 换 session = 旧 raw history 不进入
  新一轮的 `messages[]`，长期连续性交给摘要。

- **缺口**：没有 idle-based reset。`_close_reason_for` 只看 `business_day` 是否变化，同一个业务日
  内不管隔了多久（比如早 8 点和晚 10 点各发一条），都不会触发 session 轮转，历史仍原样保留在同一
  session 里。

### 2.2 历史消息组装：完全不带时间戳（比预期的缺口更彻底）

- `app/turn_service.py:455-458`：

  ```python
  history_rows = list_recent_messages_for_account(
      account_id=account_id,
      limit=settings.llm_context_messages,
  )
  ```

- `app/db/accounts.py:358-393` `list_recent_messages_for_account()` 的 SQL：

  ```sql
  SELECT id, session_id, message_id, role, content FROM messages
  WHERE account_id = ? ...
  ```

  **注意**：这条查询本身就没有 `SELECT created_at`——不是"查出来了但没用"，是从数据源头就没取。
  仓库里另有一个 `list_recent_messages_for_account_since()`（`app/db/accounts.py:425-`）会选
  `created_at`，但当前主对话路径（`turn_service.py:455`）用的不是这个函数。

- `app/turn_service.py:468-471`：

  ```python
  history = [
      {"role": row["role"], "content": row["content"]}
      for row in kept_rows
  ]
  ```

  最终喂给 LLM 的历史轮次只有 `role`/`content`，逐条消息没有任何时间信息，也没有"距上一条隔了
  多久"的提示。

- `messages` 表本身有 `created_at`（`app/db/_core.py:604-620`，北京时间字符串），`sessions` 表
  也有 `updated_at`（`_core.py:586`）——数据都在，只是没有被读取和传递到 LLM 侧。

### 2.3 Runtime block：只覆盖"当前"，不覆盖历史

- `app/prompt_builder.py:441-455` 的 `runtime` block 只拼"现在是北京时间 xxx"，是当前请求的绝对
  时间，不涉及历史消息、也不涉及"上一条消息是多久之前"。

### 2.4 小结：三层缺口对照

| OpenClaw 机制 | AI4ALL 现状 |
|---|---|
| 1. Session 硬切分（daily/idle reset） | **部分有**：daily（业务日）切分已实现且带 dreaming 摘要；idle 切分未实现 |
| 2. Fresh session 内历史逐条带时间戳 | **没有**：历史消息只有 role/content，查询源头就没取 created_at |
| 3. 当前消息带"距上次消息多久" | **没有**：runtime block 只有绝对时间，无 gap 提示 |
| 4. 历史消息剥离旧 inbound metadata，只保留正文+时间 | **部分适用**：当前历史本来就只存 role/content，没有重复注入 metadata 的问题；`_wrap_current_message_envelope`（`turn_service.py:408-`）只包裹最后一条 user 消息，不影响历史 |

---

## 3. OpenClaw 参考（第 2、3 条已在 `/Users/suchong/workspace/openclaw` 源码核实）

早前和 codex 的研究里引用路径混了两种用户名前缀（复制粘贴痕迹），故原文标注"未验证"。
2026-07-06 已直接对 `/Users/suchong/workspace/openclaw` 仓库逐条求证，结论如下（核心的第 2、3
条有确凿代码证据；第 1、4 条仅部分查到，仍需深入）：

1. **部分核实**：`session.reset.idleMinutes` 配置项确实存在（`src/config/sessions/reset-policy.ts`、
   `src/config/zod-schema.session.ts`）；但"本地凌晨 4 点 daily reset"与"换 session 后旧 transcript
   不进入本轮 `messages[]`"尚未逐行验证，暂不作为结论。

2. **✅ 已核实**：Fresh session 内回填历史，**逐条**消息（含 assistant，不止 user）各带**自己到达
   时间**的时间戳，格式为 `weekday + 绝对时间(+时区)`，对应 `[DOW YYYY-MM-DD HH:MM TZ]`：
   - `extensions/slack/src/monitor/message-handler/prepare-dm-history.ts:86-113`：fresh-session 的 DM
     历史回填循环，逐条 `formatInboundEnvelope`，`timestamp = resolveSlackTimestampMs(message.ts)`
     取每条消息自己的到达时间（非当前时间）。
   - 该回填的门控正是"新会话"：`prepare.ts:1180` 条件含 `&& !previousTimestamp`（无上次 session
     活动时间＝fresh session）。群聊历史同理，`prepare.ts:1191-1206` 的 `formatEntry` 用
     `timestamp: entry.timestamp` 逐条盖。
   - weekday 前缀 + 绝对时间的格式在 `src/auto-reply/envelope.ts:129-157`（注释说明：小模型不擅长
     自行推算星期几，故显式给出）。

3. **✅ 已核实**：当前消息头部带 `+elapsed`（距上一条多久）+ 绝对时间：
   - `src/auto-reply/envelope.ts:171-209` `formatAgentEnvelope`：`elapsedMs = currentMs - previousMs`，
     经 `formatTimeAgo` 渲染为头部的 `+2m` 之类。
   - `previousTimestamp` 来自 `readSessionUpdatedAt({ storePath, sessionKey })`（上一次 session 活动
     时间），`timestamp` 为当前消息自身到达时间，差即"距上一条多久"。此模式在 slack / telegram /
     discord / imessage / signal / matrix / msteams / line 等十余个 channel extension 中一致（各 monitor
     里传入 `previousTimestamp`），是通用机制而非个例。

4. **未核实**：历史重放剥离旧 inbound metadata 这条尚无确凿证据，且方向上存疑——`prepare-dm-history.ts:108`
   的历史行里仍保留了 `sender` 标签与 `[slack message id: ... channel: ...]` 尾注，未见"剥离"。
   在未找到明确剥离逻辑前，本条不作为参照。

> **两点补充（影响 §4 方案）**：
> - OpenClaw 的时间戳/elapsed 都是 config 可关的（`envelope.ts:76-77`，`envelopeTimestamp` /
>   `envelopeElapsed`，默认开）。
> - 实现位置是 **channel 边缘、入站时把 `[...] body` 烘焙进消息正文并持久化**，回放时时间戳已在正文
>   里，并非一个中央 assembler。AI4ALL 存的是 raw `role/content`（时间在 `created_at` 列、未进正文），
>   **无法照抄"入站烘焙"路线，只能按 §4.1 在组装期动态拼**——两边实现路径不同，勿直接搬。

---

## 4. 建议（分层，按确定性标注）

### 4.1 较确定：历史消息补时间戳 + 当前消息 gap note

- 改 `list_recent_messages_for_account()` 把 `created_at` 加进 SELECT 和返回 dict（比照
  `list_recent_messages_for_account_since` 已有的字段）。
- `turn_service.py:468-471` 构造 `history` 时，给每条历史消息前缀（或 gap 达到阈值时才前缀，避免
  正常连续对话里全是噪音）时间信息。
- 当前消息前追加一句 gap note（例如"上一条用户消息在 X，距现在 N 小时/分钟"），只在间隔超过某个
  阈值（比如 1-2 小时）时注入，避免高频闲聊场景里每轮都插入无意义的"刚刚"。

这一层收益明确、改动集中（一处 SQL + 一处组装逻辑），风险低。

### 4.2 待定：是否需要 idle-based session reset

- 现有跨业务日切分 + dreaming 摘要已经覆盖"隔夜/跨天"场景。
- 是否还需要在**同一业务日内**因为长间隔（比如早 8 点到晚 10 点）而触发 session 轮转，取决于
  4.1 的 gap note 是否已经足够让模型"意识到"这是新的对话，而不需要物理上换 session。
- 建议**先上 4.1，观察实际效果**，再决定要不要加这层，避免过度设计。

### 4.3 不建议现在做

- 照搬 OpenClaw 剥离历史 metadata 的机制：AI4ALL 历史本来就只存 role/content，没有重复注入
  inbound metadata 的问题，这层在当前架构下是无对象可剥离的。

---

## 5. 开放问题（需要和用户确认后再动代码）

1. gap note 的阈值定多少合适（1 小时？2 小时？）——建议先看几个真实账号的消息间隔分布再定，
   而不是拍一个数字。
2. 历史消息时间戳是"每条都带"还是"只在 gap 超阈值的那条前插一次"——全带会增加 token 消耗和
   prompt 噪音，只在断点插一次更接近 OpenClaw 的 elapsed 提示语义，但需要确认这样对模型是否
   足够清晰。
3. `llm_context_messages` / `llm_context_token_budget` 裁剪之后，`kept_rows` 里消息可能不连续
   （中间被丢了），gap note 计算要按"实际保留下来的相邻两条"算，还是按"数据库里真实相邻的两条"
   算——两者在裁剪发生时会不一致，需要明确口径。

---

## 6. 线上实测（2026-07-06，账号 `aid_806382741`，openclaw-weixin 影子 trace）

本节用真实微信消息抓到了 OpenClaw 与 ai4all 两侧**发给 LLM 的 prompt 原文**，对 §3 的四条参考给出线上定论。

### 6.1 抓取方法（openclaw-bridge 影子 trace）

- OpenClaw 侧 prompt 靠 `openclaw-bridge` 插件的「shadow trace」抓：把**该账号的 `channel_account_id`**（这里是 `22c23c13b7f6-im-bot`，不是内部 `aid_806382741`——bridge 的 `extractAccountId` 从 sessionKey 取 provider 后一段）加进插件配置
  `~/.openclaw/openclaw.json` → `.plugins.entries."ai4all-openclaw-bridge".config.shadowTraceAccountIds`，重启网关（`openclaw gateway restart`）。
- **关键坑**：非影子账号时 bridge 直接短路（`index.js` `before_agent_reply` 返回 `handled:true`），OpenClaw 原生 agent 根本不跑、不调 LLM，两种抓法都拿不到。必须开影子放行原生 run。
- 两个产物：① 原始 HTTP body 落 `tmp/llm_request_bodies/openclaw/*.body.json`（`requestDumpEnabled`）；② 结构化 trace 落 `debug_traces`（`source=openclaw`）。**本机后端是 PG**（`DATABASE_URL=postgresql://…`），SQLite 版 `export_prompt_trace_pair.py` 读不到，改用 `GET /admin/debug/traces?account_id=…`。

### 6.2 对 §3 四条的线上定论

1. **OpenClaw 在 weixin 这套里不是对话大脑**：历史/人设/记忆全在 ai4all 侧。OpenClaw 原生 transcript 只在它真正被跑时累积——首条影子「无历史」是假象（transcript 从零起）；开着影子连发，逐轮增长（实测 messages 数 2→4→6→…→22）。**§2.4 表格第 1 行「daily reset 部分有」的乐观评估应结合此点修正**。
2. **§3 claim 2（fresh session 历史逐条带时间戳）成立**：每条 inbound user 消息带**自己到达时间**的绝对时间戳，形如 `Mon 2026-07-06 11:39 GMT+8`（weekday + 日期 + HH:MM + 时区），装在 `Conversation info (untrusted metadata)` 的 fenced JSON 块里、盖在正文前；assistant 不带。
   - 组装链路：`inbound-meta.ts:499 formatConversationTimestamp(ctx.Timestamp)` → `:401 formatEnvelopeTimestamp`（`envelope.ts`，weekday + zoned time）→ `:524 conversationInfo.timestamp` → `:149 formatUntrustedJsonBlock` 包成 ```json 块。
   - 形态与 §3 slack/imessage 的 `[DOW date time]` 方括号前缀不同（weixin 走 untrusted-metadata JSON 块），但「逐条、用消息自己的时间」语义一致。
3. **§3 claim 3（`+elapsed` 距上次多久）在 weixin 不生效**：实测全程无任何相对时间提示，只有绝对时间戳。
4. **§3 claim 4（历史剥离 inbound metadata）实测未生效**：历史 user 消息全保留了 `Conversation info` + timestamp 块（源码 `attempt.llm-boundary.ts:460` 有剥离逻辑，但此 native-run 路径未触发或不针对该字段）。

### 6.3 ai4all 侧同轮对照（问题实物）

- 同一轮 `source=ai4all`、deepseek-v4-pro、13 条 messages：历史全是**裸 `role`/`content`、零时间戳**。模型看不出 `测试456`（12:08）距 `测试123`（11:23）隔了 40+ 分钟，也看不出更早的跑步话题是多久以前——这就是 §1 描述的问题在真实 prompt 里的样子。

### 6.4 可借鉴样板（落到 §4.1）

| 维度 | OpenClaw-weixin 做法 | ai4all 现状 / 待办 |
|---|---|---|
| 时间源 | 每条消息自己的 `ctx.Timestamp`（到达时间） | 有 `created_at`，未进 prompt |
| 格式 | weekday + 本地时区绝对时间（`formatEnvelopeTimestamp`） | 无 |
| 承载 | 盖在每条 user 正文前的 metadata JSON 块 | 需在**组装期**注入（存的是裸 role/content） |

存档：OpenClaw 侧原文 `tmp/openclaw_prompt_raw_latest.txt`；trace_id `openclaw-1783309165690`（openclaw）/ `trace-f4bb90d8-1b1a-4e91-bcc1-2aa84b3b06e3`（ai4all）。
