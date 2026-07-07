# System Prompt 体积优化：现状梳理与建议

> 临时文档。背景：system prompt 逐步变长，评估在不影响效果的前提下有多少优化空间。
> 以测试账号 `aid_806382741` 为例，数据来自 `/debug/accounts/{id}/prompt-preview` 接口的实际
> 组装结果（2026-07-05 抓取）。本文只做现状梳理 + 建议，未开始实现。

---

## 1. 总体数字

以 `aid_806382741` 当前真实状态（有 mission、有 carryover、无 daily_notes、13 个可用工具）为例：

| 项 | chars | 估算 tokens（len/1.5，项目口径见 `app/context_window.py:19`） |
|---|---|---|
| system prompt 全文 | 7261 | ~4841 |
| 13 个工具的 JSON schema（走 API `tools` 参数，不在 system prompt 字符串里，但同样占用请求体和上下文） | 7037 | ~4691 |
| **两者合计（每轮固定开销，尚未含对话历史和当前消息）** | **14298** | **~9532** |

也就是说，**工具 schema 的体积已经和 system prompt 正文基本相当**——只看 system prompt 字符串会
漏掉一半的真实开销，这是本次梳理的第一个发现。

---

## 2. System prompt 按 block 拆解（`prompt_builder.py` 口径）

| block | chars | section | 说明 |
|---|---|---|---|
| project_context | 2955 | volatile | AGENTS+TOOLS+SOUL+IDENTITY+USER+MEMORY+MISSION 拼成的**一整块**字符串 |
| safety | 2066 | stable | 内容安全守则，最大的单一 block |
| skills | 559 | stable | 当前示例账号只有 1 个 skill（weather），随 skill 数量线性增长 |
| carryover_summary | 473 | volatile | 会话延续摘要 |
| tooling | 320 | stable | 本轮可用工具名单（仅名字，非 schema） |
| factual_discipline | 312 | stable | 事实核实纪律 |
| context_evidence | 256 | stable | 上下文/证据纪律 |
| output_directives | 165 | stable | 微信回复呈现规则 |
| agent_self_state | 77 | volatile | 当下关系与心境 |
| runtime | 60 | volatile | 当前时间 |

`project_context` 的内部构成（来自 `agent_context.files`）：

| 文件 | chars | 全局/账号级 |
|---|---|---|
| TOOLS.md | 1779 | **全局**（`data/system/TOOLS.md`，所有账号共用） |
| AGENTS.md | 470 | **全局**（`data/system/AGENTS.md`，所有账号共用） |
| SOUL.md | 250 | 账号级，人格设定，改动少 |
| IDENTITY.md | 143 | 账号级，改动少 |
| MISSION.md | 96 | 账号级，改动少 |
| USER.md | 77 | 账号级，改动少 |
| MEMORY.md | 14 | 账号级，**改动最频繁**（每轮后 memory_writer 可能更新） |

---

## 3. 发现一：TOOLS.md 的工具说明和工具自身 JSON schema 的 description 大量重复

每个工具在两个地方同时向模型说明"什么时候调用、边界是什么"：

1. 工具自己的 JSON schema `description` 字段（走 API `tools` 参数）。
2. `TOOLS.md`（`data/system/TOOLS.md`）里对应的 `## xxx工具` 小节（走 system prompt 的
   `project_context` block）。

**具体例子（`update_proactive_message_settings`）**：

- schema description（372 chars，节选）：
  > "更新当前用户的主动消息设定。**收到变更指令后直接调用本工具，无需向用户确认，不可口头声称
  > 已完成。** 触发场景（用户说下列任何一句，立即调用，不要问『确定吗』）：……"
- `TOOLS.md` `## 主动消息设定工具`（327 chars，节选）：
  > "用户表达主动触达偏好（频次/时段/暂停等）时**立即调用，无需向用户确认**；设定变更只有调用
  > 工具才真正生效，不能仅凭口头声称完成。"

同一条规则（"立即调用、不要确认、不能只口头声称完成"）在两个地方各说一遍。`create_commitment`
和 `create_reminder` 之间的互斥规则也是同样模式：

- `create_commitment` schema description：*"不要用于用户已经明确要求提醒的场景（那种情况用
  create_reminder，不要两个都调）。"*
- `TOOLS.md` `## 跟进记录工具`：*"用户已经明确要求"提醒我"时改用 create_reminder，两者不要同时
  调用同一件事。"*

**反例（证明这个模式在本仓库是可以避免的）**：`mission_status` / `record_mission_moment` 两个
工具在 `TOOLS.md` 里**完全没有对应小节**——触发条件、边界、副作用说明全部只写在各自的 schema
`description` 里（分别 97 / 125 chars）。这两个工具照常工作，没有因为缺少 `TOOLS.md` 散文说明而
出问题。说明"trigger 条件只在 schema 里说一遍"这个模式在当前代码库里已经有先例，不是要发明新
约定。

