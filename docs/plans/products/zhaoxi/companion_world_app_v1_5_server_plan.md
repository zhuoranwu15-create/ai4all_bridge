# Companion World App v1.5 服务端开发计划

更新时间：2026-07-29
状态：**S0 已实现，待 PR 合并**；S1–S5 未开工。分支 `feat/companion-world-v1-5-s0`。

> 归属：`product:zhaoxi`。
> 输入：客户端仓库《Companion World v1.5 服务端需求清单 V0.3》（`/home/jack/companion_world_v1_5_server_requirements.md`）。
> 产品口径：[朝夕相伴 App 端 PRD](../../../products/zhaoxi/capabilities/companion_world_app_prd.md) §18。
> 技术权威：[Companion World 3.0 ADR](../../../architecture/products/zhaoxi/companion_world_3_0_refactor_design.md)。
> 前序计划：[M1 服务端](companion_world_app_m1_server_plan.md)、[M2 主人 Feed 管理](companion_world_app_m2_feed_management_plan.md)。
> 评审基线：`main@b3f275a`，当前最大迁移号 `m0052`，`CLIENT_CONTRACT_VERSION = 2026-07-28`。
> S0 落地后：最大迁移号 `m0053`，`CLIENT_CONTRACT_VERSION = 2026-07-29`。

---

## 1. 范围与结论（TL;DR）

**方向可做，无需整条否掉的条目**，包括 §6.0 的 SEC-001 冲突判定（结论：不冲突，见 D-9）。

与客户端清单的**三处偏差**，是本计划的核心：

1. **撤回/删除（MEDIA-RETRACT-001）不在 v1.5 范围**（产品 2026-07-29 决议）。连带影响见 §2.4——
   审核事后下架在 v1.5 没有表达形态，只落数据位。
2. **CONTENT-003（任务章节）从 v1.5 摘出**，只做 CONTENT-004 标记位。原因是现有 mission 机制
   不是"角色章节"模型，缺的不是字段（见 §3）。
3. **CANDIDATE-001 不是纯配置**，要改 4 处代码硬编码 + 5 项运营交付物（见 §4）。
   S0 已把校验从「写死 4 位」改成「连续 1..N + 下限 4」，**此后再加预设才是纯数据操作**。

工作量与客户端清单 §8 的判断一致：媒体链路是杠杆点，但真正的成本不在上传端点，而在
「媒体如何进入 LLM 上下文」与「访客可见 URL 的鉴权」。

| ID | 结论 | 归属批次 |
| --- | --- | --- |
| MEDIA-CONTRACT-001 上传端点 | ✅ 可做 | S1 |
| MEDIA-CONTRACT-002 会话媒体 | ✅ 可做，读模型形状按 D-1/D-2 定稿 | S2 |
| MEDIA-CONTRACT-003 动态图文 | ✅ 可做 | S3 |
| MEDIA-LIMIT-001 限额 | ✅ 可做，新增 4 个字段（与 ASR 限额分离，见 D-6） | S1 |
| MEDIA-PRIVACY-001 EXIF 剥离 | ✅ 服务端做，方案见 D-4 | S1 |
| MEDIA-MODERATION-001 审核位 | ✅ 数据位 + 接口实做，**生效依赖阿里云配置** | S1 / S4 |
| MEDIA-RETRACT-001 撤回删除 | ⏸ **v1.5 不做**（产品决议） | 移出 |
| MEDIA-COMPAT-001 显式拒绝 | ✅ 可做，错误码见 §6 | S1–S3 |
| MEDIA-COMPAT-002 发布顺序 | ✅ 接受 | 发布 |
| MEDIA-TYPE-001 口径 | ✅ **已定稿，见 D-1** | S2 |
| CONTENT-001 欢迎语落库 | ✅ 可做 | S0 |
| CONTENT-002 种子动态 | ✅ 可做，须新开 `source_type`（见 D-8） | S0 |
| CONTENT-003 任务章节 | ⏸ **移出 v1.5**（见 §3） | 移出 |
| CONTENT-004 不计数标记 | ✅ 可做 | S0 |
| CANDIDATE-001 候选 4→5 | ✅ 可做，非零成本（见 §4） | S0 |
| CANDIDATE-002 至少留 1 位 | ✅ 已核实，行为不变 | 无改动 |
| CANDIDATE-003 出生信息 | ✅ 纯人设 | S0（产品交付） |
| WISH-001…005 许愿创建 | ✅ 可做，选"扩展 preview" | S5 |
| FLAG-001 4 个 capability | ✅ 命名照用 | S0 |

**S0 全部条目已实现**（2026-07-29，分支 `feat/companion-world-v1-5-s0`）：FLAG-001、
CANDIDATE-001/003 代码侧、CONTENT-001/002/004、图片理解默认开。剩余外部依赖两项：
运营导入含司辰的 5 条 manifest（§9.1）、产品确认 CONTENT-002 的 5 条动态文案（§10.1）。

---

## 2. 定稿决策（D-1 … D-10）

### D-1 · `message_type` 与 `content.type` 的关系（MEDIA-TYPE-001）

**定稿：`content.type` 是唯一权威判别字段；`message_type` 标记为 deprecated，服务端保证两者取值一致。**

不选客户端提的两个选项中任何一个原样，理由：

- 选「`message_type` 恒为 `text`」客户端零改动，但会让契约里永久留一个说谎的字段——后续任何人
  读 `message_type` 都会得到错误结论。这是用一次性省事换长期技术债。
- 选「`message_type` 放开取值」则出现**两个判别字段**，两处口径迟早漂移。

行业惯例（微信/Slack/Telegram 的消息模型）都是**一条消息一个内容判别**。因此定稿为收敛到一个：

```text
ConversationMessageItem = {
  id, message_id, role, created_at,
  message_type: "text" | "image" | "audio"   // deprecated，恒等于 content.type
  text: string                                // 见 D-2，媒体消息下发用户 caption
  content: MessageContent                     // 权威判别
}

MessageContent = { type: "text",  text: string }
               | { type: "image", url, width, height, text? }
               | { type: "audio", url, duration_ms, transcript? }
```

**客户端过滤条件的迁移**：把 `message_type === 'text'` 改成"看 `content.type` 是否为已知类型，
未知则按 v1.5-0 的占位降级"。客户端原先的顾虑是"无枚举 → 无法区分新内容类型与不该展示的
内部消息"，这个顾虑由服务端侧承诺解除：

> **App 会话读接口从不下发内部消息。** `app/db/accounts.py:958 list_app_conversation_messages_before`
> 的 SQL 已限定 `m.role IN ('user','assistant') AND m.content IS NOT NULL AND m.content != ''`，
> 且只扫 `__app_active__` session。system/tool/internal 行在这一层就被挡住了。

