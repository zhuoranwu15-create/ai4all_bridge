# 微信主动发送与 context_token 语义排查（发送凭证来源 / 有效期 / 服务端响应）

**问题**：主动消息（reactivation / 提醒 / commitment 等）发给"长期沉默账号"时，我们能否确定发送会成功？具体两个子问题：

1. 出站发送到底带的是什么"会话凭证"，从哪来？
2. 这个凭证（`context_token`）有效期多长？服务端会不会因为它过期而静默拒收？

**背景**：这是一次源码审计 + 一次线上旁路验证的合并记录。审计对象是两个真实源码仓库：`/home/jack/workspace/openclaw`（核心）+ `/home/jack/workspace/openclaw-weixin`（插件）。线上验证在 aliyun1 运行中的插件 dist 上做临时诊断日志，针对沉默账号 `aid_544704489` 真实发一条后回滚。首次排查：2026-07-02。

---

## TL;DR

- **发送凭证 = `context_token`，唯一来源是"该用户最近一次发给我们的入站消息里带的 `context_token`"。** 插件本地没有任何独立的"申请 token/开会话"接口。见 `openclaw-weixin/src/messaging/process-message.ts:269-271`（唯一写入点）与 `messaging/inbound.ts`（唯一存储 `contextTokenStore`）。
- **本地存储无 TTL / 无过期逻辑。** token 只是一个字符串，连时间戳都不存；一旦拿到就永久复用，直到账号 `logout` 才被 `clearContextTokensForAccount()` 清空（`channel.ts:510`）。不会因时间、gateway 重启、进程重启自动失效。
- **服务端有效期无法从源码确定**——这是腾讯 ilink 后端的私有会话窗口策略，两个仓库里没有任何数字。
- **线上验证纠正了一个此前的推断**：腾讯 `ilink/bot/sendmessage` **确实返回带 `ret` 字段的 JSON 响应体**（不是"完全无响应"）。插件 `api.ts` 的 `sendMessage()` 从不 `JSON.parse` 这个响应体，所以 HTTP 不抛异常就当成功——但信息其实就在响应里、是可读的。
- **观测到的响应取值**（29 次真实发送样本，18:48–23:00）：`{}`（成功，7 次）与 `{"ret":-2}`（22 次）。给沉默账号 `aid_544704489` 那一发是 `{"ret":-2}`。
- **2026-07-03 受控 before/after 实验坐实了核心因果链**（见第三节，用真人收件方当 ground-truth oracle）：对同一沉默账号 `aid_544704489`，**陈旧 context_token（1 个月未刷新）发送 → 我们网关层回 `messageId`（记"假成功"）、但真人收件方没收到（ilink 静默拒收）**；该用户主动入站一条后 context_token 被刷新（值改变、bot token 不变）；**用新鲜 context_token 再发 → 真人收到**。即「沉默 → context_token 陈旧 → ilink 拒收 → 主动消息触达不了；用户再发一条 → 刷新 → 又能触达」成立。
- **本次同时修正了一处文档旧说法**：网关 `send` RPC 的返回体**只有 `{runId, messageId, channel}`、不含 ilink 的 `ret`**（第二节曾推测 `ret` 经 WS result 层回传——未复现）。因此 `_extract_send_result_error` 见 `messageId` 即短路判成功，我们这层对 ilink 的真实回执是"瞎"的——这正是"假成功"落地的地方。
- **仍未精确解出的部分**：context_token 服务端有效期的**具体时长**（本次只证明"1 个月的旧 token 已失效"，未二分窗口边界）；以及 07-02 样本里 `ret:-2` 占 76% 且含活跃会话，**限速语义是否与"token 陈旧拒收"共用 -2** 仍需分离（要拿日志级 `ret` 须重新打诊断补丁）。
- **阶段性缩窄（2026-07-05，单账号 n=1，弱证据）**：`probe_context_token_ttl.py` 每日探针给出窗口的首个数据点——同一枚 context_token（07-03 13:15 刷新后未变），放置 **~20.75h 仍成功送达**、**~44.75h 已翻转为 `ret:-2` 失败**。即服务端有效期落在 **(约 21h, 约 45h)** 区间。用户直觉的"48 小时"方向一致但偏大——首次失败时其实尚不足 48h。详见第七节。

