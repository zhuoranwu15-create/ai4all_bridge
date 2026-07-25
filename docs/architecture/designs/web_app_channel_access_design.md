# Web/App 多渠道接入设计（渠道无关化 + Web V1 最小闭环）

> 状态：设计稿（待评审 → 落地）
> 最后更新：2026-07-13（已折入 Codex 第二轮 9 项修正：①历史仅当前 active、②dreaming 双 scope、③substr 前缀、④去重顺序+ok/deduplicated、⑤解绑失效、⑥ChannelTurnInput、⑦非阻塞单飞、⑧收敛 last_inbound 双定义、⑨accounts.channel legacy；第三轮：**主动消息护栏从「可选 Phase 2」升级为 Phase 0/1 硬需求**——`_select_route` 能力过滤 + `dispatch_proactive_text` fail-fast + Web 工具全禁用（选择 A）+ Web binding `chat_id=NULL`）
> 适用范围：AI4ALL 微信个人 AI 陪伴项目，从「仅微信扫码绑定用户」扩展到「App/Web 直登用户」
> 上游依据 / 对齐：
> - [`identity_model_and_wechat_binding.md`](identity_model_and_wechat_binding.md) — 身份与绑定主参考（本文延续其口径，不改变 `aid_` / `platform_user` / `channel` 语义）
> - [`multi_node_access_refactor.md`](multi_node_access_refactor.md) — 接入端多机（central/node），本文的出站/网关结论建立在其上
> - [`openclaw_bridge_design.md`](openclaw_bridge_design.md) — 微信入站契约现状
>
> 一句话目标：**把接入层从「隐式假设微信」改成「渠道无关 + 渠道适配器」；在保证微信渠道零影响的前提下，本期为「已绑定+已 onboarding」的老用户落地一个以对话框为主体的 Web V1 界面，同时不为未来 App/媒体/推送/主动消息埋下阻塞。**

---

## 0. 三条指导原则（贯穿全文，优先级从高到低）

- **原则一（微信渠道零影响，最高约束）**：现有微信渠道的**新老用户必须照常使用**，行为字节级不变。所有改动对微信主链路是**加性**的——`/openclaw/turn`、微信身份解析、微信出站网关一律不动语义；Phase 0 的抽缝是"纯搬家 + 参数化"，以微信全量回归为准入闸。任何微信侧回归视为本期失败。
- **原则二（前向兼容，能支持不阻塞）**：抽象层一次做到位——引入渠道常量与渠道能力表、把 turn 核心与「渠道身份解析 / 回复投递」解耦。凡是未来 App/Web 会用到的接缝（媒体、推送、主动消息、流式），本期**留缝但不填实现**，确保后续增量不用回头重构主链路。
- **原则三（Web V1 先跑通，渐进交付）**：实现只做 Web V1 的**同步文本问答对话界面**，复用已有 `platform_user` 登录鉴权与 `aid_` 隔离。不做媒体、不做主动消息、不做推送、不做流式。

### Web V1 形态（本期范围的硬边界）

- **Web 不是新的注册入口**：新用户注册**保持原流程不变**（手机号 OTP → 微信 QR 绑定 → 微信侧 onboarding），Web 端**不展示任何注册/引导入口**。
- **Web 是老用户的对话界面**：**仅对「已扫码绑定成功 且 已完成 onboarding」的用户**开放。这类用户再次刷新 Web 时，进入一个**以对话框为主体的新 Web 界面**（设计见 §7）。未满足门控条件的用户，Web 维持原有页面，不出现对话入口。
- 推论：**V1 不存在 Web-first / 无微信绑定的账号**——每个 Web 用户都已有微信绑定且 onboarded。因此 Web 侧**不需要 onboarding**（账号已 onboarded）。但**注意**：主动消息虽最终仍应投微信，新增的 Web binding 会让现有路由静默改道——§8.3 护栏是 **Phase 0/1 硬需求，非可选卫生项**。

**非目标（本期明确不做）**：Web 注册/绑定入口、App 原生端、媒体/图片上传、主动消息触达 Web、离线推送（APNs/FCM）、流式（SSE/WS）响应、多账号切换 UI、Web 侧 onboarding。以上均在 §8 前向兼容中"留缝"，实现延后。

---

## 1. 现状盘点：什么可复用，微信耦合在哪

结论先行：**隔离核心已与渠道解耦，微信耦合集中在三层，其余全链路渠道无关。**

### 1.1 已经与渠道解耦（可直接复用，本期不动）

| 能力 | 证据 | 说明 |
|---|---|---|
| 隔离键 `aid_` 随机生成 | `app/db/_core.py:43` | 不从微信身份派生；微信是事后挂到 `aid_` 上的渠道 |
| `channel` 已是一等列 | `accounts` / `channel_bindings` / `binding_intents` / `outbound_messages` / `reminders` | 多渠道表结构骨架已在 |
| `platform_user` 主身份 + 登录鉴权 | `_require_session`（`app/routers/deps.py`）、`create_platform_user_session`、`resolve_session_principal` | **App/Web 用户登录体系已现成**：`Authorization: Bearer <session_token>`，7 天有效；token 绑定产品 audience |
| 账号归属 | `account_owner_bindings`（`_core.py:458`），一个 user ≤ 10 account | `platform_user` → `aid_` 归属已建模 |
| turn 核心（prompt/LLM/记忆/session/限流/计费） | `turn_service` 除身份解析外的部分 | 渠道无关，直接复用 |
| session 存储支持多 key | `sessions.session_key`（`_core.py:572`，`UNIQUE(account_id, session_key)`）| 已能承载 per-scope 的 active session（微信 `__account_active__` / Web `__web_active__`），**无需迁移**即可隔离短期会话（见 §7） |

### 1.2 微信耦合三层（本期抽象/收口的对象）

1. **接入/传输层**：`openclaw_gateway.py` 全是 `send_weixin_text` / `start_weixin_qr_login`，`DEFAULT_WEIXIN_CHANNEL="openclaw-weixin"`（`openclaw_gateway.py:35`）硬编码贯穿默认参数。
2. **入站契约**：`/openclaw/turn`（`bridge.py:175`）用 `AI4ALL_BRIDGE_SECRET` 机器鉴权；身份靠 `resolve_openclaw_identity`（`identity.py:38`）+ `resolve_account_id_for_inbound_channel_identity`（`billing.py:2570`）经 `binding_intents` 反查 `aid_`。这套是**微信 QR 绑定专用**。
3. **业务开关/文案**：onboarding 硬门 `identity.channel == "openclaw-weixin"`（`turn_service.py:887`）+ 写死"微信好友"文案（`onboarding.py:42`）。

---

## 2. 核心决策（已与产品对齐）

| # | 决策 | 取值 | 理由 |
|---|---|---|---|
| D1 | **身份模型** | **多渠道同账号，隔离短期会话 + 共享长期记忆（Model B）** | 同一 `platform_user` → 同一 `aid_`。**长期资产**（SOUL/IDENTITY/账号级 MEMORY/dreaming/agent_self/mission）跨渠道共享；**短期资产**（active session 及其消息、carryover、rolling summary、TDAI 新鲜轮召回）**按 `conversation_scope` 隔离**，微信与 Web 各自独立对话线。避免切端时把一渠道的实时短期上下文泄漏到另一渠道。详见 §7。 |
| D2 | **渠道抽象** | 引入 `channel` 常量 + **渠道能力表** + turn 核心抽缝 | 满足原则一：一次抽象，后续渠道只加一行配置 + 一个适配器 |
| D3 | **入站** | 新增 `POST /web/turn`，`_require_session` 鉴权 | 复用现成登录体系；不走微信的 `binding_intents` 反查 |
| D4 | **出站（V1）** | 仅**同步 HTTP 回复**；主动消息延后 | 前台回复本就是同步返回，不碰微信网关；主动消息暂不做 |
| D5 | **Web 门控** | 仅「已扫码绑定 + 已 onboarding」用户可见对话界面；Web 不做注册/onboarding | Web 是老用户对话入口，非注册入口；账号已 onboarded，Web 侧不重复引导 |