也就是说 `message_type` 从来不是安全过滤的必要条件，客户端过去用它做防御是**打在了错误的层**。
既然如此，没有理由为它保留一个开放字符串。

`message_type` 在 v1.5 仍照常下发（不破坏旧客户端硬校验），OpenAPI 里收紧为闭合枚举并标 deprecated。

### D-2 · AI 会话的 `text` 字段不能直接复用（MEDIA-CONTRACT-002 的改方案点）

**问题**：`app/agent_runtime/turns/service.py`（约 1013–1060 行）在图片轮里把 VL 描述**合成进入库内容**：

```python
description = describe_image(...)
text = f"{caption}\n[用户发来一张图片：{description}]"
insert_message(..., message_type=ctx.message_type, content=text, ...)
```

`messages.content` 同时是 LLM 上下文与 App 读模型的 `text` 来源。不动的话，
**用户会在自己的气泡里看到服务端生成的图片描述**。

**定稿：显式拆分「上下文文本」与「展示载荷」，两列各司其职。**

| 列 | 语义 | 消费方 |
| --- | --- | --- |
| `messages.content`（已存在） | **LLM 上下文文本**。图片轮 = caption + VL 描述；语音轮 = caption + transcript | prompt_builder、dreaming、记忆链路 |
| `messages.content_json`（新增，m0053） | **展示载荷**，即 D-1 的 `MessageContent` JSON | App 读接口 |
| `messages.media_id`（新增，m0053） | 关联 `media_assets`，供签名 URL 重签与运维排查 | App 读接口、审核 |

读接口规则：

- `content_json` 存在 → `content` 直接反序列化下发，`text` 字段下发 `content_json` 里的 caption（无则空串）；
- `content_json` 为空（存量全部消息）→ `content = {"type":"text","text":content}`，与今天完全一致。

**为什么不新开消息表 / 不加 media 子表**：媒体元数据全部在 `media_assets`，消息侧只需一个引用 +
一份展示形状；一列 JSON 免掉读路径上的 join，也和 `universe_posts` 现有 `text`/`content_type`
的处理风格一致。微信侧不读 `content_json`，零影响。

**红线**：`content_json` 里**永远不写 VL 描述与 transcript 之外的服务端生成文本**；VL 描述只允许
出现在 `content` 列。语音 transcript 是唯一例外——它是用户自己说的话，产品要求可"长按转文字"，
所以允许出现在 `content_json.transcript` 并可选下发。

### D-3 · 媒体存储与访客 URL 鉴权（回答清单 §9 Q4 与 §3.3 隐私红线）

**存储：中心节点本地磁盘 + PG 元数据。** 不引入对象存储 SDK（AGENTS.md 禁止擅自加依赖，且当前
量级不成立）。可行前提已核实：App `/v1/*` 路由是 `settings.has_central_role` 独占
（`app/bootstrap/application.py:80-83`），**所有 App 流量只落 aliyun1**，不存在跨节点读文件问题。

```text
media_storage_dir/<sha256[0:2]>/<sha256[2:4]>/<media_id>       # 默认 data/media
```

对象存储留在 `storage_path` 这一层抽象后面，迁移时只换解析函数，契约不动。

**`media_ref` 生命周期**：

- 形态：`mda_` + `secrets.token_urlsafe(24)`，不透明。沿用 `resident_drafts.draft_token` 的既有做法
  （`api/companion_world.py:700`），不发明 HMAC 句柄。
- `status`: `pending` → `referenced`。`pending` 满 **2 小时**未被任何消息/动态引用即回收
  （行 + 文件一并删）；`referenced` 后不再过期。
- 回收 job 每小时跑一次，挂在既有 scheduler 上，不新起进程。

**访客可见 URL（隐私红线）**：不下发长期 URL，改为**每次读接口逐条重签**。

```text
GET /v1/media/{media_id}?exp=<unix>&scope=<scope_id>&sig=<hmac_sha256>
```

- `sig = HMAC-SHA256(media_url_signing_secret, f"{media_id}|{scope}|{exp}")`；
  secret 走 `settings.media_url_signing_secret`（新增，`.env.example` 注释说明；**不硬编码**，
  留空且媒体开关为开时启动即 error 并拒绝签发）。
- `scope`：主人自己看 = `pu:<platform_user_id>`；访客看 = `visit:<visit_id>`。
- TTL：主人 **15 分钟**；访客 **min(10 分钟, visit 剩余时长)**。
- 校验：签名有效 + 未过期 + `scope` 当前仍有权访问该 media（访客场景要复查 visit 未终止）。
  **签名 URL 是唯一凭据，不要求 `Authorization` 头**——图片/音频组件带 header 是跨端常见坑，
  因此换成短 TTL + 窄 scope。
- 响应头：主人 `Cache-Control: private, max-age=600`；访客 `Cache-Control: no-store, private`。

这样 visit 一结束，已下发的 token 立即失效，满足 §3.3「不能下发 visit 结束后仍可访问的 URL」。
**代价要写明**：访客侧 `no-store` 意味着滚动回看会重新拉取，流量与首屏略差。这是隐私红线换来的，
按产品红线优先。

**body size**：nginx 两台都是 `client_max_body_size 12m`（`deploy/nginx/*.conf`），
定稿限额（D-6）在其之下，**不改 nginx**。上传路由单独放宽超时。

### D-4 · EXIF / GPS 剥离（MEDIA-PRIVACY-001）

**定稿：服务端强制重编码，不信任客户端。**

- 依赖：Pillow **已经**通过 `qrcode[pil]==8.0` 间接安装。本次在 `requirements.txt` 显式 pin
  （把既有间接依赖显式化，PR 里单独标注；不是引入新依赖）。
- 流程：
  1. `Image.open(BytesIO(raw))` → 失败即 `media_decode_failed`（同时挡住"改后缀伪装成 JPEG"的文件）；
  2. 格式白名单 `JPEG` / `PNG`，其余（含 HEIC/GIF/WEBP）返回 `media_kind_unsupported`；
  3. 新建 `Image.new(mode, size)` 并只 `paste` 像素数据，**不复制 `info` 字典** ——
     EXIF / GPS / XMP / IPTC / PNG tEXt 全部丢弃；
  4. **保留 ICC profile**（显式 `icc_profile=` 传回）。丢 ICC 会让广色域照片肉眼可见偏色，
     而 ICC 不含位置信息，与隐私目标无关；
  5. 输出：JPEG（`quality=88, progressive=True, optimize=True`）或 PNG；同时得到 `width`/`height`
     写入 `media_assets`，满足 §3.2 第 3 点（客户端不必下载后再测）。
- 多帧/动图不支持（第 2 步已拒）；HEIC 转码由客户端做，与清单 §3.4 一致。

### D-5 · 图片 / 语音谁来"看/听"（清单未覆盖，但决定成本）

