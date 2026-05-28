# Agent Context Files 与记忆机制技术设计

更新时间：2026-05-28

本文承接 [陪伴式聊天 PRD](../product/companion_chat_prd.md) 和 [记忆与上下文 PRD](../product/memory_prd.md)，定义 AI4ALL Phase 1 的账号级 Context Files、短期上下文、daily notes 和长期记忆文件边界。Dreaming 详细设计见 [Dreaming 记忆压缩与长期记忆技术设计](dreaming_memory_design.md)；检索式记忆后续单独拆文档。

总原则：学习 OpenClaw 的 agent workspace、分层记忆和 Dream/Dreaming 思路，但不复制 OpenClaw 的一对一运行假设。AI4ALL 是一对多服务，所有上下文、记忆、session 和后台任务都必须绑定 AI4ALL Account。

## 1. 设计目标

Phase 1 至少需要做到：

- 每个 `ai4all_account_id` 拥有独立的账号级 Context Files。
- 当前 active session 支撑短期上下文。
- daily notes 按凌晨 4 点业务日边界保存原始文字化聊天材料。
- Dreaming 支持每日 4 点 session 压缩、长期记忆片段生成、自动应用/跳过和新 session 延续。
- 超过 500 轮的 session 触发压缩和新 session 延续，但不直接更新 `MEMORY.md`。
- `MEMORY.md`、`USER.md`、`SOUL.md`、`IDENTITY.md` 共同支撑长期但精简的稳定上下文。
- 检索式记忆作为 Phase 1 可选能力，不阻塞内测发布。
- Admin/Debug 默认视图不能查看正文类内容；明文查看必须具备管理员最高权限，或具备管理员审批后的 2 小时临时明文权限，并记录操作日志。

## 2. 概念拆分

记忆机制需要分清三件事：逻辑概念、存储实体、处理过程。

### 2.1 逻辑概念

| 逻辑层 | 含义 | Phase 1 要求 |
| --- | --- | --- |
| 短期上下文 | 当前 session 中刚发生的对话和当前任务状态 | 默认从 active session 读取；session 只因新账号创建、每日 4 点、超过 500 轮而切换 |
| 长期但精简的常驻记忆 | 每次聊天都应优先可用、类似熟人会自然记得的信息 | 主要由 `MEMORY.md`，以及 `USER.md`、`SOUL.md`、`IDENTITY.md` 的稳定设定组成 |
| 需要检索的历史记忆 | 时间跨度长、内容多，需要按当前上下文检索提取的历史材料 | Phase 1 可选；基础来源是历史 daily notes |

### 2.2 存储实体

| 实体 | 作用 | 当前/目标 |
| --- | --- | --- |
| `chat_sessions` / messages | active session、短期上下文和 session 压缩输入 | 当前已有会话和消息表；需补 session 结束原因、turn_count、carryover summary |
| `memory/YYYY-MM-DD.md` | daily notes 文件视图 | 当前存在，但内容口径需从“提取信息”改为“原始文字化聊天材料” |
| `AGENTS.md` | 账号级运行规则和边界 | 当前已实现 |
| `IDENTITY.md` | AI 对外身份、名字和自我描述边界 | 当前已实现 |
| `SOUL.md` | AI 人格、陪伴风格和用户对 AI 人设的设定 | 当前已实现 |
| `USER.md` | 用户稳定资料、偏好、禁忌和主动触达偏好摘要 | 当前已实现 |
| `TOOLS.md` | 产品真实能力边界 | 当前已实现 |
| `MEMORY.md` | 精选长期记忆 | 当前已实现文件；Dreaming 写入机制需重构 |
| `dreaming_runs` / `dreaming_memory_items` / `memory_events` | Dreaming 运行记录、记忆片段、自动应用/跳过、diff、回滚和审计 | 需新增 |
| retrieval index | 基于 daily notes 的检索辅助索引 | Phase 1 可选 |

### 2.3 处理过程

| 过程 | 输入 | 输出 |
| --- | --- | --- |
| turn prompt assembly | active session、Context Files、必要记忆、runtime | 本轮 LLM prompt |
| daily notes 写入 | 已成功发送给用户的用户消息和 AI 回复 | 业务日 raw text note |
| 每日 4 点 Dreaming | active session、daily notes、现有 `MEMORY.md` / `USER.md` | session summary、dreaming memory item、new session carryover |
| 500 轮压缩 | active session | session summary、new session carryover，不更新 `MEMORY.md` |
| 用户显式纠正 | 用户消息、相关 Context/Memory | context update 或 memory event；低风险明确偏好可直接更新 |
| 检索式记忆 | 当前上下文、历史 daily notes/index | 动态装载的相关历史片段 |

