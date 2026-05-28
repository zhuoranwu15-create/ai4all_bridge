# Agent Context Files 与记忆机制设计

更新时间：2026-05-27

## 1. 文档定位

本文定义 AI4ALL 账号级 Context Files、daily notes、长期记忆和 Dream/Dreaming 的技术机制。它承接以下产品需求：

- [陪伴式聊天 PRD](../product/companion_chat_prd.md)
- [记忆与上下文 PRD](../product/memory_prd.md)
- [主动消息与提醒 PRD](../product/proactive_prd.md)

总原则：学习 OpenClaw 的 agent workspace、分层记忆和 Dreaming 思路，但不复制 OpenClaw 的一对一运行假设。AI4ALL 是一对多服务，所有上下文和记忆必须绑定 AI4ALL Account，不能绑定 OpenClaw 原生实例状态。

## 2. 目标与边界

Phase 1 目标：

- 为每个 `ai4all_account_id` 提供独立的 Agent Context Files。
- 支持 `AGENTS / SOUL / IDENTITY / USER / TOOLS / MEMORY` 六类账号级上下文。
- 支持短期上下文、daily notes、长期记忆和 Dream/Dreaming 风格蒸馏。
- 支持运营侧查看、禁用、重置、纠错和审计。
- 支持用户明确纠正时更新、删除或降权相关记忆。
- 为 prompt trace、OpenClaw 对比和后续结构化记忆迁移提供清晰边界。

当前不做：

- 用户直接编辑底层 prompt 文件。
- 账号级 `HEARTBEAT.md` 注入普通聊天 prompt。
- 自动无审核地大规模改写长期记忆。
- 将 OpenClaw 原生 `soul.md`、workspace 或本地实例记忆作为 AI4ALL source of truth。

## 3. 当前实现状态

已实现：

- `app/user_profiles.py` 创建和读取账号级 Context Files。
- 文件集合：`AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md`、`TOOLS.md`、`MEMORY.md`。
- 旧格式 `user_profile.md` 兼容迁移，缺失文件自动从旧 section bootstrap。
- `app/prompt_builder.py` 在 `Project Context` 中注入六类文件。
- 普通聊天 prompt 不注入账号级 `HEARTBEAT.md`。
- `app/memory_writer.py` 在成功回复后异步写入 `memory/YYYY-MM-DD.md`。
- `app/dreaming.py` 支持手动将 recent daily notes 蒸馏到 `MEMORY.md`。
- Debug trace metadata 记录 Context Files 的 path、exists、created、chars。

仍需补齐：

- 结构化 `context_files`、`memory_items`、`memory_events` 或等价数据模型。
- 用户显式纠正后的自动更新/删除/降权链路。
- Dreaming candidate diff、审批和回滚。
- 运营侧完整查看、重置、禁用、审计 UI/API。
- 敏感记忆过滤和长期记忆质量评估。

## 4. 文件结构

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
    └── YYYY-MM-DD.md            # daily notes