---

## 3. 数据模型改动（尽量零新表）

**本期不新增业务表、不做 DB 迁移。** 复用现有结构：

- **账号归属**：`account_owner_bindings` 已有 → Web 聊天端点**从 session 只读解析主账号**（无 `account_id` 入参、不建号，§5.0）。
- **conversation_scope（短期会话隔离）**：用 `sessions.session_key` 约定实现，**无需新列**（该列已存在，`_core.py:572`）：
  - 微信 active session key 保持 `__account_active__`（**取值不变，行为不变**）。
  - Web active session key = `__web_active__`。
  - 归档 key 由**当前 scope 的 active key** 派生（`{active_key}:{session_id}`），不再写死微信 key（见 §7.1）。
- **渠道足迹**：Web 首次对话时 `upsert_channel_binding`，写一条 `channel="web"` 的 `channel_bindings`（`channel_account_id=platform_user_id`，`session_key=web:<account_id>`，**`chat_id=NULL`**）。
  - ⚠️ **`chat_id` 必须留空（Codex 阻断项，见 §8.3）**：主动路由 `_select_route`（`common.py:32`）遍历 binding 取第一个 `chat_id`+`channel_account_id` 都非空者，而 `list_channel_bindings_for_account` 按 `last_seen_at DESC` 排序（`accounts.py:191`）——Web turn 刷新 `last_seen_at` 会把 Web binding 顶到微信前。若 Web binding 写了 `chat_id`，主动消息会误选 `channel="web"` 却仍调微信发送。留空 `chat_id` 让现有 `if not to_user_id: continue` 天然跳过 Web（双保险；真正的能力过滤见 §8.3）。
  - ⚠️ **可触达时间隔离**：Web 足迹的 `last_seen_at` **不得**影响微信送达判断——微信的 `get_account_last_inbound_at` 查询必须限定 `channel="openclaw-weixin"`（见 §7.5）。
  - ⚠️ **`accounts.channel` 是 legacy 单值字段，Web turn 不得改它（Codex ⑨）**：`get_or_create_session`（`billing.py:2618`）每次都 `SET channel=excluded.channel`；同账号多渠道后此字段会在 web/微信间来回跳，污染 Admin 展示与代码理解。定性：`accounts.channel` = legacy/默认渠道字段，**Web 建 session 时不更新它**；真实渠道状态一律看 `channel_bindings`。加「Web turn 不改 `accounts.channel`」测试（§10）。
- **onboarding**：`accounts.onboarding_state` 账号级，直接复用（Web V1 不触发）。

> `channel_bindings` 唯一键 `(account_id, channel, session_key)`（`_core.py:639`）天然支持一个 `aid_` 同挂 `openclaw-weixin` 与 `web` 两条足迹。`conversation_scope` 复用 `sessions.session_key`，也无需迁移。

**新增一个纯代码层（非 DB）：渠道能力表**，见 §4.1。

---

## 4. 渠道抽象设计（原则一的落点）

### 4.1 渠道常量与能力表

新增 `app/channels.py`（纯常量 + 能力声明，不引依赖）：

```python
# app/channels.py（示意）
CHANNEL_WEIXIN = "openclaw-weixin"
CHANNEL_WEB = "web"          # 本期新增
# CHANNEL_APP = "app"        # 预留，本期不实现

@dataclass(frozen=True)
class ChannelCapability:
    active_session_key: str               # conversation_scope（短期会话隔离键，§7.1）
    onboarding_enabled: bool              # 是否走 onboarding 状态机
    onboarding_copy_key: str              # 文案键（渠道相关欢迎语）
    # —— 回复投递按现状拆三分（Codex 反馈②）：不能用单字段表达整个渠道 ——
    default_reply_delivery: str           # 普通回复投递方式："sync_http" | ...
    supports_out_of_band_tool_final: bool # 工具最终回复是否可经网关 out-of-band 发送
    supports_proactive: bool              # 是否可被主动消息投递；同时门控「产生未来投递」的工具（§8.3）
    tdai_enabled: bool                    # 是否接 TDAI 热召回/capture（§7.4）

CHANNELS = {
    # 微信：三种投递并存——普通回复同步 HTTP 返回；工具最终回复 & onboarding 走网关 out-of-band。
    CHANNEL_WEIXIN: ChannelCapability(
        active_session_key="__account_active__",   # 取值不变
        onboarding_enabled=True, onboarding_copy_key="weixin_welcome",
        default_reply_delivery="sync_http", supports_out_of_band_tool_final=True,
        supports_proactive=True, tdai_enabled=True,
    ),
    # Web V1：全部同步 HTTP 返回，绝不进微信网关；已 onboarded 不引导；不接 TDAI（§7.4）。
    CHANNEL_WEB: ChannelCapability(
        active_session_key="__web_active__",
        onboarding_enabled=False, onboarding_copy_key="web_welcome",
        default_reply_delivery="sync_http", supports_out_of_band_tool_final=False,
        supports_proactive=False, tdai_enabled=False,
    ),
}
```

- 微信取值全部等价于**现状**（`__account_active__`、onboarding=True、工具回复走网关、TDAI 开），行为字节级不变（原则一）。
- **关键不变量**：`supports_out_of_band_tool_final=False` 时，工具最终回复**必须走同步返回、绝不调 `node_send_text`**（否则 Web 工具回复会被误发到微信网关，见 §4.2 / §7）。
- `onboarding_enabled`：`turn_service.py:887` 的硬编码 `channel=="openclaw-weixin"` 改读此位。
- `supports_proactive` / `tdai_enabled` / `active_session_key` 分别驱动 §8.3 / §7.4 / §7.1。

### 4.2 turn_service 抽缝：身份解析 ↔ 渠道无关核心

当前 `handle_openclaw_turn` 把"微信身份解析"和"turn 核心"焊在一起。抽成两段：

```
渠道适配器（per-channel front）           渠道无关核心（shared）
────────────────────────────            ──────────────────────────────
微信: resolve_openclaw_identity          run_turn_for_account(ctx: ChannelTurnInput)
      + resolve_account_id_for_             - ensure active session（按 ctx.cap
        inbound_channel_identity              .active_session_key，§7.1）
      + unbound 收口                        - upsert_channel_binding（渠道通用）
        → account_id or ignore              - onboarding（cap.onboarding_enabled）
                                            - 限流 / 计费（幂等 client_message_id）
Web: _require_session → platform_user       - prompt 构造 / LLM / 工具 / 记忆
     + 只读解析主账号(§5.0) + chat_ready       （TDAI 按 cap.tdai_enabled，§7.4）
     → account_id（不建号）                  - reply 投递：
                                              · 普通回复 → default_reply_delivery
                                              · 工具最终回复 → 仅当 cap.supports_
                                                out_of_band_tool_final 才 node_send_text；
                                                否则同步返回（Web 走这条）
```

