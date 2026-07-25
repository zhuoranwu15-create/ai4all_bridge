# Agent Runtime 对齐：技术设计文档

> 状态：**基本已落地**（2026-07-12 核实）。Batch A（runtime 能力真值 + 外部证据投喂）、Batch B（`web_fetch` + 受控 `read` + skills catalog + weather skill）、Batch C（tool evidence replay）均已在代码中：`app/tools/external_content.py`、`app/tools/web_fetch_handlers.py`、`app/tools/read_handlers.py`、`app/skills/*/SKILL.md`、`app/tool_evidence_replay.py`（接入 `turn_service.py`）。Batch D（当前消息 typed envelope）待复核。本文保留为设计依据与验收基线。
> 目标：对齐 OpenClaw Agent Runtime 的核心 substrate：本轮能力真值、tool loop、工具结果投喂、skills catalog + `read` 渐进加载、外部证据信任边界、短期工具证据回灌、当前消息信封。
> 非目标：不照搬 OpenClaw 的 68 个工具、会话树、MCP/插件生态、HEARTBEAT 主动机制；`exec/write/edit/browser/MCP` 等工程型高风险工具暂缓，不等于排除 `read` 这类 runtime 基础工具。
> 主要依据：`openclaw_study/01-05`、OpenClaw 源码关键路径、weixin_bot 当前代码核实。

---

## 1. 结论

现有文档把 runtime 对齐收缩成「web_fetch + skill + 天气 + tool 回灌」，方向不算错，但缺了 OpenClaw runtime 里更核心的两层：

1. **能力真值同步**：OpenClaw 每轮先算 `effectiveTools`，system 里明确列出本轮实际可用工具，并强调 `TOOLS.md` 只是用法说明，不代表可用性。
2. **证据信任边界**：OpenClaw 把外部抓取内容和当前消息 metadata 都当成 untrusted context，而不是普通对话文本。

weixin_bot 当前不是缺一个 ReAct loop。`app/llm.py::generate_reply_with_tools()` 已经有单轮 tool loop：模型可多轮调用工具、工具结果会在本轮回灌给模型、直到最终回答才结束。真正缺口是：

- 模型在 system prompt 中不知道「本轮到底有哪些工具真的可用」；
- 外部 tool result 当前未统一包裹成不可信证据；
- 跨 turn 只回放 user/assistant 最终文本，丢失上一轮的 `assistant.tool_calls` 与 `role:tool`；
- `skills` 参数已有但没有接入运行时，也没有 OpenClaw 式受控 `read` 工具来读取 `SKILL.md`；
- 当前用户消息缺 typed envelope，引用/图片描述/metadata 和正文容易混在一起。

本设计建议按 4 个可独立发布的 batch 落地：

1. **Batch A：runtime 能力真值 + 外部证据投喂投影**，先把已有工具用对。
2. **Batch B：OpenClaw-style `web_fetch` + 受控 `read` + skills catalog + weather skill**，补 skill 渐进加载的核心链路。
3. **Batch C：tool evidence replay**，把最近工具调用按标准 wire message 跨 turn 回灌。
4. **Batch D：当前消息 typed envelope**，把当前用户消息、引用/图片/metadata 从存储态中分离出来投喂。

---

## 2. OpenClaw 侧可借鉴点

### 2.1 Effective tools 先算，再告诉模型

OpenClaw 的工具面不是静态列表：

- `src/agents/embedded-agent-runner/compact.ts` 先调用 `createOpenClawCodingTools()`，再经过 provider normalize、MCP/LSP 合并、policy filter、runtime-compatible filter，最后得到 `effectiveTools`。
- `src/agents/system-prompt.ts` 的 `## Tooling` 用最终可用工具名渲染，并写明：
  - tool name 大小写敏感；
  - 只能调用列出的工具；
  - `TOOLS.md is usage guidance, not availability.`

对 weixin_bot 的启发：`app/tools/registry.py` 已经是 schema/handler/gating 单一事实源，`build_turn_llm_input()` 也已经构造了 `tooling["available_tool_names"]`。缺的是把它写进 system prompt。

### 2.2 Skill 是操作手册，由通用 read 渐进展开

OpenClaw 的 `src/agents/system-prompt.ts::buildSkillsSection()` 会提示模型：