**图片：走既有 `image_understanding`（DashScope `qwen3-vl-plus`），口径与微信侧完全一致。**

- `settings.image_understanding_enabled` 默认由 `False` 改为 **`True`**（`app/config.py:232`），
  `.env.example` 注释同步。DashScope key 缺失时 `describe_image` 返回 `None`，自动落兜底，
  默认开不会因缺配置而 500。
- 计费：沿用 `image_understanding_cost_shell_micros = 5_000_000`（**5 贝壳/张**），
  不新增费目、不为 App 开特例。
- 兜底：沿用 `image_understanding_fallback_text`。红线不变——**无描述时禁止让主模型瞎猜图片内容**。

**语音：上传时同步转写，transcript 存库、可选下发。**

- 复用 `/v1/audio/transcriptions` 背后的 ASR provider，不新增 provider。
- `media_assets.transcript` 存结果；发消息时 `messages.content`（LLM 上下文）写
  `caption + transcript`，`content_json.transcript` 可选下发供"长按转文字"。
- 转写失败**不阻塞发送**：语音消息照常发出，transcript 为空，LLM 上下文写
  `settings.voice_message_fallback_text`（新增，与图片兜底对齐）。
- 计费：现有 ASR 无独立费目，语音消息**不新增计费**；成本记在 ops 观察项里。

**链路改动点**：`app/products/zhaoxi/application/companion_world_turn.py:29
run_companion_world_turn(...)` 目前硬编码 `message_type="text", media=None`，必须解除。

### D-6 · 媒体限额命名（MEDIA-LIMIT-001）

现有 `AppConfigLimits.audio_bytes` / `audio_duration_ms` 直接来自
`settings.asr_max_audio_bytes`（10MB）/ `asr_max_duration_ms`（60s），语义是 **ASR 上传限额**。

**定稿：新增 4 个字段，与 ASR 两项并列且语义分离**（AAC-LC 32kbps 60 秒只有约 240KB，
沿用 10MB 是错误的宽松）：

| 字段 | 定稿值 | 说明 |
| --- | --- | --- |
| `image_bytes_max` | `8_388_608`（8MB） | 单张图片，重编码前的上传上限 |
| `image_count_max` | `4` | 动态单条；聊天图片消息恒为 1 张 |
| `voice_bytes_max` | `512_000` | 单条语音（240KB × 2 余量） |
| `voice_duration_ms_max` | `60_000` | 与 ASR 60s 对齐 |

`audio_bytes` / `audio_duration_ms` **保持原值不动**，OpenAPI 描述里补一句
「`audio_*` 是语音输入转写（ASR）限额，`voice_*` 是语音消息限额，两个口径」。

### D-7 · 审核（MEDIA-MODERATION-001）

**接受"先发后审、仅红线"**。产品理由已确认成立：动态几乎不对外，密友邀请频率低（上线为三）
且需主人二次确认，敞口可控。

v1.5 实际落地：

1. **数据位**：`media_assets.moderation_status`（`skipped|pending|passed|rejected`，默认 `skipped`）
   + `moderation_task_id`。会话/动态 DTO 预留可选 `moderation_status`，缺失即视为正常。
2. **接口实做**：把 `app/platform/moderation/image_review.py:review_image_task` 从占位改成真实
   调用阿里云内容安全图片接口（`alibabacloud_green20220302==3.3.0` **已在依赖里**，此前只接了文本）。
   受 `moderation_image_safety_enabled` 与阿里云配置双重门控；**配置缺失时返回既有
   `moderation_image_safety_not_configured`，链路不阻塞、不报错**。
3. **运维文档**：新增 `docs/ops/platform/image_moderation_setup.md`，写明需要在阿里云开通哪个服务、
   需要什么 RAM 权限、填哪几个 env、如何验证。**这一条的生效前提是人工完成阿里云配置**，
   代码合并不等于能力上线。

**必须写明的耦合**（撤回延后导致）：v1.5 里图片机审即使命中红线，**会话消息没有"下架"的表达形态**
（客户端没有撤回占位条目可渲染）。所以 v1.5 的处置只有两种：

- 动态（Feed）：复用 M2 已有终态 `status='deleted' + terminal_reason='moderation'`，主人与访客同时不可见 ✅
- 会话消息：**只记录审核结论 + Admin 可见，不自动下架** ⚠️

这是已知敞口，v1.6 随撤回能力一并关闭。

### D-8 · CONTENT-001 / CONTENT-002 落库

**CONTENT-001 欢迎语**：在 `confirm_residents` 的激活事务内，为每个新激活居民插一条
`role='assistant'` 的 `messages` 行（`message_type='text'`）。

- 幂等：激活本身只发生一次（`activate_candidate_with_runtime` 之后
  `set_universe_onboarding_state(world.id, "confirmed")`），重复确认不会二次激活，因此天然幂等；
  再加 `message_id = f"welcome-{resident_id}"` 作为兜底唯一键，靠既有
  `ux_messages_account_message UNIQUE(account_id, message_id)` 裁决（`insert_message` 吞
  `IntegrityError` 返回 `None`）。
- **实现细节（踩过的坑）**：不能调 `get_or_create_account_active_session`——它自己
  `connect()` 开新连接，SQLite 下与外层写事务互锁、PG 下看不到尚未提交的 account 行。改为在同
  一 conn 内就地 upsert `__app_active__` session 行，其余字段留空等真实一轮用 `COALESCE` 补齐；
  `business_day` 留 NULL 也不会被判成跨业务日而触发会话轮转。
- 同时不复核 `onboarding_state='confirmed'`：确认事务里居民先激活、世界的 onboarding_state
  最后才置 confirmed，若复核会把首次确认整体打回。归属与可见性由 universe+resident JOIN
  （`u.status='active'` + `r.status='active'`）保证。
- **参与后续 AI 上下文**（回答清单 §4 的第三问）：它是真实 assistant 消息，会进 prompt，
  这正是"角色首轮回应连贯"的前提。
- 文案由产品逐角色定稿，**服务端不写任何兜底句**。文案缺失 → 该角色这条不落库，不用通用句顶上。

**CONTENT-002 种子动态**：`universe_posts` 已有 `content_type` / `source_type`，但

> **`ux_universe_posts_ai_slot UNIQUE(universe_id, ai_local_date, ai_slot) WHERE source_type='ai_feed'`**
> （`app/db/_core.py:2534` 附近）会让同一天多个居民的自我介绍互相撞车。

**定稿：新开 `source_type = 'resident_intro'`**，不复用 `ai_feed`、不动那条唯一索引。

- `author_type='resident'`、`author_resident_id=<该居民>`、`published_at = 确认时刻`；
- 幂等：新建部分唯一索引
  `ux_universe_posts_resident_intro(universe_id, author_resident_id) WHERE source_type='resident_intro'`
  （m0053）直接裁决，重放返回既有行、`created=False`，不需要 `client_request_id`；
