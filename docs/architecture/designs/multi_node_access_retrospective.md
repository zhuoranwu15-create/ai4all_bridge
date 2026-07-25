# 接入端多机重构 · 复盘与后续工作指南（面向后续接手者）

> 状态：**复盘文档（活文档，随后续工作更新）**　最后更新：2026-06-14
> 定位：本文是多机接入重构「一期 MVP 上线后」的**单一入口复盘**。后续要继续这条线（运维耐久化、二期架构优化、新机扩容）的人，**先读本文**，再按需深入下面的关联文档。
> 关联文档：
> - 设计与落地计划 + 踩坑速查：[`multi_node_access_refactor.md`](multi_node_access_refactor.md)（含「附录 C 落地踩坑记录」）
> - OpenClaw 4 个补丁与升级必查：[`openclaw_patches_maintenance.md`](openclaw_patches_maintenance.md)
> - aliyun2 登录追查全过程（最深的一次踩坑追踪）：[`../../archive/investigations/multi_node_weixin_login_20260614.md`](../../archive/investigations/multi_node_weixin_login_20260614.md)
> - 机器侧操作：[`../../ops/multi_node_access_aliyun1.md`](../../ops/multi_node_access_aliyun1.md)（aliyun1 升级/回滚）、[`../../ops/multi_node_access_aliyun2.md`](../../ops/multi_node_access_aliyun2.md)（aliyun2 node 接入）
> - 上游容量背景：[`single-host-multi-openclaw-scale.md`](single-host-multi-openclaw-scale.md)

---

## 1. 一句话现状

**aliyun1（central+node）+ aliyun2（node）双机已上线，6 条端到端链路全部真机验证通过，多机主动消息生产可用。** 一期 MVP 目标达成。剩余的是运维耐久化收尾与二期架构优化，均非阻塞。

---

## 2. 现状总览（截至 2026-06-14）

### 2.1 部署形态

| 机器 | 角色 | 出口 IP | 跑什么 | 碰 SQLite |
|---|---|---|---|---|
| **aliyun1** | `central,node` | 公网 `59.110.40.50` / 内网 `172.24.16.141` | FastAPI 大脑 + SQLite（唯一写者）+ 三调度器 + 本机 openclaw 会话 + node-agent | 是（唯一） |
| **aliyun2** | `node` | 公网 `39.96.70.112` / 内网 `172.24.18.88` | `ai4all-weixin-node.service`（出站 pull + 登录 exec + 心跳）+ openclaw 会话 | **否** |

- 节点→中心走稳定指纹地址 `http://aliyun1`（MVP = `/etc/hosts` 主机名别名 → aliyun1 内网 IP + nginx :80）。
- 中心→节点走 `access_nodes.base_url` 直连（aliyun2 = `http://172.24.18.88:8190`）。
- 多机的首要价值是**出口 IP 多样化 + 故障域切分**（降微信 per-IP 风控），扩容只是顺带。

### 2.2 端到端验证矩阵（全绿）

| 链路 | 证据 |
|---|---|
| 入站转发（node→中心 `/openclaw/turn`） | aliyun2 收消息 → bridge `forwarding turn` → `http://aliyun1` |
| 被动回复（HTTP 响应原路回 + 本机发） | 16:43 `outbound: text sent OK`，微信收到 |
| 登录 push（中心→节点 QR，附录 A.4） | `/node/exec/login/start\|wait` 被 aliyun1 调用 200，扫码成功 |
| 中心 web 注册 + 绑定 | 新号 `568d…-im-bot` → `aid_956326343`，binding completed |
| 心跳（节点→`/node/heartbeat`） | `access_nodes` aliyun2 `status:online` 持续心跳 |
| **出站 pull**（主动消息 claim→send→result） | 行 26242 中心 enqueue→aliyun2 pull 同秒 claim→`⇄ res ✓ send 504ms`→中心标 sent |
| 图片理解（多机内联 base64） | aid_956326343 真机发图，得正确回复 |

### 2.3 代码落点（已在 main）

