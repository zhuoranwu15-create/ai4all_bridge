# OpenClaw 补丁与部署机制

> 状态：**现行维护文档**（现网 4 补丁的权威记录；上游 OpenClaw 变动时需重点回归）。更新时间：2026-06-13
> 用途：记录本项目"改 OpenClaw 上游 + 部署到运行时"的关键技术流程。**这两处是项目落地的关键点；一旦上游 OpenClaw 有变动，需重点回归检查。**
> 关联：[图片理解技术设计](../platform/image_understanding_design.md)

---

## 0. 背景：为什么需要 patch OpenClaw

OpenClaw（`/home/jack/workspace/openclaw`）是**上游第三方仓库**（`openclaw/openclaw.git`），我们无法把改动提交进去。有些能力（微信登录方法、图片本地路径透传）必须改 OpenClaw 才能实现，所以把改动存成 **unified diff（`patches/*.patch`）放在本仓库**留档与重放。

部署环境关键事实：
- 本机 `aliyun1` 既是开发机也是线上机。
- gateway = systemd **user** 服务 `openclaw-gateway.service`，运行**编译后的 dist**：
  `/home/jack/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/`（当前 **v2026.5.28**）。
- 源码树 `/home/jack/workspace/openclaw` 当前是 **2026.6.2**（与运行版本不一致）。
- backend = system 服务 `ai4all-weixin-backend.service`。

### ⚠️ 多机：两台机器的 OpenClaw 安装方式不同（2026-06-13 aliyun2 实测）

> 后续还要铺更多 node 机，**安装方式必须有统一标准**，否则每台机路径都不一样、patch/脚本要各写一套。

| | aliyun1（中心） | aliyun2（node） |
|---|---|---|
| 安装方式 | OpenClaw **官方安装器**（自带 pinned node） | `npm install -g openclaw` 到 `~/.npm-global` |
| 核心 dist 根 | `~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/` | `~/.npm-global/lib/node_modules/openclaw/dist/` |
| 运行 node | OpenClaw 自带 **v22.22.0**（锁定） | 系统 `/usr/bin/node` **v24.16.0** |
| OpenClaw 版本 | v2026.5.28 | v2026.6.5 |
| 插件运行时根 | `~/.openclaw/npm/projects/...`（**两机同构**） | `~/.openclaw/npm/projects/...` |

差异只在**核心**（路径 + node 版本）；weixin 插件运行时路径方案两机一致，所以 `patch -p1` 打插件补丁两机通用。

**标准与处置（已定）**：
- **新机标准 = 官方安装器**（自带 pinned node，对齐 aliyun1）：锁定官方测试过的 node 版本、消除漂移、核心路径全机群一致。
- **现实前提**：官方安装器要下载 node，国内网络可能被卡（aliyun2 当初走 npm-g 很可能就是这原因）。装不通 → 退用 npm-g，但**显式把系统 node 锁到 v22.x**（对齐官方），并把该机核心/插件路径登记进 runbook。
- **aliyun2 保持现状不重装**（v2026.6.5 健康运行；重装要重登账号、重打补丁，纯属自找中断）。
- **耐久修复（比统一安装更重要，已做）**：部署/回滚脚本改为**路径无关**（见 §3 注）—— 因为哈希 bundle 名（`9dLyvuw9` vs `BpFiu3Nn`）是随 OpenClaw 版本变的、跟安装方式无关，每次升级都会变。

---

## 1. 四个 patch 对比