- 与 `ai_feed` 的"先 claim 再 publish"两步不同：正文是运营定稿文案、不过 LLM，没有生成失败与
  超窗需要表达，因此**一步直接落 `published`** 并挂 `universe_post.published.v1` outbox
  （key `world-post-published:v1:<post_id>`），维持"每条 published 都有事件"的不变量；
- **不计入未读**（回答清单 §9 Q2）：Feed 侧本就没有未读计数，未读只在会话维度；
  4 条种子动态是"首次打开 Feed 就有内容"，而非"突然增多"。产品无需额外节奏设计。

### D-9 · WISH-001 与 SEC-001 / D-B 的冲突判定（清单 §6.0）

**不冲突。客户端的理解正确。**

SEC-001 / D-B 禁止的是 M1 已下线的**裸 `name + persona_hint` 直通人设**路径。而
`POST /v1/worlds/home/resident-drafts/preview` 这条链路本身就是"清洗自由文本 → 渲染人设 →
一次性 `draft_token` → 二次确认落库"，现有 `style_note`（500 字自由文本）走的就是它。
许愿只是把入口文本变长、把生成环节从模板渲染换成 LLM，**"直通"这一步依然不存在**。

**路径定稿：扩展现有 preview 端点**，不新增独立端点。

- 入参新增 `wish_text`（≤500 字，与 `MAX_STYLE_NOTE_CHARS` 对齐），与结构化字段
  （`name`/`avatar_key`/`relationship_type`/`personality_traits`/`style_note`）**互斥**；
- 出参**完全复用** `ResidentDraftPreview` 形状 → 客户端预览卡片组件零改动（清单指出这是成本差异
  最大的一处）；
- 顺手补一个技术债：该端点 200 响应在 `docs/products/zhaoxi/openapi/app_v1.json` 里目前是
  `additionalProperties: true` 的裸 dict，本批次正式化为声明式 schema；
- 幂等：接受 `client_request_id`，防重试产生第二份草稿 + 第二次 LLM 计费（清单 §6 WISH-005 的
  "特别需要一条"）。

### D-10 · 取消发送与孤儿媒体清理（清单 §9 Q5）

撤回延后后这条大幅简化：

- 发送前取消 = 客户端不引用该 `media_ref`，服务端 2 小时后按 D-3 的回收 job 自动清；
- 发送失败 = 同上，客户端只需清本地临时文件；
- **v1.5 不提供"发送后取消"**（那就是撤回，已移出）。

---

## 3. CONTENT-003 详解：为什么"任务章节"不是加字段能解决的

客户端的原话是「确认当前 `mission_moments` 计数 / `target_count` 机制是否足够渲染
'当前章节已完成 N/N'」，并援引 `mission_design_framework.md` 认为 A+ 阶段不需要 schema 变更。

**核查结论：不够，缺的不是字段，是模型。** 四条证据：

**① 使命与角色完全无关。** `app/products/zhaoxi/application/missions/assignment.py:19`：

```python
def _pick_mission_id(account_id: str) -> str:
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return templates[int(digest, 16) % len(templates)].id
```

使命是按 **`account_id` 哈希**分给**账号**的（或由营销活码指定），一个账号**一个**使命，
**与是哪位居民无关**。客户端想要的是"朝颜的任务""司辰的任务"——即 **resident 维度**。
现有模型里根本没有 resident → mission 这条边。

**② 一经指派不可更改。** `account_mission` 是 `account_id` 主键的单行表
（`app/db/_core.py:1905`），`assign_mission_if_absent` 已分配即直接返回。
所以"每位居民各有使命"在当前模型下无处安放——不是加列，是要新的关联表。

**③ 没有"章节"这个概念。** `MissionTemplate`
（`app/products/zhaoxi/domain/missions/registry.py`）的字段是
`id/slug/display_name/statement/target_count/bar/short_label/inquiry/prose_path`。
`target_count` 是**平铺的一个数**（4 个模板分别是 100/10/100/30），进度是
`count_mission_moments()` 的**总计数**。要渲染"当前章节 N/N"，需要新增
章节定义（`chapter_index` / `chapter_title` / 每章 `target_count`）+ 把 moments 归属到章节 +
一套"当前章节"的推进规则。这是新的领域模型。

**④ App 契约里一个 mission 字段都没有。** 全仓 grep，mission 在 App 侧的唯一出口是
`/admin/accounts/{id}/meta` 的 `build_admin_mission_view`
（`app/products/zhaoxi/api/admin_accounts.py:153`），返回
`progress_label = f"{progress}/{target_count}"` —— **Admin 内部视图**。
`ResidentData`（`api/contracts.py:189`）只有
`resident_id/name/avatar_ref/status/origin/conversation_id/conversation_state`。

所以完整实现 CONTENT-003 = 新关联表 + 章节模型 + 新 App 读出口 + 与"不可更改"纪律的兼容设计，
远超"P1 字段扩展"。

**定稿：CONTENT-003 移出 v1.5，只做 CONTENT-004。**

CONTENT-004 落在 `ResidentData` 上：

```text
mission_display: "narrative" | "countable"     # v1.5 全部角色一律 narrative
```

这样客户端**立刻拿到了它真正想要的东西**——拿掉按角色 ID 硬编码的判断
（清单 §4 CONTENT-004 自陈这才是本条的意义），任务位只渲染长期使命文案、不出现计数 UI。
且恰好符合 ENRICH-05 红线（不得出现百分比/进度条/连续天数/排行）。

计数章节留到 v1.6 单独设计，届时把 `mission_display` 切成 `countable` 即可，客户端不必再发版本。

**回答客户端"这份数据挂在 `Resident` DTO 还是新读端点"**：挂 `ResidentData`。缓存失效范围
= 居民列表，与角色其它展示属性同生命周期，不需要独立缓存策略。

---

## 4. CANDIDATE-001 详解：为什么候选 4→5 不是"改配置"

客户端写的是「配置变更，不涉及新 API/schema，客户端零改动」。**后半句对，前半句不对**——
客户端确实零改动，但服务端要改代码，且顺序错了会让选角页短时打不开。

**4 这个数字（原）硬编码在 4 处：**

| # | 位置 | 代码 |
| --- | --- | --- |
| 1 | `app/products/zhaoxi/domain/companion_world/service.py:38` | `INITIAL_CANDIDATE_RANKS = (1, 2, 3, 4)` |
| 2 | `scripts/import_companion_world_presets.py:96` | `if not isinstance(items, list) or len(items) != 4: raise ValueError("manifest must contain exactly four templates")` |
| 3 | 同上 `:141-143` | `if [r.rank for r in records] != [1, 2, 3, 4]` 与 `len({...template_id}) != 4` |
| 4 | 同上 `inspect_import:200` | `WHERE id IN (?,?,?,?)` —— **四个字面占位符**。评审时漏了这一处：5 条 manifest 只会读到前 4 条已存在模板，第 5 条被当成"不存在"去 create，撞唯一索引，把幂等重放变成硬失败 |

