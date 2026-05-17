# Agent 编排能力建设路线图

> 本文档持续维护，记录 AI4ALL 对标 OpenClaw 编排能力的演进计划、关键设计决策和讨论结论。
>
> 创建于：2026-05-17
> 最后更新：2026-05-17

---

## 背景：为什么要做这件事

AI4ALL 项目起步阶段的 LLM 调用极为简单：

```python
# 当前 llm.py 的 prompt 构建（3 行拼接）
prompt = system_prompt or settings.llm_default_prompt
if user_profile:
    prompt = f"{prompt}\n\n以下是 user_profile.md：\n{user_profile}"
if style:
    prompt = f"{prompt}\n当前风格：{style}。"
```

没有结构，没有记忆自动化，没有工具，没有 context 管理。

OpenClaw 的 agent 编排系统是一套完整的 AI 操作系统，其 prompt 由 17 个精心装配的区块构成，有三层记忆架构、完整工具体系、可插拔的 context engine。

**我们的目标不是复制 OpenClaw，而是学习其设计哲学，在我们的场景下达到 80-90 分的状态：**

> 一个有完整记忆、有自主工具能力、有结构化人格、能在长对话中保持上下文连贯的 AI 陪伴系统。

---

## OpenClaw 参考架构摘要

### 17-Block Prompt 装配顺序

OpenClaw 的 system prompt 按固定顺序装配以下区块：

| # | 区块 | 说明 |
|---|------|------|
| 1 | Tooling | 工具使用指引 + 运行时工具描述（JSON schema） |
| 2 | Execution Bias | 行动偏好：act in-turn, continue until done, recover from weak results |
| 3 | Safety | 护栏提醒 |
| 4 | Skills list | 可用技能摘要 + 文件路径（agent 按需 read） |
| 5 | OpenClaw Control | 网关配置工具使用说明 |
| 6 | Self-Update | 仅用户请求时触发 |
| 7 | Workspace | 工作目录路径 |
| 8 | Documentation | 文档路径 |
| — | **← 缓存边界 →** | 以上稳定，以下 volatile |
| 9 | Project Context | AGENTS.md / SOUL.md / TOOLS.md / IDENTITY.md / USER.md / MEMORY.md |
| 10 | Sandbox | 若启用 |
| 11 | Current Date & Time | 仅 timezone（cache stable，不到时分） |
| 12 | Output Directives | 附件/语音/标签格式 |
| 13 | Heartbeats | 若启用 |
| 14 | Runtime metadata | host / OS / model / thinking level |
| 15 | Reasoning | 可见性 + toggle 提示 |
| 16 | Provider contributions | stable prefix（上方）+ dynamic suffix（下方） |

**缓存边界** 是关键设计：稳定内容在前（可被 prefix cache 复用），volatile 在后（每次变化不破坏缓存）。

### 三层记忆架构

```
MEMORY.md          ← 长期记忆（精选，每次 session 注入）
memory/YYYY-MM-DD  ← 日记（按需检索，不全量注入）
DREAMS.md          ← Dreaming 输出（人类可读，非直接注入）

自动化流程：
  对话结束 → LLM 提炼 → 追写 memory/YYYY-MM-DD.md
  每日凌晨 → Dreaming 蒸馏 → 晋升重要条目到 MEMORY.md
```

### 工具体系

Agent 可调用工具（部分）：

| 类别 | 工具 |
|------|------|
| 记忆 | memory_search（混合检索）, memory_get |
| 文件 | read, write, edit |
| 执行 | exec, bash |
| 网络 | fetch, web_search |
| 调度 | cron（定时任务） |
| 子代理 | subagent spawn/deliver |

### Context Window 管理

- 自动 compaction：对话历史超出 token 预算时自动摘要
- Memory Flush：compaction 前先让 agent 把重要内容写入记忆文件
- 不同摘要可用不同（更便宜）的模型
- Successor transcript：compaction 后建新 transcript，旧的存档

---

## 我们的实现计划

### 总体分层

```
M1  结构化 Prompt 装配     ← 地基，改变所有后续工作的基础
M2  记忆自动化             ← 最大体验提升（分 2A 基础层 + 2B 深化层）
M3  Context 智能压缩       ← 支撑长对话的基础设施
M4  工具体系               ← 架构跃迁，从"聊天机器人"到"agent"
```

依赖关系：M1 → M2 → M3，M4 设计可与 M2/M3 并行但实现在 M1 之后。

---

### Milestone 1：结构化 Prompt 装配

**状态：** 📋 待开始

**目标：** 把 3 行拼接改为有明确区块、有顺序、有大小控制的 prompt builder。

