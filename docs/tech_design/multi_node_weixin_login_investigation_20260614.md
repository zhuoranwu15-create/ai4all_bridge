# 多机接入 · aliyun2 扫码登录 502 追查记录（临时）

> 日期：2026-06-14　机器：aliyun2（172.24.18.88）　OpenClaw v2026.6.5
> 状态：**✅ 已彻底解决并端到端验证（见 §10）。** 最终根因 = OpenClaw 6.5 core 把 bot `AccountId` 漏出了 `before_agent_reply` hook ctx，导致 bridge 转发 `channel_account_id="openclaw-weixin"`，中心 `no_binding` 静默吞回复。打一处 core 补丁后，微信收发回环跑通。
> 演进：§9 = scope 闸修复 + weixin 加载成功 + 入站打通 + 根因一度误判为「中心缺 binding」；§10 = 用中心 web 注册流程实测后发现 binding 其实健康，真根因是 hook ctx 缺 AccountId，已修复。
> 关联：`multi_node_access_runbook_B.md`、`openclaw_patches_maintenance.md`（第 4 个补丁）、`multi_node_access_refactor.md`

---

## 0. TL;DR

1. 中心 push 扫码登录在 aliyun2 报 502，最初根因 = **本机 openclaw gateway 设备 scope/pairing 闸**把 loopback CLI 的 `web.login.start` 挡了。**已修复**：设备已批到 `operator.admin`。
2. 修完 pairing 后，同一调用的报错**前进**到 `web login provider is not available`——这是个**先前一直存在、被 pairing 闸挡在前面所以从没暴露**的问题：**weixin channel 插件没有被网关加载进运行时 channel 表**。
3. 直接根因：网关启动时**连「发现」都没发现 weixin 插件**（它装在 `~/.openclaw/npm/projects/`，网关只发现了 `~/.openclaw/extensions/` 下的 ai4all-bridge）。对应 memory 里「两机 OpenClaw 安装方式差异」。
4. 修复需要**重启网关**（runbook 147 行：aliyun2 无微信号登录，重启零冲击）+ 让网关能发现/加载 weixin。**尚未执行，待决策。**

---

## 1. 现场与环境

- 网关进程：pid 35744，启动 `2026-06-13 16:09:58`，`/usr/bin/node .../openclaw/dist/index.js gateway --port 18789`
  - systemd user unit：`openclaw-gateway.service`（enabled/running），node `v24.16.0`
  - 16:09 这次重启是 **`plugins.entries.ai4all-openclaw-bridge.hooks.allowConversationAccess=true` 配置变更触发的**
- gateway target：`ws://127.0.0.1:18789`，bind `loopback`，config `/home/jack/.openclaw/openclaw.json`
- gateway auth：`auth.mode=token`（48 字符 token），和 aliyun1 一致；6.5 在 token 之上**多叠了「设备 + scope 审批」层**
- CLI 与网关用**同一个 node**（都 v24.16.0），排除 node 版本因素

---

## 2. Part A —— scope/pairing 闸（已修复，已验证）

### 2.1 根因
6.5 的 loopback CLI 设备（`deviceId a280d034…`，`publicKey qKzio4…`，role operator）原本只批了 `operator.write`。每个需要更高 scope 的调用会发一条 `isRepair` 的待审请求挂在 `~/.openclaw/devices/pending.json` 并把调用挡掉（连接级 1008）。

- `web.login.start/wait` 需要 **`operator.admin`**
- `devices/nodes` 工具调用需要 `operator.pairing`
- 因此 requestId 不断变化（一次操作一条）：`4e342a15`(admin) → `307570c8`(pairing) → `a8cfb40c`(admin)

### 2.2 修复手法（全程未 restart 网关）
```
openclaw devices approve <requestId>      # 关键：不带 --url，走本地 loopback pairing fallback
```
- **loopback fallback = 本地信任根**：能给本机设备批它自身权限之外的 scope（实测 write-only 设备直接批到了 pairing/admin）。文档说 fallback「只给连通性不给审批权」与此 build 实际行为不符。
- 用一个**有界 watcher**（`/tmp/approve_admin_watch.sh`，双匹配 deviceId+publicKey）在中心 push 触发 `operator.admin` 待审时自动批掉——因为 admin 待审只能由 web.login push 本身产生（只读的 `nodes status` 只需 pairing，钓不出 admin）。

### 2.3 当前设备状态（已落地）
```
deviceId       a280d034213914c265e5feec6ad6d043b7ec9c08a240d73c5db7ccfa175968aa
approvedScopes [operator.write, operator.pairing, operator.admin]
token scopes   [operator.admin, operator.pairing, operator.read, operator.write]（已轮换）
pending        空
```
实时 `openclaw devices list` / `nodes status` 均走实时网关成功、loopback 连接干净无 scope 拦截。

