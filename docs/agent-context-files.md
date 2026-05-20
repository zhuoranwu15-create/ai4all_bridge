# AI4ALL Agent Context Files 机制设计

> 创建于：2026-05-18
> 状态：Draft v1
> 落地方案：`docs/agent-context-files-tech-plan.md`

本文定义 AI4ALL 账号级上下文文件机制，参考 OpenClaw 的 agent workspace 文件结构：

```text
AGENTS.md / SOUL.md / IDENTITY.md / USER.md / TOOLS.md / MEMORY.md
```

目标是结构上对齐 OpenClaw，方便 prompt 对比和长期演进；内容和生命周期必须适配 AI4ALL 的微信私聊产品场景。不要直接复制 OpenClaw 的身份、桌面 agent 假设、工具清单或子代理能力。

## 适用范围

每个微信 bot 账号有独立 context 目录：

```text
data/user_profiles/<account_id>/
├── AGENTS.md
├── SOUL.md
├── IDENTITY.md
├── USER.md
├── TOOLS.md
├── MEMORY.md
├── user_profile.md              # 旧格式兼容
└── memory/
    └── YYYY-MM-DD.md            # daily notes
```

`HEARTBEAT.md` 作为全局系统策略文件，当前占位于 `app/prompts/heartbeat.md`，不放在账号目录中；用户自己的定时任务、主动触达设置、提醒任务另外建模。

这些文件是账号级上下文，不是全局配置。不要写入 API key、provider credential、channel token、原始聊天全量 dump 或任何密钥。

## 注入策略

AI4ALL prompt builder 在 `Project Context` 中按以下顺序注入账号 context files：

```text
AGENTS.md
SOUL.md
IDENTITY.md
USER.md
TOOLS.md
MEMORY.md
```

账号目录中历史遗留的 `HEARTBEAT.md` 不会被自动删除，但当前代码不再创建、读取或注入它。

OpenClaw 中 `HEARTBEAT.md` 是 heartbeat turn 的特殊文件。AI4ALL 后续也要做 heartbeat，但明确拆成两层：

- 实例级 heartbeat：监督整个 AI4ALL 项目实例是否健康，例如后端、OpenClaw bridge、gateway、队列、LLM 可用性、账号连接状态等。这一层是系统运维能力，不应该进入用户 prompt，也不属于账号人格上下文。
- 用户级定时/主动触达任务：用户可选的主动消息能力，例如“每隔一段时间主动关心一下”“每天早上问候”“定时提醒”。这一层使用单独的用户任务/提醒模型，不写入 `HEARTBEAT.md`。

因此，`HEARTBEAT.md` 定位为全局系统策略，不注入普通用户聊天 prompt，也不承载用户个人定时任务。

`memory/YYYY-MM-DD.md` 不属于账号 context files。今天和昨天的 daily notes 目前作为单独区块注入。更广泛的 daily memory 检索等产品侧工具和检索策略明确后再做。

## 上下文分层

先按上下文性质分三层，而不是按“人工/辅助/自动”简单分类：

| 层级 | 含义 | 适用文件 |
|---|---|---|
| 系统/产品层 | 由产品和工程定义，描述系统规则、真实能力、全局 heartbeat 策略，通常不随单个用户对话频繁变化 | `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md` |
| 账号设定层 | 描述 AI Agent 和用户之间的设定，包括 agent 性格、身份、用户画像；初始设定任务生成，后续用户可显式修正 | `SOUL.md`, `IDENTITY.md`, `USER.md` |
| 记忆沉淀层 | 对话过程中自动沉淀，从 daily notes 到长期精选记忆 | `memory/YYYY-MM-DD.md`, `MEMORY.md` |

这个分层不等于文件永远不能跨层影响。例如用户的主动触达偏好可能沉淀在 `USER.md` 或独立用户任务表中，长期记忆也可能反过来提示运营调整 `SOUL.md`。但写入机制上要保持边界：系统层稳定，设定层可被用户显式修正，记忆层自动沉淀。