- **入口 DTO 用新类型 `ChannelTurnInput`，不要复用 `TurnContext`（Codex ⑥）**：仓库已有 `app/turn_context.py:7` 的 `TurnContext`，那是**工具执行阶段**上下文，与这里的「渠道归一化输入」不是同一对象；且现状 turn 核心仍大量读 `OpenClawTurnRequest` 的 `message_type/media/raw/sender_id/chat_id/session_key` 等字段。新增独立内部类型 `ChannelTurnInput` 至少含：`account_id / channel / cap(ChannelCapability)`、通用渠道 identity、`message_id / message_type / text / media / raw`、`sender_id / sender_name / chat_id / channel_session_key`、`client_message_id?`、`background_loop`。避免 Web 靠**伪造一个 `OpenClawTurnRequest`** 接入核心。
- **抽缝原则**：新建 `run_turn_for_account(...)`，把 `handle_openclaw_turn` 身份解析之后的逻辑平移进去并**按 `cap` 参数化**（scope key、onboarding 开关、投递方式、TDAI 开关）。`handle_openclaw_turn` 传微信 `cap` → **取值与现状完全等价**，行为字节级不变；`/web/turn` 传 Web `cap`。
- ⚠️ 比原设计多触及的点（Model B 代价）：`ensure active session`、carryover seed、`_send_tool_final_reply` 分支、TDAI 调用都要**读 `cap`**，不再是无脑复用。这几处是 Phase 0 的真正工作量与回归面（见 §7、§9）。

---

## 5. Web V1 接口与门控

三个端点，全部放在 `app/routers/web.py`（与其余 `_require_session` 端点同域），鉴权统一 `Depends(_require_session)` → `platform_user`。

### 5.0 账号解析：服务端只读、不建号、V1 无 account_id

关键修正（Codex ⑥）：现状 `GET /web/me`（`web.py:1076`）**只返回单个默认 account**、且内部调 `get_or_create_default_ai4all_account_for_user`（**含写**）。因此：

- **V1 无多账号切换** → 聊天端点 **request body 不接受 `account_id`**，一律由服务端从 session 解析该用户的**主账号**。这直接消灭了「传他人 account_id 越权」的攻击面。
- 聊天/历史端点**只读解析、绝不建号**：新增只读 helper `get_primary_owned_account(platform_user_id)`，查 `account_owner_bindings`，**明确取「最早的默认账号」——`status='active' ORDER BY created_at ASC, id ASC LIMIT 1`**（Codex 补充定义）。**不得**调用 `get_or_create_default`（它写库，与"Web 不是注册入口"冲突）。
- 解析结果非 `chat_ready`（未绑定 / 未 onboarding）→ 拒绝（`409 not_chat_ready`）。gate 命中的用户此账号必然存在，故正常路径不触发建号。
- `/web/me` 自身维持现状（它是账号管理面，建默认号是既有行为，不在本次收敛范围）。

### 5.1 门控判定（复用现有 `GET /web/me`）

Web 首页脚本调 `GET /web/me`（已存在），据此判断进「对话页」还是「原有页面」。门控条件（两个都满足）：

1. **已扫码绑定成功**：该账号有 completed 微信绑定（`binding_intents.status=="completed"` / `list_channel_bindings_for_account` 有 `openclaw-weixin` 足迹）。
2. **已完成 onboarding**：`onboarding_state` 到达终态（`get_account_onboarding_state`，非 `pending`/中间态）。

给 `GET /web/me` 返回的 `account` 补两个**派生只读字段**（加性，不改老字段）：`onboarding_state`、`chat_ready`（= 上述两条与）。前端只看 `chat_ready`：

- `chat_ready == true` → 跳/渲染对话页（§6），拉历史（§5.2）。
- 否则 → 维持原有页面，**不出现对话入口**（原则一：老流程零变化）。

> **`chat_ready` 由单一 helper 计算（Codex 补充）**：`/web/me`、`/web/messages`、`/web/turn` 三处**共用同一个** `compute_chat_ready(account)`，避免口径漂移。其中 onboarding 判定复用现成 `is_onboarding_done()`（`onboarding.py:96`），微信绑定判定用**当前有效的 `openclaw-weixin` binding**。

> 门控 = **前端跳转 + 后端派生字段**，不新增写操作、不改注册链路。

### 5.2 拉取历史：`GET /web/messages`

对话界面首屏要展示**该用户在 Web 这条对话线上的历史**。新增只读端点：

```
GET /web/messages?limit=50&before_id=<int>          # 无 account_id，服务端解析主账号（§5.0）
→ Cache-Control: no-store
→ { "messages": [ {role, text, created_at, message_id}, ... ], "next_cursor": "..." }
```

- **账号**：服务端只读解析（§5.0），非 `chat_ready` → `409`。
- **只展示当前 Web active session（Codex ①，与已确认需求对齐）**：**只查 `session_key='__web_active__' AND status='active'` 这一段的消息**，**不返回**归档段（`__web_active__:<id>`），也**不含微信侧对话**。若当前无 active session（用户此前只在微信聊、Web 侧尚未开过）→ 返回**空列表**，前端显示轻引导（§6.2）。
- 数据源：现有对话消息存储，按上述**当前 active session** 过滤，时间正序返回。V1 只读、只返回文本消息，media 先降级占位文案。
- **约束**：`limit` 上限（如 ≤100，越界截断）；响应 `Cache-Control: no-store`（含身份相关内容，禁缓存）。
- **分页**：游标 `before_id: int`，**仅在当前 active session 内**向上翻页（不跨归档段）；V1 可仅实现首屏 `limit`。

### 5.3 发送：`POST /web/turn`

**请求**（新增 `WebTurnRequest`，`app/schemas.py`；**无 `account_id`**）：

```json
{
  "text": "你好",
  "client_message_id": "c-8f3a…"   // 必填，客户端幂等键
}
```

**入参约束**：
- `text`：去空白后非空；`1..MAX_WEB_TEXT_LEN`（如 4000，与微信文本上限对齐），越界 `422`。
- `client_message_id`：**必填**；格式受限 `^[A-Za-z0-9_-]{8,64}$`，越界 `422`。

**响应**（`WebTurnResponse`，`Cache-Control: no-store`）：

```json
{
  "status": "ok",              // ok | rate_limited | not_chat_ready | turn_in_progress | error
  "reply": "……",              // 同步回复文本；no_reply 时为 null
  "no_reply": false,
  "metadata": { "session_id": "...", "message_id": "..." }
}
```

- V1 **同步返回**，无需网关/node 派发。账号服务端只读解析（§5.0），非 gate → `409 not_chat_ready`。
- 内部走 `run_turn_for_account(cap=CHANNELS["web"])`（§4.2）：`__web_active__` scope、同步返回、不接 TDAI。

### 5.4 幂等契约（Codex ⑤，复用现有 message_id 去重）

现状 turn_service **已有基于 `message_id` 的去重**：`get_duplicate_reply`（`turn_service.py:27` 引入，命中返回**已持久化 reply**）。Web 幂等**复用**这套，不新造，但**必须重排执行顺序**——现状微信流程是**先限流后查重**（RPM `turn_service.py:1004`、daily `:1024` 都在 `get_duplicate_reply` `:1040` 之前），已完成请求重试时会**先被限流而非返回原 reply**，不满足幂等承诺。

**映射与前端约定**：
1. **前端**：一次发送生成一个 `client_message_id`；**超时/失败重试复用同一个值**（不得每次点击新生成）。
2. **服务端映射**：把 `client_message_id` 确定性映射为**账号内唯一**的 turn `message_id`（如 `web:{account_id}:{client_message_id}`），喂给现有去重逻辑。

**Web turn 严格顺序（Codex ④，与微信现状不同）**：
1. 解析主账号（§5.0）、校验 `chat_ready` gate。
2. 按映射后的 message_id 查 `get_duplicate_reply`，**命中立即返回已持久化 reply（在限流之前）**。
3. 取得 `(account_id, __web_active__)` scope single-flight（§7.6）；被占用 → `409 turn_in_progress`。
4. **取锁后再查一次重复**（避免并发竞态：两个相同请求同时越过第 2 步）。
5. 再执行限流 / 落入站 / LLM / 工具 / 计费 / reply 持久化。
6. `finally` 释放锁。