---

## 一、源码结论（确定的部分）

### 1. 发送凭证的唯一来源

`openclaw-weixin/src/`：

- `messaging/process-message.ts:269-271`：**唯一**写入 token 的地方——只有处理一条真实入站消息时，才会从 `full.context_token`（来自 `getUpdates` 响应）调用 `setContextToken(accountId, from_user_id, token)`。
- `messaging/inbound.ts:19,98-113`：`contextTokenStore`（`Map<accountId:userId, token>`）是唯一存储，`setContextToken`/`getContextToken` 是唯一读写入口，没有第二条路径能产生或刷新 token。
- 出站发送时（`channel.ts:224/265/282`、`process-message.ts:370`）统一调用 `getContextToken(accountId, to)` 取出最近一次缓存值塞进 `sendMessageWeixin`；`send.ts:66-67` 若为空只是 `logger.warn`，**不阻止发送**。

> 结论：我们本地没有独立的"会话/token 申请接口"，唯一来源就是"最近一次这个用户发给我们的消息里带的 `context_token`"。代码层面确定、无第二种可能。

### 2. 本地 token 无过期机制

完整读了 `messaging/inbound.ts`（token 存储全部实现）：

- `contextTokenStore` 只存 token 字符串本身，**没有任何时间戳字段**（不存 `expires_at`/`issued_at`）。
- 持久化文件 `{accountId}.context-tokens.json` 同样只是 `{userId: token}` 的纯字符串映射（`persistContextTokens`/`restoreContextTokens`，`inbound.ts:39-78`）。
- 全仓库搜 `expire/expiry/ttl/48h/会话窗口/有效期` 等关键词，在 `openclaw-weixin` 和 `openclaw` 核心里**均无命中**（核心里 context 相关文件全是 LLM 上下文窗口/compaction，与 weixin 的 `context_token` 无关——核心框架根本不知道这个 token 的存在，纯插件内部概念）。
- 唯一清空路径是 `clearContextTokensForAccount()`，只在账号 `logout` 时调用（`channel.ts:510`）。

> 结论：从我们代码视角看，这个 token 一旦拿到就**永久有效、一直复用**，直到 `logout`。

### 3. 别混淆：账号级登录会话超时 ≠ 单联系人 context_token

`api/session-guard.ts` 的 `SESSION_EXPIRED_ERRCODE = -14` 是**整个 bot 账号级别的登录会话超时**（只在 `getUpdates` 响应里检查，`monitor.ts:116-140`），触发后把整个账号暂停 1 小时（`SESSION_PAUSE_DURATION_MS`）。这跟单个联系人的 `context_token` 是否仍被服务端接受**完全是两回事**，代码上无关联。

### 4. 响应体从不被解析（审计发现，已被线上验证证实）

`README.md` 里 `sendMessage` 只文档化了请求体，**没有**文档化响应体（对比 `getUpdates`/`getConfig` 都写清了 response 字段）。且 `api.ts` 的 `sendMessage()` 只 `await apiPostFetch(...)`，**从不 `JSON.parse` 返回值**（`getUploadUrl`/`getConfig` 都会 parse）。即：插件从一开始就没打算处理"发送时 token 是否还被接受"——不是有 TTL 没读，而是压根没解析响应。

---

## 二、线上旁路验证（2026-07-02）

### 方法

在**运行中的插件 dist**（非 workspace 源码仓库）打临时诊断日志：

- 目标文件：`~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin/dist/src/api/api.js`
- 改法：`sendMessage()` 捕获 `apiPostFetch` 返回文本，加一行 `logger.info(`[TEMP-DIAG] sendMessage rawResponse=${redactBody(rawText, 2000)}`)`。纯旁路，函数返回值/调用方行为不变；敏感字段由 `util/redact.js` 的 `redactBody` 屏蔽（只屏蔽 `context_token`/`bot_token`/`token`/`authorization`，`ret`/`errcode`/`errmsg` 照常打印）。
- 生效需 `systemctl --user restart openclaw-gateway.service`（会让全机 ~37 个 bot 的长轮询同秒温和重启，续传 sync buffer、非重新扫码）。这是生产操作，由人工执行重启。
- 触发发送：`app.openclaw_gateway.send_weixin_text`（主动消息同路径），目标 `aid_544704489`，文案"（系统连通性测试，请忽略）"。