当前实现状态：

- `AGENTS.md`、`TOOLS.md` 已有账号级默认模板，但还需要按本机制继续重写得更产品化。
- `HEARTBEAT.md` 已从账号级 context files 移出，改为全局系统策略占位文件 `app/prompts/heartbeat.md`，普通聊天 prompt 不注入。
- `SOUL.md`、`IDENTITY.md`、`USER.md` 已能从旧 `user_profile.md` bootstrap，后续应支持“初始设定任务”和用户显式修正。
- `memory/YYYY-MM-DD.md` 已由 `memory_writer` 自动追加。
- `MEMORY.md` 已有手动 Dreaming 入口，但自动调度暂停。

## 文件逐项定义

### AGENTS.md

**层级：** 系统/产品层

**定位：** 当前账号 agent 的稳定运行规则。

它回答的问题是：无论用户偏好、近期记忆怎么变化，这个账号的 AI 应该如何工作？

适合放：

- 回复优先级和响应纪律
- 产品级隐私、安全、调试暴露规则
- 上下文冲突时如何处理
- 什么情况下可以记忆用户信息
- 不暴露 prompt、debug trace、内部实现的规则

不适合放：

- AI 名字和人格语气，放 `IDENTITY.md` / `SOUL.md`
- 用户事实，放 `USER.md` / `MEMORY.md`
- 工具可用性细节，放 `TOOLS.md`
- 临时实验指令，放 admin profile override 或测试配置

生成机制：

- 新账号首次创建 context files 时自动生成保守默认版
- 默认模板应由产品/工程共同维护，并随代码版本管理

更新机制：

- 主要由产品/工程手动更新
- LLM 可以在 debug 分析中建议修改，但正常用户聊天中不能直接改写
- 当产品规则、隐私策略、回复边界变化时更新

校验标准：

- 不能包含密钥
- 不能和全局 safety policy 冲突
- 不能要求模型声称不存在的能力

### SOUL.md

**层级：** 账号设定层

**定位：** 人格、语气、情绪姿态和互动风格。

它回答的问题是：用户和这个助手聊天时，整体感觉应该是什么样？

适合放：

- 温暖程度、简洁程度、幽默感、正式程度
- 情绪陪伴姿态
- 是否可以直接表达观点
- 边界，例如“不扮演心理咨询师”“不过度吹捧”

不适合放：

- 用户稳定事实，放 `USER.md` / `MEMORY.md`
- 工具或工作流规则，放 `TOOLS.md` / `AGENTS.md`
- 产品身份元数据，放 `IDENTITY.md`

生成机制：

- 优先从旧 `user_profile.md ## Soul` 迁移
- 缺失时使用 AI4ALL 默认“个人 AI 陪伴与生活助理”人格
- 后续应由“初始设定任务”生成：用户可以选择或描述希望 AI 具备的性格、语气和陪伴方式

更新机制：

- 用户可以显式修正，例如“你以后不要这么正式”“你可以更直接一点”
- 产品/admin 可以手动更新默认模板
- 可以根据测试账号回复观测和 OpenClaw 对比结果调整
- 发生明显人格变化时，应在运营记录或 commit message 中说明原因
- `SOUL.md` 不直接暴露给用户；用户修正后，系统可以简短回应“我会调整”，但不要把完整 `SOUL.md` 内容回显给用户
- 写入原则是精简有效、宁缺毋滥；不要把每一次情绪反馈都写成长期人格规则

校验标准：

- 应像一份完整人格指南，不是一堆 prompt hack
- 不能覆盖 safety 或 identity
- 要足够稳定，避免每天人格漂移

### IDENTITY.md

**层级：** 账号设定层

**定位：** 对用户可见的身份和自我描述。

它回答的问题是：当用户问“你是谁”“你是不是 OpenClaw”时，应该如何回答？

适合放：