- **一期四阶段全落地**（角色配置 + 加性 schema + node-facing API + node-agent + 出站咽喉拆分）。standalone 全量回归绿（556→559 passed，少量环境相关失败非回归）。
- **关键修复 `c4f97da`**：`db.should_inline_dispatch_for_account(account_id, settings)` —— 出站 dispatch 改 **per-account 节点感知**（远程账号一律 enqueue 走 pull），已部署 aliyun1 并生产实测路由正确。
- **耐久化 `06a4249`**：第 4 个 OpenClaw core 补丁抽成 `scripts/patch_openclaw_accountid.sh`（host-agnostic、幂等、自动发现），并折进 `scripts/deploy_image_understanding.sh` 步骤 `[1b/4]`。
- **aliyun2 四个 OpenClaw 补丁全打**：QR 登录 / 解绑登出 / 图片理解 / **accountId hook ctx**。

---

## 3. 升级过程中暴露的问题（复盘核心）

> 这些是「设计阶段没料到、落地时才炸出来」的真问题。每条给**根因 + 当时怎么解的 + 留下什么教训**。

### 3.1 OpenClaw 是上游第三方，关键能力只能靠「手术补丁」——脆弱性是结构性的

我们无法把改动提交进 OpenClaw（`openclaw/openclaw.git`），4 处必需能力全靠 patch：

| 补丁 | 组件 | 没有它的症状 |
|---|---|---|
| QR 登录 gateway methods | weixin 插件 | 中心 push 登录无 `web.login.start/wait` provider |
| 图片理解 media 路径透传 | core `get-reply` | 图片**静默退回空文本**（不报错） |
| 解绑登出 `logoutAccount` | weixin 插件 | 解绑留孤儿 bot |
| **accountId hook ctx**（6.5 回归） | core `get-reply` | 多机入站**全部 `no_binding` 静默不回复**（无报错） |

**结构性风险**：core 补丁打在**带内容哈希的 bundle**（`get-reply-9dLyvuw9.js` → `BpFiu3Nn.js`，每次升级哈希变）上，无法写稳定 `.patch`，只能字符串锚点手术。插件升级覆盖 `node_modules` 会**静默丢补丁**。
**已缓解**：部署/回滚脚本改成**路径无关 + 按内容 grep 自动发现 bundle**；每个补丁都有「升级必查」grep 命令（见 patches 文档 §3）。**根治留二期**（见 §5）。

### 3.2 两台机 OpenClaw 安装方式不同 → 路径/node 版本漂移

| | aliyun1 | aliyun2 |
|---|---|---|
| 安装 | 官方安装器（自带 pinned node v22.22.0） | `npm i -g`（系统 node v24.16.0） |
| 版本 | v2026.5.28 | v2026.6.5 |
| core dist 根 | `~/.openclaw/tools/node-v*/...` | `~/.npm-global/lib/...` |

aliyun2 当初走 npm-g **很可能是官方安装器要下载 node、国内网络卡**。后果：每台机路径不一样、补丁脚本要兼容多布局、还撞上 **6.5 比 5.28 多出的回归**（§3.4、§3.5）。
**已定标准**：新机优先官方安装器；装不通则 npm-g + **显式锁 node v22.x**，并把路径登记进 runbook。aliyun2 保持现状不重装（健康运行，重装要重登重打补丁）。

### 3.3 weixin 插件「配置了 channel 实例才会被网关加载」（追查耗时最久的一个坑）

aliyun2 中心 push 扫码报 502。剥洋葱式追查（详见 investigation 文档）：
1. **表层**：6.5 设备 **scope/pairing 闸**挡了 loopback CLI 的 `web.login.start`（需 `operator.admin`）。→ `openclaw devices approve <id>`（**不带 `--url`**，走本地信任根 fallback）。
2. **前进**：报错变 `web login provider is not available` —— 一个**一直存在、被 pairing 闸挡在前面从没暴露**的问题。
3. **根因**：weixin 插件**网关启动时连「发现」都没发现**（装在 `~/.openclaw/npm/projects/`，网关只扫到 `~/.openclaw/extensions/` 的 bridge）。
4. **真解**：weixin 插件**只有配置了 channel 实例后才会被网关发现/加载**（manifest 无 `activation` 字段）。`openclaw channels login` 顺带写了 channel 配置 → 强制重启 → 这次才 `discovered openclaw-weixin` + `starting weixin provider`。