### 关键坑：插件 logger 不写 journald

`openclaw-weixin/dist/src/util/logger.js` 的日志**写文件不写 stdout**：`/tmp/openclaw/openclaw-YYYY-MM-DD.log`（每行一个 JSON，默认级别 INFO）。用 `journalctl --user -u openclaw-gateway.service` 是**抓不到**插件 `logger.info` 的——那里只有 openclaw 核心的 `[ws]`/`[plugins]` 行。诊断行要去日志文件里 `grep TEMP-DIAG`。

```bash
grep -F "TEMP-DIAG" /tmp/openclaw/openclaw-$(date +%F).log
```

### 观测结果

29 次真实发送（18:48:45–23:00:11，含验证发送与期间所有有机发送）响应体分布：

| rawResponse | 次数 |
|---|---|
| `{"ret":-2}` | 22 |
| `{}` | 7 |

- 验证发送（→ `aid_544704489`，18:48:45.107）：`{"ret":-2}`。
- `ret:-2` 对应 ai4all 侧 `app/openclaw_gateway.py` 的 `_RATE_LIMIT_RET`（限速码）。~~注意：`ret` 也经 WS gateway result 层回传，被 `_extract_send_result_error` 捕获~~ —— **此说法已被 07-03 实验推翻**：网关 `send` 返回体不含 `ret`（见第三节）。`ret:-2` 只在 `api.ts` 的 rawText 层可见，须靠诊断补丁抓。

### 结论与局限

- **证实**：ilink `sendmessage` 返回带 `ret` 的 JSON 响应体，机器可读。此前"即使服务端拒绝也无法感知"的推断需修正为"信息可读、只是插件层没读"。
- **未证实**：`ret:-2` 占样本 76% 且含活跃会话回复，**无法归因为 context_token 过期**；样本里**没有**出现独立的"token 过期"错误码。因此本次**没有**解出 context_token 的服务端有效期，也**没有**坐实"沉默账号发送必失败"。
- **顺带存疑（另立问题）**：76% 的发送返回 `ret:-2` 本身值得单独查——究竟是限速阈值配置、还是 `-2` 语义比"限速"更宽泛（软拒绝）。这与本文的 context_token 主题不同，勿在此结论上过度延伸。

---

## 三、受控 before/after 实验（2026-07-03，决定性）

### 设计

针对同一沉默账号 `aid_544704489`（映射 openclaw-weixin bot=`f26d8195e21d-im-bot`、user=`o9cq…@im.wechat`，`channel_bindings.last_seen_at=2026-06-06 13:50`），做「旧 token 发 → 用户入站刷新 → 新 token 发」三步，全程**只读文件对照 + 用真人收件方当 ground-truth oracle**，不改 dist、不重启。凭证以指纹（长度 + `sha256[:12]` + head/tail）记录，不落明文。

### 证据链

| 步骤 | 时间 | context_token 指纹 | bot token | 发送/结果 |
|---|---|---|---|---|
| **① 旧 token 发送** | 07-03 10:57 | `a8a2630489d3`（mtime 06-06 13:50，1 月未刷新） | `273aa24013e3` | 网关回 `{runId,messageId,channel}`，`_extract_send_result_error=None`（判"成功"）。**真人收件方未收到** → ilink 静默拒收 |
| **② 用户主动入站** | 07-03 13:15:04 | 文件在 13:15:04.987 被重写为 `7919e81362a8`（入站日志 13:15:04.749，晚 238ms） | `273aa24013e3`（**未变**） | 日志 `inbound message: from=o9cq…` |
| **③ 新 token 发送** | 07-03 14:55 | `7919e81362a8`（新鲜） | — | 网关回 `messageId`。**真人收件方收到** ✓ |