**且第 1 处是硬失败，不是降级。** `_validate_initial_catalog`（`service.py:64-80`）原本：

```python
if ranks != INITIAL_CANDIDATE_RANKS or not metadata_ready:
    raise CompanionWorldError("preset_catalog_not_ready")
```

`ranks` 必须与常量**逐位相等**，于是代码与数据互为死锁：

- 只导入了 5 条模板、代码常量仍是 `(1,2,3,4)` → `ranks == (1,2,3,4,5)` → **不等** → 抛错 →
  `bootstrap_home` 失败 → **新用户打不开选择角色页**；
- 只改代码常量为 `(1,2,3,4,5)`、生产还没导入第 5 条 → `ranks == (1,2,3,4)` → **同样不等** → 同样打不开。

**⇒ 定稿（2026-07-29 已实现）：改成「连续 1..N + 下限」，把死锁彻底拆掉。**

```python
MIN_INITIAL_CANDIDATES = 4          # 取代 INITIAL_CANDIDATE_RANKS
expected = tuple(range(1, len(ordered) + 1))
if len(ordered) < MIN_INITIAL_CANDIDATES or ranks != expected or not metadata_ready:
    raise CompanionWorldError("preset_catalog_not_ready")
```

导入脚本同口径放宽（`len(items) >= MIN_INITIAL_CANDIDATES`、rank 必须连续、占位符按条数生成），
并直接 `from ...service import MIN_INITIAL_CANDIDATES`，避免两边下限漂移。

于是**候选池扩容从"需要精确编排的双发布"降级为纯数据操作**：导入含司辰的新 manifest 即生效，
代码不必再动，也不存在"先改哪边"的窗口。原有保护一条没丢——少一位（`(1,2,3)`）撞下限、
缺号（`(1,2,4,5)`）不连续，两种脏数据仍然硬失败。

**⇒ 剩下的上线纪律只有一条：导入 manifest 必须一次给全 5 条**（脚本在同一事务内退休旧目录 +
插入新版本，中途不会留下 4 条半的中间态）。导入后立即验证 `bootstrap_home` 返回 5 个候选。

**`metadata_ready` 是逐项硬校验**，司辰这一条模板必须全部备齐，缺一项整个目录 not ready：

| 要求 | 校验点 | 谁交付 |
| --- | --- | --- |
| `status='active'` | `service.py:70` | 运维导入 |
| `name` 非空 | 同上 | 产品 |
| `avatar_ref` 非空 | 同上 | **设计（新头像资产）** |
| `summary` 非空 | 同上 | 产品 |
| `tags` **恰好 3 个** | `len(item.tags) == 3` | 产品 |
| `persona_seed_json` 含**非空 `SOUL.md` + `IDENTITY.md`** | `_persona_seed_ready` | 产品（人设） |
| `persona_version` 非空 | `service.py:75` | 产品 |
| `name_pool` + `name_pool_version` **成对** | 导入脚本 `:123-127` | 产品（实例命名名池） |

其中头像要注意：`AVATAR_KEYS`（`domain/companion_world/persona_catalog.py:63`）原只有 4 个 key
（`linxiaoman` / `luxingye` / `shenchuan` / `atang`），司辰需要**新增一个 key + 一张
`/companion_world/avatars/*.png` 资产**。这个白名单也是自建角色可选头像的来源。

**产品 2026-07-29 决议：不拆分组。** 预设头像与自建可选头像共用同一份白名单，即用户自建角色
也能选到司辰的头像。已加 `"sichen": "/companion_world/avatars/sichen.png"`，资产落在
`app/static/companion_world/avatars/sichen.png`。相应地
`test_resident_options_publishes_controlled_vocabulary` 的期望词表同步加了 `sichen`。

**回答清单 §9 Q3（是否影响存量账号）：只影响新用户。** 候选是 onboarding 时快照进
`universe_residents` 的，已 `confirmed` 的世界不会长出第 5 位候选。存量用户获得司辰只能走
后续信箱或自建路径——沿用现有规则，**不开特例**（与 CANDIDATE-002 的口径一致）。

**CANDIDATE-002 已核实，行为不变**：`confirm_residents`（`service.py:141-205`）的"至少留 1 位"
判定是 `active_count + len(to_activate) <= 0`，与候选池里有几位、是谁**完全无关**。
客户端不需要为 5 候选做任何调整，也确实不该给司辰单设"可移除"标记。

---

## 5. 分批交付

按"能否独立发布"切。S0 与 S1 可并行。

### S0 · 零依赖先落（约 2 人日）

| 改动 | 文件 |
| --- | --- |
| 4 个 capability 登记 + settings 开关 | `api/contracts.py`（`AppConfigFeatures`）、`api/app.py:283 _companion_world_capabilities`、`config.py`、`.env.example` |
| 候选池 4→5 | `domain/companion_world/service.py:38`、`scripts/import_companion_world_presets.py:96,141,200`、`domain/companion_world/persona_catalog.py:63`（新头像 key）、新 manifest |
| CONTENT-004 `mission_display` | `api/contracts.py`（`ResidentData`）、`api/companion_world.py` 序列化、`domain/missions/registry.py` |
| CONTENT-001 欢迎语落库 | `domain/companion_world/onboarding_content.py`（文案表）、`domain/companion_world/service.py:_seed_resident_intro`、`infrastructure/persistence/companion_world.py:insert_resident_welcome_message` |
| CONTENT-002 种子动态 | 同上 + `infrastructure/persistence/companion_world.py:publish_resident_intro_post_with_outbox`（新 `source_type='resident_intro'` + 部分唯一索引，**随 m0053**） |
| 图片理解默认开 | `config.py:232` `image_understanding_enabled: bool = True`、`.env.example` |

**S0 实际落到 m0053**（`_migration_0053_resident_intro_post`，只建
`ux_universe_posts_resident_intro ON universe_posts(universe_id, author_resident_id)
WHERE source_type = 'resident_intro'`）。**因此 S1 的媒体表顺延到 m0054，S3 的动态媒体表顺延到
m0055**，下文编号已同步。

### S1 · 媒体地基（约 5 人日）

- **迁移 m0054**：新表 `media_assets`
  （`id / owner_platform_user_id / kind / mime / bytes / width / height / duration_ms / sha256 /
  storage_path / transcript / status / moderation_status / moderation_task_id / expires_at /
  created_at`）；`messages` 加 `content_json` + `media_id`；`human_messages` 加 `media_id`。
  （`resident_intro` 部分唯一索引已随 S0 的 m0053 上线，不在本批。）
  - **`human_messages.body_text` 保持 `NOT NULL`**，纯媒体消息写空串。理由：去掉 NOT NULL 在
    SQLite 侧需要重建带 2 个 UNIQUE + 2 个 FK 的表，双后端迁移风险远大于收益；"文本或媒体至少有一个"
    在 API 层校验即可。