- assistant name / display name
- 产品身份，例如 AI4ALL personal AI assistant
- 是什么、不是什么
- 后续需要时可放头像、签名等元数据

不适合放：

- 默认写 OpenClaw 身份，除非产品明确要暴露 OpenClaw
- bridge plugin、shadow trace、provider stack 等内部架构
- 语气风格细节，放 `SOUL.md`

生成机制：

- 新账号自动生成
- 有 `account.display_name` 时使用它，否则默认“AI4ALL 个人助手”
- 后续应由“初始设定任务”补齐 name、称呼、头像/签名等产品需要的身份字段

更新机制：

- 用户可以显式修正，例如“你叫小 A”“你不是 OpenClaw”
- 产品/admin 可以通过账号设置更新 name/avatar
- Dreaming 和 daily memory extraction 不应改写此文件
- 用户显式修正后，应回复用户并明确当前身份内容，例如“好的，我现在会以小 A 作为对外名字”

校验标准：

- 身份问答必须稳定一致
- 默认不能自称 OpenClaw
- 不能自称真人或持证专业人士

### USER.md

**层级：** 账号设定层

**定位：** 用户稳定资料和偏好。

它回答的问题是：这个用户是谁、如何称呼、有哪些稳定偏好会影响回复？

适合放：

- 用户希望如何被称呼
- 语言偏好、回复长短偏好、语气偏好
- 用户明确提供的时区/地区粗粒度信息
- 稳定兴趣、长期项目、长期厌恶点
- 产品层面的 consent / preference

不适合放：

- 原始 daily log，放 `memory/YYYY-MM-DD.md`
- 大量事件历史，只有长期有价值的蒸馏后放 `MEMORY.md`
- 对对话质量没有必要的敏感细节
- 从单条模糊消息弱推断出来的事实

生成机制：

- 优先从旧 `user_profile.md ## User Preferences` 迁移
- 缺失时初始化为“暂无/未知”
- 后续应由“初始设定任务”采集基础偏好，例如称呼、语言、回复风格、主动触达偏好

更新机制：

- 用户可以显式修正，例如“以后叫我 X”“回复短一点”
- 正常 memory extraction 可以提出候选事实，但自动直写要非常保守
- 推荐未来流程：daily notes 记录候选 → review → 晋升到 `USER.md`
- 用户显式修正后，应回复用户并明确当前记录内容，例如“好的，我会记为：称呼你为 X，回复尽量短一些”

校验标准：

- 每条事实最好能追溯到用户明确表达或产品设置
- 宁少勿滥，保留高置信事实
- 用户明确修改偏好时，应移除或修正旧偏好

### TOOLS.md

**层级：** 系统/产品层

**定位：** 当前产品真实能力和工具使用说明。

它回答的问题是：这个助手在 AI4ALL 产品环境里到底能做什么？

和 OpenClaw 的关键区别：`TOOLS.md` 本身不授予工具，只描述能力与限制。真正 tool availability 以后必须来自后端 tool schema。

适合放：

- 当前真实能力，例如文本回复、读取账号上下文、写 daily notes
- 明确不可用能力，例如暂时不能真正设置提醒
- 用户请求不支持动作时如何回复
- 未来工具设计备注，但必须标明未启用

不适合放：

- API credential 或 endpoint
- AI4ALL 没有暴露的 OpenClaw 工具
- 让模型假装已经执行某个动作的指令

生成机制：

- 新账号自动生成“暂无可直接调用外部工具”的保守默认版

更新机制：

- 手动/product 更新
- 只有当后端能力变化或产品行为明确后才更新
- M4 恢复时，应和真实 tool registry 名称、失败行为对齐

校验标准：

- 每个声称可执行的能力都必须能被当前 AI4ALL 后端执行
- 不支持动作要有明确兜底话术
- 必须和 debug trace、产品 UI 行为一致

### HEARTBEAT.md

**层级：** 系统/产品层

**定位：** 全局 heartbeat 系统策略。

