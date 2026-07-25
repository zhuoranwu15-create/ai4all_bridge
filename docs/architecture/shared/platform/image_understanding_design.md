# 图片理解能力 — 技术方案与调研结论

> 状态：**已上线**（DashScope `qwen3-vl-plus` 图片理解已落地生产）。本文为现行设计记录。
> 更新时间：2026-06-07
> 关联：[语音输入技术设计](../../products/zhaoxi/voice_input_design.md)（本方案直接借鉴其"上游产物 → 文本链路"哲学）

---

## 0. 一句话结论

微信图片当前被**完全丢弃**：图片进来后 bridge 的 `before_agent_reply` 钩子只能拿到空 `cleanedBody`，后端存成一条空 content 的 text 消息。OpenClaw 其实**已经把图片下载到了本机确定路径** `~/.openclaw/media/inbound/<uuid>.<ext>`，只是这个引用没透传给 bridge。方案核心：**patch OpenClaw 把这张图的本地路径透传给 bridge → 后端读本地文件 → 调 DashScope `qwen3-vl-plus` 产出多维描述 → 描述文本进现有文本对话链路**，从而复用人设、记忆、限流、扣费、账号隔离。

---

## 1. 需求与场景

用户在文字聊天过程中发来一张图片，**可能带跟进问题，也可能没有**。三种场景：

| 场景 | 用户行为 | 期望接话 |
|---|---|---|
| A. 图+文同条 | 图片带文字 / 直接问"这是哪" | 一轮内结合图片内容直接回答 |
| B. 纯图片 | 只发图，无文字 | 按人设自然接话（**评论 + 一句轻提问**） |
| C. 图后追问 | 先发图，下一条才问"这什么牌子" | 用上一轮已落库的图片描述回答 |

**关键事实**：微信里图片和跟进问题是**两条独立消息 = 两个独立 turn**。所以图片的理解结果**必须落进对话历史**，下一轮提问才能引用。

---

## 2. 调研过程与证据

### 2.1 现有消息链路（代码事实）

- 入口：`app/turn_service.py::handle_openclaw_turn`。全程只用 `payload.text`；对非文本消息只存一条 content（语音会兜底成 `[voice message]`，图片则是空串）。
- 入参 schema：`app/schemas.py::OpenClawTurnRequest` **已预留** `message_type` 和 `media: MediaPayload{media_id,url,path,format,duration_ms}`，但 turn_service 未消费 `media`。
- LLM：`app/llm.py`，`generate_reply_with_tools` 走主对话 LLM，模型由 family×tier 解析（`tier_for_task("main_reply")`=active family 的 pro 档，当前生产 `deepseek-v4-pro`），不再读已废弃的 `settings.llm_model`。见 [LLM family×tier 设计](../../agent-runtime/llm_family_tier_design.md)。
- Bridge：`openclaw-bridge/index.js:481-482` 写死 `message_type:"text"`、`text: event.cleanedBody || ""`；payload 里 `raw:{ctx,event}` 原样透传给后端。
- VL 验证脚本：`test3.py` —— 已跑通 DashScope `qwen3-vl-plus` 的 OpenAI 兼容接口（`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`），`settings.dashscope_api_key` 已就位（`app/config.py:95`）。

### 2.2 OpenClaw 侧（为什么 bridge 拿不到图）

- `before_agent_reply` 钩子事件体只有 `{cleanedBody}`（OpenClaw `src/plugins/hook-types.ts`）。
- 钩子上下文 `PluginHookAgentContext` 只含频道字段（agentId/sessionKey/...），**不含 MediaPath**。
- 入站媒体事实（`MediaPath/MediaUrl/MediaTypes`，`src/auto-reply/reply/inbound-media.ts`）挂在 reply 的完整 `ctx` 上；`[media attached: <path> (<type>) | <url>]` 标记（`src/auto-reply/media-note.ts`）是在钩子**之后**的 prompt 组装阶段才注入。
- bridge `handled:true` 短路后，OpenClaw 原生 agent run 与媒体 staging 都被跳过。

### 2.3 生产实测证据（决定性）

线上机 `jack@59.110.40.50`（aliyun1，单机部署：FastAPI backend + openclaw-weixin + scheduler 同机）。