| | QR 登录：`openclaw-weixin-gateway-methods-runtime.patch` | 图片理解：`openclaw-before-agent-reply-media.patch` | 解绑登出：`openclaw-weixin-logout-account-runtime.patch` | 多机账号路由：`before-agent-reply-accountid`（§2.6，2026-06-14） |
|---|---|---|---|---|
| 目标组件 | openclaw-weixin **渠道插件** `src/channel.ts` | openclaw **核心** `src/auto-reply/reply/get-reply.ts` | openclaw-weixin **渠道插件** `dist/src/channel.js` | openclaw **核心** `get-reply-*.js`（与图片补丁同文件） |
| 作用 | 给插件加 `gatewayMethods: ["web.login.start","web.login.wait"]`（登录流程） | 在 `before_agent_reply` 钩子触发前，把入站媒体绝对路径以 `[media attached: <path> (<type>)]` 注入钩子的 `cleanedBody` 副本 | 给 gateway 加 `logoutAccount`，解绑时删 weixin 账号文件 + 索引除名，闭合「跨层裂脑」（详见 [解绑登出补丁](openclaw_weixin_gateway_logout_patch.md)） | 把 bot `AccountId` 注入 `before_agent_reply` hook ctx，使 bridge 能转发正确 `channel_account_id`（多机入站绑定解析依赖） |
| 是否含 dist hunk | **✅ 含**（`dist/src/channel.js`，编译路径稳定、无哈希） | **❌ 仅 src**（核心 dist 是带内容哈希的 bundle，无法写稳定 patch） | **✅ 含**（`dist/src/channel.js`，路径稳定） | **❌ 仅手术 dist**（哈希 bundle，暂无 patch 文件；锚点字符串稳定，见 §2.6） |
| 生产生效方式 | 可直接 `git apply` 到运行 dist，无需重新构建 | 由 `scripts/deploy_image_understanding.sh` **手术式改当前哈希 dist 文件** | `patch -p1` 到运行 dist + `node --check` + 重启 gateway（详见专门文档 §5） | `scripts/patch_openclaw_accountid.sh`（幂等、自动发现；已折进 deploy 脚本 `[1b/4]`，§2.6） |

> ⚠️ **解绑登出补丁的特殊关注点（v2.4.4 源码未发布）**：官方 GitHub 仓库 tag 止于 v2.4.3，腾讯只发布了 **2.4.4 的 npm 产物（dist）**，源码未推。实测 v2.4.4 dist（53 模块）⊃ v2.4.3 源码（33 模块），多 20 个模块。**因此不能从 workspace v2.4.3 build 覆盖 dist（会丢 20 个功能）**；在腾讯发 2.4.4+ 源码前，手术热补丁是唯一正确路径，且插件每次升级会覆盖丢失。详见 [openclaw_weixin_gateway_logout_patch.md](openclaw_weixin_gateway_logout_patch.md)。

---

## 2. 新 patch 细节：`openclaw-before-agent-reply-media.patch`

**改了什么**：在核心 `get-reply.ts` 的 `before_agent_reply` 钩子调用前，若 `hasInboundMedia(ctx)`，取 `ctx.MediaPath/MediaPaths` 首个绝对路径 + `ctx.MediaType/MediaTypes` 类型，拼成 `[media attached: <abs_path> (<type>)]` 前置到传给钩子的 `cleanedBody` **副本**（真正的 prompt 不改）。这一处是"让 bridge 看到图片本地路径"的唯一关键点 —— 因为媒体说明（media-note）原本是在钩子**之后**才注入 prompt 的。

**配套的 bridge 侧**：`openclaw-bridge/index.js` 用 `parseInboundMediaMarker()` 解析该标记 → 转发 `message_type=image` + `media{path,format}`（不复用 `extractMediaMarkers`，后者为日志会截断长路径）。

### 如何使用（两种场景）

**A. 正规路线 —— 从源码重新构建 OpenClaw 核心：**
```bash
cd /home/jack/workspace/openclaw
git apply /opt/workspace/ai4all_bridge/patches/openclaw-before-agent-reply-media.patch
pnpm build           # 再把 dist 部署到运行位置
```
（patch 行号对应源码树 2026.6.2，可干净 apply。）

**B. 当前快路线 —— 原地改运行 dist（无需构建）：**
```bash
bash scripts/deploy_image_understanding.sh        # 幂等，带 *.bak.imageunderstanding 备份
bash scripts/rollback_image_understanding.sh      # 回滚
```
部署脚本动作：① 手术改核心 dist；② 同步 bridge 插件到 `~/.openclaw/extensions/ai4all-openclaw-bridge/index.js`；③ `.env` 开 `IMAGE_UNDERSTANDING_ENABLED`；④ 重启 backend + gateway。