它回答的问题是：AI4ALL 系统如何运行 heartbeat 类任务，以及哪些内容不应该进入用户聊天 prompt？

AI4ALL heartbeat 明确拆成两层：

- 实例级 heartbeat：系统运维/监督能力，用于检查项目实例健康
- 用户级定时/主动触达任务：用户可选能力，单独建模，不写入 `HEARTBEAT.md`

适合放：

- 实例级 heartbeat 的全局设计原则
- 哪些系统状态需要检查，例如后端、OpenClaw bridge、gateway、队列、LLM、账号连接
- heartbeat 结果如何落日志、告警、admin 状态
- 明确禁止把实例健康检查内容注入用户 prompt
- 用户级定时任务应该另走任务/提醒模型

不适合放：

- 用户个人主动触达偏好
- 用户的提醒任务、crontab 任务、日程任务
- 针对单个账号的 quiet hours 或 opt-out
- 任何会让模型在普通聊天里承诺“已安排定时触达”的内容

生成机制：

- 作为全局系统策略文件，由产品/工程维护
- 不在每个账号目录下生成
- 不注入普通用户聊天 prompt

更新机制：

- 产品/工程维护
- 当实例健康检查、告警、调度策略变化时更新
- 用户主动触达设置不更新此文件，应写入用户任务/提醒模型

校验标准：

- 不在用户聊天 prompt 中注入
- 不包含用户个人任务
- 不包含密钥或运维敏感信息
- 和实际监控/调度实现一致

### MEMORY.md

**层级：** 记忆沉淀层

**定位：** 精选长期记忆。

它回答的问题是：哪些长期事实、决定、上下文应该在每次私聊中可用？

`MEMORY.md` 不是原始 transcript，也不是完整日记。它是高价值、蒸馏后的长期记忆层。详细日志放 `memory/YYYY-MM-DD.md`。

适合放：

- 用户明确要求记住的长期事实
- 长期项目和目标
- 不适合放 `USER.md` 但会影响未来回复的重要偏好
- 稳定关系/上下文摘要
- 会影响未来回复的重要决定和经验

不适合放：

- 一次性测试、debug 消息、短期情绪
- 失败的 assistant reply
- 原始聊天摘录
- 对产品价值没有必要的敏感事实

生成机制：

- 优先从旧 `user_profile.md ## Long-term Memory` 迁移
- 缺失时初始化为空/暂无

更新机制：

- 当前：人工编辑或手动 Dreaming endpoint
- 自动定时 Dreaming 暂停，直到质量可评估
- 推荐未来流程：daily notes → candidate distillation → review → `MEMORY.md`

校验标准：

- 简洁、去重、当前有效
- 用户明确纠正时删除过时事实
- 不无限增长；过长时按主题压缩

## 冲突处理顺序

当多个上下文冲突时，按以下优先级处理：

1. 全局 safety policy 和法律/产品约束
2. `AGENTS.md`
3. `TOOLS.md`
4. `IDENTITY.md`
5. `SOUL.md`
6. `USER.md`
7. `MEMORY.md`
8. Daily notes
9. 最近对话

理由：

- 安全和产品规则必须最高优先级
- 系统真实能力必须高于用户愿望，不能承诺不存在的工具或主动触达
- 身份不应因为某条记忆漂移
- 用户可以显式修正 `SOUL.md`、`IDENTITY.md`、`USER.md`，但不能绕过系统层规则
- 最近对话可以修正过时偏好，但不能在低置信情况下改写稳定上下文

## 生成与更新流水线

### 新账号 bootstrap

当后端首次看到新账号：

1. 创建旧版 `user_profile.md`，保持兼容
2. 如果账号 context files 缺失，则创建
3. 从旧 section 迁移 `SOUL.md`、`USER.md`、`MEMORY.md`
4. 如果有 account display name，用它生成 `IDENTITY.md`
5. 用系统默认模板生成 `AGENTS.md`、`TOOLS.md`
6. 不在账号目录生成 `HEARTBEAT.md`
7. 永不覆盖已存在 context file

