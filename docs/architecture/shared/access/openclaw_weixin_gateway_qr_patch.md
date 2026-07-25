# openclaw-weixin Gateway QR Login Patch

更新时间：2026-05-20

## 背景

AI4ALL Web onboarding 需要后端自动发起 OpenClaw 微信二维码登录：

```text
POST /web/binding-intents
-> openclaw gateway call web.login.start
-> 返回二维码给 Web 前端
-> openclaw gateway call web.login.wait
-> 完成 channel_account_id 绑定
```

本机 OpenClaw CLI/Gateway 设备已经批准 `operator.pairing` 和 `operator.admin` scope。批准后，`web.login.start` 仍然返回：

```text
web login provider is not available
```

因此问题不是设备权限。

## Root Cause

OpenClaw host 通过 channel plugin 的 `gatewayMethods` 元数据寻找 Web QR 登录 provider：

```text
/opt/homebrew/lib/node_modules/openclaw/dist/server-methods-CxcGaVP0.js

WEB_LOGIN_METHODS = ["web.login.start", "web.login.wait"]
resolveWebLoginProvider()
  = listChannelPlugins().find(plugin =>
      plugin.gatewayMethods includes one of WEB_LOGIN_METHODS
    )
```

官方 `@tencent-weixin/openclaw-weixin@2.4.3` 已经实现：

```text
weixinPlugin.gateway.loginWithQrStart
weixinPlugin.gateway.loginWithQrWait
```

但该版本没有声明：

```text
weixinPlugin.gatewayMethods = ["web.login.start", "web.login.wait"]
```

所以 host 无法发现它，最终报 `web login provider is not available`。

## 补丁策略

补丁只增加 provider discovery 元数据，不改微信登录协议、不改二维码生成、不改账号保存逻辑。

源码仓库：

```text
/Users/suchong/workspace/ai4all/openclaw-weixin
```

已修改：

- `src/channel.ts`
- `src/channel.test.ts`
- `CHANGELOG.md`
- `CHANGELOG.zh_CN.md`

运行时安装包：

```text
/Users/suchong/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin
```

已修改：

- `src/channel.ts`
- `dist/src/channel.js`

生产环境如果直接热补丁已安装包，必须补 `dist/src/channel.js`，因为 OpenClaw runtime 加载的是构建后的 JS。

## 具体改动

在 `src/channel.ts` 中增加：

```ts
export const WEIXIN_GATEWAY_METHODS = ["web.login.start", "web.login.wait"];
```

在 `weixinPlugin` 顶层增加：

```ts
gatewayMethods: WEIXIN_GATEWAY_METHODS,
```

在运行时 `dist/src/channel.js` 中增加等价 JS：

```js
export const WEIXIN_GATEWAY_METHODS = ["web.login.start", "web.login.wait"];
```

以及：

```js
gatewayMethods: WEIXIN_GATEWAY_METHODS,
```

## 验证记录

已执行：

```bash
openclaw gateway restart
```

已执行真实 Gateway RPC：

```bash
openclaw gateway call web.login.start \
  --json \
  --timeout 45000 \
  --params '{"accountId":"bind-smoke-codex-005","force":false,"timeoutMs":35000,"verbose":false}'
```

返回：

```json
{
  "qrDataUrl": "https://liteapp.weixin.qq.com/q/...",
  "message": "用手机微信扫描以下二维码，以继续连接：",
  "sessionKey": "bind-smoke-codex-005"
}
```

这说明 provider discovery 已恢复，OpenClaw host 已经能把 `web.login.start` 路由到 `openclaw-weixin`。

已执行：

```bash
node --check /Users/suchong/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js
```

通过。

AI4ALL 主项目已执行：

```bash
.venv/bin/pytest -q
```

结果：

```text
95 passed, 5 warnings
```

插件源码仓库已安装依赖并执行：

```bash
npx vitest run src/channel.test.ts
npm run typecheck
```

结果：

```text
src/channel.test.ts: 1 passed
typecheck: passed
```

注意：插件仓库的 `npm test` 脚本固定启用全局 coverage 阈值。单独执行：

```bash
npm test -- --run src/channel.test.ts
```

会出现“测试用例通过，但全仓覆盖率不满足阈值”的退出码失败。这不是本次新增断言失败；针对新增测试应使用 `npx vitest run src/channel.test.ts`，全量发布前再执行仓库完整 `npm test`。

## 运行时热补丁重放步骤

适用于 OpenClaw 已安装官方 `@tencent-weixin/openclaw-weixin@2.4.3`，但官方包尚未包含此修复的环境。

1. 确认安装包路径：

```bash
openclaw plugins list
```

或检查：

```text
~/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin
```

2. 应用等价改动：

```text
src/channel.ts
dist/src/channel.js
```

可参考：

```text
patches/openclaw-weixin-gateway-methods-runtime.patch
```

3. 重启 Gateway：

```bash
openclaw gateway restart
```

4. 验证：

```bash
openclaw gateway call web.login.start \
  --json \
  --timeout 45000 \
  --params '{"accountId":"bind-smoke-manual-001","force":false,"timeoutMs":35000,"verbose":false}'
```

期望返回 `qrDataUrl`、`message`、`sessionKey`，而不是 `web login provider is not available`。

## 升级官方插件后的处理

升级 `@tencent-weixin/openclaw-weixin` 后必须重新检查：

```bash
rg -n "gatewayMethods|web\\.login\\.start|WEIXIN_GATEWAY_METHODS" \
  ~/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin/src/channel.ts \
  ~/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js
```

如果官方新版已经声明 `gatewayMethods`，删除本地补丁记录中的待办即可，不要重复打补丁。

如果官方新版仍未声明，需要重新应用运行时热补丁，并重新执行上面的 Gateway RPC 验证。

## 风险边界

- 补丁只影响 OpenClaw host 是否能发现 Weixin QR 登录 provider。
- 不改变 `loginWithQrStart` / `loginWithQrWait` 的参数和返回值。
- 不改变已登录微信账号的消息收发路径。
- 不改变 AI4ALL 的身份映射和绑定表结构。
- 风险主要来自官方插件升级覆盖 `node_modules` 后补丁丢失。