---

## 2.5 ✅ 多机下图片理解：已选「bridge 传字节 / 内联 base64」并落地（2026-06-13）

`before-agent-reply-media` 补丁透传的是 **接图那台机器的本地绝对路径**；后端 `app/image_understanding.py` 也是**按本地文件路径读图**（`open(real_path,"rb")`，且经 `image_inbound_dir` 白名单根 + realpath 校验防穿越），bridge 只转发**路径字符串**（`media{path,format}`），**不传图片字节**。

单机（aliyun1：接图、后端、读图同机）成立。但多机 node（aliyun2）下：

- 微信图片落在 **aliyun2** 磁盘 → 路径注入标记 → aliyun2 的 bridge 把路径转发到**中心 aliyun1** 的后端；
- aliyun1 后端 `open(path)` **读不到 aliyun2 上的文件**，且该路径会被 `image_inbound_dir` 白名单校验直接拒绝。

**结论**：node 机上单纯打这个补丁 + 装 bridge **走不通**（曾评估三条路线：node 本地处理 / 传字节 / 共享存储）。

**已选并实现 = 传字节（内联 base64）**：node 的 bridge 把已下载的图片读成字节 → base64 内联进 `/openclaw/turn` 的 `media.data_base64`（+`format`+`size`）→ 中心 `image_understanding.py` 用字节构造 data URL 调 VL，**不再依赖中心能访问 node 本地路径**。零新基建、零新依赖、单机/多机同一条代码路径；OSS 对象存储路线否决（每台 node 要配 bucket+凭证+新依赖）。

代码改动（已合入本分支）：
- `app/schemas.py::MediaPayload` 加 `data_base64` / `size`。
- `app/image_understanding.py` 新增 `_data_url_from_b64()`，`describe_image()` 加 `image_b64`/`image_format`，来源优先级 **b64 > path > url**；字节路径不经 `image_inbound_dir` 白名单（字节无路径概念），仍受 `image_max_bytes` 上限保护。
- `app/turn_service.py` 图片轮把 `data_base64`/`format` 传给 `describe_image`。
- `openclaw-bridge/index.js` 图片轮用 `node:fs` 读 `inboundMedia.path` → base64 内联（cap=`AI4ALL_IMAGE_MAX_BYTES`，默认 5MB；超限/读失败**降级**为仅转发 `path`，单机 aliyun1 中心可直读，向后兼容）。
- `scripts/send_mock_turn.py` 加 `--image-bytes` 自测入口。

⚠️ **运维前置（node 上线图片理解必做）**：node→中心是经 **aliyun1 nginx**（`http://aliyun1` → `/openclaw/turn`），默认 `client_max_body_size 1m` 会把 base64 大图 **413** 截断。须在 aliyun1 nginx 该 location 调到 `client_max_body_size 12m;`。三方大小要协调：bridge cap(5MB) ≤ 中心 `image_max_bytes`(10MB) ≤ nginx(12MB，需 ≥ base64 膨胀 ~6.7MB + raw 开销)。aliyun1 本机 bridge 直连 `127.0.0.1`、不过 nginx，不受影响。

**安全/隐私**：base64 仅在内存临时传给 VL，**不落库** —— 不进 `messages.raw`（base64 在 `payload.media` 不在 `payload.raw`），不进审核库（`app/platform/moderation/service.py::_media_to_dict` 的 `allowed_keys` 白名单不含 `data_base64`，天然过滤）。

> 多机接入阶段，node 机现可打全部三个补丁（QR 登录 + 解绑登出 + 图片理解）；图片补丁配套需上 bridge 字节改造 + nginx body-size。

## 2.6 ✅ 第 4 个补丁：`before-agent-reply-accountid`（多机入站账号路由，2026-06-14）

**为什么需要（多机才暴露的 6.5 回归）**：多机入站靠 bridge 把「哪个 bot 账号收到的消息」(`channel_account_id`) 转给中心，中心据此解析 binding → 账号。但 **OpenClaw v2026.6.5 core 组装 `before_agent_reply` hook ctx 时漏掉了 bot `AccountId`**：