对照发送本身不刷新 context_token：①发送前后指纹与 mtime 逐字节相同，排除"是我们发送动作改了 token"的混淆。

### 结论

- **坐实核心因果链**：陈旧 context_token → ilink 拒收（真人没收到）；入站刷新后新鲜 token → 投递成功（真人收到）。「用户长期沉默 → 主动消息触达不了」的根因就是 context_token 陈旧被服务端拒。
- **两类用户凭证同构**：bot token（账号级）跨入站纹丝不动、与联系人沉默无关；唯一变量是 context_token（联系人级），且**只随入站移动、不随我们的重启/升级/发送移动**（重启 `channel.ts:407 restoreContextTokens` 重载、升级只覆盖 `node_modules`、不碰状态目录 `~/.openclaw/openclaw-weixin/accounts/*`）。即我们自身运维不会造成"不可逆的单向失联"。
- **"假成功"落地点**：网关 `send` 回 `messageId` 即被判成功，对 ilink 的 `ret` 完全不可见。当前主动消息即便被 ilink 拒，ai4all 侧仍记"已投递"——这是应改进的可观测性缺口。

### 局限

- 只证明"1 个月的旧 token 已失效"，**未二分服务端有效窗口的具体时长**（下一步可对不同静默时长的账号分档试探）。
- 步骤①未拿到日志级 `ret`（网关层不含 `ret`，且 `api.ts` 的 raw 响应走 `logger.debug`、默认 INFO 不落盘）；"真人没收到"是间接证据，与 07-02 观测到的 `{"ret":-2}` 一致但未在本轮重新抓码。要精确区分"陈旧拒收"与"限速 `ret:-2`"，仍须重打诊断补丁。

---

## 四、回滚与遗留

- 诊断补丁**已回滚**：`api.js` 已从备份 `api.js.diagbak-20260702` 还原，`diff` 与备份完全一致，`node --check` 通过。
- **待办：运行中进程仍持有内存中的补丁代码，需再 `systemctl --user restart openclaw-gateway.service` 一次彻底清除**（重启才会重新加载还原后的 dist）。
- 手术式改 dist 的通病：**升级/重装插件会覆盖，改动是易失的**（参见 `memory: image-understanding-deploy` / `openclaw-bridge-deploy-state` 同类先例）。若要长期保留"解析并处理 sendMessage 响应体"，须回填到真源码 `openclaw-weixin/src/api/api.ts` 并走正式 deploy。
  - **进展（2026-07-03）：真源码回填已完成**（见第五节），三仓库源码改动已就绪但**未提交、未部署**。

## 五、可观测性修复：把 iLink 业务码打通到 ai4all（2026-07-03，已上线 aliyun1·端到端坐实）

**目标**：终结第三节坐实的"假成功"——让 iLink 的 `ret`/`errcode`/`errmsg` 一路从插件冒到 ai4all，使被下游软拒（含陈旧 context_token 拒收、限速）的发送**显式失败**，而非记"已投递"。

### 改动（四层，源码已就绪）

| 层 | 文件 | 改动 |
|---|---|---|
| 插件 API | `openclaw-weixin/src/api/types.ts` | `SendMessageResp` 补 `ret?/errcode?/errmsg?` |
| 插件 API | `openclaw-weixin/src/api/api.ts` | `sendMessage` 解析响应体、INFO 落日志、返回 `SendMessageResp`（原本丢弃） |
| 插件发送 | `openclaw-weixin/src/messaging/send.ts` | `sendMessageWeixin` 把业务码装进 `meta`（clean accept 时无 meta） |
| 插件通道 | `openclaw-weixin/src/channel.ts` | `sendWeixinOutbound` 透传 `meta` 进 send 结果 |
| **核心** | `openclaw/src/gateway/server-methods/send.ts` | `buildGatewayDeliveryPayload` 白名单**新增 `meta` 透传**（此前只放行 `runId/messageId/channel/chatId/channelId/toJid/conversationId/pollId`，`meta` 被裁——这正是 `ret` 到不了 ai4all 的最后一道墙） |
| ai4all | `app/openclaw_gateway.py` | `_extract_send_result_error` 改为**业务码优先**（不再被恒非空的 `messageId` 短路），并从顶层+`meta` 两处读码 |