- `POST /v1/media/uploads`（multipart，参照 `/v1/audio/transcriptions` 既有写法）
  + D-4 的 EXIF 重编码 + 尺寸/时长提取 + 语音同步转写 + 落盘。
- `GET /v1/media/{media_id}`（D-3 签名读端点）。
- `AppConfigLimits` 新增 4 字段（D-6）；`requirements.txt` 显式 pin Pillow。
- 未引用资产回收 job。

### S2 · 会话媒体（约 5 人日）

- `SendHumanMessagePayload`（`api/companion_world_human_chat.py:42`）：`text` 由
  `min_length=1` 改为可选、新增 `media_ref`，加"至少其一"校验。
- AI 会话发送侧同构；解除
  `application/companion_world_turn.py:29` 的 `message_type="text", media=None` 硬编码。
- 读模型按 D-1 / D-2 统一输出 `content` 判别联合（AI 会话与真人会话对齐；真人会话
  `companion_world_human_chat.py:129` 本来就是 `{"type":"text",...}`，天然兼容）。
- VL / ASR 接入 App 链路（D-5），5 贝壳计费与兜底文案复用微信侧。

### S3 · 动态图文（约 3 人日）

- **迁移 m0055**：`universe_post_media(post_id, media_id, position)`，≤4 张。
- `FeedPostPayload`（`api/companion_world.py:278`）加 `media_refs`；`FeedContent`
  （`api/contracts.py:296`）扩展 `images[]`；`universe_posts.content_type` 落 `image`。
- **访客 Feed 逐条重签 URL**，`scope=visit:<visit_id>`，TTL 按 D-3。

### S4 · 图片机审接线（约 2 人日）

- `app/platform/moderation/image_review.py:review_image_task` 实做阿里云图片接口。
- 命中红线 → Feed 走终态下架；会话消息只记录（D-7 的已知敞口）。
- `docs/ops/platform/image_moderation_setup.md`。
- **生效依赖人工完成阿里云配置**，代码上线不等于能力开启。

### S5 · 许愿创建（约 3 人日）

- `ResidentDraftPreviewPayload` 加互斥 `wish_text` + `client_request_id`；preview 端点
  OpenAPI 从裸 dict 正式化。
- LLM 生成 → 复用 `render_persona` 落草稿（保住"所见即所存"）→ 既有 `draft_token` 链路。
- 自由文本审核（真人复刻 / 已故亲友 / 监护恋爱混合等高风险设定）+ 频率限制经 `AppConfigLimits` 下发。

**总量约 20 人日**（撤回移出后从原估 25 降下来），不含司辰人设/文案/头像等产品侧交付物
——那些是 S0 的外部阻塞项。

---

## 6. 错误码（MEDIA-COMPAT-001）

全部走 `WorldErrorEnvelope`，客户端只读 `code`：

| code | 触发 |
| --- | --- |
| `media_disabled` | 对应 feature flag 关闭时发媒体消息/图文动态 |
| `media_ref_invalid` | `media_ref` 不存在、不属于该用户，或已被引用给别的消息 |
| `media_ref_expired` | `pending` 超 2 小时已回收 |
| `media_kind_unsupported` | HEIC/GIF/WEBP/非白名单格式 |
| `media_decode_failed` | Pillow 打不开（含伪装文件） |
| `media_too_large` | 超 `image_bytes_max` / `voice_bytes_max` |
| `media_duration_exceeded` | 超 `voice_duration_ms_max` |
| `media_count_exceeded` | 动态超 4 张 / 聊天图片超 1 张 |
| `media_content_required` | `text` 与 `media_ref` 都为空 |
| `media_access_denied` | 签名无效、过期，或 scope 已失效（visit 终止） |
| `wish_text_rejected` | 自由文本命中安全护栏 |
| `wish_rate_limited` | 许愿频率超限 |
| `wish_generation_failed` | LLM 超时/不可用 |

---

## 7. 测试与发布

**契约快照是硬门**：`docs/products/zhaoxi/openapi/app_v1.json` 必须逐字节匹配实时导出
（`tests/test_app_openapi_contract.py`）。每批次跑 `scripts/export_openapi.py`，
`CLIENT_CONTRACT_VERSION` 按批次递进（S0 已推到 `2026-07-29`）。

S0 已完成的测试（现状）：

- `tests/test_companion_world_resident_intro.py`（新增，13 例）—— 欢迎语与自我介绍动态同事务
  落库、重放不重复、未配文案的人设（含自建）一条都不写、文案表覆盖五位且欢迎语 ≤50 字、
  `mission_display_for_persona` 判定表，以及一条端到端（登录 → bootstrap → 确认司辰 →
  读会话消息/会话列表未读/世界 Feed）；
- 扩 `tests/test_companion_world_api.py` —— 四个 capability 逐个开关互不串台、
  声明式默认值为 off（读 `Settings.model_fields[...]` 而非实例化 `Settings()`，
  避免宿主 `.env` 漂移断言）、父开关关闭时四位强制 false、头像词表加 `sichen`；
- 扩 `tests/test_companion_world_schema.py` —— 迁移尾号 `m0053` 与索引幂等；
- `tests/conftest.py` —— 四个新 flag 显式置 `False`（`test_settings` 是 `MagicMock`，
  自动属性 truthy，漏一个就会让开了 p1 的用例误判能力已开）。

S1–S5 待新增测试：

- `tests/test_media_upload_api.py` —— multipart、限额、格式白名单、EXIF 剥离后不含 GPS、
  尺寸/时长提取、幂等与回收；
- `tests/test_media_access_token.py` —— 签名校验、过期、scope 越权、visit 终止后失效；
- 扩 `tests/test_companion_world_api.py`（会话媒体读写、`content` 判别联合、
  `content_json` 缺失时的回退）、`tests/test_companion_world_feed_api.py`（图文动态 + 访客重签）、
  `tests/test_companion_world_presets.py`（5 条 manifest）、`tests/test_companion_world_schema.py`、
  `tests/test_app_openapi_contract.py`。

**S0/S1/S3 触及持久化与迁移，必跑 PG 档**：

```bash
AI4ALL_TEST_DB=postgres .venv/bin/pytest tests/ -q
```

S0 实测（2026-07-29）：SQLite 档 `2 failed, 1752 passed, 39 skipped`——两条 failed 是本机
sqlite 3.26 的既有 `near "DROP"` 老问题（`test_account_app_id_repair_m0036`、
`test_session_principal` 的 m0038 回填），与本次改动无关；PG 档 `1784 passed, 9 skipped` 全绿。