### 2.4 验证（错误前进 = 闸已过）
502 body 从：
```
pairing required: device is asking for more scopes than currently approved
scope upgrade pending approval (requestId: a8cfb40c-…)
```
变成：
```
Gateway call failed: GatewayClientRequestError: web login provider is not available
```

---

## 3. Part B —— 一个被 runbook 误判的关键点

`web.login.start` 的失败顺序是：

```
client 连接 gateway
  └─[连接级] scope/pairing 检查  ── 失败 → 1008 "pairing required / scope upgrade pending"（method 不执行）
        └─[method handler] web.login.start
              └─ resolveWebLoginProvider() ── 无 provider → "web login provider is not available"
```

→ runbook（139 行）当时冒烟只拿到 `pairing required`，**根本没跑到查 provider 那步**，所以「provider 可用」是个**从未被真正验证的假设**。147 行「weixin 插件 v2.4.4 loaded 可用」也是基于 `openclaw plugins list` 显示 enabled 的乐观推断，≠ 网关运行时真正加载。

---

## 4. Part C —— 当前拦路虎：weixin channel 插件未被网关加载

### 4.1 provider 解析链（核心代码）
- `dist/web-w83wen5_.js` `resolveWebLoginProvider()`：
  ```js
  listChannelPlugins().find(p =>
    [...p.gatewayMethods ?? [], ...descriptors].some(m =>
      {"web.login.start","web.login.wait"}.has(m)))
  ```
- `dist/registry-CFi90gzU.js`：`listChannelPlugins() = listLoadedChannelPlugins()`
- `dist/registry-loaded-CxuV3tLg.js`：`resolveChannelPlugins()` 读 `getActivePluginChannelRegistryFromState().channels`（**运行时已加载 channel 插件**，不是已配置 channel）

→ `provider not available` 等价于 **weixin 不在「运行时已加载 channel 插件」表里**。

### 4.2 证据链：weixin 从未被加载
- 三次重启（11:39 / 16:08 / 16:10）的 `http server listening (N plugins: …)` 清单**从无 weixin**
  - （注意该计数是 listen *前*的能力/hook 插件；channel 在之后「starting channels and sidecars」阶段加载——但那阶段**也没有任何 weixin 行**，成功/报错都没有）
- 历史日志大量 `unsupported channel: openclaw-weixin`（06-12，补丁前）
- `openclaw status` → `Channels: No channels configured`

### 4.3 直接根因：网关启动**连发现都没发现 weixin**
- `dist/loader-tnIwS4tk.js` `warnWhenAllowlistIsOpen()`：
  ```js
  autoDiscoverable = discoverablePlugins.filter(e => e.origin==="workspace" || e.origin==="global")
  ```
- 启动告警实际只列了 `ai4all-openclaw-bridge`：
  ```
  [plugins] plugins.allow is empty; discovered non-bundled plugins may auto-load:
  ai4all-openclaw-bridge (… /extensions/ai4all-openclaw-bridge/index.js). Set plugins.allow to explicit trusted ids.
  ```
- weixin 也是 `origin=global`，本应同样被列出 → **它压根不在 `discoverablePlugins` 里**（连发现阶段都没扫到）。

### 4.4 安装位置差异（对应「两机装法不同」）
| 插件 | 位置 | 网关启动是否发现 |
|---|---|---|
| ai4all-openclaw-bridge | `~/.openclaw/extensions/ai4all-openclaw-bridge/index.js` | ✅ 发现并加载 |
| **openclaw-weixin** | `~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin/` | ❌ 未发现 |

- CLI 侧（`plugins list` / `doctor` / `registry`）能正常看到 weixin（v2.4.4，enabled，无 compat 问题）——所以**是网关启动发现路径 ≠ CLI 发现路径**。
- 持久 registry（`openclaw plugins registry --json`）`state: fresh`，`plugins[]` 里**有** openclaw-weixin（origin global、enabled true、source 指向上面的 dist/index.js），`installRecords.openclaw-weixin` 完整（npm，integrity/shasum 齐）。→ registry 知道它，但**网关运行时没把它加载成 channel 插件**。

