# openclaw-weixin Gateway 解绑登出补丁（logoutAccount）

更新时间：2026-06-09
状态：**已部署到线上运行 dist 并验证通过**；真源码回填待上游（见「§7 关键风险」）。
关联：[OpenClaw 补丁与部署机制](openclaw_patches_maintenance.md)、[openclaw-weixin QR 登录补丁](openclaw_weixin_gateway_qr_patch.md)、[解绑流程设计](unbind_flow.md)

---

## 1. 背景：解绑的「跨层裂脑」

一次微信账号的「绑定」会在**三层**各留状态，「解绑」必须三层一致地撤销：

| 层 | 解绑应做 | 状态载体 |
|---|---|---|
| AI4ALL DB | `unbind_and_wipe_account` 删 `channel_bindings`/子表 + `accounts.status=deactivated` | `data/ai4all.sqlite3` |
| AI4ALL 文件 | `rmtree(data/user_profiles/{account})` | 人设/记忆 .md |
| **openclaw-weixin 文件** | 删 `accounts/{bot}.json` + `.sync.json` + `.context-tokens.json`，并从 `accounts.json` 索引移除 | `~/.openclaw/openclaw-weixin/` |

AI4ALL 侧早已接好线：`web_me_unbind`（`app/main.py`）在解绑前调 `_cleanup_openclaw_weixin_accounts` → `logout_weixin_account` → OpenClaw gateway 的 `logoutAccount`。**但官方 `@tencent-weixin/openclaw-weixin@2.4.4` 的 gateway 没有实现 `logoutAccount`**，于是 core 返回 `"does not support logout"`，被 AI4ALL catch 成 `status="unsupported"`。结果：第三层（openclaw-weixin 文件）从未清理 —— 账号文件 + 索引残留，gateway 仍在替这个「已解绑」的 bot 向 iLink 长轮询拉消息 = **孤儿 bot**（消息进 AI4ALL 后被入站收口丢弃）。

本补丁补上 `logoutAccount`，让「登出」在文件层真正成立，闭合裂脑断点。

---

## 2. Root Cause

- openclaw-weixin 的「登录」本质 = 磁盘上几个文件存在；「登出」本质 = 删文件 + 移出索引 + 戳配置触发 gateway 重载。
- v2.4.4 已实现 `loginWithQrStart`/`loginWithQrWait`/`stopAccount`，但 **gateway 对象里没有 `logoutAccount` 方法**，OpenClaw core 的 `channels logout` 路由因而失败。

---

## 3. 补丁策略

只往 gateway 对象**新增**一个 `logoutAccount` 方法 + 补 3 个 import，**不改**任何既有登录/收发/账号保存逻辑。所需 3 个 helper（`clearWeixinAccount`/`unregisterWeixinAccountId`/`deriveRawAccountId`）在线上 `dist/src/auth/accounts.js` 已导出，`normalizeAccountId`/`clearContextTokensForAccount` 已在 `channel.js` 作用域内。

**目标文件（线上运行 dist）：**
```text
~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js
```

**留档 patch（本仓库，可重放）：**
```text
patches/openclaw-weixin-logout-account-runtime.patch
```

**权威源码实现（参照，非部署用）：** workspace 仓库 `openclaw-weixin` commit `8ab7f5c`
（`src/channel.ts` 的 `logoutAccount` + `src/channel-logout.test.ts` 测试）。

---

## 4. 具体改动（2 处）

① `dist/src/channel.js` 第 4 行 import 增加 `unregisterWeixinAccountId, clearWeixinAccount, deriveRawAccountId`。

② 在 `stopAccount` 与 `loginWithQrStart` 之间插入 `logoutAccount`：

```js
logoutAccount: async (ctx) => {
    // core 调本方法前已执行 stopChannel（停 webhook），这里只清本地凭证/状态，不影响其他账号。
    const accountId = ctx.accountId;
    if (!accountId) return { cleared: false, loggedOut: false };
    const normalizedId = normalizeAccountId(accountId);
    const candidates = new Set([accountId, normalizedId]);   // 兼容 raw(@im.bot) / normalized(-im-bot) / derived 三种写法
    const rawId = deriveRawAccountId(normalizedId);
    if (rawId) candidates.add(rawId);
    let cleared = false;
    for (const id of candidates) {
        clearWeixinAccount(id);            // 删 {id}.json / .sync.json / .context-tokens.json + framework-allow
        unregisterWeixinAccountId(id);     // 从 accounts.json 索引移除
        clearContextTokensForAccount(id);  // 清进程内存里的回包 token
        cleared = true;
    }
    await triggerWeixinChannelReload();    // 戳 openclaw.json channelConfigUpdatedAt → gateway 重载，停止为该账号长轮询
    return { cleared, loggedOut: cleared };
}
```
（完整实现见 `patches/openclaw-weixin-logout-account-runtime.patch`，含逐 id 的 try/catch 与告警日志。）

**`candidates` 三写法是关键防裂脑点**：`clearWeixinAccount` 按 `${id}.json` 拼文件名删除，调用方可能传 `ff855701b982@im.bot`（raw），而磁盘文件名是 `ff855701b982-im-bot`（normalized）。只用一种写法会删空、留下孤儿。

---

## 5. 部署流程（稳健版，含回滚兜底）

> ⚠️ 这是热补丁运行中的 dist，改坏会让全部在册微信账号掉线。务必按可逆区→扰动区顺序，扰动前完成整库备份与语法预检。