- hook ctx 只有 `{agentId, sessionKey, sessionId, workspaceDir, trigger, messageProvider, channelId}`，`channelId` 是微信**用户**不是 bot，sessionKey 第 4 段是 `direct` 不含 bot id。
- 于是 bridge `extractAccountId`（`~/.openclaw/extensions/ai4all-openclaw-bridge/index.js:99`）三层取值全落空 → 兜底 `return provider` → 转发 `channel_account_id="openclaw-weixin"` → 中心 `no_binding` → **静默不回复**。
- aliyun1 旧版 **v2026.5.28 不漏**（单机一直正常）；这是 6.5 的回归，**aliyun1 升级到 6.5 时也会需要本补丁**。完整追查见 [`../../archive/investigations/multi_node_weixin_login_20260614.md`](../../../archive/investigations/multi_node_weixin_login_20260614.md) §10。

**改了什么**：在 core dist `get-reply-*.js` 里、`runBeforeAgentReply({ cleanedBody ... }, { ... })` 的 ctx 字面量中（`trigger:` 行后）加一行：

```js
accountId: sessionCtx.AccountId ?? ctx.AccountId,
```

`sessionCtx.AccountId`/`ctx.AccountId` 是 bot 账号（core 自己当 `agentAccountId`/`originatingAccountId` 用），补进后 bridge 第一步 `ctx.accountId` 即命中。

**耐久化脚本（✅ 2026-06-14 已落地）**：`scripts/patch_openclaw_accountid.sh` —— host-agnostic、幂等、自动发现 node + 哈希 bundle，备份到 `*.bak.accountid`，已打则跳过；默认重启 gateway，`--no-restart` 仅打补丁。**在哪台跑就打哪台**（今天 = aliyun2 接入节点；aliyun1 升到 6.x 时也跑）：
```bash
bash scripts/patch_openclaw_accountid.sh                 # 打补丁 + 重启 gateway
# 或仅打补丁（调用方自行重启）：bash scripts/patch_openclaw_accountid.sh --no-restart
```
该脚本也已**折进** `scripts/deploy_image_understanding.sh`（步骤 `[1b/4]`，传 `--no-restart`，复用同一 `$DIST`，由该脚本步骤 4 统一重启）→ 图片补丁与 accountId 补丁现为**同一入口**一起重放。

**底层手术（脚本内部逻辑，供参考）**：在 core dist `get-reply-*.js` 的 `runBeforeAgentReply(...)` ctx 字面量里、`trigger: opts?.isHeartbeat ? "heartbeat" : "user",` 行后插入 `accountId: sessionCtx.AccountId ?? ctx.AccountId,`，幂等判据 = 文件是否已含 `accountId: sessionCtx.AccountId`。
- aliyun2 已落地：备份 `get-reply-BpFiu3Nn.js.bak-accountid-20260614164059`，`node --check` 通过，重启后微信收发回环 E2E 验证通过。
- 注：aliyun1 v5.28 core 含**完全相同**的锚点结构（同 `sessionCtx`/`ctx` 命名）但当前未打此补丁（5.28 不漏，本机账号正常）→ 暂不主动改其在跑 core；待其升级到 6.x 时随脚本一并打。

**升级/重装必查**：`grep -c 'sessionCtx.AccountId ?? ctx.AccountId' ~/.npm-global/lib/node_modules/openclaw/dist/get-reply-*.js`，返回 0 则重打。

## 2.7 ⚠️ 第 5 个补丁（尚未固化为正式 patch 文件）：context_token 送达回执透传（2026-07-03）

**为什么需要**：排查发现微信静默拒收陈旧 `context_token` 的发送（详见 [`weixin_context_token_send_semantics.md`](../../../ops/platform/troubleshooting/weixin_context_token_send_semantics.md)），而插件 `sendMessage()` 从不解析 iLink 响应体、核心 `send` RPC 的白名单也裁掉了 `meta` 字段，导致这类静默拒收在 ai4all 侧被误判为"已发送"（`messageId` 恒非空）。