**返回值口径（Codex ④）**：现有重复响应是 `status="duplicate"`，但 `WebTurnResponse` 枚举（§5.3）里**没有该值**。V1 统一对客户端返回 `status="ok"`、在 `metadata` 标记 `deduplicated=true`（前端当正常成功处理，不必区分是否命中去重）。

> 幂等的正确性由账号内唯一 message_id + 持久化 reply 保证：即便同步连接在长耗时 turn 中断开，重试同一 `client_message_id` 也只会拿回已存结果，不会二次执行（与 §5.5 超时兜底配合）。未来 App 端沿用同一契约。

### 5.5 长耗时 turn 与超时兜底（Codex ⑧）

微信之所以把工具最终回复走 out-of-band，正是因为 openclaw 同步回复会超时。**Web 没有 out-of-band 通道**，长耗时 tool turn 若超过 Nginx/浏览器超时，同步 reply 会丢。V1 兜底（利用"reply 已持久化 + 幂等 + messages 可回拉"）：

- **超时上限对齐**：Nginx `proxy_read_timeout` 与浏览器 fetch 超时都设到**覆盖最坏 tool turn 时长**（如 120s），写进部署文件（§8bis）。
- **权威记录持久化**：turn 的 reply 落库；同步返回只是"尽力而为"的快路径。
- **断线不丢**：同步连接超时/断开时，前端用同一 `client_message_id` 重试（命中 §5.4 幂等 → 拿回已存 reply），或回拉 `GET /web/messages`。二者都不二次执行 turn。
- V1 不引入 SSE/WS（Phase 3）；"持久化 + 幂等 + 回拉"组合已能在无推送下保证不丢不重。

### 5.6 解绑对 Web 资格的影响（Codex ⑤补充）

V1 微信解绑后，Web 对话资格**立即失效**：

- 解绑后 `chat_ready` **立即变 `false`**（gate 依赖「有效 `openclaw-weixin` binding」，binding 失效则 `compute_chat_ready` 不再命中，§5.1）。
- `GET /web/messages` 与 `POST /web/turn` **每次请求都重新校验 `chat_ready`**，不只依赖页面首屏 gate——解绑后两端点一律 `409 not_chat_ready`。
- Web 的 session 与消息**可保留在 DB**（不删数据），但解绑后**不可读取、不可继续对话**。
- 测试：解绑前收发成功；解绑后 `GET /web/messages`、`POST /web/turn` 都返回 `409 not_chat_ready`。

**未来（登记不实现）**：后续解绑可加 `unbind_mode=all|weixin_only`；「仅解绑微信、保留 Web」意味着 Web 需要**独立的使用资格**，届时**不能再以微信 binding 作为 Web gate**。此选择**不应复用 `keep_memories`** 语义（那是记忆保留开关，与「是否保留 Web 使用权」是两件事）。

---

## 6. Web 对话界面设计（V1，一版建议）

定位：**移动优先、以对话框为主体的单页界面**。用户群来自微信（多在手机上），因此按手机竖屏为第一设计目标，桌面自适应加宽即可。不做侧边栏、不做多会话列表——V1 是「单账号单对话流」。

### 6.1 布局（线框）

```
┌───────────────────────────────┐
│  ◀   [头像]  百景            ⋮ │   顶部栏：AI 头像 + AI 名字（取自账号
│        「你的星空观测伙伴」      │   IDENTITY）+ 一句人设副标题
├───────────────────────────────┤
│                               │
│  [AI] 昨天你说想看流星雨…       │   消息流：AI 气泡左、用户气泡右
│                               │   首屏拉 GET /web/messages（仅 Web
│          我记得，今晚几点？[我] │   scope 历史，不含微信侧），时间正序
│                               │
│  [AI] 22:30 后东南方向最好      │
│                               │
│            · · ·（对方输入中）  │   发送后等待态
├───────────────────────────────┤
│ [ 输入消息…            ] [发送] │   底部输入区：Enter 发送 /
└───────────────────────────────┘   Shift+Enter 换行；发送中禁用防重
```

### 6.2 交互流程

1. **进入**：首页/dashboard 脚本调 `GET /web/me` → `chat_ready==true` 则给出/跳转到对话页 `/user/chat.html`；否则维持原有页面（§8bis 说明跳转与部署）。
2. **首屏**：`GET /web/messages?limit=50` 渲染 **Web scope** 历史；Web 首次进入时该 scope 可能为空（因为用户此前只在微信聊）→ 显示一句轻引导（如"继续和百景聊聊吧"），**不触发 onboarding**（账号已 onboarded，长期记忆已在）。
3. **发送**：输入 → 乐观插入用户气泡 → `POST /web/turn` → 收到 `reply` 插入 AI 气泡；`no_reply` 则仅收起输入态不插泡；失败给行内"重试"。
4. **不做跨渠道实时**：Model B 下 Web 是独立对话线，**不展示、不推送微信侧消息**。Web 自身多标签页的一致性通过聚焦时轻量轮询 `GET /web/messages` 兜底；实时（SSE/WS）留到 Phase 3。

### 6.3 视觉与技术取向（建议，非硬约束）

- 视觉：留白充分、对话为绝对主体；气泡圆角、AI/用户双色区分；顶部栏轻量。气质贴合"AI 陪伴"而非"客服工单"。
- 技术：**沿用现有原生静态页技术栈**（当前不是 SPA，是 `app/static/*.html` 多页 + `/ui/*` 提供，见 §8bis）；**新增一张 `chat.html`**，与 `home/dashboard` 同源风格，不引新框架。AI 名字/头像/副标题从账号 profile（IDENTITY）取，`GET /web/me` 补 `ai_display_name` 等展示字段（加性）。
- **安全（Codex ⑧）**：消息文本**只能经 `textContent` 渲染，严禁拼 `innerHTML`**——历史/回复均为用户可控内容，拼 HTML 会 XSS。
- **错误码前端行为（Codex ⑧）**：
  - `401` → 清 token、回登录/首页；
  - `429`/`rate_limited` → 展示限流文案、按退避重试，不狂点；
  - `409 turn_in_progress` → 等待/退避后用**同一 `client_message_id`** 重试（§5.4）；
  - `409 not_chat_ready` → 回原有页面（不应出现在已 gate 用户）；
  - `5xx`/超时 → 提示重试；重试复用 `client_message_id`，必要时回拉 `GET /web/messages`（§5.5）。
- 健壮性：发送禁用防抖、长文本滚动到底、移动端键盘不遮输入框。

> 这只是一版建议；视觉细节可在 Phase 1 用 `frontend-design` 打磨。后端契约（§5）与前端解耦。

### 6.4 onboarding 说明

Web V1 **不涉及 onboarding**：门控要求进入者已在微信侧 onboarded（`onboarding_state` 终态）。渠道能力表里 `web` 的 `onboarding_enabled=False`（§4.1）。未来若开放 Web-first 注册再启用 `web_welcome` 文案槽——本期只留槽不实现。

---

## 7. 会话隔离模型（Model B：隔离短期、共享长期）

D1 定为 **Model B**：微信与 Web 是**独立对话线**（各自 `conversation_scope`），只共享**长期记忆**。本节把 Codex 反馈的 P9/P10/P11/P1/P3 逐条落成实现。分界线：