**a) 图片落库后是空消息**。生产库 `data/ai4all.sqlite3` 中 919 号入站消息：`message_type=text`、`content=''`、`created_at=2026-06-07 00:12`。其 `raw` 实测：
```
event keys:  ['cleanedBody']            # 仅此一项，且为空
ctx keys:    ['agentId','channelId','messageProvider','sessionId','sessionKey','trigger','workspaceDir']
flags:       MediaPath=False MediaUrl=False media_attached=False media://=False image=False
```
→ bridge 对图片消息**确实是"瞎"的**，连标记都没有。

**b) 但 OpenClaw 把图下载到了确定路径**：
```
~/.openclaw/media/inbound/82e6e5f2-f4a0-4ea9-91b2-f0ec07cebedb.jpg   # 00:12, 242KB
```
mtime 与 919 号消息时间精确对应 → **919 = 这张图**。同目录还有多张（6-06 20:39/20:44/20:59 等），均为用户此前发的图，全部丢失。

**c) 单机 + 同用户**：backend 服务用户与 `~/.openclaw/media/inbound/`（`drwx------ jack`）同为 `jack` → **后端可直接读该文件，无需传字节流**。

**d) 已有 patch OpenClaw 的先例**：`patches/openclaw-weixin-gateway-methods-runtime.patch` 已 patch openclaw-weixin 的 `src/channel.ts` → "patch 透传媒体引用"是有先例、可落地的。

---

## 3. 总体设计：两段式（VL 理解 → 现有人设链路）

```
微信图片
  → openclaw-weixin 下载到 ~/.openclaw/media/inbound/<uuid>.<ext>
  → [patch] bridge 拿到该图的本地路径/类型
  → bridge 转发 message_type=image + media{path,format} (+caption text)
  → /openclaw/turn
  → [新] image_understanding.py: 读本地文件 → base64 → DashScope qwen3-vl-plus → 多维描述
  → 把描述写成一条 user 历史消息: "{caption}\n[用户发来一张图片：{描述}]"
  → 复用 generate_reply_with_tools（带 Soul/IDENTITY/USER/记忆）生成"接话"
  → 微信文本回复
```

**为什么两段式（而非直接把图喂主对话模型）**：
1. 主对话模型 provider 可配且当前非 DashScope；VL 调用隔离成 stateless 模块，对 `llm.py` 零侵入。
2. 描述文本进历史后，**账号隔离/记忆/限流/扣费/人设全自动复用**，与语音方案完全一致。
3. 最终接话由带 Soul 的主链路生成，**保证符合人设**，而非把 VL 第三人称描述直接外发。

---

## 4. 已锁定的产品决策

| 决策点 | 选择 | 含义 |
|---|---|---|
| 纯图片(B 场景)默认接话 | **评论 + 一句轻提问** | 按人设共情式评论图片，并自然带一句开放提问引导继续聊 |
| 图后追问(C 场景)支撑 | **靠历史文字描述** | v1 不重存原图，追问走纯文本链路；描述没覆盖的细节无法回溯（已知取舍） |
| VL 计费形态 | **独立成本事件** | 图片理解单独记一次成本事件（类比 web_search），带总开关，便于观测/灰度 |

**派生要求**：因为 C 场景靠文字描述，VL 的 system prompt 必须产出**多维描述**（内容 / 可见文字 OCR / 品牌 / 场景 / 情绪），而不是 test3.py 里只测情绪那版。

---

## 5. 取图集成方案（唯一需 patch OpenClaw 的点）

bridge 拿不到图是**钩子层没暴露媒体**，不是图没下载。推荐 **B 方案**，两种等价实现二选一：

- **B1（最省事，复用现有解析）**：patch 让有媒体时把 `[media attached: <abs_path> (<type>)]` 注入到 `before_agent_reply` 的 `cleanedBody`。bridge **已有 `extractMediaMarkers`（index.js:118）** 可直接解析 → 转发 `message_type=image` + `media{path,format}`。
- **B2（更干净）**：patch 在 before_agent_reply 的 hook context 增加 `MediaPath/MediaUrl/MediaType` 字段，bridge 直接读。

两者后端侧实现完全一致。落地时先在线上机确认 patch 注入点（core `get-reply.ts` 钩子上下文构造处，或 openclaw-weixin 侧），按现有 `patches/` 流程产出补丁。

> 备选 C（无需 patch、但脆弱）：后端收到图片通道的空 content turn 时，按 sessionId + 时间窗去 `media/inbound/` 找最近文件。无稳定关联键，**不推荐**，仅作 patch 不可行时的兜底。