`meta` 走的是核心 `OutboundDeliveryResult.meta` 那个官方「channel docking」字段——核心保持通道无关，只转发；微信语义只落在插件+ai4all 两端。

### 部署现实：只能手术式改安装态 dist（不能 rebuild 覆盖）

**运行态与源码严重漂移，实测确认：**

- 核心 gateway 是 **npm 安装版 v2026.5.28**（`~/.openclaw/tools/node-*/lib/node_modules/openclaw/dist/`），**不是** `workspace/openclaw` 源码构建。
- 微信插件安装版是 **2.4.4**，源码仓库仅 **2.4.3**——安装态**独有**源码没有的功能：`api.js` 的 abort-signal 长轮询中断、`send.js` 的 `runId` 透传 + `sendMessageItemWeixin`、`channel.js` 的 `WEIXIN_GATEWAY_METHODS`/`replyProgressMessages`/logout 补丁。
- **故整文件覆盖会大面积回退线上功能** → 部署只能把增量**逐处手术拼接**进安装态 dist（backup + 编辑 + `node --check` + gateway restart），与既往 image/logout 补丁同法。

### 手术式 dist 补丁清单（2026-07-03 aliyun1，可复现·未来做成正式补丁的蓝本）

> 备份统一后缀 `.bak-ctxret-20260703`；每步改完 `node --check`；全部改完 `systemctl --user restart openclaw-gateway.service`。回滚 = 恢复 4 个备份 + 重启。**升级/重装会覆盖，需重打。**

**目标文件（4 个）**

| # | 文件（安装态绝对路径） | 版本锚 |
|---|---|---|
| A | `~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/send-Czbq6yKa.js` | 核心 v2026.5.28（chunk 名随重装变化，按 `buildGatewayDeliveryPayload` 定位） |
| B | `<PLUG>/dist/src/api/api.js` | 插件 2.4.4 |
| C | `<PLUG>/dist/src/messaging/send.js` | 插件 2.4.4 |
| D | `<PLUG>/dist/src/channel.js` | 插件 2.4.4 |

`<PLUG>` = `~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin`

**A. 核心 send chunk** —— `buildGatewayDeliveryPayload` 里 `if ("pollId" in params.result) …` 那行之后新增：

```js
	if ("meta" in params.result) payload.meta = params.result.meta;
```

**B. 插件 `api.js`**

B1. import 区新增（放在 `import { redactBody, redactUrl } …` 附近）：

```js
import { SESSION_EXPIRED_ERRCODE } from "./session-guard.js";
```

B2. 把原 `export async function sendMessage(params) { await apiPostFetch({…}); }` 整体替换为下段（含两个 helper）：

```js
function parseSendMessageResp(rawText) {
    const trimmed = (rawText ?? "").trim();
    if (!trimmed)
        return {};
    try {
        const parsed = JSON.parse(trimmed);
        return parsed && typeof parsed === "object" ? parsed : {};
    }
    catch {
        return {};
    }
}
function classifySendMessageResp(resp) {
    const ret = resp.ret;
    if (resp.errcode === SESSION_EXPIRED_ERRCODE)
        return `session_expired(errcode=${SESSION_EXPIRED_ERRCODE})`;
    if (ret === undefined || ret === 0)
        return "accepted";
    if (ret === -2)
        return "ret_-2(TENTATIVE:rate_limit_or_reject)";
    return `error(ret=${ret})`;
}
export async function sendMessage(params) {
    const rawText = await apiPostFetch({
        baseUrl: params.baseUrl,
        endpoint: "ilink/bot/sendmessage",
        body: JSON.stringify({ ...params.body, base_info: buildBaseInfo() }),
        token: params.token,
        timeoutMs: params.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
        label: "sendMessage",
    });
    const resp = parseSendMessageResp(rawText);
    const to = params.body?.msg?.to_user_id ?? "";
    logger.info(`sendMessage outcome to=${to} classification=${classifySendMessageResp(resp)} ` +
        `ret=${resp.ret ?? "none"} errcode=${resp.errcode ?? "none"} errmsg=${resp.errmsg ?? "none"} ` +
        `raw=${redactBody(rawText, 500)}`);
    return resp;
}
```