| 资产 | 归属 | 处理 |
|---|---|---|
| active session + 其消息 | **短期，按 scope 隔离** | §7.1 conversation_scope |
| carryover_summary | **短期，按 scope 隔离** | §7.2 |
| rolling_summary | **短期，按 scope 隔离** | §7.2（存在 session 行上，随 §7.1 自动隔离） |
| TDAI 新鲜轮 recall/capture | **短期，不跨渠道** | §7.4（Web V1 不接 TDAI） |
| SOUL / IDENTITY / 账号级 MEMORY / dreaming / agent_self / mission | **长期，跨渠道共享** | 账号级，注入两渠道 prompt，无需改动 |

### 7.1 conversation_scope（P9）

现状：`ACCOUNT_ACTIVE_SESSION_KEY="__account_active__"` 全局硬编码（`_core.py:21`），所有渠道共用一个 active session（`session_lifecycle.py:111`）。最小改造（**不做 DB 迁移**）：

- 微信继续用 `__account_active__`（**行为不变**）；Web 用 `__web_active__`。取值来自能力表 `cap.active_session_key`。
- 把 `get_or_create_account_active_session_with_dreaming` 及相关生命周期函数改为**接收 `active_session_key` 参数**，替换写死的常量。
- **归档 key 基于当前 active key 生成**：`archived_session_key = f"{cap.active_session_key}:{session_id}"`（现状写死微信 key，`session_lifecycle.py:~98`）。这样 Web 关闭的 session 归档为 `__web_active__:{id}`，与微信段可区分。
- `#重置会话` 只清**当前 scope** 的 active session，不动另一渠道。
- **Dreaming scheduler 必须扫两个 scope（Codex ②，Phase 0 必做）**：现状每日轮转扫描 `list_active_sessions_for_business_day_before`（`admin.py:865/879`）**两个分支都写死 `ACCOUNT_ACTIVE_SESSION_KEY`**，只会关微信 active session。若不改，`__web_active__` 永不轮转/dreaming，Web 内容进长期记忆时间不稳定，且 §5.2 会长期读到本该关闭的旧 active。改造：
  - `list_active_sessions_for_business_day_before(active_keys=[...])` 接收一组合法 active scope key（默认含 `__account_active__` 与 `__web_active__`），`WHERE session_key IN (...)`。
  - scheduler 同扫两个 scope；`_rotate_session_with_dreaming`（`session_lifecycle.py:84`）**按 session 自身的 active key 归档**（不写死微信 key）。
  - 测试两个 scope 都能独立轮转并各自生成 carryover。

### 7.2 短期资产隔离：carryover / rolling（P10）

现状 `get_latest_closed_carryover_for_account`（`accounts.py:762`）取「账号最近关闭的**任意** session」carryover，`WHERE account_id=? AND status='closed' ORDER BY id DESC`——**跨渠道泄漏**（微信昨天关的 session 会被 Web 今天首开继承）。

- 修正：新 session 只承接**同 scope** 的上一段 carryover。因归档 key 带 scope 前缀（§7.1），查询加**精确前缀匹配**即可，**无需迁移**。
- ⚠️ **不要用 `LIKE '__web_active__:%'`（Codex ③）**：SQL `LIKE` 里 `_` 是**单字符通配符**（`__web_active__` 里每个下划线都会匹配任意字符），这不是严格的 scope 前缀匹配、会误命中。改用 SQLite/PG 都兼容的 `substr(session_key,1,?) = ?`，参数传 `len(active_key + ':')` 与 `active_key + ':'`（比转义 `%`/`_` 更不易写错）。
- `rolling_summary` 存在 session 行上（`accounts.py` `update_session_rolling_summary`），session 一旦按 scope 隔离，rolling 自动隔离，无额外工作。

### 7.3 长期记忆共享通道

「共享长期记忆」具体指：账号级 **MEMORY profile**（memory_writer + dreaming 固化）、SOUL/IDENTITY、agent_self_state、mission——全部**账号级**，本就注入两个渠道的 prompt，无需改动。即 Web 拿得到"AI 记得我是谁、长期偏好"，但拿不到微信**此刻这段**的短期上下文。

### 7.4 TDAI 处理（P11）——Web V1 不接 TDAI

现状 TDAI namespace 仅按 `account_id`（`tdai_client.py`），recall 请求体只有 `{query, session_key}`、**无记忆分层过滤能力**。因此"只召回长期、屏蔽新鲜轮"当前做不到；且若 Web capture 到同 namespace，反向也会让**微信召回到 Web 的新鲜轮**。

- **V1 决策**：`cap.tdai_enabled=False` 的渠道（Web）**recall 与 capture 都跳过**。长期记忆经 §7.3 的 MEMORY block 注入，Web 仍"被记得"，但不发生任何 TDAI 跨渠道新鲜轮泄漏（双向都堵）。
- 实现：复用现有 `tdai_recall_enabled` / allowlist 机制，在 turn 核心按 `cap.tdai_enabled` 短路 recall/capture。微信不受影响。
- 未来（Phase 3 依赖）：若要 Web 也享受语义热召回且不泄漏，需 TDAI gateway 支持**按记忆层级或 conversation_scope 过滤 recall**（capture 打 scope tag、recall 只取"本 scope 新鲜 + 全 scope 已晋升"）。本期只登记，不实现。

### 7.5 可触达时间按渠道隔离（P1）

现状 `get_account_last_inbound_at` = `SELECT MAX(last_seen_at) FROM channel_bindings WHERE account_id=?` **无 channel 过滤**，被 reactivation 资格（`planning.py:140`）与 `touch_state.py:40` 使用。Web 活跃会污染"微信最近入站时间"→ 误判可触达/延迟拉活。

- ⚠️ **有两份同名定义，必须先收敛（Codex ⑧）**：`accounts.py:164` 与 `accounts.py:1160` **各有一份** `get_account_last_inbound_at`，import 时**后者遮蔽前者**。只按附录行号改第一份**不会生效**。实现时先**合并成一个函数**，再让 `channel` 成为**必传参数**（或新增明确的 `get_account_last_weixin_inbound_at()`），两个主动消息调用点同改。
- 修正：微信送达/窗口判断的 last_inbound 查询**限定 `channel="openclaw-weixin"`**。Web 的 `last_seen_at` 不参与微信送达判断。
- V1 Web 无主动消息，此修正主要是**防止 Web 活跃干扰微信主动消息节奏**。

### 7.6 并发与幂等（P3）

Model B 下微信与 Web 是不同 scope，**不需要账号级全局锁**，两渠道可并行。但**同一 Web scope 内**多标签页/重复点击/超时重试会并发，导致上下文乱序、重复计费、重复工具调用。

- **非阻塞单飞（Codex ⑦，V1 选定这一种，不做「串行等待」）**：每个 `(account_id, active_session_key)` 同时只允许一个 turn 在跑。scope 已被占用时——**无论 `client_message_id` 相同还是不同**——直接返回 `409 turn_in_progress`；前端保留原 `client_message_id`、退避后重试（§6.3）。锁必须 `finally` 释放并清理空闲 key。微信与 Web 两 scope 互不阻塞。
- **幂等**：`client_message_id`（§5.3 请求体已含）做请求级幂等键，重试命中返回同一结果、不重复扣费/发工具（顺序见 §5.4）。
- ⚠️ **部署约束**：当前生产是**单 Uvicorn worker**，进程内锁即可满足 V1；一旦增加多 worker，须换成 **PG/分布式 single-flight**（如行锁 / advisory lock）。此约束写进部署走查。

---

## 8. 前向兼容：为未来"留缝不填"（原则一）