## 3. 当前代码基线与差距

当前可复用部分：

- `app/user_profiles.py` 可创建和读取 `AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md`、`TOOLS.md`、`MEMORY.md`。
- 旧 `user_profile.md` 兼容迁移仍可保留作为过渡。
- `app/prompt_builder.py` 已按 `AGENTS/SOUL/IDENTITY/USER/TOOLS/MEMORY` 注入 `Project Context`。
- 普通聊天 prompt 不注入账号级 `HEARTBEAT.md`。
- 普通聊天 prompt P0 不默认读取或注入 daily notes。
- `app/memory_writer.py` 已改为在普通聊天回复成功后异步追加 raw daily notes，不再做 LLM extraction。
- `sessions` 已补齐 `ended_at`、`close_reason`、`turn_count`、`business_day`、`carryover_summary` 和 `metadata_json`，并用 `__account_active__` 兼容 key 表示账号当前 active session。
- active session 已支持按业务日和最大轮次懒切换；旧 session 归档为 `__account_active__:<old_session_id>`，新 session 继续使用 `__account_active__`。
- `app/dreaming.py` 已能读取 recent daily notes 并手动更新 `MEMORY.md`。
- Debug trace metadata 记录 Context Files 的 path、exists、created、chars。

当前主要差距：

- `app/dreaming.py` 当前直接覆盖 `MEMORY.md`，需要按 [Dreaming 记忆压缩与长期记忆技术设计](dreaming_memory_design.md) 改为 LLM 压缩、memory item、自动应用/跳过、source message、审计和回滚。
- `read_daily_notes()` 仍保留读取今天和昨天的能力；普通聊天 P0 不调用它注入 prompt。后续若需要使用 daily notes，必须改为按场景、预算和隐私策略动态装载，不应默认全量注入。
- 每日 4 点 session 切换目前是下一次入站时懒执行；还缺少独立调度器在 4 点主动扫描并触发 Dreaming。
- 超过 500 轮 session 目前生成确定性 carryover excerpt；还缺少 LLM 压缩摘要和更保守的 memory item 自动应用/跳过链路。
- 缺少用户显式纠正后的 context/memory 更新链路。
- Admin/Debug 现有正文查看能力需要按隐私红线重构为默认脱敏。

## 4. 账号工作区与文件定义

当前文件视图：

```text
data/user_profiles/<ai4all_account_id>/
├── AGENTS.md
├── SOUL.md
├── IDENTITY.md
├── USER.md
├── TOOLS.md
├── MEMORY.md
├── user_profile.md              # 旧格式兼容，暂保留
└── memory/
    └── YYYY-MM-DD.md            # daily notes 文件视图
```

目标长期形态是“结构化状态 + Markdown 可读视图”：

- DB/结构化存储作为 source of truth。
- Markdown 作为 prompt 输入、运营排障和 OpenClaw 对齐视图。
- 文件写入必须有 actor、source、diff 和 rollback 记录。

文件定位：

| 文件 | 定位 | 写入来源 |
| --- | --- | --- |
| `AGENTS.md` | 稳定运行规则、隐私边界、调试暴露边界 | 产品/工程模板，普通聊天不直接改写 |
| `IDENTITY.md` | AI 对外身份、名字、是否暴露 OpenClaw 的回答边界 | 默认模板、首次聊天 onboarding、用户显式修改 |
| `SOUL.md` | AI 人格、语气、陪伴风格 | 默认模板、首次聊天 onboarding、用户显式风格偏好 |
| `USER.md` | 用户称呼、语言、回复长短、长期偏好、禁忌 | 用户显式设置、低风险明确偏好更新 |
| `TOOLS.md` | 当前产品真实能力边界 | 工程/产品随能力上线更新 |
| `MEMORY.md` | 精选长期记忆 | Dreaming 合格记忆片段自动写入，用户明确纠正可触发删除/降权 |

`HEARTBEAT.md` 不放入账号目录，不注入普通聊天 prompt，也不承载用户个人提醒、主动触达或定时任务。用户提醒、commitment、内容推送和主动触达状态都应存在 DB 状态中。

## 5. Session 生命周期

Phase 1 的 session 起止规则固定：

- 新 AI4ALL Account 创建后，开启该账号的第一个 active session。
- 每日凌晨 4 点结束当前 session，触发 Dreaming，并开启新 session。
- 当前 session 超过 500 轮时结束当前 session，触发压缩，并开启新 session。
- Phase 1 不设置“长时间未聊天自动结束 session”等其他规则。

Session 的核心边界是 AI4ALL Account，不是 OpenClaw `session_key`。`session_key`、`channel_account_id`、`chat_id` 和 `sender_id` 属于通道路由、兼容和排障字段；它们不能决定业务上下文是否切分。当前代码里 `sessions(account_id, session_key)` 是兼容实现，P0 主场景已把上下文读取和写入收敛到账号级 active session。