- 扫描 `<available_skills>`；
- 如果某个 skill 明确适用，先用 `read` tool 按 `<location>` 精确读取它的 `SKILL.md`；
- 如果 `<version>` 与上一轮不同，重新读取；
- skill 文件引用相对路径时，按 skill 目录解析，再用绝对路径读取；
- 最多先读一个；
- 不猜路径。

上一版文档把它写成专用 skill 读取工具，是不够对齐的。OpenClaw 的机制不是专用 skill tool，而是“catalog 给精确位置，通用 `read` 负责受控读取”。weixin_bot 应新增受控 `read` 工具：只读白名单目录，首批服务于 `app/skills/**/SKILL.md` 和 skill support files；专用 skill reader 不暴露给模型。

### 2.3 外部内容必须带信任边界

OpenClaw 的 `src/security/external-content.ts` 提供 `wrapWebContent()`，`web_fetch` 返回内容会带：

- `<<<EXTERNAL_UNTRUSTED_CONTENT id="...">>>`
- 安全提醒；
- `externalContent: { untrusted: true, source: "web_fetch", wrapped: true }`

这不是只有安全价值，也会改善对话效果：模型能区分「外部证据」与「用户/系统指令」，用户追问来源时也有依据。

### 2.4 当前消息和存储历史不是同一种形态

OpenClaw 的当前用户消息会加 user-role 的 untrusted envelope：

- `Conversation info (untrusted metadata)`：chat_id、message_id、sender、timestamp 等；
- `Reply target ... (untrusted, for context)`；
- `Forwarded message context ...`；
- 最后才是用户正文。

而 transcript 存储态仍是干净文本。这个「存储态 vs 投喂态分离」对 weixin_bot 很关键：我们现在已经把 raw metadata 存在 `messages.raw_json`，但投喂给 LLM 时只剩 `role/content`。

### 2.5 短期历史保留工具调用现场

OpenClaw 的会话上下文能包含 assistant tool call 与 tool result。zhuoran 抓包里就能看到：

`user -> assistant(tool_calls) -> tool(web_search result) -> assistant(final answer)`

weixin_bot 本轮内已经这么做；跨轮没有。

### 2.6 这次漏掉 read 的原因与修正

上一版把“文件能力”整体归为非目标，是一个过粗的边界判断。对 OpenClaw 来说，`read` 不只是 coding agent 的文件工具，它还是 skills 渐进加载的基础设施：没有 `read`，模型只能看到 skill catalog，无法按需打开 `SKILL.md`，也无法读取 skill 引用的支持文件。

修正后的边界是：

- `read`：本线必须补，作为只读、受控、可截断的 runtime 基础工具；
- `write/edit/exec/browser`：仍可暂缓，因为它们会引入写入、副作用、审批、浏览器状态等更大的安全面；
- `list/grep`：不是第一阶段必须项；如果后续 skill 支持文件变多，再评估受控 `list` / `grep`，但不能替代 `read`。

---

## 3. weixin_bot 当前代码事实

### 3.1 已具备

| 能力 | 当前代码 |
|---|---|
| 单轮 tool loop | `app/llm.py::generate_reply_with_tools()`，最多 `llm_max_tool_rounds` 轮，默认 3，上限 8 |
| tool schema/handler 单一事实源 | `app/tools/registry.py` |
| tool invocation 持久化 | `tool_invocations` 表；`app/llm.py::_record_tool_invocation_start/finish()` |
| provider tool message 兼容 | `app/llm_adapters.py` 支持 openai_chat / anthropic_messages / openai_responses |
| prompt block 元数据 | `app/prompt_builder.py` 已有 stable/volatile 与 block metrics |
| debug trace | `debug_traces.messages_json` 可保存实际 LLM messages |
| 账号隔离 | messages/tool_invocations/profile/context 都以 `account_id` 约束 |

### 3.2 缺口