| 未来能力 | 本期留的缝 | 本期是否实现 |
|---|---|---|
| **App 原生端** | 入站走同一 `/web/turn` 或平行 `/app/turn`，共用 `run_turn_for_account`；`channel="app"` 加一行能力表 | 否 |
| **媒体/图片** | `WebTurnRequest.media` 字段预留；核心已支持 media（微信内联 base64 路径） | 否（V1 文本） |
| **主动消息触达 Web** | 能力表 `supports_proactive`；`outbound_messages.channel` 已有列 | 否 |
| **离线推送 APNs/FCM** | 投递抽象 `reply_delivery` 已分叉；未来加 `push` 分支 + device token 表 | 否 |
| **流式响应 SSE/WS** | `/web/turn` 先同步；未来加 `/web/turn/stream` 复用同一核心 | 否 |

### 8.3 主动消息护栏（V1 硬需求，原则一——**不是可选项**）

⚠️ **纠正上一版定级**：这道护栏原被放在「可选 Phase 2」，但**新增 Web binding 本身就是微信侧的回归向量**，必须在 Phase 0/1 完成，否则违反原则一。

**因果链（已逐处核实）**：
- `list_channel_bindings_for_account`（`accounts.py:191`）按 `last_seen_at DESC, id DESC` 排序；Web turn 每次 `upsert_channel_binding` 刷新 Web binding 的 `last_seen_at` → 排到微信前面。
- `_select_route`（`common.py:32`）遍历取**第一个** `chat_id`+`channel_account_id` 非空者，**无渠道能力过滤** → 可能误选 `channel="web"`。
- `dispatch_proactive_text` 底层 `send_weixin_text`（`outbound.py:287`）**无条件走微信网关**（不看 `channel` 参数）→ `channel="web"` 的主动消息被发往微信网关，误投/失败。
- 影响面：commitment 投递、reactivation 拉活等**现有正常工作的**账号级主动路由，会因为账号多了一条 Web binding 而静默改道——这是对微信的回归。

**Phase 0/1 必做（一个字段 `supports_proactive` 驱动三处 + 一道 binding 卫生）**：
1. **账号级路由过滤**：`_select_route` 只选 `CHANNELS[binding.channel].supports_proactive == True` 的渠道 → V1 只落 `openclaw-weixin`。
2. **投递 fail-fast**：`dispatch_proactive_text` 对 `supports_proactive=False` 的渠道直接失败/跳过并记原因，**绝不进 `send_weixin_text`**。
3. **工具默认集按 cap 过滤（产品选择：A. Web 全禁用，已确认）**：`supports_proactive=False` 的渠道（Web）**不挂** `create_reminder / create_commitment / update_reminder` 这类「产生未来主动投递」的工具（现状都在默认集 `registry.py:66-70`，`_DEFAULT_ALWAYS`）。理由：Web V1 只做同步问答、不做主动消息（原则三）；禁用比「跨渠道强绑微信路由」改动更小、无跨渠道投递语义、零误投。
4. **binding 卫生（双保险）**：Web binding `chat_id=NULL`（§3），即便前三条有疏漏，`_select_route` 现有 `continue` 也会跳过 Web。

> 工具 gating 落法：默认工具集组装处按渠道 `cap` 过滤（加性、渠道参数化，复用现有 `default_when_flag` 思路）。**微信 `cap` 下工具集等价现状**（原则一）。`list_reminders`/`cancel_reminder` 是只读/取消、不产生新投递，V1 Web 一并不挂以保持最小面；未来若要 Web 管理提醒再按需开只读。
> reminder handler（`reminder_handlers.py:45-54`）现状用**当前 turn 的** `ctx.identity.channel` 落库——Web 全禁用后不会在 Web turn 触发，无需改它；未来若开放选 B（允许 Web 建提醒）才需让它显式绑定微信路由。

---

## 8bis. 前端与部署触点（Codex ⑦）

**现状（已核实）**：不是 SPA，是**原生多页静态站**——`app/static/*.html`（`home.html`/`dashboard.html`/`account.html`…），FastAPI 以 `/ui/*` 提供；Nginx 是**显式 location 白名单**（`deploy/nginx/ai4company.top.conf`：`location = /` → home、`location = /user/dashboard.html` → dashboard、逐个 CSS/资源），**无 catch-all**。已绑定用户当前从首页流程进入 `dashboard.html`。

**V1 决策（新增 chat.html，保留 dashboard 功能，最小扰动）**：

- **新增 `app/static/chat.html`**（不替换 dashboard）：对话页。dashboard 继续承载**钱包 / 解绑 / 邀请码**等账号管理功能，二者并存、互相有入口。
  - 取舍理由：替换 dashboard 会牵动既有账号管理功能与其 Nginx/跳转，违背原则一最小扰动；新增页面隔离风险。
- **入口跳转**：`home.html` 的 `resumeExistingSession()` / 已登录分支读 `chat_ready`——命中则提供"进入对话"入口或跳 `/user/chat.html`；dashboard 顶部也加一个"对话"入口。未 gate 用户完全不变。
- **Nginx**：新增 `location = /user/chat.html`（proxy_pass `…/ui/chat.html`）及其所需静态资源 location；`proxy_read_timeout` 覆盖最坏 tool turn（§5.5，如 ≥120s）。**改动落到 `deploy/nginx/ai4company.top.conf`，随部署走查。**
- **静态资源**：chat.html 复用现有 `site.css`/`style.css` 同源风格，避免新增未在 Nginx 白名单里的资源路径（否则 404）。

> 部署清单（Phase 1 交付项）：`app/static/chat.html`（新增）、`home.html`/`dashboard.html`（加入口跳转）、`deploy/nginx/*.conf`（加 location + 超时）。

---

## 9. 分期落地计划（原则二，渐进）

> 每期末在 `standalone` 全量回归零回归；每期可独立上线。

> ⚠️ 与初版相比，Model B 使 Phase 0 从「纯身份抽缝」扩大到「会话生命周期 + carryover + 投递 + last_inbound + TDAI 的 scope 参数化」。工作量与回归面上升，但每一处**微信取值都等价现状**，仍以微信全量回归为硬闸。

**Phase 0 — 渠道抽象 + scope 参数化（微信行为字节级不变；原则一准入闸）**
- 新增 `app/channels.py`（能力表，微信取值 == 现状）。
- `turn_service`：抽出 `run_turn_for_account(ctx)`，`handle_openclaw_turn` = 微信身份解析 + 传微信 `cap` 调核心。
- **session 生命周期参数化**（§7.1）：`get_or_create_account_active_session_with_dreaming` 等接收 `active_session_key`；归档 key 基于当前 scope；`#重置` 只清当前 scope。
- **carryover 按 scope**（§7.2）：`get_latest_closed_carryover_for_account` 加 scope 过滤。
- **投递三分**（§4.1/4.2）：`_send_tool_final_reply` 等按 `cap.supports_out_of_band_tool_final` 分支。
- **dreaming scheduler 扫两个 scope**（§7.1）：`list_active_sessions_for_business_day_before` 接收 `active_keys`，`_rotate_session_with_dreaming` 按 session 自身 active key 归档（否则 Web session 永不轮转）。
- **last_inbound 按渠道**（§7.5）：先**收敛 `accounts.py:164`/`:1160` 两份同名定义**为一个，`channel` 必传，微信调用点传 `openclaw-weixin`。
- **`accounts.channel` 不被 Web 改写**（§3 / Codex ⑨）：Web 建 session 路径不更新 `accounts.channel`。
- **TDAI 按 cap 短路**（§7.4）：recall/capture 读 `cap.tdai_enabled`（微信 True）。
- **主动路由能力过滤 + fail-fast**（§8.3，原则一硬需求）：`_select_route`（`common.py:32`）只选 `supports_proactive=True` 渠道；`dispatch_proactive_text`（`outbound.py:329`）对 `supports_proactive=False` 渠道 fail-fast，不进 `send_weixin_text`。**纯加性**——V1 只有微信 `supports_proactive=True`，微信路由行为等价现状。
- 验收：**微信全链路全量回归绿、逐项无行为差异**（每个改动点微信取值等价现状）。