**C. 插件 `send.js`**（保留现有 `runId` 透传）

C1. `sendMessageWeixin` 之前新增 helper：

```js
function buildSendResultMeta(resp) {
    if (!resp)
        return undefined;
    const meta = {};
    if (resp.ret !== undefined)
        meta.ret = resp.ret;
    if (resp.errcode !== undefined)
        meta.errcode = resp.errcode;
    if (resp.errmsg !== undefined)
        meta.errmsg = resp.errmsg;
    return Object.keys(meta).length ? meta : undefined;
}
```

C2. `sendMessageWeixin` 内：`await sendMessageApi({…})` 改为捕获返回（函数顶部 try 前 `let resp;`，try 内 `resp = await sendMessageApi({…})`）；结尾 `return { messageId: clientId };` 改为：

```js
    const meta = buildSendResultMeta(resp);
    return meta ? { messageId: clientId, meta } : { messageId: clientId };
```

**D. 插件 `channel.js`** —— `sendWeixinOutbound` 成功分支（文本发送路径，`emitWeixinMessageSent({…success: true…})` 之后那条 `return`；**不动 media 路径的同名 return**）改为：

```js
        return result.meta
            ? { channel: "openclaw-weixin", messageId: result.messageId, meta: result.meta }
            : { channel: "openclaw-weixin", messageId: result.messageId };
```

**说明**：任意上线顺序均安全——插件设了 `meta` 但核心没转发 = no-op；核心转发但插件没设 = 无 `meta`。全效需 A+B+C+D 全上；ai4all 侧改动（业务码优先）单独上线即对「CLI 直返顶层 `ret`」生效。

### 上线验证（2026-07-03，已坐实）

补丁上线并重启网关后：

- **V1 观测层**：插件日志新增 `sendMessage outcome ... classification/ret/errcode/errmsg/raw` 行（`/tmp/openclaw/openclaw-YYYY-MM-DD.log`），每发可见。
- **V2 成功路径不回归**：对新鲜 token 账号（`aid_544704489`，当日 13:15 刚被入站刷新）发送 → iLink 回 `{}`、`classification=accepted`、无 `meta`、ai4all 判成功不误抛。
- **V3/V4 端到端（决定性）**：对 **31 个「aliyun1 归属 + 20–29 天无进站」的陈旧账号**批量发「好久没消息，忙啥呢」→ **31/31 全部 `{"ret":-2}`**（插件日志 `classification=ret_-2`），经 WS send RPC（非 CLI）→ ai4all **全部抛 `OpenClawRateLimited(ret=-2)`**。WS 结果里 `ret` 只经 `meta` 暴露，故此即证明 `插件→核心 meta 透传→ai4all` 链路打通。**这 31 条在旧代码下均是"假成功"（messageId 短路判成功），现全部正确现形。**
- **旁证（相关性，非定论）**：本轮陈旧账号清一色 `ret:-2`、新鲜账号 `{}`（连发也不 -2），说明 `ret:-2` **与 context_token 陈旧强相关**、本修复能覆盖"沉默用户不可达"这一场景。这 31 条被拒 = 未投递，真实用户未被打扰。
- **⚠️ `ret:-2` 的确切语义未经证实、官方无定义**：搜遍 `openclaw` / `openclaw-weixin` 源码、安装态 dist 与 README，**唯一有官方定义的码是 `ret===0`=成功、`errcode===-14`=账号级会话超时（getUpdates 用，与 sendMessage 无关）；`-2` 无任何定义**。代码里凡把 `-2` 说成"限速/拒收/过期"的（api.ts 的 TENTATIVE 注释、ai4all `_RATE_LIMIT_RET=-2`）**都是我们自己的假设**。**反证**：07-02 样本里 `-2` 占 76% 且含活跃会话的正常回复 → `-2` 更像**宽泛的通用拒绝码**（限速/软拒/陈旧可能共用），非"陈旧专用"。响应体只有 `ret`、`errmsg` 为空，无法从服务端拿到细分。要坐实语义仍须第一节探针到期日重打诊断补丁抓 `errcode`/`errmsg`，并分离限速噪声。