**改了什么（四层，源码已就绪，见 troubleshooting 文档 §5 完整 diff）**：
1. 插件 `api.ts`：`sendMessage` 解析响应体、透出 `ret/errcode/errmsg`。
2. 插件 `send.ts`：把业务码装进 `meta`。
3. 插件 `channel.ts`：透传 `meta` 到发送结果。
4. **核心** `send.ts` 的 `buildGatewayDeliveryPayload` 白名单新增放行 `meta`（此前只放行 `runId/messageId/channel/...`，是 `ret` 到不了 ai4all 的最后一道墙）。
5. `app/openclaw_gateway.py` 的 `_extract_send_result_error` 改为业务码优先，不再被恒非空的 `messageId` 短路。

**当前状态（⚠️ 与其它 4 个补丁不同，尚未达到同等耐久化水平）**：
- 仅**手术式**改了 aliyun1 运行中的 4 个 dist 文件（备份后缀 `.bak-ctxret-20260703`），已端到端验证（31 个陈旧账号全部从"假成功"变为显式 `OpenClawRateLimited`）。
- **未**沉淀成 `patches/*.patch` 文件，**未**有配套的 `deploy_*.sh`/`rollback_*.sh` 脚本（对比 §2.2/§2.6 的其它补丁）。
- **aliyun2 是否已打同一补丁未经确认**——多机路由下账号可能归属 aliyun2 节点，若该机未打补丁，其账号仍会退回"假成功"语义，且两台机器的可观测性口径不一致。
- 任何一次 OpenClaw 核心或 openclaw-weixin 插件的升级/重装都会静默抹掉这 4 处手术改动，**且症状隐蔽**：不会报错，只是重新开始把静默拒收误判为已发送。

**待办（详见 §5 TODO）**：把 4 处 dist 改动整理成正式 `patches/*.patch` + 幂等部署/回滚脚本；确认并按需补打 aliyun2；升级后必查清单（§3）补一条针对本补丁的 grep 检测。

## 3. ⚠️ 升级 OpenClaw 后必查（重点）

OpenClaw 升级/重装会覆盖 dist，图片理解会**静默退回成空文本（不报错）**。届时需重新部署。当前已知脆弱点：

1. ~~**部署脚本写死了哈希文件名** `get-reply-9dLyvuw9.js`~~ ✅ **已修复（2026-06-13）**：`scripts/deploy_image_understanding.sh` 与 `rollback_image_understanding.sh` 现在**自动发现**核心 bundle —— 按内容 `grep -rl 'runBeforeAgentReply({ cleanedBody'` 定位（兼容已打/未打补丁两态），并自动探测核心 dist 根（官方安装器 / npm-g 两种布局）与 node。可用 env 覆盖：`OPENCLAW_CORE_DIST_DIR`、`OPENCLAW_NODE`、`OPENCLAW_BRIDGE_DST`。升级后哈希名变化不再需要改脚本。
2. **钩子调用签名若上游改了** `runBeforeAgentReply({ cleanedBody }, {…})`，脚本的字符串匹配会失效 → 需更新匹配串与源码 patch。
3. **media-note 注入时机若上游调整**（目前在钩子之后），需重新确认本方案前提仍成立。
4. **`ctx.MediaPath/MediaPaths/MediaTypes` 字段名若上游变更**，源码 patch 需同步。
5. 旧 weixin patch 同理：升级后确认 `gatewayMethods` 是否仍生效（`grep -r WEIXIN_GATEWAY_METHODS`）。
   - ⚠️ **v2026.6.5+ 新增钩子安全闸（2026-06-13 aliyun2 实测）**：非内置(path)插件的会话钩子默认被拦，`openclaw plugins inspect ai4all-openclaw-bridge --runtime` 会报 `typed hook "before_agent_reply" blocked ... must set plugins.entries.ai4all-openclaw-bridge.hooks.allowConversationAccess=true`。装/升级 bridge 后须 `openclaw config set plugins.entries.ai4all-openclaw-bridge.hooks.allowConversationAccess true` 再重启 gateway，否则 bridge 加载了但**所有钩子静默失效**（aliyun1 旧版 v2026.5.28 无此闸）。