当前兼容字段：

```text
sessions
- id
- account_id
- session_key: __account_active__ | __account_active__:<closed_session_id>
- status: active | closed
- created_at
- ended_at
- close_reason: account_created | daily_dreaming | max_turns
- turn_count
- business_day
- carryover_summary
- metadata_json
```

轮次口径：

- 产品上默认“500 轮”指 500 个用户/AI 往返轮次。
- 当前实现用“成功写入用户消息且成功返回用户可见 assistant 回复”的完整往返近似；调试命令和失败回复不计入 `turn_count`。

## 6. Daily Notes

daily notes 是历史材料，不是长期记忆摘要。

写入时机：

- 普通聊天回复成功后异步写入。
- 不阻塞用户回复。
- 写入失败只记录日志和可观测事件，不影响主链路。

写入内容：

- 文本消息：保存用户原文和已发送给用户的 AI 回复正文。
- 语音消息：保存 ASR 转写文本，不保存原始语音文件作为 daily notes 正文。
- 图片消息：保存 AI 识别后的图片描述，不保存原始图片本身。
- 不写入调试消息、系统内部 trace、失败回复、未发送给用户的 hidden 链路、hidden commitment prompt 或候选生成 prompt。
- daily notes 只归档已经写入 `messages` 的用户可见对话事实，不负责改写 session 对话记录。
- daily notes 不做 LLM extraction，不保存“提取后的长期记忆摘要”；长期记忆片段生成和自动应用交给 Dreaming。

业务日边界：

- daily notes 的一天从凌晨 4 点开始，到次日凌晨 4 点前结束。
- 例如 2026-05-25 03:30 属于 2026-05-24 业务日；2026-05-25 04:00 属于 2026-05-25 业务日。

建议文件格式：

```markdown
# 2026-05-25

## turn msg_123

metadata:
- session_id: sess_...
- user_message_id: ...
- assistant_message_id: ...
- sent_at: 2026-05-25 10:31:00
- modality: text

User:
...

AI:
...
```

如果后续采用 DB source of truth，Markdown 文件仍可作为导出视图，但 Dreaming 和检索应读取结构化记录或经过审计的导出结果。

## 7. Prompt 装载策略

`Project Context` 固定顺序：

```text
AGENTS.md
SOUL.md
IDENTITY.md
USER.md
TOOLS.md
MEMORY.md
```

优先级：

1. 全局 Safety 和法律/产品约束。
2. `AGENTS.md`。
3. `TOOLS.md` 与真实 runtime capability。
4. `IDENTITY.md`。
5. `SOUL.md`。
6. `USER.md`。
7. `MEMORY.md`。
8. 按需装载的 daily notes / 检索结果。
9. 最近对话。

动态装载规则：

- 普通聊天默认装载 active session 最近消息、六类 Context Files 和 `MEMORY.md`。
- P0 普通聊天不读取 daily notes 注入 prompt；当前普通聊天链路已移除默认读取和注入。
- daily notes 不应在改为原始材料后默认全量注入 prompt；未来只能在检索、压缩或 Dreaming 产物明确可控后，按场景和 token 预算装载片段或压缩结果。
- 检索式记忆启用后，按当前上下文检索 daily notes，再装载相关片段。
- 新用户 onboarding 需要额外装载默认身份、默认 Soul 和未完成设置项。
- Web Search、ASR、提醒、主动消息等任务场景只装载该任务需要的信息。
- Safety、真实能力边界和账号隔离规则始终保持最高优先级。

## 8. Dreaming

Dreaming 是独立复杂机制，详细设计见 [Dreaming 记忆压缩与长期记忆技术设计](dreaming_memory_design.md)。本文件只保留与 Context Files 的边界。

核心口径：

- Dreaming 不是简单地把 daily notes 复制进 `MEMORY.md`，而是 LLM 压缩、记忆片段生成、自动应用/跳过和事后回滚过程。
- 本版本 session 压缩和跨 session `carryover_summary` 都必须由 LLM 生成；只有 LLM 失败时才使用 deterministic fallback。
- 每日 4 点 Dreaming 可以生成 `MEMORY.md` / `USER.md` 等长期记忆片段，并为新 session 生成 `carryover_summary`。
- 500 轮压缩主要保证上下文窗口和连续性；如生成长期记忆片段，只自动应用高置信、高重要度且普通敏感度的片段，其余自动跳过并保留 debug 记录。
- 长期记忆片段必须排除敏感信息，并绑定 source、importance、confidence、sensitivity、apply_status、skip_reason、diff 和审计事件。