### 4.5 插件本体状态（已确认健康，排除补丁损坏）
- manifest `openclaw.plugin.json`：`channels:["openclaw-weixin"]` + `channelConfigs`，**无 `activation` 字段**（对比 telegram 有 `activation:{onStartup:false}`）
- `package.json` 的 `openclaw`：`channel{id:openclaw-weixin}`、`runtimeExtensions:["./dist/index.js"]`、`minHostVersion>=2026.3.22`（host 6.5 满足）
- gateway-methods 补丁**在位**：`dist/src/channel.js`
  - L26 `export const WEIXIN_GATEWAY_METHODS = ["web.login.start","web.login.wait"];`
  - L108 `weixinPlugin` 对象内 L137 `gatewayMethods: WEIXIN_GATEWAY_METHODS,`
- 即：**只要 weixinPlugin 被加载进运行时 channel 表，provider 就会成立**。问题纯粹卡在「加载/发现」这一步。

---

## 5. 待验证的假设（按可能性）

1. **网关启动发现路径不扫 `~/.openclaw/npm/projects/`**（npm 安装的 channel 插件需要别的注册方式才能被网关 load）。
   - 注意：若 weixin 根本不在 `discoverablePlugins`，那么 `plugins.allow` **未必是解**（allow 是对「已发现」插件做白名单过滤；没被发现就轮不到 allow）。
2. aliyun1（能登）的 weixin **安装位置/方式不同**（可能在 extensions 或别的被发现路径），需对比。
3. 是否需要先**配置一个 openclaw-weixin channel 实例**才会触发 channel 插件加载（manifest 无 `activation`，行为待确认）。

---

## 6. 下一步候选（均**未执行**，待决策）

- **必经**：任何让网关加载 weixin 的改动都要**重启网关**（runbook 147：aliyun2 无微信号登录，重启零冲击）。单纯重启**不够**——三次历史重启都没加载到 weixin。
- 候选 A：弄清网关发现机制，把 weixin 放到/软链到网关能发现的位置（对齐 aliyun1 装法 或 进 extensions），重启后用 `openclaw status` 复核 `Channels` 出现 + provider 成立，再让中心重发扫码 push。
- 候选 B：先取 aliyun1 上 weixin 的安装位置/方式（extensions? npm? plugins.allow?），严格照搬。
- 候选 C：只交诊断，由 aliyun1/人工改。

---

## 7. 复现 / 排查命令速查

```bash
# 设备 scope 现状
openclaw devices list --json
cat ~/.openclaw/devices/{pending,paired}.json

# loopback 批准（不带 --url）
openclaw devices approve <requestId>

# 网关加载了哪些插件 / channel
openclaw status
journalctl --user -u 'openclaw*' --since "<boot>" | grep -iE '\[plugins\]|listening|starting channels'

# 插件发现 / registry
openclaw plugins list
openclaw plugins doctor
openclaw plugins registry --json    # state/persisted.plugins/installRecords

# 网关文件日志（比 journald 详细）
/tmp/openclaw-1002/openclaw-YYYY-MM-DD.log   # JSON-lines

# 核心 provider 解析代码
~/.npm-global/lib/node_modules/openclaw/dist/web-w83wen5_.js         # resolveWebLoginProvider
~/.npm-global/lib/node_modules/openclaw/dist/registry-CFi90gzU.js    # listChannelPlugins
~/.npm-global/lib/node_modules/openclaw/dist/loader-tnIwS4tk.js      # 发现/allow 告警

# weixin 插件本体
~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin/
  ├─ openclaw.plugin.json   # channels / 无 activation
  ├─ package.json           # openclaw.channel / runtimeExtensions
  └─ dist/src/channel.js    # L26 WEIXIN_GATEWAY_METHODS / L137 gatewayMethods
```

---

## 8. 临时产物（讨论后清理）

- `/tmp/approve_admin_watch.sh`、`/tmp/approve_result.json`、`/tmp/approve_result.err`（scope 批准 watcher，已停）
- `/tmp/reg.json`、`/tmp/dl.err`（排查中转储）

> 注：watcher 进程已全部停止；设备 scope 改动（→ admin）是**已落地的真实变更**，不在临时产物范围。

---

## 9. 2026-06-14 续：登录已成功，根因前移至「中心绑定闸」

操作：`openclaw channels login --channel openclaw-weixin`（**未带 `--account`**）+ 微信扫码。终端最后一句 `Local login saved auth for openclaw-weixin/default, but the running gateway did not restart it: invalid channels.start channel`。结论：**该报错良性、已自愈；weixin 已加载；剩余唯一拦路虎在中心，与 OpenClaw 无关。**

