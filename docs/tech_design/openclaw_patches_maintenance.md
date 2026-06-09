# OpenClaw 补丁与部署机制（临时文档 / 待跟进）

> 状态：**临时草稿，待后续跟进整理**。更新时间：2026-06-07
> 用途：记录本项目"改 OpenClaw 上游 + 部署到运行时"的关键技术流程。**这两处是项目落地的关键点；一旦上游 OpenClaw 有变动，需重点回归检查。**
> 关联：[图片理解技术设计](image_understanding_design.md)

---

## 0. 背景：为什么需要 patch OpenClaw

OpenClaw（`/home/jack/workspace/openclaw`）是**上游第三方仓库**（`openclaw/openclaw.git`），我们无法把改动提交进去。有些能力（微信登录方法、图片本地路径透传）必须改 OpenClaw 才能实现，所以把改动存成 **unified diff（`patches/*.patch`）放在本仓库**留档与重放。

部署环境关键事实：
- 本机 `aliyun1` 既是开发机也是线上机。
- gateway = systemd **user** 服务 `openclaw-gateway.service`，运行**编译后的 dist**：
  `/home/jack/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/`（当前 **v2026.5.28**）。
- 源码树 `/home/jack/workspace/openclaw` 当前是 **2026.6.2**（与运行版本不一致）。
- backend = system 服务 `ai4all-weixin-backend.service`。

---

## 1. 三个 patch 对比

| | QR 登录：`openclaw-weixin-gateway-methods-runtime.patch` | 图片理解：`openclaw-before-agent-reply-media.patch` | 解绑登出：`openclaw-weixin-logout-account-runtime.patch` |
|---|---|---|---|
| 目标组件 | openclaw-weixin **渠道插件** `src/channel.ts` | openclaw **核心** `src/auto-reply/reply/get-reply.ts` | openclaw-weixin **渠道插件** `dist/src/channel.js` |
| 作用 | 给插件加 `gatewayMethods: ["web.login.start","web.login.wait"]`（登录流程） | 在 `before_agent_reply` 钩子触发前，把入站媒体绝对路径以 `[media attached: <path> (<type>)]` 注入钩子的 `cleanedBody` 副本 | 给 gateway 加 `logoutAccount`，解绑时删 weixin 账号文件 + 索引除名，闭合「跨层裂脑」（详见 [解绑登出补丁](openclaw_weixin_gateway_logout_patch.md)） |
| 是否含 dist hunk | **✅ 含**（`dist/src/channel.js`，编译路径稳定、无哈希） | **❌ 仅 src**（核心 dist 是带内容哈希的 bundle，无法写稳定 patch） | **✅ 含**（`dist/src/channel.js`，路径稳定） |
| 生产生效方式 | 可直接 `git apply` 到运行 dist，无需重新构建 | 由 `scripts/deploy_image_understanding.sh` **手术式改当前哈希 dist 文件** | `patch -p1` 到运行 dist + `node --check` + 重启 gateway（详见专门文档 §5） |

> ⚠️ **解绑登出补丁的特殊关注点（v2.4.4 源码未发布）**：官方 GitHub 仓库 tag 止于 v2.4.3，腾讯只发布了 **2.4.4 的 npm 产物（dist）**，源码未推。实测 v2.4.4 dist（53 模块）⊃ v2.4.3 源码（33 模块），多 20 个模块。**因此不能从 workspace v2.4.3 build 覆盖 dist（会丢 20 个功能）**；在腾讯发 2.4.4+ 源码前，手术热补丁是唯一正确路径，且插件每次升级会覆盖丢失。详见 [openclaw_weixin_gateway_logout_patch.md](openclaw_weixin_gateway_logout_patch.md)。

---

## 2. 新 patch 细节：`openclaw-before-agent-reply-media.patch`

**改了什么**：在核心 `get-reply.ts` 的 `before_agent_reply` 钩子调用前，若 `hasInboundMedia(ctx)`，取 `ctx.MediaPath/MediaPaths` 首个绝对路径 + `ctx.MediaType/MediaTypes` 类型，拼成 `[media attached: <abs_path> (<type>)]` 前置到传给钩子的 `cleanedBody` **副本**（真正的 prompt 不改）。这一处是"让 bridge 看到图片本地路径"的唯一关键点 —— 因为媒体说明（media-note）原本是在钩子**之后**才注入 prompt 的。

**配套的 bridge 侧**：`openclaw-bridge/index.js` 用 `parseInboundMediaMarker()` 解析该标记 → 转发 `message_type=image` + `media{path,format}`（不复用 `extractMediaMarkers`，后者为日志会截断长路径）。

### 如何使用（两种场景）

**A. 正规路线 —— 从源码重新构建 OpenClaw 核心：**
```bash
cd /home/jack/workspace/openclaw
git apply /home/jack/workspace/ai4all_bridge/patches/openclaw-before-agent-reply-media.patch
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

## 3. ⚠️ 升级 OpenClaw 后必查（重点）

OpenClaw 升级/重装会覆盖 dist，图片理解会**静默退回成空文本（不报错）**。届时需重新部署。当前已知脆弱点：

1. **部署脚本写死了哈希文件名** `get-reply-9dLyvuw9.js`。升级后哈希会变，脚本会找不到文件。
   - 临时办法：`grep -rl 'runBeforeAgentReply({ cleanedBody }' <dist目录>` 找到新文件名，改脚本 `DIST` 变量。
   - **待办**：把脚本改成自动发现该文件（去掉写死文件名）。
2. **钩子调用签名若上游改了** `runBeforeAgentReply({ cleanedBody }, {…})`，脚本的字符串匹配会失效 → 需更新匹配串与源码 patch。
3. **media-note 注入时机若上游调整**（目前在钩子之后），需重新确认本方案前提仍成立。
4. **`ctx.MediaPath/MediaPaths/MediaTypes` 字段名若上游变更**，源码 patch 需同步。
5. 旧 weixin patch 同理：升级后确认 `gatewayMethods` 是否仍生效（`grep -r WEIXIN_GATEWAY_METHODS`）。
6. **解绑登出 patch 同理且更脆弱**：插件升级覆盖 `node_modules` 后 `logoutAccount` 丢失，解绑会退回留孤儿 bot。升级后必查：
   `grep -c logoutAccount ~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js`，返回 0 则重打 `patches/openclaw-weixin-logout-account-runtime.patch`。详见 [专门文档](openclaw_weixin_gateway_logout_patch.md)。

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

- [ ] 部署脚本自动发现哈希 dist 文件名（去掉写死的 `get-reply-9dLyvuw9.js`）。
- [ ] `patches/` 增加 README，统一说明两个 patch 的用法与适用场景。
- [ ] 评估长期方案：是否统一从源码树构建并部署 OpenClaw（消除"源码 6.2 / 运行 5.28"漂移与哈希 dist 手术补丁的脆弱性）。
- [ ] 确认旧 weixin patch 当前在运行环境的生效状态与留档完整性。