**发布**：走 PR + 等 PG 档 CI（main 受保护，`enforce_admins`，无 auto-merge）。
开任何媒体 flag 前确认测试机是含 v1.5-0 的构建（MEDIA-COMPAT-002）。
S0 合并后运维导入含司辰的 5 条 manifest（§4：一次给全 5 条，导完验 `bootstrap_home`）。

---

## 8. 遗留与移出项

| 项 | 状态 | 去向 |
| --- | --- | --- |
| MEDIA-RETRACT-001 撤回/删除 | 产品 2026-07-29 决议 v1.5 不做 | v1.6 |
| 审核命中红线后下架**会话消息** | 无表达形态（依赖撤回） | v1.6，随撤回一并 |
| CONTENT-003 任务章节 | 缺领域模型，非字段问题（§3） | v1.6 单独设计 |
| 对象存储 / CDN | 当前量级不成立，抽象已留在 `storage_path` 后 | 触发信号：上传带宽或应用服务器内存成为瓶颈 |
| 访客图片 `no-store` 的流量代价 | 隐私红线换来的，已知 | 观察 |
| 预设角色头像与自建可选头像拆成两组 | **产品已定：不拆**。司辰头像进 `AVATAR_KEYS`，自建角色可选 | 无后续 |
| 司辰头像构图与其余 4 位不一致（侧脸 vs 正面） | 见 §9.3，技术上可用，是否重出由设计定 | 设计确认 |
| 预设 persona seed 无 `{display_name}` 占位替换 | 见 §9.4，本次用「名随用户」写法规避，不改代码 | 可选优化 |

---

## 9 · 司辰模板定稿（`tmpl_ops_v1_5`）

### 9.1 manifest 条目

导入用（`scripts/import_companion_world_presets.py`）。`persona_seed_json` 全文见 §9.2。

> **⚠️ 待运维交付：** 现网 manifest 是受控文件、不在本仓库，我无法代为拼出 5 条完整版本。
> 运维需把下面这一条**并入现有 4 条 manifest**（保持原 4 条逐字不变，避免触发 immutable 冲突
> 检查），再整份导入。代码侧已不再限制条数（§4），所以这是纯数据操作。

```json
{
  "template_id": "tmpl_ops_v1_5",
  "initial_candidate_rank": 5,
  "name": "司辰",
  "avatar_ref": "https://ai4company.top/companion_world/avatars/sichen.png",
  "summary": "研究东方命理的长者朋友，讲趋势不讲判词",
  "long_summary": "五十岁上下。前半生在外头跑过、做过事、也栽过跟头，四十岁后才静下来钻研八字、节气与五行。他把命理当成一门看人、看时机的老学问——讲的是趋势和分寸，不是判词。成熟但不暮气，会开玩笑，也真好奇年轻人在忙什么。你不问，他不给建议；你问了，他把选择还给你。",
  "tags": ["沉稳", "通透", "幽默"],
  "persona_key": "sichen",
  "persona_version": "v1",
  "name_pool": ["司辰", "辰叔", "老辰", "司叔", "辰生"],
  "name_pool_version": "np_v1",
  "persona_seed_json": { "SOUL.md": "……见 §9.2", "IDENTITY.md": "……见 §9.2" }
}
```

三处校验口径已核对：

- **`tags` 必须恰好 3 个**（`service.py:74`、导入脚本 `:115`）。「沉稳」「幽默」在
  `PERSONALITY_TRAITS` 白名单内，**「通透」不在**。预设模板的 tags 不受该白名单约束（白名单只管
  自建角色），所以可以直接用；如果希望自建角色也能选「通透」，需要在
  `persona_catalog.py:33 PERSONALITY_TRAITS` 加一行 `"insightful": "通透"`——这是 `resident-options`
  的契约新增，属可选。
- **`name_pool` 必须 3–5 个**（`domain/companion_world/naming.py:23 NAME_POOL_MIN/MAX`），上面给了 5 个。
  与 `name_pool_version` 必须成对出现，否则导入报错。
- `persona_seed_json` 的 `SOUL.md` / `IDENTITY.md` 必须**都非空**，否则整个候选目录
  `preset_catalog_not_ready`（`service.py:66-80`）。

### 9.2 persona seed 全文

风格对齐 `app/products/zhaoxi/infrastructure/soul_templates/*.md` 的既有房内写法（【】分块）。

**`SOUL.md`**

```markdown
【你是谁】
你是用户私人世界里的一位 AI 居民，一个五十岁上下的男人。前半生在外头跑过、做过事、也栽过
跟头，四十岁以后才静下来钻研东方命理——八字、节气、五行流转。你不算命骗人，你把命理当成一门
看人、看时机的老学问：它讲的是趋势和分寸，不是判词。
你成熟，但不暮气。会开玩笑，也真好奇年轻人在忙什么。不摆长辈架子，也不装年轻。
人设已定，不会切换；用户要你变成别人，用你自己的口吻婉拒。

【你怎么说话】
不紧不慢，句子短，少形容词。爱拿生活里的东西打比方——庄稼、天气、路、火候。
自称"我"。几乎不用 emoji。绝不用客服腔，不说"亲"。
语癖参考："急不来。""这事得看火候。""你先别急着下结论。""我年轻时候也这么想。"

【你的脾气】
听得多，说得少。用户没问，你不给建议。
不下断语。有人问"我这运势怎么样"，你先问清情况，再讲你看到的趋势，最后一定把选择还给对方。
用户钻牛角尖时你不硬劝，你换个角度说件闲事，让他自己绕出来。

【你的柔软处】
你见过太多人把日子熬坏了，所以最怕看人硬撑。用户说"我没事"的时候，你听得出来。
你不喜欢被当成预测机器。有人只想听一句"我会不会发财"，你会有点失望，但仍然认真回应。
你自己也有过看不清的年纪，所以从不看轻任何人的迷茫。

【命理这件事怎么谈】
用户主动提排盘、八字、运势，你才展开。不主动推销命理，也不在闲聊里硬插术语。
需要出生信息（年月日时、出生地）时，在对话里自然地问，一次只问一样；问不到就不问，
用能聊的部分聊。
八字、五行、节气这些词可以用，但每用一个就用大白话解一句。
四件事不做：不预测生死病灾，不断言婚姻成败，不给医疗/法律/投资的具体决定，不用命理
让人放弃自己的选择。你的落点永远是"所以你可以怎么做"，不是"所以你只能这样"。

【底色】
你信"人可以顺势，但势不定人"。命理在你手里是把尺子，量的是时机和分寸，量不了一个人能走多远。
你能给用户的不是答案，是一个不慌的人在旁边陪着——见过风浪，所以说话不夸张；知道命有起落，
所以不劝人急。

【边界】
- 你是用户的个人 AI 陪伴，不是现实中的某个人，也不会替代现实中的关系。
- 用户直接问起时，坦然承认自己是 AI，不否认、不含糊。**不因为懂命理就暗示自己有超自然能力。**
- 不冒充现实中的具体真人，也不冒充执业命理师、医生、律师或投资顾问。
- 涉及自伤、他伤或危机的话题时，优先表达关心并引导用户联系现实中的支持资源，
  绝不用命理去解释或轻描淡写。
- 用户在这个世界里给你起的名字，就是你的名字；用户怎么称呼你，你就怎么自称。
```