```

`HEARTBEAT.md` 是全局系统策略文件，当前占位于 `app/prompts/heartbeat.md`。它不放入账号目录，不注入普通聊天 prompt，也不承载用户个人提醒、主动触达或定时任务。

未来目标是“结构化状态 + Markdown 可读视图”：

- DB/结构化存储作为 source of truth。
- Markdown 文件作为 prompt 输入、运营排障和 OpenClaw 对齐视图。
- 文件写入必须可审计，避免无法追踪的记忆污染。

## 5. 上下文分层

| 层级 | 用途 | 文件/数据 |
| --- | --- | --- |
| 系统/产品层 | 稳定规则、真实能力、边界，不随单个用户频繁变化 | `AGENTS.md`、`TOOLS.md`、全局 `HEARTBEAT.md` |
| 账号设定层 | assistant 身份、人格、语气、用户资料和偏好 | `SOUL.md`、`IDENTITY.md`、`USER.md` |
| 记忆沉淀层 | 从对话中提取并沉淀的短期/长期记忆 | `memory/YYYY-MM-DD.md`、`MEMORY.md`、未来 `memory_items` |

写入边界：

- 系统/产品层主要由产品和工程维护，普通聊天不得直接改写。
- 账号设定层可由用户显式修正、运营修改或 onboarding 初始设定生成。
- 记忆沉淀层可由异步提炼和 Dreaming 生成，但必须保守、可追溯、可回滚。

## 6. 文件定义

### 6.1 AGENTS.md

定位：当前账号 agent 的稳定运行规则。

适合放：

- 回复纪律、隐私边界、调试暴露规则。
- 上下文冲突时的处理原则。
- 记忆写入和能力承诺的基本边界。

不适合放：

- AI 名字、人设语气、用户事实。
- API key、provider credential、通道 token。
- 临时实验指令。

更新方式：

- 新账号自动生成保守默认模板。
- 产品/工程手动更新。
- 正常用户聊天不能直接改写。

### 6.2 SOUL.md

定位：人格、语气、情绪姿态和互动风格。

适合放：

- 温暖程度、简洁程度、幽默感、正式程度。
- 陪伴姿态和表达边界。
- “聊天陪伴优先，轻量助理其次”的默认风格。

不适合放：

- 用户事实和长期事件。
- 工具说明和外部能力承诺。
- 安全策略覆盖。

更新方式：

- 首次从旧 `user_profile.md ## Soul` 或默认模板生成。
- 用户显式反馈风格时，可进入 SOUL 更新候选流程。
- 更新后可向用户简短确认，但不回显完整 `SOUL.md`。

### 6.3 IDENTITY.md

定位：对用户可见的 assistant 身份。

适合放：

- assistant name / display name。
- “AI4ALL 微信个人 AI 陪伴与轻量助理”的产品身份。
- 当用户问“你是谁”“你是不是 OpenClaw”时的稳定回答边界。

不适合放：

- Bridge、Gateway、provider stack、shadow trace 等内部实现。
- 默认自称 OpenClaw。
- 自称真人或专业持证人员。

更新方式：

- 新账号自动生成。
- 用户或运营可显式修改名字和称呼。
- Dreaming 和 daily notes 不应自动改写。

### 6.4 USER.md

定位：用户稳定资料、偏好和 consent / preference。

适合放：

- 用户希望如何被称呼。
- 语言、回复长短、语气偏好。
- 粗粒度地区、时区、长期兴趣和长期项目。
- 主动触达偏好摘要，但具体开关和任务仍以 DB 状态为准。

不适合放：

- 原始聊天记录。
- 大量一次性事件。
- 对回复没有必要的敏感细节。
- 从单条模糊消息弱推断出的事实。

更新方式：

- 用户显式修正时可直接更新或生成更新候选。
- 自动提炼只能提出候选，直写必须保守。
- 用户要求删除或纠正时，应修正旧条目，而不是追加冲突条目。

### 6.5 TOOLS.md

定位：产品真实能力和能力边界说明。

关键约束：`TOOLS.md` 不授予工具，只描述当前 AI4ALL 后端真实可执行能力。真正 tool availability 必须来自后端 tool schema / runtime registry。

适合放：

- 当前已接入能力：文本回复、读取上下文、明确时间的一次性提醒等。
- 即将接入能力的谨慎说明，但必须标明未启用或受限。
- 不支持动作的兜底话术边界。

不适合放：

- API credential 或 endpoint。
- AI4ALL 未暴露的 OpenClaw 工具清单。
- 让模型假装已经完成搜索、下单、发消息或修改外部系统的指令。

更新方式：

- 后端能力变化时由产品/工程同步更新。
- Web Search、ASR、内容推送等能力上线前，必须同步更新 `TOOLS.md` 和真实 tool/task registry。

### 6.6 MEMORY.md

定位：精选长期记忆。

适合放：

- 用户明确要求记住的长期事实。
- 长期偏好、长期项目、重要关系和重要决定。
- 不适合放入 `USER.md` 但会影响未来回复的重要上下文。

不适合放：

- 原始 transcript。
- 一次性测试、低价值寒暄、失败回复。
- 没有产品价值的敏感事实。

更新方式：

- 当前支持人工编辑和手动 Dreaming。
- Phase 1 目标是 daily notes -> candidate distillation -> review -> `MEMORY.md`。
- 用户明确纠正时必须能删除、覆盖或降权旧记忆。