**Phase 1 — Web V1 对话界面闭环（本期主目标）**
- 后端：`GET /web/me` 补 `onboarding_state`/`chat_ready`/`ai_display_name`（加性，`chat_ready` 走共享 `compute_chat_ready`，§5.1）；只读账号解析 helper（`get_primary_owned_account`，**不建号**，§5.0）；新增 `GET /web/messages`（**仅当前 Web active session**、`before_id` 分页、`limit` 上限、`no-store`）；新增 `POST /web/turn`（`_require_session` + 服务端解析主账号 + `chat_ready` 校验 + `text`/`client_message_id` 校验 + 复用 `get_duplicate_reply` 幂等 + `(account_id,__web_active__)` **非阻塞单飞**（§7.6）+ 严格顺序（§5.4）+ `run_turn_for_account(cap=WEB)` + `no-store`）；`WebTurnRequest/Response`；Web `upsert_channel_binding`（**不改 `accounts.channel`**）。
- **工具默认集按 cap 过滤（§8.3，A. Web 全禁用）**：默认工具集组装处按渠道 `cap` 过滤，Web（`supports_proactive=False`）不挂 `create_reminder/create_commitment/update_reminder`（及一并不挂 `list_reminders/cancel_reminder`）；微信工具集等价现状。
- 前端：新增 `chat.html`（§6，`textContent` 渲染、错误码行为）；`home/dashboard` 加 `chat_ready` 入口跳转；首屏拉 Web scope 历史 + 发送闭环。
- 部署：`deploy/nginx/*.conf` 加 `location = /user/chat.html` + `proxy_read_timeout`（§8bis）。
- 验收：老用户刷新 Web → 见对话页、能收发、**看不到微信侧短期对话**；重试同 `client_message_id` 不重复扣费；未 gate 用户原页面不变；**Web 活跃后主动消息仍选微信 binding、绝不选 web**；Web turn 工具集不含提醒/commitment；微信侧不受影响。

> 注：上一版的「Phase 2 = 可选主动消息护栏」已**并入 Phase 0/1 硬需求**（§8.3），不再是独立可选阶段。

**Phase 3+（本期不做，仅登记）**：TDAI 按记忆层级/scope 过滤 recall（让 Web 也享受语义热召回而不泄漏新鲜轮）、媒体、主动消息**触达** Web（区别于 §8.3 的「不误投」护栏）、流式（SSE/WS）、Web-first 注册/onboarding、App 原生端。

---

## 10. 测试方案

聚焦测试（对应 CLAUDE.md 的窄改动优先策略）：

- **Phase 0（原则一硬闸）**：
  - `tests/test_turn_service.py` 现有微信用例**全绿**（抽缝 + scope 参数化后微信行为不变）。
  - 新增 `run_turn_for_account` 直调单测。
  - **scope 参数化守卫**：微信 `cap` 下 active key 仍是 `__account_active__`、归档 key/carryover/last_inbound/TDAI 行为逐项等价现状。
  - **dreaming 双 scope 轮转（Codex ②）**：给同一 `aid_` 造 `__account_active__` 与 `__web_active__` 两条到期 active session，跑 scheduler → 两者**都被关闭**并各自生成 carryover，归档 key 各带自身 scope 前缀。
  - **主动路由能力过滤（§8.3，原则一硬需求）**：账号同时挂微信 + web binding（web 的 `last_seen_at` 更新、更靠前）→ `_select_route` 仍选 `openclaw-weixin`、绝不选 web；`dispatch_proactive_text` 对 `supports_proactive=False` 渠道 fail-fast、mock `send_weixin_text` 零调用。纯微信账号路由等价现状。
- **Phase 1**：新增 `tests/test_web_turn.py`：
  - **门控**：未 gate 用户 `GET /web/me` 返回 `chat_ready=false`；直接打 `POST /web/turn` → `409 not_chat_ready`。
  - **鉴权/账号解析**：未登录 → 401；服务端解析主账号只读、**不建号**（断言无新账号写入）；无 chat_ready 账号 → 409。
  - **入参约束**：空/超长 `text` → 422；`client_message_id` 缺失/格式错 → 422；`limit` 越界截断。
  - **对话**：文本返回 `reply`；限流 → `rate_limited`；`no_reply` 分支；响应含 `Cache-Control: no-store`。
  - **幂等/在途**：同 `client_message_id` 重复 → 返回同一持久化 reply、只扣费一次；in-flight 重复 → `409 turn_in_progress`。
  - **短期隔离（Model B 核心断言）**：同 `aid_` 先微信、后 Web —— 断言两者**不同 session**（`__account_active__` vs `__web_active__`）；`GET /web/messages` **看不到**微信侧消息；Web 新 session **不继承**微信 carryover/rolling。
  - **长期共享**：账号级 MEMORY/SOUL 在 Web prompt 中可见（长期记忆共享成立）。
  - **投递不误发**：Web 工具最终回复走同步返回，**断言不调用 `node_send_text`**（mock 网关零调用）。
  - **TDAI 关断**：Web turn 不触发 recall/capture（mock TDAI 零调用）；微信 turn 仍调用。
  - **last_inbound 渠道隔离**：Web 活跃后，微信 `get_account_last_inbound_at(channel="openclaw-weixin")` 不被刷新（并断言两份同名定义已收敛为一个）。
  - **`accounts.channel` 不被 Web 改写（Codex ⑨）**：Web turn 后账号 `channel` 仍为原值（如 `openclaw-weixin`），不被置成 `web`。
  - **messages 仅当前 active（Codex ①）**：`GET /web/messages` 不返回归档段消息；当前无 active session 时返回空列表；`before_id` 只在当前 session 内分页。
  - **解绑失效（Codex ⑤）**：解绑前 Web 收发正常；解绑后 `GET /web/messages`、`POST /web/turn` 均 `409 not_chat_ready`。
  - **幂等/串行**：同 `client_message_id` 重复 `POST /web/turn` 只计费/回复一次；并发同 scope 请求串行不乱序。
  - **账号隔离守卫**：user A 的 session 只解析到 A 的主账号（无 `account_id` 入参，跨用户越权在构造上不可能）；再加一条守卫断言 A 的 turn/messages 绝不落到 B 的 `aid_`。
  - **XSS**：含 `<script>`/HTML 的历史与回复在前端经 `textContent` 渲染，不执行（前端用例或约定）。
  - **Web 无 onboarding**：gate 命中用户首条 turn 不触发欢迎语。
  - **Web 工具禁用（§8.3，选择 A）**：Web turn 的默认工具集**不含** `create_reminder/create_commitment/update_reminder`（断言工具 spec 列表）；微信 turn 仍含。
  - **主动消息不误投 Web（§8.3）**：Web 活跃后账号有 web binding，跑 commitment/reactivation 投递 → 仍走微信 binding、`send_weixin_text` 的目标是微信 `chat_id`，绝不落 `channel="web"`。
- 触及共享核心（turn_service、session_lifecycle、carryover、TDAI、schema）按 CLAUDE.md 跑**全量回归**再提交（原则一）。

手测脚本：新增 `scripts/send_mock_web_turn.py`（先 OTP 注册/登录拿 `session_token`，再 `GET /web/me` 看 `chat_ready`，再 `POST /web/turn`），默认显式 `--url http://127.0.0.1:8180`。