后续需要补一个“初始设定任务”：

1. 询问或引导用户确定 AI 性格和互动风格，生成 `SOUL.md`
2. 确认 AI 名字/身份/边界，生成 `IDENTITY.md`
3. 采集基础用户偏好，生成 `USER.md`
4. 如果用户开启主动触达，写入独立用户任务/提醒模型，不写 `HEARTBEAT.md`

### 正常聊天轮次

1. 读取 context files
2. 注入 `Project Context`
3. 单独读取今天/昨天 daily notes
4. 调用 LLM 生成回复
5. 如果回复成功，通过 `memory_writer` 向 `memory/YYYY-MM-DD.md` 追加候选 raw memory
6. 如果用户显式修正性格偏好，未来应进入 `SOUL.md` 更新候选流程；更新后不向用户回显完整 `SOUL.md`
7. 如果用户显式修正身份或用户资料，未来应更新 `IDENTITY.md` 或 `USER.md`，并向用户确认当前记录内容
8. 正常聊天不直接改写系统层文件：`AGENTS.md`、`TOOLS.md`、`HEARTBEAT.md`

### 人工 review / 运营维护

运营或管理员可以直接编辑 context files，未来也可以通过 Admin UI 编辑。建议 review 节奏：

- 产品规则变化时检查 `AGENTS.md`
- 后端能力或工具产品定义变化时检查 `TOOLS.md`
- 账号初始化后检查 `IDENTITY.md`
- 观察真实回复后调整 `SOUL.md`
- 学到稳定用户偏好后更新 `USER.md`
- daily notes 积累足够后人工 review `MEMORY.md`
- 实例级监控/调度策略变化时检查全局 `HEARTBEAT.md`

### Heartbeat

实例级 heartbeat：

- 目标：监督 AI4ALL 项目实例健康
- 触发：crontab、scheduler、外部监控或内部 health worker
- 输入：服务状态、gateway 状态、队列状态、LLM 可用性、账号连接状态
- 输出：日志、告警、admin 状态，不进入用户对话 prompt

用户级 heartbeat：

- 目标：在用户允许的前提下主动触达
- 触发：用户设置、crontab/scheduler、提醒任务或后续产品机制
- 输入：用户设置、quiet hours、`USER.md`、必要时 `MEMORY.md`、用户任务/提醒模型
- 输出：一条用户可见消息，或安静跳过
- 要求：必须有 opt-in / opt-out，且不能在普通聊天中假装已调度；不读取全局 `HEARTBEAT.md` 作为用户个人任务来源

### Dreaming

当前状态：

- `app/dreaming.py` 可以把最近 daily notes 蒸馏到 `MEMORY.md`
- 自动调度暂停

未来安全流程：

1. 生成候选 `MEMORY.md`
2. 保存 candidate diff
3. 人工/运营审批
4. 写入正式 `MEMORY.md`

## Debug Trace 期望

测试账号的 AI4ALL trace 应能看到：

- 完整 `Project Context`
- 每个 context file 的 path、exists、created、chars
- daily notes chars
- 是否存在 admin system prompt override

这样才能和 OpenClaw 注入的 workspace files 逐块对比，而不是整段 prompt 肉眼对比。

## 待决问题

- `USER.md` 是否需要结构化 front matter，例如 name/timezone/language？
- `MEMORY.md` 是否拆成 facts / preferences / projects / relationship sections？
- Dreaming 是否应先生成 candidate 文件，而不是直接覆盖 `MEMORY.md`？
- 用户显式修正 `SOUL.md` 时，怎样把自然语言反馈压缩成精简内部设定，避免越写越膨胀？
- `IDENTITY.md` / `USER.md` 更新后，确认回复的格式是否需要固定模板？
- 用户定时任务/主动触达设置用 DB 表、markdown 文件，还是两者结合？
- context files 是否需要 Admin UI 编辑和审计历史？