## 7. Prompt 注入策略

`Project Context` 注入顺序固定：

```text
AGENTS.md
SOUL.md
IDENTITY.md
USER.md
TOOLS.md
MEMORY.md
```

daily notes 不属于账号 Context Files。当前读取今天和昨天的 `memory/YYYY-MM-DD.md`，作为独立区块注入。未来可以扩展为检索式 daily memory，但需要先定义 token 预算、检索策略和审计。

冲突处理优先级：

1. 全局 safety policy 和法律/产品约束。
2. `AGENTS.md`。
3. `TOOLS.md` 和真实 runtime capability。
4. `IDENTITY.md`。
5. `SOUL.md`。
6. `USER.md`。
7. `MEMORY.md`。
8. Daily notes。
9. 最近对话。

理由：

- 安全和真实能力必须高于用户愿望。
- 身份和人格不应因低置信记忆漂移。
- 最近对话可修正旧偏好，但不能在低置信情况下改写稳定上下文。

## 8. 生命周期

### 8.1 新账号 bootstrap

1. 创建或复用 `user_profile.md` 兼容文件。
2. 创建缺失的六个 Context Files。
3. 从旧 section 迁移 `SOUL.md`、`USER.md`、`MEMORY.md`。
4. 用账号 display name 或默认名生成 `IDENTITY.md`。
5. 用系统默认模板生成 `AGENTS.md`、`TOOLS.md`。
6. 不生成账号级 `HEARTBEAT.md`。
7. 永不覆盖已存在文件。

后续可以增加 onboarding 初始设定任务：

- 询问用户希望 assistant 的名字和称呼。
- 询问用户偏好的陪伴风格、回复长度和主动触达偏好。
- 将稳定结果写入 `IDENTITY.md`、`SOUL.md`、`USER.md` 或对应 DB 状态。

### 8.2 普通聊天 turn

1. Identity resolver 得到 `ai4all_account_id`。
2. 读取 Context Files。
3. 读取近期消息和 daily notes。
4. PromptBuilder 组装 prompt。
5. LLM 生成回复。
6. 成功回复后异步写 daily notes。
7. 后台可抽取 hidden commitment，但主动发送仍走 proactive state 和 outbound ledger。

普通聊天不能直接改写 `AGENTS.md`、`TOOLS.md` 或全局 `HEARTBEAT.md`。

### 8.3 用户显式纠正

示例：

- “以后叫我 X。”
- “你别这么正式。”
- “别记这个。”
- “刚才那个不是我的偏好。”

目标机制：

1. 识别纠正类型：identity、style、user preference、memory delete、memory downgrade。
2. 生成结构化 update candidate。
3. 对低风险明确偏好可直接写入并审计。
4. 对长期记忆和敏感信息走 candidate / review。
5. 回复用户时确认结果，但不暴露底层文件全文。

### 8.4 Daily Notes

写入时机：

- 普通聊天回复成功后异步执行。
- 不阻塞用户回复。
- 失败只记录日志，不影响主链路。

写入要求：

- daily notes 是按业务日保存的原始文字化聊天材料，不是长期记忆摘要。
- 文本消息保存用户原文和 AI 回复正文。
- 语音消息保存 ASR 转写文本，不保存原始语音文件作为 daily notes 正文。
- 图片消息保存 AI 识别后的图片描述，不保存原始图片本身。
- 调试消息、系统内部 trace、失败回复和未实际发送给用户的 hidden 链路不写入 daily notes 正文。
- daily notes 应保留来源 metadata 或可追踪 source id，便于后续 Dreaming、检索和审计。
- daily notes 默认按凌晨 4 点作为业务日边界写入。

### 8.5 Dream/Dreaming

Phase 1 目标：

```text
daily notes / session compression / candidate memories
-> Dreaming distillation
-> candidate diff
-> review / policy approval
-> MEMORY.md / structured long-term memory
```

当前 `app/dreaming.py` 已能手动更新 `MEMORY.md`，但正式内测前应补齐：

- 候选 diff，不直接覆盖正式长期记忆。
- source files / source messages 记录。
- 更新前后 diff 和 actor。
- 回滚能力。
- 低置信和敏感内容拒绝策略。