---

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **微信新老用户受影响（最高）** | 所有改动加性；scope/carryover/投递/last_inbound/TDAI 参数化后**微信取值逐项等价现状**；**微信全量回归为准入硬闸**；注册/绑定链路完全不动 |
| Model B 抽缝面扩大（session_lifecycle/carryover/TDAI/投递）触及共享核心 | 每个改动点提供"微信取值等价现状"单测（Phase 0 守卫）；分阶段小步提交；全量回归 |
| **短期上下文跨渠道泄漏**（Model B 的核心失败模式）| §7.1 scope 隔离 session、§7.2 carryover 按 scope、§7.4 Web 关 TDAI、§5.2 messages 仅 Web scope；隔离断言列为 Phase 1 必过用例 |
| 同一 Web scope 并发（多标签/重试） | §7.6：`(account_id, __web_active__)` **非阻塞单飞**（占用即 `409 turn_in_progress`）+ `client_message_id` 幂等；微信/Web 两 scope 互不阻塞。多 worker 后须换 PG/分布式锁 |
| 未 gate 用户绕过前端直打接口 | `POST /web/turn` / `GET /web/messages` 后端强校验 `chat_ready` + 账号归属 |
| Web 越权访问他人账号 | 强制查 `account_owner_bindings`；越权守卫（turn + messages 两端） |
| Web 活跃污染微信主动消息节奏 | §7.5 last_inbound 按 `channel` 隔离 |
| **新增 Web binding 使现有微信主动路由静默改道（原则一回归，阻断项）**| §8.3 **Phase 0/1 硬需求**：`_select_route` 能力过滤 + `dispatch_proactive_text` fail-fast + Web 工具禁用 + Web binding `chat_id=NULL`；「Web 活跃后仍选微信」列为必过用例 |
| Web 创建出无法投递的提醒/commitment | §8.3 选择 A：Web turn 不挂 `create_reminder/create_commitment/update_reminder`（能力表 gating） |

---

## 12. 与身份模型文档的对齐声明

本文**沿用** [`identity_model_and_wechat_binding.md`](identity_model_and_wechat_binding.md) 的核心口径：`aid_` 仍是唯一业务**隔离键**、`platform_user` 仍是主身份、`channel_bindings` 仍只记通道足迹不代表 owner。

**一处需要同步更新的细化**：该文档现述"对话 session 的核心边界是 AI4ALL Account"。本文引入 **Model B** 后，更精确的表述是——**隔离键仍是账号；但账号内的短期 active session 按 `conversation_scope` 细分**（微信 `__account_active__` / Web `__web_active__`），长期记忆仍账号级共享。落地后应在身份模型文档补：(a) `web` 渠道取值；(b) `conversation_scope` 概念与"短期隔离/长期共享"的边界。这不改变账号隔离不变量，只是在账号**内部**增加了渠道维度的短期分线。

---

## 附录 A — 关键代码触点索引

| 关注点 | 文件:行 | 本期动作 |
|---|---|---|
| `aid_` 生成 | `app/db/_core.py:43` | 不动 |
| 建号入口 | `app/db/billing.py:2059` | 不动（复用） |
| 微信入站身份反查 | `app/db/billing.py:2570` | 不动（仅微信用） |
| openclaw 身份解析 | `app/identity.py:38` | 不动 |
| 微信入站端点 | `app/products/zhaoxi/api/bridge.py:175` | 不动 |
| turn 核心 / onboarding 门 | `app/turn_service.py:787,887` | **抽缝 + 门控改能力表（微信取值不变）** |
| session active key 硬编码 | `app/db/_core.py:21`（`ACCOUNT_ACTIVE_SESSION_KEY`）| **改由 `cap.active_session_key` 传入（微信仍 `__account_active__`）** |
| session 生命周期 / 归档 key | `app/session_lifecycle.py:84,98,111`（`get_or_create_account_active_session_with_dreaming` / `_rotate_session_with_dreaming`）| **参数化 `active_session_key`；归档 key 基于当前 scope（§7.1）** |
| dreaming 每日轮转扫描（写死微信 key）| `app/db/admin.py:865,879`（`list_active_sessions_for_business_day_before`）| **接收 `active_keys` 一组合法 scope，同扫 `__account_active__` 与 `__web_active__`（§7.1 / Codex ②）** |
| carryover 继承 | `app/db/accounts.py:762`（`get_latest_closed_carryover_for_account`）| **加 scope 前缀过滤，用 `substr(...)=?` 非 `LIKE`（§7.2 / Codex ③）** |
| 工具最终回复 out-of-band | `app/turn_service.py:666`（`_send_tool_final_reply`→`node_send_text`）| **按 `cap.supports_out_of_band_tool_final` 分支；Web 同步返回（§4.2）** |
| 微信可触达时间（**两份同名定义**）| `app/db/accounts.py:164` **与** `:1160`（`get_account_last_inbound_at`，后者遮蔽前者）| **先收敛成一个 + `channel` 必传，微信调用传 `openclaw-weixin`（§7.5 / Codex ⑧）** |
| `accounts.channel` 单值字段被覆写 | `app/db/billing.py:2618`（`get_or_create_session`，`SET channel=excluded.channel`）| **定性 legacy；Web 建 session 不更新它（§3 / Codex ⑨）** |
| TDAI recall/capture | `app/tdai_client.py`（namespace 仅 account_id，recall 无分层）| **按 `cap.tdai_enabled` 短路；Web V1 不接（§7.4）** |
| turn 去重（幂等基础）| `app/turn_service.py:27,794`（`get_duplicate_reply`）| **复用做 Web `client_message_id` 幂等（§5.4）** |
| platform_user 登录鉴权 | `app/routers/deps.py:91` | 复用 |
| `GET /web/me`（门控数据源，返回单账号+含写）| `app/routers/web.py:1076`（`get_or_create_default…`）| **补派生字段（加性）；聊天端点勿复用此建号路径（§5.0）** |
| 只读账号解析（新增）| `app/db`（`get_primary_owned_account`，查 `account_owner_bindings`）| **新建：只读、不建号（§5.0）** |
| 注册/绑定链路 | `app/routers/web.py`（OTP/QR 相关） | **不动（原则一）** |
| 账号归属 | `app/db/_core.py:458`（`account_owner_bindings`） | 复用 + 只读解析 |
| 渠道足迹 | `app/db/_core.py:639`（`channel_bindings`） | 复用（加 `web` 足迹） |
| 微信出站网关 | `app/openclaw_gateway.py:35,307` | 不动（V1 Web 不用） |
| **主动路由选择（无能力过滤 + 按 last_seen 排序）**| `app/products/zhaoxi/proactive/contract/common.py:32`（`_select_route`）+ `accounts.py:191`（排序）| **只选 `supports_proactive=True` 渠道（§8.3，Phase 0 硬需求）** |
| **主动投递无条件走微信网关** | `app/products/zhaoxi/proactive/delivery/outbound.py:287,329`（`send_weixin_text` / `dispatch_proactive_text`）| **对 `supports_proactive=False` 渠道 fail-fast，不进网关（§8.3）** |
| **产生未来投递的工具（默认集）**| `app/tools/registry.py:66-70`、`reminder_handlers.py:45`、`commitment_handlers.py:26`| **Web（`supports_proactive=False`）按 cap 不挂 create/update reminder+commitment（§8.3 选择 A）** |
| 新增：渠道能力表 | `app/channels.py`（新建） | **新建** |
| 新增：Web 对话端点 | `app/routers/web.py`（`/web/turn`、`/web/messages`）+ `app/schemas.py` | **新建** |
| 前端静态页（原生多页，非 SPA）| `app/static/home.html:260`、`dashboard.html` | **加 `chat_ready` 入口跳转** |
| 新增：对话页 | `app/static/chat.html`（新建，`textContent` 渲染）| **新建** |
| Nginx 路由/超时 | `deploy/nginx/ai4company.top.conf:40`（显式 location 白名单）| **加 `/user/chat.html` location + `proxy_read_timeout`（§8bis）** |