---

## 6. 具体改动清单（diff 级，最小改动）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `openclaw-bridge/index.js` | patch 生效后：填 `message_type:"image"` + `media{path,url,format}`；caption 仍走 `text`。复用 `extractMediaMarkers` 解析路径 |
| 2 | `app/image_understanding.py`（新增） | `describe_image(image_path_or_url, caption) -> str|None`。基于 test3.py，扩展为多维描述 prompt；读本地文件→base64 data URL；超时/失败返回 None。复用 httpx + `settings.dashscope_api_key`，**不引入新依赖** |
| 3 | `app/config.py` + `.env.example` | 新增 `image_understanding_enabled`、`image_understanding_model="qwen3-vl-plus"`、`image_understanding_timeout_seconds`、`image_max_bytes`、`image_inbound_dir`（默认 `~/.openclaw/media/inbound`）。每项带行内注释 |
| 4 | `app/turn_service.py` | `message_type=="image"` 分支：开关开则调 VL → 合成 content `"{caption}\n[用户发来一张图片：{描述}]"` 进历史 → 主链路；VL 失败走兜底话术；`modality="image"` 传入 `write_memory`（已支持，`memory_writer.py:48/72`） |
| 5 | 计费（参照 web_search 扣费路径） | VL 调用记一次独立图片理解成本事件 |
| 6 | `scripts/send_mock_turn.py` | 加 `--image-path` / `--image-url`，本地全链路自测（绕过 OpenClaw） |
| 7 | `tests/test_image_turn.py`（新增） | mock VL，覆盖 A/B/C 三场景 content 合成、失败兜底、账号隔离、memory modality |

**账号隔离不变量**：VL 调用、文件读取、记忆写入全部按 `account_id` 约束；不新增任何无 `account_id` 的查询/写入。

---

## 7. 配套口径

- **数据 / 隐私**：`messages.content` 存 VL 描述文本（脱敏口径同语音）；v1 不持久化原图；debug 默认脱敏截断。读取 `media/inbound` 文件仅在本机、仅当轮使用，不复制留存。
- **失败体验**：VL 超时/失败 → 例如「这张图我没太看清，你可以说说它，或者再发我一次～」。**红线：禁止让主模型在无描述时瞎猜图片内容**（同语音设计）。
- **成本 / 贝壳**：VL 一次调用计一次图片理解成本事件；后续主回复按正常 token 扣减。
- **限流**：复用现有 RPM/日配额；图片轮额外占一次 VL 调用，评估是否对图片单独限速。

---

## 8. 分阶段落地 + 测试

1. **后端先行（不依赖 patch）**：`image_understanding.py` + turn 图片分支 + mock 脚本 + 单测。用 `send_mock_turn.py --image-path` 本地/线上全链路验证 A/B/C 三场景与失败兜底。
2. **真图验证 VL 质量**：直接用线上 `~/.openclaw/media/inbound/*.jpg` 真图，跑多维描述 prompt，确认描述足以支撑常见追问（C 场景前提）。
3. **OpenClaw patch（B1/B2）+ bridge 联调**：产出补丁，bridge 填 media，真机微信发图端到端验证。
4. **可选增强**：原图保留、图片单独限流/计费细化、单段式多模态、OCR 强化。

**测试策略**（遵循 CLAUDE.md）：窄改动跑 `tests/test_image_turn.py` 等聚焦测试；触及共享链路（turn/持久化/计费/prompt）再跑全量。测试用内存 SQLite，无需起服务。

---

## 8.5 多机（node）：bridge 传字节 / 内联 base64（2026-06-13 落地）

单机方案靠「中心读 node 本地路径」成立；多机下 node（aliyun2）的图落在 node 磁盘，中心（aliyun1）读不到、且被 `image_inbound_dir` 白名单拒绝（详见 [补丁维护 §2.5](../access/openclaw_patches_maintenance.md)）。已选 **传字节 / 内联 base64**：

```
微信图片 → openclaw-weixin 下载到 node 本地 <uuid>.<ext>
  → [patch] before_agent_reply 的 cleanedBody 注入本地路径标记
  → [新] bridge 用 node:fs 读该文件 → base64 内联进 media.data_base64(+format+size)
  → POST 中心 /openclaw/turn
  → [新] image_understanding.describe_image(image_b64=...) 用字节构 data URL → DashScope VL
  → 之后与单机完全一致(描述进历史 → 主链路接话)
```