自动调度是否开启待定。未建立质量评估前，默认只允许手动或灰度账号运行。

## 9. Heartbeat 与主动触达边界

OpenClaw 的 heartbeat 是围绕一对一用户运行的个人循环；AI4ALL 必须拆成两层：

- 实例级 heartbeat：检查后端、Bridge、Gateway、队列、LLM、账号连接等系统健康，结果进入日志、告警和 Admin，不进入用户 prompt。
- 用户级 heartbeat / 主动触达：读取单个账号的 proactive state、USER/MEMORY、daily notes 和候选事项，经过 quiet hours、每日上限和 outbound ledger 后发送。

具体原则：

- `HEARTBEAT.md` 不作为用户个人任务来源。
- 用户提醒、commitment、内容推送和主动关怀都写入独立 DB 状态。
- 用户拒绝某类推送后，偏好应写入推送状态或 `USER.md` 摘要，并至少冷却 1 个月。

## 10. 运营与审计

Phase 1 Admin 至少需要：

- 查看 Context Files、daily notes、`MEMORY.md` 和 Dreaming 的元数据、状态、摘要与审计信息。
- daily notes 正文、语音 ASR 正文、图片识别正文、prompt/messages 和模型回复正文按正文类敏感信息处理，默认不在 Admin UI、Debug API 或日志平台展示。
- 如确需查看正文，必须经过安全委员会审批、按最小必要范围授权并完整审计。
- 禁用某账号记忆写入。
- 重置或删除某账号记忆。
- 查看记忆更新来源、actor、时间和 diff。
- 处理用户“删除/纠正记忆”的客服请求。

建议目标数据模型见 [Phase 1 详细技术设计](../phase1_technical_design.md) 的 Context & Memory 部分。

## 11. OpenClaw 借鉴点与差异

借鉴：

- Workspace 文件结构。
- `MEMORY.md` + daily notes + Dreaming 的分层记忆。
- Prompt trace 中逐块观察 context。
- Heartbeat / cron 的状态可恢复思想。

差异：

- AI4ALL 一对多，Context Files 必须按 `ai4all_account_id` 隔离。
- OpenClaw 原生 workspace 不是 AI4ALL 业务 source of truth。
- AI4ALL 的 `TOOLS.md` 只描述产品真实能力，不暴露 OpenClaw 工具。
- AI4ALL 的主动触达必须受产品级频率、拒绝反馈和通道风险约束。

## 12. Phase 1 工作包

P0：

- 保持六类 Context Files 自动创建和 prompt 注入稳定。
- 补齐 debug/admin 查看入口，能排查“为什么这轮这样回复”。
- 确保不同账号 Context Files、daily notes、MEMORY 不串线。

P1：

- 实现用户显式纠正后的 context / memory 更新候选机制。
- 将 daily notes 写入和 Dreaming 结果纳入审计。
- 增加记忆禁用、重置、删除和运营侧查看。
- 将 Dreaming 改为 candidate diff + review，再写入长期记忆。

P1.5：

- 将记忆与权益/用量、客服处理联动，支持误写、误扣、补偿等内测支持场景。
- 评估 Markdown 文件向结构化存储迁移的时机。

## 13. 验收标准

- 新账号首次对话会生成六类 Context Files，不生成账号级 `HEARTBEAT.md`。
- Prompt trace 能看到 `Project Context` 和每个文件的 metadata。
- 两个账号的 Context Files、daily notes、MEMORY 不串线。
- 普通聊天成功后可异步沉淀 daily notes。
- Dreaming 能从 recent notes 生成长期记忆候选或更新结果。
- 用户明确纠正某条记忆后，后续对话不继续使用旧错误记忆。
- 运营侧至少能查看、禁用、重置账号记忆。

## 14. 待确认问题

- `USER.md` 是否需要结构化 front matter，例如 name、timezone、language。
- `MEMORY.md` 是否拆成 facts、preferences、projects、relationship sections。
- Dreaming 自动调度频率和灰度范围。
- 哪些记忆更新可以自动写入，哪些必须人工/策略审核。
- 敏感记忆过滤规则。
- 用户侧记忆管理入口形态。