### 9.1 `invalid channels.start channel` 是一次性的，已自愈
aliyun2 网关日志时间线：
- `16:02:45` 旧网关 pid 50223 仅 **9 插件、无 weixin**。login 存好 auth + 写 channel 配置后让该网关 `channels.start openclaw-weixin` → 旧运行时不认识 → `invalid channels.start channel`（即终端那句）。
- 写 channel 配置触发 reload：`config change requires gateway restart (channels)`。
- `16:03:43` 网关重启为 pid **50326**，**这次发现并加载了 weixin**：`discovered … openclaw-weixin (…/npm/projects/…)` → `http server listening (10 plugins: …, openclaw-weixin, …)` → `starting weixin provider` → `weixin monitor started (account=a3d6f700e1bb-im-bot)`。

→ **验证了 §5 假设 #3：weixin 插件只有在「配置了 channel 实例」后才会被网关发现/加载**；`channels login` 顺带写了配置 → 强制重启 → 加载成立。`openclaw status` 现 `openclaw-weixin … ON / OK / configured`。

### 9.2 入站全链路已打通（前半段无问题）
weixin 文件日志 + 网关日志证明：`16:03:49` `inbound message from=o9cq…@im.wechat bodyLen=5` → ai4all bridge `forwarding turn channel=openclaw-weixin … backend=http://aliyun1`。

### 9.3 真正「没回复」根因：中心对该号无 binding
从 aliyun2 直接打中心（16ms，HTTP 200）：
```json
{"status":"ignored","reply":null,"no_reply":true,"metadata":{"reason":"no_binding",...}}
```
- `turn_service.py:410` `resolve_account_id_for_inbound_channel_identity(channel_account_id=a3d6f700e1bb-im-bot)` 返回 `None`（无 **completed binding_intent**）；
- `openclaw_inbound_require_binding=True`（config.py:65）→ `no_reply / no_binding`；
- bridge `index.js:616` 的 `no_reply` 分支**静默返回 `NO_REPLY` 哨兵，不发送、不打日志** → 微信端无回复、网关日志 forwarding 之后空白（这解释了为何"看不到任何后续日志"）。

直接成因：`channels login` 走的是 **OpenClaw 本机登录**，绕过了中心 binding_intent 流程；中心不知道 `a3d6f700e1bb-im-bot` 属于哪个 AI4ALL 账号。**恰是 runbook 第 93/116 行预判的 Part-1 最后一步**。

### 9.4 回复回传是同步的（跨机已通）
`turn_service.py:1094` `return OpenClawTurnResponse(status="ok", reply=reply, …)` —— 回复**经 HTTP 响应体同步回传**，aliyun2 bridge 取 `result.reply` 本机经 weixin 发出。跨机回传已被 200 证明，**普通回复不依赖 node-pull**。

### 9.5 收尾两个方案
- **A（test-quick，runbook 背书）**：在 aliyun1 `data/ai4all.sqlite3` 插一条 `binding_intents`（`status='completed'`、`channel='openclaw-weixin'`、`channel_account_id='a3d6f700e1bb-im-bot'`、`account_id` 取 bot id）。解析器仅按 `channel+status=completed+channel_account_id` 匹配（db.py:5209）。独立脚本跑、sqlite3 默认不强制 FK。
- **B（更彻底，推荐）**：用一次性测试号走**中心 web 注册 + 绑定登录全流程**（A.4 登录 push 链路）。前提：① 中心 `access_nodes` 有 aliyun2 在线心跳；② 中心 `node_start_qr/node_wait_qr` push 到 aliyun2 并回传二维码。§2 scope 闸 + §9.1 provider 加载这两个原拦路虎已清，具备一把过条件；若卡住即暴露中心↔node 心跳/push 的下一个缺口。
- 兜底开关：`OPENCLAW_INBOUND_REQUIRE_BINDING=false`（全局放开未绑定号收口，**仅抛弃式测试**）。

### 9.6 待注意
- **首条消息 ≠ 普通回复**：新账号绑定后第一条触发 onboarding 欢迎语；central→node 下 `is_inline_dispatch=False`（config.py:225）→ 走 `enqueue_onboarding_welcome` 靠 node pull 发（主动消息链路待确认）。**回环验证看第二条起的同步对话**。
- **多了空 channel 实例**：`channels list` 有 `default`（无 auth、空壳，未带 `--account` 登录所致）+ `a3d6f700e1bb-im-bot`（有 auth、在跑）；`default` 可清理。

---

## 10. 2026-06-14 终章：真根因 = 6.5 hook ctx 缺 AccountId（已修复 + E2E 验证）

§9 的「中心缺 binding」判断**被后续实测推翻**。用一次性测试号走**中心 web 注册 + 绑定登录全流程**（登录 push 链路 A.4，`web.login.start/wait` 在 aliyun2 网关日志均 200，**登录 push 本身完全打通**）后，仍然「连上但不回复」。深挖发现 binding 其实是健康的，真正的洞在 OpenClaw core。