- 来源优先级 **b64 > path > url**（`describe_image` 内部统一）。单机 aliyun1 仍走同一条 bridge 代码：读字节成功就传字节；若读失败/超限自动**降级**为仅传 `path`（中心可直读），向后兼容。
- 字节路径不经 `image_inbound_dir` 白名单（字节无路径概念），仍受 `image_max_bytes` 上限保护。bridge 侧另有 cap（`AI4ALL_IMAGE_MAX_BYTES`，默认 5MB）。
- **运维前置**：node→中心经 aliyun1 nginx，须把 `/openclaw/turn` 的 `client_max_body_size` 调到 12m（默认 1m 会 413）。三方大小协调：bridge 5MB ≤ 中心 10MB ≤ nginx 12MB。
- **隐私**：base64 仅内存临时用于 VL，不落库（不进 `messages.raw`，moderation `_media_to_dict` 白名单天然过滤 `data_base64`）。

## 9. 线上机可复用的排查命令

```bash
# 1) 最近入站消息（找空 content 的图片消息）
.venv/bin/python3 - <<'PY'
import sqlite3
db=sqlite3.connect("data/ai4all.sqlite3"); db.row_factory=sqlite3.Row
for r in db.execute("select id,created_at,message_type,account_id,substr(content,1,50) c "
                    "from messages where direction='inbound' order by id desc limit 15"):
    print(r["id"],r["created_at"],r["message_type"],r["account_id"],repr(r["c"]))
PY

# 2) 某条消息的完整 raw（确认 event/ctx 字段、有无媒体引用）
#    把 919 换成目标 id
.venv/bin/python3 - <<'PY'
import sqlite3,json
db=sqlite3.connect("data/ai4all.sqlite3"); db.row_factory=sqlite3.Row
r=db.execute("select message_type,content,raw_json from messages where id=919").fetchone()
p=json.loads(r["raw_json"] or "{}"); p=p.get("raw_payload",p)
print(r["message_type"],repr(r["content"]))
print("event:",p.get("event")); print("ctx keys:",sorted((p.get("ctx") or {}).keys()))
PY

# 3) OpenClaw 实际下载的入站图片
ls -lat ~/.openclaw/media/inbound/ | head

# 4) 后端 turn 日志（确认 message_type / text）
journalctl -u ai4all-weixin-backend.service -n 50 --no-pager | grep "openclaw_turn received"
```

---

## 10. 待办 / 开放项

- [ ] 确认 OpenClaw patch 注入点（B1 注入 cleanedBody vs B2 扩 hook context），产出 `patches/` 补丁。
- [ ] 确认线上 `.env` 已配 `DASHSCOPE_API_KEY` 供 backend 使用。
- [ ] 多维描述 prompt 定稿（内容/OCR/品牌/场景/情绪），用真图回归。
- [ ] 图片理解成本事件的计价口径（固定贝壳 or 按 token）。
- [ ] 是否对图片轮单独限流。
- [ ] 多图消息（一条多张）是否 v1 支持，还是只取首图。

---

## 附录：关键代码/路径索引

| 项 | 位置 |
|---|---|
| turn 入口 | `app/turn_service.py::handle_openclaw_turn` |
| 入参 schema（已含 media） | `app/schemas.py::OpenClawTurnRequest` / `MediaPayload` |
| LLM 调用 | `app/llm.py::generate_reply_with_tools` |
| VL 验证脚本 | `test3.py`（DashScope qwen3-vl-plus） |
| DashScope key | `app/config.py:95` `dashscope_api_key` |
| bridge 转发 + 媒体标记解析 | `openclaw-bridge/index.js:118 extractMediaMarkers` / `:471 payload` |
| 记忆 modality 支持 | `app/memory_writer.py:48/72` |
| OpenClaw 入站媒体事实 | `openclaw/src/auto-reply/reply/inbound-media.ts` |
| OpenClaw 媒体标记格式 | `openclaw/src/auto-reply/media-note.ts`（`[media attached: <path> (<type>) | <url>]`） |
| OpenClaw 钩子事件类型 | `openclaw/src/plugins/hook-types.ts`（before_agent_reply 仅 cleanedBody） |
| 线上图片落地目录 | `~/.openclaw/media/inbound/<uuid>.<ext>` |
| 已有 patch 先例 | `patches/openclaw-weixin-gateway-methods-runtime.patch` |