**教训**：runbook 当时「provider 可用」「weixin loaded」都是**基于 `plugins list` 显示 enabled 的乐观推断**，≠ 网关运行时真正加载。**CLI 发现路径 ≠ 网关启动发现路径**，验证要看网关日志的 `http server listening (N plugins: …)` 和 `starting channels`，不能只看 CLI。

### 3.4 6.5 的会话钩子安全闸（bridge 装了但钩子静默失效）

v2026.6.5+ 默认拦截非内置插件的会话钩子：`before_agent_reply blocked … must set …hooks.allowConversationAccess=true`。装/升级 bridge 后必须 `openclaw config set plugins.entries.ai4all-openclaw-bridge.hooks.allowConversationAccess true` 再重启，否则 bridge 加载了但**所有钩子静默失效**。aliyun1 旧版 5.28 无此闸。

### 3.5 6.5 core 漏 bot AccountId 出 hook ctx（最隐蔽的回归，真正的「不回复」根因）

scope 闸 + provider 加载都解决、登录 push 也跑通后，**仍然连上但不回复**。深挖发现：
- 6.5 core 组装 `before_agent_reply` hook ctx 时**漏掉了 bot `AccountId`**（同作用域 `sessionCtx.AccountId` 明明就是 bot 账号，core 自己当 `agentAccountId` 用，就是没放进 hook ctx）。
- → bridge `extractAccountId` 三层取值全落空 → 兜底返回 provider 字面量 `"openclaw-weixin"` → 中心 `resolve_account_id_for_inbound_channel_identity` 匹配不到 binding → `no_binding` → bridge `no_reply` 分支**静默吞掉**（无回复、日志空白）。
- 决定性证据：直连中心 `channel_account_id=568d…-im-bot`→`status:ok` 正常回复；`=openclaw-weixin`→`no_binding`。差异只在这一个字段。

**修复**：core dist hook ctx 字面量加 `accountId: sessionCtx.AccountId ?? ctx.AccountId,`（第 4 个补丁）。
**教训**：① 这是**多机才暴露的版本回归**（5.28 不漏，单机一直好用）——**aliyun1 从 5.28 升 6.5 时必打此补丁，否则多机入站全静默**；② 症状极隐蔽（无报错、日志空白），排查链很长（scope→provider→加载→hook ctx 层层前移），investigation 文档完整记录了这条「假设被推翻两次」的追踪路径，值得保留。

### 3.6 出站 inline 分支非 node-aware（一度让所有远程主动消息送不达）

出站 pull 首次验证时发现：必须**绕过** `dispatch_proactive_text`、直接 `enqueue_proactive_text` 才跑通。根因：`settings.is_inline_dispatch` 是**全局**开关，dispatch 只判它、不看账号归属节点。aliyun1=`central,node` + `LOCAL_NODE_INLINE_DISPATCH=true` 时，发给**远程 aliyun2 账号**的主动消息被 aliyun1 本机 inline 误发（无会话→失败 + 按 id 抢走 aliyun2 队列行）→ **当前配置下所有发往 aliyun2 的提醒/承诺/心跳/reactivation 都送不达**。
**修复**：`should_inline_dispatch_for_account(account_id, settings)` —— 仅当账号归属本机 node 才 inline，远程一律 enqueue。三处调用点改用它，+7 单测，已部署 aliyun1 生产实测正确。
**教训**：「全局开关」在多机下几乎都暗含「应该 per-account 判定」的 bug；任何分流条件都要过一遍「远程账号会怎样」。

### 3.7 nginx body-size 与多机图片理解

多机下图片在 node 磁盘，中心读不到本地路径 → 选「bridge 传字节 / 内联 base64」。但 node→中心经 aliyun1 nginx，默认 `client_max_body_size 1m` 会把 base64 大图 **413** 截断。三方大小要协调：bridge cap(5MB) ≤ 中心 `image_max_bytes`(10MB) ≤ nginx(12MB)。已在 aliyun1 nginx 调到 12m。

---

## 4. 当前技术债与风险清单