高层流程：

```text
active session + business-day daily notes + current MEMORY/USER
-> LLM session compression
-> LLM carryover summary
-> dreaming memory items
-> auto apply / skip
-> rollback only for correction / recovery
```

## 9. 用户显式纠正

用户显式纠正优先级高于旧记忆。

典型输入：

- “以后叫我 X。”
- “你别这么正式。”
- “这个别记。”
- “刚才那个不是我的偏好。”
- “你记错了，我不是做这个的。”

目标流程：

1. 识别纠正类型：identity、soul/style、user preference、memory delete、memory downgrade。
2. 定位可能冲突的 Context File 或 `MEMORY.md` 条目。
3. 低风险明确偏好可直接写入并记录 `memory_events`。
4. 长期记忆、敏感信息和删除/降权请求生成 memory item，由策略自动应用或跳过；敏感和低置信内容不进入长期记忆。
5. 回复用户时确认结果，但不暴露底层文件全文。

## 10. 检索式记忆

检索式记忆 Phase 1 可选。

基础链路：

```text
current turn intent
-> query builder
-> daily notes index search
-> extract / rerank / safety filter
-> prompt snippet
```

要求：

- 基础来源是历史 daily notes。
- 只返回与当前上下文相关的片段。
- 支持 token 预算和最大片段数限制。
- 命中、装载和使用情况需要可观测。
- 如果没有启用检索式记忆，模型不能编造“记得很久以前聊过”的细节。

## 11. Admin 与隐私红线

后台默认视图可以查看：

- Context Files metadata、文件是否存在、字符数、更新时间。
- `MEMORY.md` 摘要、dreaming memory item 状态、diff 摘要和审计状态。
- daily notes metadata：业务日、消息数、来源类型、写入状态、错误信息。
- session metadata：开始/结束时间、结束原因、turn_count、summary 摘要。
- Dreaming 任务状态、source 文件列表、生成片段数量、自动应用/跳过数量、失败原因。

后台默认视图不能查看：

- 用户具体聊天记录。
- daily notes 正文。
- 语音 ASR 正文。
- 图片识别正文。
- prompt/messages 全文。
- 模型回复正文。

明文查看正文必须遵守 [运营与后台 PRD](../product/admin_ops_prd.md)：管理员可在必要 debug 和事故排查时查看；普通后台用户需要管理员审批后的 2 小时临时明文权限；所有明文查看都记录操作日志。

## 12. 开发切分

建议按以下顺序推进：

1. 已完成：Daily notes writer 重构，替换提取式 `memory_writer.py`，按业务日写入原始文字化聊天材料。
2. 已完成 P0：Prompt 装载调整，普通聊天不再默认全量注入 raw daily notes；后续只保留场景化、预算化动态装载。
3. 已完成 P0：Session 生命周期字段、业务日懒切换、最大轮次懒切换、`close_reason` 和 deterministic carryover。
4. Dreaming memory item：把 `app/dreaming.py` 从直接覆盖 `MEMORY.md` 改为生成 memory item、diff 和 source metadata。
5. Auto apply / skip / rollback：新增 memory events、自动应用/跳过状态、skip reason 和事后回滚能力。
6. 用户纠正链路：支持称呼、AI 名字、风格偏好和错误记忆删除/降权。
7. Admin 脱敏：默认只展示 metadata、摘要和 diff 摘要，正文查看走管理员最高权限或 2 小时临时明文权限。
8. 可选检索式记忆：基于 daily notes 建索引，按需动态装载。

## 13. 验收点

- 新账号首次对话生成六类 Context Files，不生成账号级 `HEARTBEAT.md`。
- 不同账号 Context Files、daily notes、session、`MEMORY.md` 不串线。
- daily notes 按凌晨 4 点业务日边界写入原始文字化聊天材料。
- daily notes 不保存原始图片或原始语音文件。
- 普通聊天成功后写 daily notes 不阻塞用户回复。
- 每日 4 点后的下一次入站能结束旧 session、开启新 session，并生成 carryover summary；独立 4 点调度器后续补齐。
- Dreaming 能生成 `MEMORY.md` memory item 和 diff，并按规则自动应用或跳过，而不是无审计直接覆盖。
- 超过 500 轮的 session 会开启新 session 并携带 LLM carryover；LLM 失败时才使用 deterministic fallback，不会激进更新 `MEMORY.md`。
- 用户明确纠正记忆后，后续对话不继续使用旧错误记忆。
- 后台默认视图不能查看正文类内容；明文查看需要管理员最高权限或 2 小时临时明文权限，并记录操作日志。
- 如果启用检索式记忆，检索结果来自本账号历史 daily notes，且按需动态装载。