**粗估可压缩量**：`TOOLS.md` 里和 schema description 明显重复的小节——提醒工具(225)、跟进记录
工具(219)、主动消息设定工具(327)，共约 770 chars，占 `TOOLS.md` 全文（1779 chars）的 43%。
（内容邀请回复工具 165 chars 结构上也是同一模式，但因为对应两个工具目前不在这个账号的可用列表里，
没法直接对比 schema，先不计入这次估算。）

图片理解能力(161)、不可承诺能力(128)这两节**不是**某个工具的调用说明，是通用能力边界声明，不对应
任何 schema，不属于这个重复模式，不建议动。

---

## 4. 发现二：全局静态文件和高频变动文件被拼进同一个 block，可能影响 prompt cache 命中

`prompt_builder.py:475-489` 的 `_build_project_context()` 把 `AGENTS.md`、`TOOLS.md`（全局，几乎
不变）和 `SOUL.md`/`IDENTITY.md`/`USER.md`/`MISSION.md`（账号级，低频变）以及 `MEMORY.md`
（账号级，**每轮后可能被 memory_writer 改写**）拼成**同一个字符串**，作为一个 `ContextBlock`
（`section=volatile`）。

`app/user_profiles.py:556-587` 的 `read_agent_context()` 其实已经按 `AGENTS/TOOLS/SOUL/
IDENTITY/USER/MEMORY/MISSION` 分好了 key，粒度是现成的——问题只在 `_build_project_context()`
把它们又拼回了一个整体。

影响：只要 `MEMORY.md` 因为这轮对话被更新一个字，整个 `project_context` block 的文本就和上一轮
不再逐字节相同。如果 LLM 供应商一侧有 prompt / context 前缀缓存（按供应商计费口径可能有折扣），
这种拼接方式会让 `AGENTS.md`/`TOOLS.md`/`SOUL.md` 等本来没变的内容也跟着"看起来变了"，无法参与
缓存命中。

**这一条需要先确认**：当前使用的模型（示例账号是 `deepseek-v4-pro`）在 DeepSeek API 侧是否有
前缀缓存机制和相应计费优惠。如果没有，这一条纯粹是理论优化，实际收益为零；如果有，拆分 block 的
收益可能比第 3 条的字符削减更大，因为 `AGENTS.md`+`TOOLS.md` 两个全局文件（2249 chars）理论上
可以对所有账号、几乎每一轮都命中缓存。

若确认有意义，改法很小：`_build_project_context()` 按"全局静态"（AGENTS+TOOLS，放进 stable
section）和"账号级"（SOUL+IDENTITY+USER+MEMORY+MISSION，继续放 volatile）拆成两个
`ContextBlock`，数据层不需要改（`agent_context.blocks` 已经是分好的 dict）。

---

## 5. 不建议动的部分

- **safety block（2066 chars，占 system prompt 全文 28%）**：内容安全守则，是"唯一可靠的执行层"
  （见 block 内注释），且安全相关文案的裁剪需要专门的安全评审，不属于本次"prompt 编排效率"的
  优化范围。
- **skills block**：当前 559 chars 只因为示例账号只挂了 1 个 skill；这个 block 大小是账号配置
  决定的，不是编排层面的浪费。
- **AGENTS.md 的"不写小作文"和 output_directives 的"默认 1-3 句"之间有轻微语气重叠**：一个是
  人设口吻的表达，一个是机械规则，虽然方向一致但表述不同、体量都很小（各几十字），价值有限，
  不建议单独动。

---

## 6. 建议（按性价比排序）

1. **精简 TOOLS.md 里和工具 schema description 重复的触发条件描述**（发现一）。收益确定、风险
   低、改动范围小（一个 markdown 文件），且仓库里已有 mission 工具的先例证明这样做不会影响效果。
   需要逐个工具核对哪些是纯重复、哪些是 schema 里没有的补充信息（比如跨工具的边界提示，若 schema
   description 里没写全，要保留而不是一并删掉）。
2. **确认 DeepSeek API 是否有前缀缓存计费优惠，再决定是否拆分 project_context block**（发现二）。
   如果确认有意义，这是个纯编排层改动（`_build_project_context` 拆两个 ContextBlock），不影响
   模型看到的实际内容和顺序语义。
3. 不建议现在动 safety block 和 skills block。

---

## 7. 开放问题（需要确认后再动手）

1. 是否要先做一次 TOOLS.md 逐工具 diff（列出每个 `## xxx工具` 小节里哪句话和对应 schema
   description 完全重复、哪句话是独有信息），再决定具体删哪些句子，还是我直接按第 6 节的思路
   动手改一版给你看？
2. DeepSeek API 的前缀缓存机制需要谁来确认（是否有人已经知道答案，还是需要我去查文档/问
   DeepSeek）？这决定发现二这条要不要投入。
3. 是否要把这次的 char/token 统计方法（`/debug/accounts/{id}/prompt-preview` + 手动拉工具
   schema）固化成一个小脚本，方便以后跟踪 system prompt 体积的变化趋势，而不是每次都手动拼？