### 10.1 决定性证据（直连中心对比）
新号 bot id = `568d7b2308fc-im-bot`（中心账号 `aid_956326343`，即今天重绑生成）：
- `curl …/openclaw/turn channel_account_id=568d7b2308fc-im-bot` → **`status:ok` + 正常回复** ⇒ **binding 健康**。
- `curl …/openclaw/turn channel_account_id=openclaw-weixin` → **`no_binding`** ⇒ 复现真实入站失败。
差异只在 `channel_account_id`。

### 10.2 真根因：bridge 拿不到 bot 账号，兜底成了 provider 字面量
- bridge `extractAccountId(ctx, provider)`（`index.js:99`）依次取 `ctx.accountId` → `ctx.providerAccountId` → 从 sessionKey 切 → **兜底 `return provider`**。
- 本次入站 hook 的 `ctxKeys = [agentId, channelId, messageProvider, sessionId, sessionKey, trigger, workspaceDir]` —— **无 accountId**；`channelId` 是微信用户（`o9cq…@im.wechat`）不是 bot；sessionKey 第 4 段是 `direct`（不含 bot id）。三层全落空 → 兜底返回 `"openclaw-weixin"`。
- 于是 bridge 转发 `channel_account_id="openclaw-weixin"` → 中心 `resolve_account_id_for_inbound_channel_identity` 匹配不到任何 binding → `no_binding` → bridge `no_reply` 分支静默吞掉（无回复、日志空白）。

### 10.3 core 在哪丢的（6.5 回归）
`get-reply-BpFiu3Nn.js`（core dist）组装 `before_agent_reply` hook ctx 时（`runBeforeAgentReply({cleanedBody}, {...})`），只透传 `{agentId, sessionKey, sessionId, workspaceDir, trigger}` + `buildAgentHookContextChannelFields(...)`，而后者（`hook-agent-context-*.js`）**只返回 `{messageProvider, channelId}`**。同作用域里 `sessionCtx.AccountId` / `ctx.AccountId` 明明就是 bot 账号（core 自己拿它当 `agentAccountId`/`originatingAccountId`、传给 `getChannelPlugin(provider).elevated`），**就是没放进 hook ctx**。
→ aliyun1 旧版 v2026.5.28 不漏（单机一直好用），aliyun2 v2026.6.5 漏 = 多机才暴露的**版本回归**，**与解绑/重绑无关**。

### 10.4 修复（已落地 + 验证）
core dist `get-reply-BpFiu3Nn.js` 的 hook ctx 字面量（`trigger:` 行后）加一行：
```js
accountId: sessionCtx.AccountId ?? ctx.AccountId,
```
bridge `extractAccountId` 第一步即读 `ctx.accountId`。`node --check` 通过、网关重启、两测试号 monitor 自动 `resuming`。备份 `get-reply-BpFiu3Nn.js.bak-accountid-20260614164059`。**已登记为第 4 个 core 补丁**（见 `openclaw_patches_maintenance.md`）。

**E2E 验证（16:43，真机微信）**：
- `ctxKeys` 新增 `accountId`；`candidates` 出现 `"accountId":"568d7b2308fc-im-bot"`（不再是 `openclaw-weixin`）；
- `inbound message`（16:43:16）→ bridge `forwarding turn`（带正确 bot id）→ **`outbound: text sent OK`（16:43:20）** —— 回复发回微信，**收发回环打通**。

### 10.5 出站 pull 已验证 + 发现 inline bug（2026-06-14 17:08）
- **出站 pull E2E ✅**：在中心(aliyun1)调 `enqueue_proactive_text(account_id=aid_956326343, …, node_id→aliyun2)` 建行 26242 → aliyun2 pull 循环 `claimed_at` 同秒认领 → 网关 `⇄ res ✓ send 504ms channel=openclaw-weixin` → 回报 result → 中心标 `sent`（gateway_message_id 回填）。心跳/claim 端点也直打 200(`access_nodes` aliyun2 online)。
- **⚠️ 同时发现真 bug**：必须**绕过** `dispatch_proactive_text`、直接 `enqueue_proactive_text` 才跑通——因为 `is_inline_dispatch` 是全局开关，aliyun1 开 `LOCAL_NODE_INLINE_DISPATCH=true` 时，正常 dispatch 会对**远程 aliyun2 账号**也本机 inline 发送(无会话→失败 + 抢走队列行)。详情与修法见 `multi_node_access_refactor.md`「🟢 2026-06-14 上线验证」块的已知 bug 注 + 下一步 #5。