| 缺口 | 当前代码 |
|---|---|
| 本轮工具真值没进 prompt | `PromptBuilder.assemble(tools=...)` 有参数，但 `build_turn_llm_input()` 未传 |
| 外部内容未统一 wrapper | `web_search` 结果直接 `json.dumps(tool_result)` 进 LLM |
| 缺 `web_fetch` | `app/tools/definitions.py` / `registry.py` 无此工具 |
| skills 未接入运行时 | `PromptBuilder.assemble(skills=...)` 有参数，但未传；无 skill catalog loader，也无受控 `read` |
| 跨 turn tool 证据丢失 | `list_recent_messages_for_account()` 只取 `messages.role/content` |
| 当前消息无 typed envelope | 当前 user 作为普通 history row 投喂；raw metadata 不进 prompt |

一个需要修正的旧假设：`tool_invocations.message_id` 当前存的是外部入站 `payload.message_id/event_id`，不是 `messages.id` DB 自增 id。它能与 `messages.message_id` 匹配，但不能按 DB id 匹配。

---

## 4. 设计原则

1. **核心同构，危险工具暂缓**：对齐 OpenClaw 的 runtime substrate；`exec/write/edit/browser/MCP` 这类高风险工程工具不作为第一阶段目标。
2. **读侧重建优先**：跨 turn tool 证据先从 `tool_invocations` read-side 重建，不改 `messages` schema。
3. **存储态保持干净**：不把 envelope、skill catalog、tool replay 写进 `messages.content`。
4. **外部证据一律 untrusted**：web/search/fetch/replay 进入 LLM 前都加外部证据信封或 metadata。
5. **read 是受控基础工具**：首版只读白名单目录，realpath containment，强截断，不开放任意文件系统。
6. **账号隔离不可破**：所有 DB 查询和文件读写必须以 `account_id` 或全局只读白名单约束。
7. **主要场景优先**：`web_fetch` 第一版用 Python `httpx` 实现主路径能力，不为极端 case 复制 OpenClaw 全部 provider/cache/sandbox 复杂度。
8. **可灰度可回滚**：新 runtime 机制加 settings 开关，出问题能关闭。

### 4.1 Runtime 基础件清单

| 基础件 | 是否本线必须 | 原因 |
|---|---|---|
| `effectiveTools` / 本轮工具真值 | 必须 | OpenClaw runtime 的起点；避免 prompt 说明和真实 schema 脱节 |
| tool result projection / external wrapper | 必须 | 外部证据不能被当成系统指令或用户原话 |
| `web_fetch` | 必须 | 已知 URL 获取、天气 skill、官网/网页证据都依赖它 |
| 受控 `read` | 必须 | OpenClaw skills 按 `<location>` 读取 `SKILL.md` 的基础工具 |
| skills catalog + `<location>/<version>` | 必须 | 只塞 catalog，不把全部 skill 正文进 prompt |
| tool evidence replay | 必须 | 让模型跨 turn 看见自己实际查过/调用过什么 |
| 当前消息 typed envelope | 必须 | 区分用户正文、metadata、引用/媒体/外部材料 |
| provider adapter replay 测试 | 必须作为验收 | 不是独立优化项，但 replay 一旦进 messages，必须保证三类 adapter 不丢失 |
| `list` / `grep` | 后置评估 | 对大型 support files 有价值，但 skill 首轮加载不依赖 |
| `exec/write/edit/browser/MCP` | 暂缓 | 能力面和安全面都远大于微信陪伴 runtime 首批需求 |

---

## 5. Batch A：本轮工具真值 + 外部证据投喂投影

### 5.1 本轮可用工具 block

**目标**：让模型知道本轮真正能调用什么，避免「TOOLS.md 写了但本轮没有」导致假装搜索/假装有能力。

**改动路径**：

| 文件 | 改动 |
|---|---|
| `app/prompt_builder.py` | 改写 `tools` block 文案为「本轮可用工具」，强调只可调用列出的工具、`TOOLS.md` 是用法说明不是可用性 |
| `app/turn_service.py` | 在 `_build_tooling_envelope()` 后，把 `tooling["available_tool_names"]` 传给 `PromptBuilder.assemble(tools=...)` |
| `tests/test_prompt_builder.py` | 覆盖新文案 |
| `tests/test_turn_context_history.py` 或新增 `tests/test_turn_tool_surface.py` | 覆盖 enabled/disabled 工具名进入 prompt |

建议文案：