6. **解绑登出 patch 同理且更脆弱**：插件升级覆盖 `node_modules` 后 `logoutAccount` 丢失，解绑会退回留孤儿 bot。升级后必查：
   `grep -c logoutAccount ~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js`，返回 0 则重打 `patches/openclaw-weixin-logout-account-runtime.patch`。详见 [专门文档](openclaw_weixin_gateway_logout_patch.md)。
7. **多机账号路由 patch（§2.6，core，v2026.6.5+ 必需）**：core 升级覆盖 dist 后 `accountId` 注入丢失 → 多机入站全部 `no_binding` 静默不回复（**症状隐蔽，无报错**）。升级后必查：`grep -c 'sessionCtx.AccountId ?? ctx.AccountId' ~/.npm-global/lib/node_modules/openclaw/dist/get-reply-*.js`，返回 0 则按 §2.6 重打。**aliyun1 从 5.28 升到 6.5 时同样要打。**
8. **context_token 送达回执透传 patch（§2.7，插件+core，2026-07-03）**：升级/重装插件或核心会同时抹掉 4 处手术改动，退回"假成功"语义（**症状隐蔽，无报错，只是静默拒收又被误判为已发送**）。升级后必查：`grep -c classifySendMessageResp <PLUG>/dist/src/api/api.js` 与 `grep -c 'params.result.meta' <核心 send 主 chunk>`，任一返回 0 则按 §2.7 重打。**aliyun2 当前是否已打未经确认，需先补一次核查。**

---

## 4. 验证

```bash
# 核心 dist 是否已打补丁
grep -c "hookCleanedBody" /home/jack/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/get-reply-*.js

# 真机发图后，确认入站被识别为 image
journalctl -u ai4all-weixin-backend.service -n 80 --no-pager | grep "openclaw_turn received"
# 期望：type=image，且落库 content 为合成的多维描述，cost_events 有 cost_type=image_understanding
```

---

## 5. 待跟进 TODO

- [x] ~~部署脚本自动发现哈希 dist 文件名~~ 已完成（2026-06-13，见 §3 注）。
- [x] ~~`patches/` 增加 README，统一说明四个 patch 的用法与适用场景。~~ ✅ 已完成（2026-06-15，`patches/README.md`：4 补丁一览 + 适用机型/版本 + 升级必查 grep，索引到本文）。
- [x] ~~**把 `before-agent-reply-accountid`（§2.6）注入折进 `scripts/deploy_image_understanding.sh`**~~ ✅ 已完成（2026-06-14）：抽成独立耐久化脚本 `scripts/patch_openclaw_accountid.sh`（host-agnostic、幂等、自动发现），并被 deploy 脚本步骤 `[1b/4]` 复用。aliyun2 已用脚本确认幂等落地。
- [x] ~~**多机图片理解方案**：在 §2.5 三条路线里选定并实现~~ 已选 **传字节/内联 base64** 并落地（2026-06-13，见 §2.5）。剩余：node 真机端到端验证 + aliyun1 nginx `client_max_body_size` 调整。
- [ ] **统一 OpenClaw 安装标准**：新机优先官方安装器；装不通则 npm-g + 锁 node v22.x（见 §0 多机表）。
- [ ] 评估长期方案：是否统一从源码树构建并部署 OpenClaw（消除版本漂移与哈希 dist 手术补丁的脆弱性）。
- [ ] 确认旧 weixin patch 当前在运行环境的生效状态与留档完整性。
- [ ] **（新增，2026-07-08）context_token 送达回执透传（§2.7）耐久化**：整理成 `patches/*.patch` + 幂等 `deploy_*.sh`/`rollback_*.sh`（参考 §2.6 的 `patch_openclaw_accountid.sh`）；确认 aliyun2 是否已打并按需补打；补进 `patches/README.md`。