**新建文件：** `app/prompt_builder.py`

**我们的 12 个区块（对标 OpenClaw 17 块，适配微信场景）：**

**稳定区（缓存边界之上）—— session 间几乎不变，可被 prefix cache 复用：**

| # | 区块 | 来源 | 上限 | 对应 OpenClaw |
|---|------|------|------|--------------|
| 1 | **Tooling** | 运行时生成 tool schema | - | Block 1 |
| 2 | **Execution Bias** | 固定文本 | - | Block 2 |
| 3 | **Safety** | 全局固定文本（可定期更新） | 1000 chars | Block 3 |
| 4 | **Identity** | account.display_name + 角色定义 | 500 chars | Block 9 (IDENTITY.md) |
| 5 | **Soul** | user_profile.md `## Soul` 节 | 3000 chars | Block 9 (SOUL.md) |
| 6 | **Skills** | 硬编码能力列表（初期），后期动态加载 | 1000 chars | Block 4 |

**← 缓存边界 →**

**Volatile 区（每次 session 可变）：**

| # | 区块 | 来源 | 上限 | 对应 OpenClaw |
|---|------|------|------|--------------|
| 7 | **User** | user_profile.md `## User Preferences` 节 | 2000 chars | Block 9 (USER.md) |
| 8 | **Long-term Memory** | user_profile.md `## Long-term Memory` 节 | 3000 chars | Block 9 (MEMORY.md) |
| 9 | **Daily Notes** | memory/今天.md + memory/昨天.md（若存在） | 2000 chars | Block 9 (daily notes) |
| 10 | **System Prompt Override** | profiles.system_prompt（运营覆盖，最高优先级） | 不限 | Block 16 |
| 11 | **Output Directives** | 固定微信格式规范 + profiles.style | 500 chars | Block 12 |
| 12 | **Runtime** | 当前日期 + 模型名 | - | Block 11 + 14 |

> **Block 1 Tooling：** M4 前为空占位，M4 后注入工具 schema。格式待 M4 时确定（倾向 markdown 描述 + JSON schema 分段）。
>
> **Block 3 Safety：** 全局一份固定文本，存放于 `app/prompts/safety.md`（或 config 字符串），可定期更新。运营层覆盖通过 Block 10（System Prompt Override）实现，不改 Safety 本身。
>
> **Block 6 Skills：** 初期硬编码"我能做的事"列表（日程查询、提醒设置、天气搜索等）。预留 `skills: list[str]` 接口，后期替换为动态文件读取，接口不变。
>
> **缓存边界说明：** 当前 provider（DeepSeek）prefix cache 行为待确认。结构上稳定内容在前是好工程实践，cache 收益后期验证。

**关键原则：**
- 每个区块独立上限，超出截断并记 warning
- 区块缺失时优雅降级（不崩溃）
- Block 10 Override 存在时覆盖 Block 3-6（Safety 除外）的内容，Safety 不可被运营覆盖

**新建文件：**
- `app/prompt_builder.py` — PromptBuilder 类，12 个 build_block_* 方法
- `app/prompts/safety.md` — 全局安全护栏文本

**验收：**
- 单元测试覆盖每个区块的 truncation 行为
- `app/llm.py` 中 3 行拼接完全替换为 `PromptBuilder`
- Block 1（Tooling）在空工具列表时输出空字符串（不注入），不崩溃
- Block 3（Safety）内容可独立更新 `safety.md` 而无需改代码

---

### Milestone 2A：基础记忆自动化

**状态：** 📋 待开始（依赖 M1）

**目标：** 每次对话结束后，自动提炼并追写当日记忆文件。

**文件结构变更：**

```
data/user_profiles/<account_id>/
├── user_profile.md              ← 长期记忆（人工+Dreaming维护）
└── memory/
    ├── 2026-05-17.md            ← 自动生成，每条对话后追加
    └── 2026-05-18.md
```

**实现位置：** `app/memory_writer.py`（新建，异步调用）

**触发时机：** `openclaw_turn` 写入 outbound 消息后，后台 `asyncio.create_task`

**提炼 Prompt（草稿）：**

```
从以下对话片段中，提取值得长期记住的信息。
包括：用户明确说出的偏好、重要事件、情绪状态变化、
对我（AI）的反馈、明确的个人信息。
每条用 - 开头，一行一条，语言简洁。
如果没有值得记住的内容，输出 NOTHING。

对话：
{recent_turns}
```