```text
【本轮可用工具】
以下工具由运行时按账号、场景和开关过滤后提供。只有本节列出的工具可以调用；TOOLS.md 是用法说明，不代表本轮可用性。
- create_reminder
- list_reminders
- session_status
- web_search
```

注意：onboarding 轮不提供工具，应该显示无工具或不显示该 block，保持当前 onboarding 行为。

### 5.2 LLM-visible tool result projection

**目标**：DB 继续存 raw tool result，但发给 LLM 的工具结果先做投喂投影：

- 内部工具：保持原 JSON；
- 外部工具（`web_search` / `web_fetch`）：确保有 untrusted evidence wrapper；若工具结果已带 `externalContent.wrapped=true`，不得二次包裹；
- 统一截断，避免单个 tool result 打爆上下文。

**改动路径**：

| 文件 | 改动 |
|---|---|
| `app/tools/external_content.py`（新增） | `wrap_external_content(text, source)`、`project_tool_result_for_llm(tool_name, result, max_chars)` |
| `app/llm.py` | `_execute_and_record_tool_call()` 仍返回 raw result；append `role:"tool"` 时用 projection 后的 JSON |
| `tests/test_llm_tools.py` | 覆盖 web_search result 进入 messages 时带 external marker；DB 记录仍是 raw |

投影格式建议：

```json
{
  "status": "succeeded",
  "externalContent": {
    "untrusted": true,
    "source": "web_search",
    "wrapped": true
  },
  "data": "<<<EXTERNAL_UNTRUSTED_CONTENT source=\"web_search\" id=\"...\">>>\n...\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\"...\">>>"
}
```

实现细节：

- marker id 用 `secrets.token_hex(8)`；
- 包装前先移除常见 LLM special token 字面量；
- 单条投影默认上限 6000 字符，跨 turn replay 再单独截断到更小。

---

## 6. Batch B：`web_fetch` + 受控 `read` + skills catalog + weather

### 6.1 `web_fetch`

**目标**：补一个 OpenClaw contract 等价的 URL 抓取工具，支撑 skill 按需取实时数据。它不是搜索；未知信息仍用 `web_search`。

实现口径：

- 第一版用 Python `httpx` 原生实现，不用 shell `curl` wrapper；
- 对齐 OpenClaw 的主要用户可见能力：HTTP(S) URL、markdown/text/JSON/HTML 可读抽取、截断、SSRF guard、外部内容 wrapper、结构化 metadata；
- 不在第一版复制 OpenClaw 的完整 provider fallback、Firecrawl、cache、Cloudflare Markdown token hint、pinned DNS 等复杂能力；
- 后续如果网页抽取质量不足，再加 provider fallback 或 readability 增强。

**工具 schema**：

```json
{
  "name": "web_fetch",
  "description": "抓取一个公网 HTTP(S) URL 的文本/JSON/HTML 可读内容。用于读取已知 URL；搜索未知信息用 web_search。",
  "parameters": {
    "type": "object",
    "properties": {
      "url": {"type": "string"},
      "extractMode": {"type": "string", "enum": ["markdown", "text"]},
      "maxChars": {"type": "integer", "minimum": 200, "maximum": 20000}
    },
    "required": ["url"]
  }
}
```

说明：schema 使用 OpenClaw 风格的 `extractMode/maxChars`；Python handler 可兼容 `extract_mode/max_chars` 别名，便于本项目内部调用。

**改动路径**：

| 文件 | 改动 |
|---|---|
| `app/tools/_url_guard.py`（新增） | URL/hostname/IP/DNS/redirect SSRF guard |
| `app/tools/web_fetch_handlers.py`（新增） | 执行 `httpx` fetch、解析文本/JSON/HTML、截断、返回 OpenClaw-style result |
| `app/tools/definitions.py` | 新增 `get_web_fetch_tools()` |
| `app/tools/registry.py` | 注册 `web_fetch`，`CALL_PLAIN`，默认常驻，无 runtime flag |
| `app/tools/__init__.py` | 导出 |
| `app/user_profiles.py` | `TOOLS.md` 模板补简短说明 |

**安全策略**：