```bash
PLUGIN=~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin
F=$PLUGIN/dist/src/channel.js
TS=$(date +%Y%m%d_%H%M%S); BK=~/.openclaw/_logout_patch_backup_$TS; mkdir -p "$BK"

# ① 整库 + 文件备份（账号库最不可再生）
tar czf "$BK/openclaw-weixin-store.tgz" -C ~/.openclaw openclaw-weixin
cp -a ~/.openclaw/openclaw.json "$BK/"; cp -a "$F" "$BK/channel.js.orig"
ls -la "$PLUGIN/../"*/  >/dev/null 2>&1; ls -la ~/.openclaw/openclaw-weixin/accounts/ > "$BK/accounts.before.txt"

# ② 应用补丁
( cd "$PLUGIN" && patch -p1 < /opt/ai4all-weixin-bot/patches/openclaw-weixin-logout-account-runtime.patch )

# ③ 语法预检（gate；v2.4.4 是 ESM type:module）—— 不通过就还原、绝不重启
node --check "$F" || { cp -a "$BK/channel.js.orig" "$F"; echo "SYNTAX FAIL, rolled back"; exit 1; }

# ④ 重启 gateway，确认起得来
systemctl --user restart openclaw-gateway.service
systemctl --user is-active openclaw-gateway.service

# ⑤ 验证：对一个【已确认的孤儿账号】登出（勿对在用账号测试，见 §6）
openclaw channels logout --channel openclaw-weixin --account <ORPHAN_BOT_ID>
```

**回滚：**
```bash
# 代码层：
cp -a "$BK/channel.js.orig" "$F" && systemctl --user restart openclaw-gateway.service
# 账号文件误删（理论不会，目标已锁孤儿）：
systemctl --user stop openclaw-gateway.service
tar xzf "$BK/openclaw-weixin-store.tgz" -C ~/.openclaw
systemctl --user start openclaw-gateway.service
```

---

## 6. 验证记录（2026-06-09，线上 aliyun1）

部署版本 v2.4.4。验证目标选**唯一确认的孤儿** `ff855701b982-im-bot`（AI4ALL DB 已无活绑定），既验证又顺手清理存量。结果：

- `node --check` 通过；gateway 重启 `active`，无 channel.js 加载报错。
- `openclaw channels logout` **不再报 `does not support logout`**；3 个账号文件删除、`accounts.json` 索引 **46→45**、`openclaw.json` `channelConfigUpdatedAt` 刷新（reload 已触发）。
- **爆炸半径 = 1**：其余 132 个账号文件前后不变，45 个在用账号零影响。
- 三层对账归零：在册 45 bot，孤儿数 = 0。

**找孤儿的对账法**（部署后扫存量用）：在册 bot 集（`~/.openclaw/openclaw-weixin/accounts.json`）减去 `channel_bindings` 派生 key（`channel_account_id`/`session_key`/`raw_identity_json` 全部 `@`、`.` 归一化为 `-`）。
> 教训：曾把 `8e9f228eec77-im-bot` 误判为孤儿，实际它有活绑定。**任何账号删除前必须先对账确认无活绑定**，不能凭记忆。

---

## 7. ⚠️ 关键风险：2.4.4 源码未发布，升级即静默丢失

> **这是本补丁最重要的关注点，也是 OpenClaw 落地的脆弱点之一。**

- 官方 GitHub 仓库（= workspace `openclaw-weixin`）**tag 止于 v2.4.3**，无 v2.4.4。`logoutAccount` 源码只存在于 main 分支 commit `8ab7f5c`（基于 v2.4.3）。**腾讯只发布了 2.4.4 的 npm 产物（dist），没有把 2.4.4 源码推到 GitHub。**
- 实测 **v2.4.4 dist（53 模块）⊃ v2.4.3 源码（33 模块）**，dist 多出 **20 个模块**（streaming 全家、voice-outbound、batch-session、buttons/model-callback-handler、abort-fence、run-context、lane-scheduler、reply-progress…）。
- **因此：绝不能从 workspace v2.4.3 源码 `npm run build` 覆盖线上 dist** —— 会静默丢掉这 20 个 2.4.4 功能，大规模回退（同 [图片理解部署](openclaw_patches_maintenance.md) 的覆盖教训）。
- **结论**：在腾讯发布 2.4.4+（含 logout）源码前，**手术热补丁是唯一正确部署路径**。`@tencent-weixin/openclaw-weixin` 每次升级/重装都会覆盖 `node_modules`，使本补丁丢失 → 解绑又退回留孤儿。

### 升级官方插件后必做
```bash
# 1) 检测补丁是否还在（升级后大概率被覆盖）
grep -c "logoutAccount" ~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js
#   返回 0 → 补丁已丢失，需重打

# 2) 确认官方新版是否已自带 logoutAccount；若没有，重放本补丁（§5 ②③④）
# 3) 若官方新版已自带，删除本待办，勿重复打补丁
```

---

## 8. 风险边界

- 补丁只**新增** gateway `logoutAccount`，不改登录协议、二维码、消息收发、账号保存逻辑。
- 单次 `logoutAccount` 调用爆炸半径 = 单个账号（`candidates` 仅同一 base id 的不同写法，不会波及他人）。
- 不改 AI4ALL 身份映射与绑定表结构。
- 主要风险来自官方插件升级覆盖 `node_modules` 后补丁丢失（见 §7）。