### 上线后语义变化（口径待修）

`ret:-2` 从"假成功"变"显式失败"（抛 `OpenClawRateLimited`）——主动消息成功率会真实下掉，告警/看板口径需同步。仍无法从 `-2` 单独区分"限速 vs 陈旧拒收"（响应体只有 `ret`），该区分靠第一节探针到期日 + 重打诊断补丁抓 `errcode`/`errmsg` 收口。

## 六、要拿到确凿的服务端窗口数字，下一步

代码审计已到天花板。要坐实 context_token 的服务端有效期，只能设计更受控的验证：

1. 隔离变量——选一个**确知长期无入站、且当前未限速**的账号单独发，避免被 `ret:-2` 限速噪声淹没。
2. 先厘清 `ret:-2` 语义（限速 vs 通用拒绝），必要时把 `errcode`/`errmsg` 也打全（本次响应体只有 `ret`，未见 `errmsg`）。
3. 若能构造"同一账号 token 新鲜 vs 放置 N 小时后"两组对照发送，观察 `ret` 是否随时间翻转，才能反推窗口。

---

## 七、阶段性发现：token 有效期窗口的首个单账号观测（2026-07-05）

第六节第 3 点建议的"同一账号 token 随时间对照发送"实验，已由 `scripts/probe_context_token_ttl.py`（cron 每日 10:00 向 `aid_544704489` 发一条"第 N 天+日期+冷笑话"）落地。这里记录它给出的**首个数据点**——注意是 **n=1 单账号、弱证据**，不是结论。

### 观测事实

该用户的 `context_token` 自 **2026-07-03 13:15:04** 刷新后**值一直未变**（`sha12=7919e81362a8`，两天内无新入站消息刷新它）。用同一枚 token 在不同时刻发送：

| 发送时刻 | 距 token 刷新 | 网关回执 | 真人是否收到 |
|---|---|---|---|
| 2026-07-04 10:00 | ~20h45m | `ok`（messageId） | ✅ 收到（用户确认） |
| 2026-07-05 10:00 | ~44h45m | `ret:-2` | ❌ 未收到 |
| 2026-07-05 17:24（手动补触发） | ~52h | `ret:-2` | ❌ 未收到 |

日志：`data/context_token_probe/{probe.jsonl,state.json,cron.out}`。

### 推测（未证实）

- 同一枚 token、无其他变量变化，回执从"成功+真人收到"翻转为 `ret:-2`，**时间相关性强**：服务端有效期窗口疑似落在 **(约 21h, 约 45h)** 之间。
- 用户直觉"超过 48h 就发不出"方向一致，但**偏大**——首次失败发生在 ~44.75h，尚不足 48h。所以严格说是"**≤ ~45h**"，48h 只是量级近似。
- 这与第三节"沉默 → token 陈旧 → ilink 拒收"因果链自洽，可视为其**时间维度的补充**：陈旧不是布尔量，而是有一个约 1 天量级的服务端窗口。

### 局限（务必保留，勿据此当定论）

1. **n=1**：单账号、单枚 token，一次翻转。不能排除偶发（当日限速、账号侧其他状态）。
2. **`ret:-2` 语义仍未证实**（见第四、五节）：`-2` 无官方定义，"过期 / 限速 / 软拒"可能共用此码。因此"失败"未必等同"token 过期"。07-05 两次失败相隔 7h 仍 `-2`，**削弱**了纯限速假设、**相对支持**陈旧假设，但仍非定论。
3. **窗口很宽**：(21h, 45h) 未二分。要收窄需在 token 刷新后按 24h/30h/36h/42h 等多点发送，或对多个隔离账号并行取样。
4. 要彻底坐实"过期 vs 限速"，仍须回到第五/六节方案：插件侧抓 `errcode`/`errmsg`，把失败原因从服务端拿到手。