- 只允许 `http` / `https`；
- 禁止空 host、userinfo、file/data 等 scheme；
- hostname 归一化后拒绝 `localhost`、`*.localhost`、`*.local`、`*.internal`、云 metadata host；
- IP literal 拒绝 loopback/private/link-local/multicast/reserved/metadata，包括 IPv4-mapped IPv6；
- DNS 解析所有地址，任一命中私有/特殊地址即拒绝；
- `httpx.Client(trust_env=False)`，不使用环境代理；
- 响应体按 bytes 限制读取，默认 512KB；
- redirect 第一版可手动逐跳跟随，最多 3 跳；每一跳都重新跑同一 guard；
- 超时默认 8s，connect 3s。

说明：这不是 OpenClaw 的完整 pinned-DNS guard。第一版用 preflight DNS + 禁 env proxy + 手动重定向 guard，覆盖主要 SSRF 风险即可，避免为极端 case 引入过多复杂度。

**返回格式**：

```json
{
  "url": "https://example.com/a",
  "finalUrl": "https://example.com/a",
  "status": 200,
  "contentType": "text/html",
  "extractMode": "markdown",
  "extractor": "raw-html",
  "externalContent": {
    "untrusted": true,
    "source": "web_fetch",
    "wrapped": true
  },
  "truncated": false,
  "length": 1234,
  "rawLength": 1180,
  "wrappedLength": 1234,
  "fetchedAt": "2026-06-24T...",
  "tookMs": 480,
  "text": "<<<EXTERNAL_UNTRUSTED_CONTENT id=\"...\">>>\n...\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\"...\">>>"
}
```

与 Batch A 的关系：

- `web_fetch` 自身返回的 `text/title/warning` 就应是 wrapped external content，贴近 OpenClaw；
- `project_tool_result_for_llm()` 必须识别 `externalContent.wrapped=true`，避免二次包裹；
- `web_search` 若现有 provider 没有 wrapped content，则仍由 projection 包裹。

### 6.2 受控 `read`

**目标**：补 OpenClaw skills 所依赖的基础工具。模型从 `<available_skills>` 看到 `<location>` 后，调用 `read(path=...)` 读取 `SKILL.md`，再按说明调用其它工具。

这不是通用文件系统开放。第一版只做只读、白名单、强截断。

**工具 schema**：

```json
{
  "name": "read",
  "description": "Read allowed text files by path. First use case: load SKILL.md and skill support files from the skills catalog.",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "offset": {"type": "integer", "minimum": 1},
      "limit": {"type": "integer", "minimum": 1, "maximum": 400}
    },
    "required": ["path"]
  }
}
```

**改动路径**：

| 文件 | 改动 |
|---|---|
| `app/tools/read_handlers.py`（新增） | `handle_read(args, ctx)`，只读 allowlisted roots |
| `app/tools/definitions.py` | 新增 `get_read_tools()` |
| `app/tools/registry.py` | 注册 `read`，默认常驻 |
| `app/skills/__init__.py`（新增） | skill discovery、location 渲染、version hash |
| `app/prompt_builder.py` | skills block 改成 OpenClaw 式“按 exact `<location>` 调 read” |
| `app/turn_service.py` | 非 onboarding 轮传入 skill catalog |

**读取规则**：

- 默认允许 `app/skills/**`，以及后续明确加入的只读文档根；
- 模型看到的 `<location>` 建议使用稳定虚拟路径，如 `skills/weather/SKILL.md`，handler 映射到真实路径；
- `realpath` 必须落在 allowlisted root 内，防 symlink escape；
- 禁止读取 `.env`、SQLite DB、日志、密钥、用户 profile、任意项目源码；
- 文本输出按行数和 bytes 双重截断，返回 continuation hint；
- 第一版只支持文本，不支持图片；
- 专用 skill reader 不作为模型工具暴露，最多是 `read` handler 内部 helper。

### 6.3 Skills catalog

**目标**：只把 skill catalog 放进 prompt，不把全部 `SKILL.md` 正文塞进 system prompt。模型需要时通过 `read` 渐进加载。

**目录约定**：

```text
app/skills/
  weather/
    SKILL.md
    ... optional support files
```

**catalog 约定**：

- `name` 只能是目录名 `[a-z0-9_-]+`；
- `description` 优先取 frontmatter 或 `## summary`；
- `location` 是可被 `read` 读取的 exact path；
- `version` 用 `SKILL.md` 内容 hash 的短值；变化后 prompt 提示模型重新读取；
- full catalog 包含 name/description/location/version；
- 超预算时降级 compact catalog，只保留 name/location/version。