| # | 项 | 严重度 | 现状 | 处置 |
|---|---|---|---|---|
| D1 | OpenClaw core 补丁打在哈希 bundle 上、升级静默丢失 | 高 | 已缓解（自动发现 + 升级必查 grep） | 二期根治（§5.2） |
| D2 | 两机安装方式/版本不统一 | 中 | 已定标准、aliyun2 维持现状 | 新机按标准；评估源码构建 |
| D3 | 中心 DB 单点（无 HA） | 中 | 本期接受 | 二期 Litestream 热备 + 一键切换 |
| D4 | aliyun2 空壳 `default` channel | 低 | **延后**：weixin 插件不支持 `--delete`，手改网关 state 在生产节点风险高（无害，已留备份） | 维持，或下次维护窗口手动清 |
| D5 | 中心↔节点仅 bridge secret（无 mTLS） | 低 | 阿里云内网 + 安全组，MVP 接受 | 按需评估 |
| D6 | 存量账号迁 aliyun2 只能重扫码（会话不可热迁） | 低 | 设计接受 | 二期调度器前手动按需迁 |
| D7 | `test_content_invitations` 等用例依赖真实 `LLM_API_KEY`（既有测试隔离遗留） | 低 | 跑全量需本机 `.env` | 与本期无关，记录在案 |

---

## 5. 面向后续的工作建议

### 5.1 短期 / 运维耐久化（低风险，建议尽快）

1. **`patches/` 补 README**：统一说明 4 个补丁的用途、适用机型、升级必查命令（patches 文档 TODO 已列）。
2. **aliyun1 升级到 6.x 的预案**：升级前务必把第 4 个 accountId 补丁纳入流程（`scripts/patch_openclaw_accountid.sh`），否则升级后多机入站全静默；同时确认 scope 闸 + `allowConversationAccess` + weixin channel 加载三件套。
3. **`default` 空壳 channel 清理**（D4）：仅在有维护窗口、且 aliyun2 上其它真实 bot 会话可承受网关重启时手动处理；当前无害可不动。
4. **统一 OpenClaw 安装标准落地到新机 bring-up runbook**：新机优先官方安装器，装不通则 npm-g + 锁 node v22.x + 登记路径。

### 5.2 二期 / 架构优化方向

1. **脱离哈希手术补丁（根治 D1）**：评估「统一从源码树构建并部署 OpenClaw」，或推动腾讯发布 2.4.4+ weixin 源码后改回稳定 `.patch`。这是补丁脆弱性的根本解。
2. **自动负载调度器**：心跳/容量 → 最闲分配 + 再均衡提示（承接上游 §4-A）。当前 `pick_node` 是 MVP（配置指定 / session_count 最小）。
3. **一键中心切换脚本** `scripts/promote_central.py`：把 §8.3 计划内切换流程封装（复用 `backup_data.py` 的 SQLite Online Backup + rsync + 翻转指纹地址）。设计钩子已埋好（指纹地址、角色配置驱动）。
4. **Litestream 热备**：对 aliyun1 SQLite 持续复制到 aliyun2（warm standby），收窄切换窗口与数据丢失边界。新增依赖，§8.2 钩子已铺路。
5. **指纹地址从 hosts 升级到 SLB/私网 DNS/VIP**：当前 MVP 用 `/etc/hosts` 别名，零设施依赖；规模上来后平滑替换为可重定向的基础设施地址。
6. **一机多 openclaw 实例**（上游 §4-A）：当前一节点一 openclaw，必要时预留子结构。

---

## 6. 后续接手导航（先读这些）

| 你要做什么 | 先读 |
|---|---|
| 理解整体设计与决策 | 本文 §2 + `multi_node_access_refactor.md` §0-§9 |
| 看落地踩了哪些坑 | 本文 §3 + `multi_node_access_refactor.md` 附录 C |
| 上新 node 机 | `../../ops/multi_node_access_aliyun2.md` + 本文 §5.1.4 |
| 升级 OpenClaw（尤其 aliyun1→6.x） | `openclaw_patches_maintenance.md` §3 升级必查 + 本文 §5.1.2 |
| 排查「连上但不回复」 | `../../archive/investigations/multi_node_weixin_login_20260614.md`（整篇就是这条路径） |
| 做中心切换 / HA | `multi_node_access_refactor.md` §8 + 本文 §5.2.3/§5.2.4 |
| 改出站/主动消息分流 | 本文 §3.6 + `db.should_inline_dispatch_for_account` |

---

> **维护约定**：本文是活文档。每完成一项 §5 工作、或暴露新的坑，回填对应小节并更新顶部「最后更新」。二期工作启动时，§5.2 的条目应转为带状态标记的进度跟踪。