**Prompt Builder 配合调整：** M1 的区块 6（Daily Notes）加载今天 + 昨天的 memory/*.md。

**验收：**
- 发一条消息后，memory/YYYY-MM-DD.md 有内容追写
- 第二天对话时，昨日笔记自动进入 prompt
- 提炼结果为 NOTHING 时不写入文件

---

### Milestone 2B：Dreaming（记忆蒸馏）

**状态：** 📋 待开始（依赖 2A）

**目标：** 每日定时对 daily notes 做蒸馏，晋升重要条目到 Long-term Memory。

**实现位置：** `app/dreaming.py`（新建）

**触发方式：** APScheduler 或系统 cron（每日凌晨 3:00）

**蒸馏逻辑（简化版，保留 OpenClaw 的核心机制）：**

```
输入：最近 7 天的 memory/YYYY-MM-DD.md
输出：更新后的 user_profile.md ## Long-term Memory 节

Prompt 草稿：
"从以下 7 天的对话摘要中，提炼出应该长期记住的核心信息。
要求：
- 精炼（不超过 20 条）
- 去重，合并相似条目
- 去掉过时或已无关的信息
- 重点保留：用户的偏好、习惯、重要事件、对 AI 的明确要求
直接输出新的 Long-term Memory 列表，每条用 - 开头。"
```

**验收：**
- 手动触发蒸馏后，user_profile.md 的 Long-term Memory 节被更新
- 蒸馏有日志，可追查

---

### Milestone 3：Context 智能压缩

**状态：** 📋 待开始（依赖 M2A）

**目标：** 把硬截断（取最近 N 条）替换为智能压缩。

**触发条件（任一满足）：**
- 对话轮次 > 30 轮
- 估算 token 数 > 阈值（待定，约 8000 tokens）

**压缩策略：**

```
1. 保留最近 8 条完整消息（"尾部"）
2. 对更早的消息调用 LLM 做摘要
3. Memory Flush：压缩前先触发一次 2A 的记忆提取
4. 摘要作为 system 消息插入 history 头部
```

**DB 变更：**

```sql
ALTER TABLE sessions ADD COLUMN compaction_summary TEXT;
ALTER TABLE sessions ADD COLUMN compacted_at TEXT;
ALTER TABLE sessions ADD COLUMN compacted_upto_message_id INTEGER;
```

**验收：**
- 超过 30 轮的对话能正常继续（不报 token 超限）
- 压缩摘要可在 Admin UI 查看
- 压缩前 Memory Flush 有日志确认

---

### Milestone 4：工具体系

**状态：** 📋 待开始（依赖 M1，设计可并行）

**目标：** 给 LLM 赋予调用工具的能力，从"文字生成"到"有行动力的 agent"。

**首批工具（最小可用集）：**

| 工具名 | 功能 | 对应 OpenClaw |
|--------|------|-------------|
| `memory_get` | 读取某天的 daily notes | memory_get |
| `memory_search` | 关键词搜索所有 daily notes | memory_search（简化版） |
| `get_datetime` | 获取当前精确日期时间 | session_status |
| `set_reminder` | 设置一条定时提醒（写 DB，推送机制 TBD） | cron |

**工具调用 pipeline：**

```
LLM 第一次调用（prompt + history + tools schema）
  ↓ 返回 tool_call？
    是 → 执行工具 → 将结果追加 messages → LLM 第二次调用 → 最终回复
    否 → 直接返回
```

**目录结构：**

```
app/
├── tools/
│   ├── __init__.py          ← 工具注册表 + schema 导出
│   ├── memory_tools.py      ← memory_get, memory_search
│   ├── time_tools.py        ← get_datetime
│   └── reminder_tools.py    ← set_reminder
└── tool_runner.py           ← 执行 + 结果格式化
```

**Prompt Builder 配合：** M1 区块 1（Tooling）加载工具 schema 描述。

**验收：**
- 用户说"今天几号了"，AI 调用 get_datetime 而非凭记忆猜
- 用户说"帮我记住…"，memory_get 可验证已写入
- 工具调用链路在日志中可追查

---

## 预期完成后的能力评估

| 能力维度 | OpenClaw | AI4ALL 完成后 | 备注 |
|---------|---------|--------------|------|
| Prompt 结构化装配 | ██████████ 100% | ████████ 85% | 缺 cache boundary 精细控制 |
| 长期记忆（自动） | ██████████ 100% | ████████ 80% | 缺向量检索，无 wiki 层 |
| 短期记忆（daily notes） | ██████████ 100% | ████████ 80% | 缺 REM/Light 加权 |
| Dreaming 蒸馏 | ██████████ 100% | ██████ 70% | 有机制，无评分权重系统 |
| Context 压缩 | ██████████ 100% | ████████ 80% | 缺 provider 插件化 |
| 工具体系（基础） | ██████████ 100% | ██████ 60% | 首批 4 个工具 |
| Skills 系统 | ██████████ 100% | ██ 20% | 暂不做 |
| 多 agent 编排 | ██████████ 100% | ░░ 0% | 阶段外 |

**综合估算：80% 区间，完全满足当前阶段产品目标。**

---

## 大致时间线

```
Week 1   M1（Prompt 装配）
Week 2   M2A（基础记忆自动化）
Week 3   M3（Context 压缩）+ M4 工具设计
Week 4   M4 前两个工具（memory_get / memory_search）
Week 5   M2B（Dreaming）+ M4 其余工具
Week 6   联调、测试、文档
```

---

## 关键设计决策记录

> 每次讨论后在此追加，格式：日期 + 决策内容 + 理由

### 2026-05-17：不追 OpenClaw 多 agent 编排

**决策：** 当前阶段不实现子代理派发（subagent spawn）和跨 agent 记忆共享。

**理由：** 产品场景是一对一私聊陪伴，不需要并行任务拆解。先把单 agent 的记忆、工具、人格做扎实。

---

### 2026-05-17：记忆写入使用异步后台任务，不阻塞回复

**决策：** memory_writer 用 `asyncio.create_task` 在回复发出后异步执行，失败不影响主流程。

**理由：** 用户等待时间是核心体验，记忆提炼可以比回复慢，失败可以下次补写。

---

### 2026-05-17：Dreaming 简化版不实现 6 维度加权评分

**决策：** 蒸馏使用 LLM 直接判断重要性，不实现 OpenClaw 的 frequency × relevance × diversity × recency × consolidation × richness 评分系统。

**理由：** 评分系统需要大量信号积累才有效，早期用 LLM 判断足够用。后期数据量上来可以迭代。

---

### 2026-05-17：Safety 区块全局固定 + 运营覆盖分离

**决策：** Block 3（Safety）使用全局固定文本（`app/prompts/safety.md`），可定期更新。运营针对特定账号的覆盖通过 Block 10（System Prompt Override）实现。Safety 本身不可被运营 Override 覆盖。

**理由：** 安全边界应当是全局一致的，不能由单个运营人员绕过。运营层覆盖聚焦于人格/风格调整，而非安全策略。

---

### 2026-05-17：Skills 区块初期硬编码，预留动态升级接口

**决策：** Block 6（Skills）初期使用硬编码能力列表（包含日程查询、提醒、搜索等预期能力描述）。`PromptBuilder` 接受 `skills: list[str]` 参数，初期调用方传入硬编码列表，后期替换为文件/DB 动态加载，接口不变。

**理由：** 硬编码足以启动，但不能把列表内嵌进 `prompt_builder.py`，否则后期升级需改核心文件。接口隔离保证兼容性。

---

### 2026-05-17：Prompt 区块从 9 扩展到 12 块，对标 OpenClaw

**决策：** 将初始 9 块方案升级为 12 块，新增 Tooling（Block 1）、Safety（Block 3）、Skills（Block 6），将 Output Directives 从 Style 中独立出来，Runtime 覆盖原 Date/Format。

**理由：** 日程管理、提醒、搜索等业务场景需要工具体系支撑，Tooling 必须从设计阶段就预留；Safety 和 Skills 是 OpenClaw 架构中有明确用途的块，早期缺失会导致后续补入困难；Output Directives 独立有助于明确微信格式约束与人格风格的边界。

---

## 待讨论事项

> 记录尚未决定、需要进一步讨论的问题

- [ ] **M2A 触发时机：** 每条消息后提炼，还是每 N 条提炼一次？每条更及时但 LLM 调用成本翻倍。
- [ ] **memory_search 的实现：** 第一版用关键词（grep），还是直接上向量检索（sqlite-vec）？
- [ ] **工具调用是否对用户透明：** 当 AI 调用工具时，用户能看到"正在查找记忆…"之类的提示吗？
- [ ] **Dreaming 的调度方式：** APScheduler 内嵌进程，还是独立 cron job？
- [ ] **Daily notes 的存储格式：** 纯 markdown 追加，还是有结构（YAML front-matter + 内容）？

---

## 参考资料

- OpenClaw 文档路径：`/opt/homebrew/lib/node_modules/openclaw/docs/`
- 关键文档：
  - `concepts/agent-loop.md` — 完整 agent 生命周期
  - `concepts/memory.md` — 三层记忆架构
  - `concepts/agent-workspace.md` — workspace 文件清单
  - `plugins/hooks.md` — 30+ hook 点完整清单
  - `concepts/session.md` — session 隔离策略