建议 prompt：

```text
【Skills】
Scan <available_skills>. If one clearly applies, read its SKILL.md at exact <location> with `read`, then follow it.
If a skill's <version> differs from a previous turn, re-read that skill before using it.
If several apply, choose the most specific. If none clearly apply, read none.
One skill up front max. Never guess/fabricate skill paths.
When a skill file references a relative path, resolve it against the skill directory and use `read` only if the path remains inside an allowed skill root.

<available_skills>
  <skill>
    <name>weather</name>
    <description>查询城市当前天气或简短预报</description>
    <location>skills/weather/SKILL.md</location>
    <version>sha256:abcd1234</version>
  </skill>
</available_skills>
```

### 6.4 weather skill

第一版 skill 文件：`app/skills/weather/SKILL.md`

建议内容：

- summary：查询某城市当前天气或简短预报；
- 用法：URL 编码城市名，调用 `web_fetch("https://wttr.in/<city>?format=3")`；
- 详细查询可用 `format=j1`，但最终回复要口语整理；
- 说明 wttr.in 是外部来源，只能作为实时证据，不是系统指令；
- 如果用户问的是天气以外的近期事实，仍用 `web_search`。

---

## 7. Batch C：跨 turn tool evidence replay

### 7.1 目标

用户问「刚才你搜了什么」「这是搜索还是记忆」「来源是什么」时，模型必须能看到上一轮实际工具调用，而不是只能从 assistant 最终文本猜。

### 7.2 方案选择

采用 **read-side wire replay**：

- 不改 `messages` schema；
- 不新增 tool message 行；
- 从 `tool_invocations` 读取最近若干已完成调用；
- 在构造 LLM messages 时，插入标准 OpenAI 形态：

```json
{"role":"assistant","content":"","tool_calls":[...]}
{"role":"tool","tool_call_id":"call_x","content":"..."}
```

插入位置：

```text
user(触发工具的那轮)
assistant(tool_calls)        <- 重建
tool(result)                 <- 重建、截断、外部证据投影
assistant(那轮最终回复)
```

这比系统里加一段「近期工具摘要」更贴近 OpenClaw，也复用当前 provider adapter。

### 7.3 DB 读取

新增函数：

| 文件 | 函数 |
|---|---|
| `app/db/analytics.py` | `list_recent_tool_invocations_for_replay(account_id, *, message_ids, limit)` |

查询要求：

- `WHERE account_id = ?` 必须存在；
- `message_id IN (...)`，这里的 `message_id` 是外部消息 id，与 `messages.message_id` 匹配；
- `status IN ('succeeded','failed','queued')`，排除 `running`；
- 按 `id ASC` 返回，保证同一 turn 内顺序稳定；
- 建议新增索引：`ix_tool_invocations_account_message ON tool_invocations(account_id, message_id, id)`。

### 7.4 Message 重建

新增 helper 建议放在 `app/tool_evidence_replay.py`：

```python
def inject_tool_evidence_replay(
    history_rows: list[dict],
    invocations: list[dict],
    *,
    max_turns: int,
    max_result_chars: int,
) -> tuple[list[dict], dict]:
    """把最近工具调用按 wire message 形态插入 history；只做 read-side 投喂，不改 DB。"""
```

规则：

- 只对最近 `llm_tool_evidence_turns` 个有工具调用的 user turn 注入，默认 2；
- 每个 invocation 生成一个 assistant tool_call + 一个 tool result，避免没有 round grouping 时误合并；
- `tool_call_id` 优先用 DB 中的值；缺失则生成稳定 id：`replay_{invocation_id}`；
- `arguments` 用 DB `args` 重新 JSON dump；
- `role:"tool".content` 使用 Batch A 的 `project_tool_result_for_llm()`，跨 turn 默认上限 1500-2500 字符；
- 如果找不到匹配的 user message row，不注入；
- metadata 记录注入数量、turn 数、截断数量、开关状态。

### 7.5 配置

新增 settings 与 `.env.example`：

```python
llm_tool_evidence_replay_enabled: bool = True
llm_tool_evidence_turns: int = 2
llm_tool_evidence_max_result_chars: int = 2000
```

