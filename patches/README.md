# OpenClaw 补丁说明（patches/）

OpenClaw（`openclaw/openclaw.git`）是上游第三方仓库，我们无法把改动提交进去。本项目落地依赖的 5 处 OpenClaw 能力全部以**手术补丁**形式维护：能写成稳定 unified diff 的存为 `*.patch`；打在带内容哈希的 core bundle / 存在版本漂移的插件 dist 上、无法整文件覆盖的，改以幂等脚本按锚点重放。

> **权威文档**（细节、追查过程、升级必查命令以这些为准，本 README 只做索引）：
> - [`../docs/architecture/designs/openclaw_patches_maintenance.md`](../docs/architecture/designs/openclaw_patches_maintenance.md) — 4 补丁对比、升级必查 §3、验证 §4
> - [`../docs/architecture/designs/openclaw_weixin_gateway_logout_patch.md`](../docs/architecture/designs/openclaw_weixin_gateway_logout_patch.md) — 解绑登出补丁专文
> - [`../docs/archive/investigations/multi_node_weixin_login_20260614.md`](../docs/archive/investigations/multi_node_weixin_login_20260614.md) — accountId 回归的逐层追查

---

## 4 个补丁一览

| 补丁 | 目标组件 | 作用 | 形态 / 如何应用 |
|---|---|---|---|
| `openclaw-weixin-gateway-methods-runtime.patch` | weixin **渠道插件** `dist/src/channel.js` | 加 `gatewayMethods: ["web.login.start","web.login.wait"]`，支撑中心 push 扫码登录 | 含 dist hunk（编译路径稳定、无哈希）→ 可直接 `git apply` / `patch -p1` 到运行 dist |
| `openclaw-before-agent-reply-media.patch` | core `src/auto-reply/reply/get-reply.ts` | `before_agent_reply` 钩子前把入站媒体绝对路径以 `[media attached: <path> (<type>)]` 注入钩子的 `cleanedBody` 副本，使 bridge 能识别图片 | **仅 src hunk**（core dist 是哈希 bundle）→ 生产用 `scripts/deploy_image_understanding.sh` 手术改当前哈希 dist |
| `openclaw-weixin-logout-account-runtime.patch` | weixin **渠道插件** `dist/src/channel.js` | 给 gateway 加 `logoutAccount`，解绑时删 weixin 账号文件 + 索引除名，闭合解绑裂脑 | 含 dist hunk → `patch -p1` 到运行 dist + `node --check` + 重启 gateway |
| **（无 .patch 文件）** accountId hook ctx | core `get-reply-*.js`（与图片补丁同文件） | 把 bot `AccountId` 注入 `before_agent_reply` hook ctx，使多机入站能转发正确 `channel_account_id`；**修复 v2026.6.5 core 回归**（否则多机入站全部 `no_binding` 静默不回复） | 哈希 bundle、无稳定 diff → `scripts/patch_openclaw_accountid.sh`（host-agnostic、幂等、自动发现；已折进 deploy 脚本 `[1b/4]`） |
| `openclaw-core.send-meta.src.patch` + `openclaw-weixin.ret-meta.src.patch` | core `gateway/server-methods/send.ts` + weixin 插件 `api.ts`/`types.ts`/`send.ts`/`channel.ts` | 把 iLink `sendMessage` 的 `ret`/`errcode`/`errmsg` 经 `meta` dock 打通到 ai4all，治沉默用户主动消息的"假成功"（详见 [`../docs/troubleshooting/weixin_context_token_send_semantics.md`](../docs/troubleshooting/weixin_context_token_send_semantics.md) 第五节） | core 哈希 bundle + 插件 dist 版本漂移，均无法整文件覆盖 → **`.src.patch` 仅存档源码 diff（未来 fork 后走正式分支的蓝本）；线上生效靠 `scripts/apply_openclaw_ret_meta_patch.py`（host-agnostic、幂等、自动发现、锚点缺失即安全中止）** |

> 第 4、5 个补丁刻意不存 dist `.patch`：core dist 文件名随版本变哈希（`send-Czbq6yKa.js` 等），写死路径的 diff 每次升级都会失效。锚点字符串稳定，脚本按内容 grep / glob 自动发现。第 5 个的源码 diff 另存 `*.src.patch` 备档（**fork 与否待议**；在那之前 `.src.patch` + 打补丁脚本即唯一事实源）。

---

## 适用机型 / 版本

| 补丁 | aliyun1（中心，官方安装器 v2026.5.28） | aliyun2 及后续 node（npm-g v2026.6.5+） |
|---|---|---|
| QR 登录 | 需要 | 需要 |
| 图片理解 media | 需要 | 需要（+ bridge 字节改造 + aliyun1 nginx `client_max_body_size 12m`） |
| 解绑登出 | 需要 | 需要 |
| accountId hook ctx | **当前不需要**（5.28 core 不漏该字段）；**升级到 6.x 时必打** | **必需**（6.5 回归） |
| sendMessage ret→meta（治假成功） | 需要（2026-07-03 已上线并坐实） | 需要（待打；`apply_openclaw_ret_meta_patch.py` 已含 npm-g 路径自动发现） |

core dist 根路径因安装方式不同：
- 官方安装器（aliyun1）：`~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/`
- npm-g（aliyun2）：`~/.npm-global/lib/node_modules/openclaw/dist/`

weixin 插件运行时路径两机同构：`~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin/dist/src/channel.js`

---

## ⚠️ 升级 / 重装 OpenClaw 后必查

OpenClaw 升级或插件重装会覆盖 dist，**静默丢补丁**（图片退回空文本、解绑留孤儿、多机入站静默不回复——均无报错）。每次升级后逐条 grep，返回 0 即重打。路径按上表的安装布局替换。

```bash
# 图片理解 media（core dist 自动发现）
grep -rc 'runBeforeAgentReply({ cleanedBody' <core_dist>/get-reply-*.js

# accountId hook ctx（与图片同 bundle；6.5+ 必需）
grep -c 'sessionCtx.AccountId ?? ctx.AccountId' <core_dist>/get-reply-*.js
# 返回 0 → bash scripts/patch_openclaw_accountid.sh

# QR 登录 gatewayMethods
grep -rc WEIXIN_GATEWAY_METHODS <weixin_plugin_dist>/channel.js

# 解绑登出 logoutAccount
grep -c logoutAccount <weixin_plugin_dist>/channel.js
# 返回 0 → patch -p1 < patches/openclaw-weixin-logout-account-runtime.patch

# sendMessage ret→meta（治假成功；core + 插件多处，用打补丁脚本统一核对/重放）
.venv/bin/python scripts/apply_openclaw_ret_meta_patch.py            # dry-run 报告状态
# 有 ANCHOR-MISSING/未打 → .venv/bin/python scripts/apply_openclaw_ret_meta_patch.py --apply
```

升级 bridge 后另需（6.5+ 安全闸，否则钩子静默失效）：
```bash
openclaw config set plugins.entries.ai4all-openclaw-bridge.hooks.allowConversationAccess true
```

---

## 一键重放（aliyun1）

`scripts/deploy_image_understanding.sh` 已把 media + accountId 两个 core 补丁串成同一入口（步骤 `[1b/4]` 调 `patch_openclaw_accountid.sh --no-restart`，末步统一重启）。两个 weixin 插件补丁（QR / 登出）仍各自 `patch -p1`。

> 长期根治方向（脱离哈希手术补丁）见 `openclaw_patches_maintenance.md` §5 / 复盘文档 §5.2。