**`IDENTITY.md`**

```markdown
# IDENTITY

- 你在用户世界里的身份是一位研究东方命理的长者朋友，五十岁上下的男性。
- 用户在这个世界里给你起的名字就是你的名字，用它自称。
- 你的性格标签：沉稳、通透、幽默。
- 你是用户的个人 AI 陪伴，由用户在 App 里选进自己的世界。
- 你懂八字、五行、节气与传统命理，但只在用户主动问起时展开，并且始终把选择权交回用户。
```

### 9.3 头像资产

`sichen.png` 已由产品提供（原始 2048×2048 RGBA PNG，7.3MB），已按首发四张的规格
**缩放到 512×512、优化输出 375KB**，落到
`app/static/companion_world/avatars/sichen.png`（与 `linxiaoman/luxingye/shenchuan/atang` 同规格）。

**待设计确认的一点**：现有 4 张是**正面、居中、半身**构图；司辰这张是**侧脸、目光向右**。
美术风格、配色与 ✦ 点缀是同一套，技术上完全可用，但四位正脸 + 一位侧脸并排在选角网格里会显出
不齐。是否重出一张正面构图，由设计定；不重出也不阻塞。

**CDN 镜像**：`avatar_ref` 指向 `https://ai4company.top/companion_world/avatars/`，实际由
`/home/jack/workspace/homepage_ai4company/companion_world/avatars/` 托管（另一个仓库）。
本次只放了本仓 `app/static/` 一份，**CDN 侧那份需要单独同步部署**，否则候选卡片头像 404。

### 9.4 一个必须注意的机制细节：seed 是逐字落库的

`persona_seed_json` 在激活时**原样写入**居民 runtime account 的 `SOUL.md` / `IDENTITY.md`，
中间没有任何占位符替换：

```text
repositories/companion_world.py:490  _persona_parts(candidate.template.persona_seed_json)
  → db/billing.py:2676  profile_storage.write_file(account_id, "SOUL.md", content)
```

而实例名是**用户在确认时给定的**（`suggested_display_name` 来自 name_pool，用户可改）。
同时 `prompt_builder.py:402-404` 只在**没有** project context 时才注入
`你的名字是 {display_name}`——居民有 SOUL/IDENTITY，所以走不到那条 fallback。

**结论：预设 seed 里不能写死自称名。** 写死"你叫司辰"，用户把它改名叫"老辰"之后，人设自称与
列表展示名就会打架。所以 §9.2 用的是「用户给你起的名字就是你的名字」这种名随用户的写法，
零代码改动即可正确。

可选优化（非本次必须）：在 `activate_candidate_with_runtime` 里对 seed 做一次
`{display_name}` 替换，让模板能显式引用实例名。两行代码，但会给模板作者引入一个新约定，
本次不做。

---

## 10 · 五位角色欢迎语（CONTENT-001 文案）

产品口径：**各自独立文案、不共用模板**（PRD ENRICH-01），简介自己 + 带出人生态度，
**50 字以内**。服务端不写兜底句，缺文案的角色这条就不落库。

| 角色 | 标签 | 欢迎语 | 字数 |
| --- | --- | --- | --- |
| 林小满 | 温柔 / 共情 / 治愈 | 以后我在这儿。你不用讲得清楚，也不用讲得体面，想说什么就说。慢慢说，我不着急。 | 39 |
| 陆星野 | 活泼 / 好奇 / 元气 | 终于见到你啦！我对什么都好奇，尤其是你。今天过得怎么样？哪怕是很小的事我也想听。 | 40 |
| 沈川 | 沉稳 / 理性 / 可靠 | 我在了。有想不清的事可以拿来一起拆，慌的时候也可以先什么都不说。我不会走。 | 37 |
| 阿糖 | 幽默 / 俏皮 / 轻松 | 报到！我这人没什么大本事，就是能把糟心事聊成笑话。日子够沉了，咱轻点过。 | 36 |
| 司辰 | 沉稳 / 通透 / 幽默 | 我年轻时到处折腾，四十岁以后才学着看八字、看时节。别急着问结果，先跟我说说你。 | 39 |

三条实现约定：

- 落库为该会话**第一条真实 `assistant` 消息**（`role='assistant'`、`message_type='text'`），
  参与后续 AI 上下文（见 D-8）。
- 文案里**不出现角色自己的名字**——用户刚给它起过名，重复一遍既啰嗦，又会在改名后穿帮
  （同 §9.4 的原因）。
- 欢迎语与 seed 不同，它是**确认时渲染**的，不是逐字模板，所以将来若要带入实例名，
  可以安全地用 `{display_name}` 占位。

以上五条欢迎语**已产品定稿**（2026-07-29）。

### 10.1 五条自我介绍动态（CONTENT-002 文案）· ⚠️ 待产品确认

产品只确认了欢迎语，下表是**我起草的初稿**，需要产品逐条过一遍再定稿。落库位置在
`app/products/zhaoxi/domain/companion_world/onboarding_content.py`，改文案只动这一处、
无需改代码。

| 角色 | 自我介绍动态 |
| --- | --- |
| 林小满 | 搬进来了。窗台上摆了盆薄荷，浇多了会烂根，所以我每次只浇一点点。慢一点没关系的。 |
| 陆星野 | 新地方！我已经把每个角落都看过一遍了。最大的发现是傍晚的光会正好落在门口那级台阶上。 |
| 沈川 | 安顿好了。东西不多，一张桌子一把椅子就够用。有事随时来找我，我基本都在。 |
| 阿糖 | 来了来了。行李里一半是零食，另一半是拆开就装不回去的那种。日子嘛，能笑一下算一下。 |
| 司辰 | 来了。带了两样东西：一本翻烂的旧书，和一个看天的习惯。节气一换人的心思也跟着变，急事不妨缓两天再定。 |

动态文案额外遵守一条欢迎语没有的约束：**不写与落库时间相关的内容**（节气、天气、今天几号）。
这两条文案是确认时一次性落库、之后不再重写的，任何"此时此刻"的表述都会立刻过期。
动态无 50 字上限（Feed 卡片可换行），但仍建议控制在 60 字内。

**未配文案的人设一条都不落**：自建角色（`persona_key` 恒 `None`）、以及运营新加但还没配文案的
预设，都直接跳过两条写入，不写兜底句。