### 7.6 Adapter 测试矩阵（Batch C 验收条件）

这不是独立优化项，而是 tool evidence replay 的验收条件：一旦 replay 以标准 wire message 插入历史，provider adapter 不能剥掉或改坏它。

- `openai_chat`：标准 `assistant.tool_calls` + `role:"tool"` 原生；
- `anthropic_messages`：测试已覆盖 tool_calls/tool_result 转换；
- `openai_responses`：已有从 messages 重建 function_call/output 的逻辑。

需要新增/扩展 adapter 测试，确保 replay message 不被剥掉；若某 provider 不支持该形态，必须在 adapter 层转换，而不是退回到 system summary。

---

## 8. Batch D：当前消息 typed envelope

### 8.1 目标

当前用户消息不再只是普通 `content`，而是在投喂态包成：

````text
Current message context (untrusted metadata):
```json
{
  "channel": "openclaw-weixin",
  "message_type": "text",
  "timestamp": "2026-06-24 14:03:00 +08:00"
}
```

User message:
...
````

引用、转发、图片描述后续可逐步接入同一 envelope。

### 8.2 实现方式

`messages.content` 存储态不变。`build_turn_llm_input()` 在构造 `history` 时识别当前 message row，并只在 LLM 投喂态替换：

- 需要给 `build_turn_llm_input()` 增加 `current_message_id`、`current_message_type`、`current_message_metadata` 或更小的参数；
- 当前 message row 通过 `row["message_id"] == current_message_id` 识别；
- 历史旧消息保持原样；
- debug trace 记录的是投喂态 messages，messages 表仍记录干净正文。

### 8.3 范围控制

第一版只做：

- channel；
- message_type；
- 北京时间 timestamp；
- 当前用户正文。

暂不解析复杂 quote/forward payload，避免把 OpenClaw 多渠道复杂度一次性搬进来。图片描述当前已经被写入正文，后续再拆成 `Media description (untrusted)`。

---

## 9. 开关与配置

建议新增：

```python
llm_tool_surface_prompt_enabled: bool = True
llm_external_content_wrapper_enabled: bool = True
llm_tool_evidence_replay_enabled: bool = True
llm_tool_evidence_turns: int = 2
llm_tool_evidence_max_result_chars: int = 2000
llm_current_message_envelope_enabled: bool = False
llm_read_tool_enabled: bool = True
llm_skills_prompt_enabled: bool = True
web_fetch_timeout_seconds: float = 8.0
web_fetch_max_response_bytes: int = 524288
web_fetch_max_chars: int = 6000
web_fetch_max_redirects: int = 3
```

`current_message_envelope` 建议默认 False，先在 debug account 灰度；其他默认 True。

---

## 10. 测试方案

### Batch A

- `tests/test_prompt_builder.py`
  - 本轮工具 block 文案；
  - `TOOLS.md` 不等于 availability；
- `tests/test_turn_tool_surface.py`
  - `web_search_enabled=False` 时 prompt 不列 web_search；
  - `web_search_enabled=True` 时列 web_search；
  - onboarding 轮不列工具；
- `tests/test_llm_tools.py`
  - web_search result 投喂给 LLM 时有 external marker；
  - DB `tool_invocations.result` 仍是 raw result。

### Batch B

- `tests/test_web_fetch_tools.py`
  - schema/registry/handler；
  - 成功抓取 text/json/html，支持 `extractMode/maxChars`；
  - 返回 `externalContent.wrapped=true` 且 `text` 已包 external marker；
  - `127.0.0.1`、`localhost`、`10.0.0.1`、`169.254.169.254`、DNS 解析到内网全部拒绝；
  - redirect 跳转到内网必须拒绝；
  - `trust_env=False` 不使用代理；
- `tests/test_read_tool.py`
  - `read("skills/weather/SKILL.md")` 成功；
  - `offset/limit` 生效，超长文件有 continuation hint；
  - `../`、symlink escape、`.env`、DB、用户 profile、非 allowlisted root 全部拒绝；
- `tests/test_skills.py`
  - catalog 含 weather；
  - catalog 有 `name/location/version`；
  - prompt 指向 exact `<location>`，要求用 `read`；
  - SKILL.md 内容变化后 version 变化；
  - 超预算时能降级 compact catalog；
- `tests/test_tool_registry.py`
  - 双向注册一致性自动覆盖新增工具。

### Batch C

- `tests/test_tool_evidence_replay.py`
  - 有 matching `messages.message_id` + `tool_invocations.message_id` 时注入 wire message；
  - 顺序为 user -> assistant(tool_calls) -> tool -> assistant(final)；
  - 多 invocation 稳定排序；
  - 只注入最近 K 个工具 turn；
  - result 截断；
  - 开关关闭不注入；
  - account A 不会读到 account B 的工具证据。
- 扩展 `tests/test_llm_adapters.py`
  - openai_chat / anthropic / responses 都接受 replay 形态。

### Batch D

- `tests/test_current_message_envelope.py`
  - 当前消息投喂态带 envelope；
  - 历史旧消息不改；
  - `messages` 表存储内容不含 envelope；
  - debug trace 可看到 envelope。

### 回归

这一线触及 prompt 组装、工具执行、DB 读取、provider adapter。每个 batch 先跑聚焦测试；Batch C/D 合并前建议跑：

```bash
.venv/bin/pytest tests/test_llm_tools.py tests/test_llm_adapters.py tests/test_tool_registry.py tests/test_turn_context_history.py -v
```

整线完成后跑：

```bash
.venv/bin/pytest tests/ -v
```

---

## 11. 手验用例

启动服务：

```bash
.venv/bin/uvicorn app.main:app --reload --port 8180
```

用主要测试账号：

```bash
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --account aid_806382741 --text "北京今天天气怎么样"
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --account aid_806382741 --text "刚才你查了什么来源？"
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account aid_806382741
```

预期：

- 第一轮可触发 `read("skills/weather/SKILL.md")` -> `web_fetch`；
- 第二轮能基于 replay 的 tool evidence 回答来源；
- prompt 里能看到本轮可用工具；
- 外部内容带 untrusted wrapper；
- messages 表里用户/助手正文仍是干净文本。

---

## 12. 风险与回滚

| 风险 | 对策 |
|---|---|
| 工具 block 变长 | 只列工具名，不重复完整 description |
| 外部 wrapper 增 token | 同轮上限 6000，跨轮 replay 上限 2000 |
| replay 破坏 provider 消息格式 | 标准 wire 形态 + adapter 聚焦测试；有开关回滚 |
| web_fetch SSRF | URL/DNS/IP guard、禁 env proxy、禁或手动校验 redirect、超时与体积限制 |
| read 泄露本地文件 | allowlisted roots + realpath containment + 禁敏感文件名 + 强截断 |
| skill 被路径穿越 | catalog location 稳定虚拟路径；`read` 只接受 allowlisted realpath |
| account 数据串线 | 所有 DB 查询强制 `account_id`；测试覆盖 A/B 隔离 |
| 当前消息 envelope 影响语气 | 默认关闭，debug account 灰度 |

---

## 13. 暂不纳入本线

- 动态用户画像 / TDAI 化记忆：见 `docs/plans/记忆机制_tdai化对齐.md`。
- 事实纪律 wording、人味、人格内容厚度：见 `docs/plans/对话效果_prompt纪律对齐.md`。
- prompt cache boundary 真接 provider cache：单独第 4 线。
- 分条发送、语音、富媒体：属于投递体验，不是本 runtime batch。
- `exec/write/edit/browser/MCP`：属于高风险工程工具，等核心 substrate 稳定后再单独评估。
- `list/grep`：不是 skills 首批必要条件；当 skill support files 增大时再评估受控只读版本。
- search claim guard：可作为后续质量门禁；第一版先靠工具真值、外部 wrapper 和 evidence replay 降低假装搜索。

---

## 14. 实施顺序

推荐 commit 拆分：

1. `runtime tool surface + external content projection`
2. `web_fetch tool with ssrf tests`
3. `read tool + skills catalog + weather skill`
4. `tool evidence replay`
5. `current message envelope`

每个 commit 都能独立验证和回滚。第一批最小可见收益来自 1 和 4：模型知道自己本轮有什么工具，也能在下一轮看到自己上一轮真的查过什么。
