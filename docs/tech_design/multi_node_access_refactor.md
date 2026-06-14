# 接入端多机重构落地设计（中心大脑 + 瘦接入节点，aliyun1/aliyun2 双机 MVP）

> 状态：设计稿（待评审 → 落地）
> 最后更新：2026-06-11
> 适用范围：AI4ALL 微信个人 AI 陪伴项目（bridge + OpenClaw）
> 上游依据：本文是 [`single-host-multi-openclaw-scale.md`](single-host-multi-openclaw-scale.md) §4-B（横向分片 + 路由）+ §4-C（出口 IP 多样化）+ §5（bridge 改造最小集）的**具体落地实现**，聚焦「线上 aliyun1 单机 → aliyun1+aliyun2 双机」的无损切换 MVP。
>
> 配套子文档（机器侧操作）：
> - [`multi_node_access_runbook_B.md`](multi_node_access_runbook_B.md) — **新机 aliyun2**（node）：现在可做的零代码被动链路验证 + 一期后接入。
> - [`multi_node_access_runbook_A.md`](multi_node_access_runbook_A.md) — **线上 aliyun1**（central+node）：无损升级与回滚。

---

## 进展与状态（2026-06-13）

### 🟢 2026-06-14 上线验证：aliyun1(central)+aliyun2(node) 双机已部署，微信收发回环 E2E 打通

双机均已按 runbook 部署上线（非仅设计/单测）：
- **aliyun1** = `central`（线上大脑 + DB + 节点面向 API），从 5.28 起的单机服务平滑升级。
- **aliyun2** = `node`：main 分支，`ai4all-weixin-node.service`(`scripts/run_access_node.py`) + `openclaw-gateway.service` 双服务在跑，`AI4ALL_ROLE=node / NODE_ID=aliyun2 / CENTRAL_URL=http://aliyun1 / NODE_BASE_URL=http://172.24.18.88:8190`。

**端到端验证矩阵（aliyun2 节点，真机微信）**：

| 链路 | 状态 | 证据 |
|---|---|---|
| 入站转发（node→中心 `/openclaw/turn`） | ✅ 已验证 | weixin 收消息 → bridge `forwarding turn` → `http://aliyun1` |
| 被动回复（HTTP 响应原路回 + 本机发） | ✅ 已验证 | 16:43 `outbound: text sent OK`，微信收到回复 |
| 登录 push（中心→节点 QR，附录 A.4） | ✅ 已验证 | 节点 agent `/node/exec/login/start|wait` 被 aliyun1 调用 200；微信扫码成功 |
| 中心 web 注册 + 绑定 | ✅ 已验证 | 新号 `568d…-im-bot` → 账号 `aid_956326343`，binding completed |
| 心跳（节点→`/node/heartbeat`） | ✅ 已验证 | 直打 200，`access_nodes` aliyun2 `status:online`、`created_at 10:55` 起持续心跳 |
| **出站 pull（主动消息 claim→send→result）** | ✅ **已验证** | 2026-06-14 17:08 中心 `enqueue_proactive_text`→行 26242（node=aliyun2）→aliyun2 pull `claimed_at` 同秒→网关 `⇄ res ✓ send 504ms`→中心标 `sent`（gateway_message_id 回填） |
| 图片理解（多机内联 base64） | ✅ 已验证 | 用户在 aid_956326343 微信发图，得到正确回复（2026-06-14） |

> **⚠️ 已知 bug（出站 inline 分支非 node-aware，2026-06-14 发现）**：`settings.is_inline_dispatch`（`config.py:225`）是**全局**开关，`dispatch_proactive_text`（`proactive/messaging.py:310`）只判它、**不看账号归属节点**。aliyun1=`central,node` + `LOCAL_NODE_INLINE_DISPATCH=true` 时，发给**远程（aliyun2）账号**的主动消息会被 aliyun1 走 `send_proactive_text` → `claim_pending_outbound_message(by id)` + 本机 `send_weixin_text`（aliyun1 无该会话）→ **失败 mark_failed，且把行从 aliyun2 pull 队列里按 id 抢走**。即**当前配置下所有发往 aliyun2 账号的提醒/承诺/心跳/reactivation 都不会真正送达**。本次 pull 验证是**绕过** dispatch、直接 `enqueue_proactive_text` 才跑通的。
> **✅ 已修复并上线（2026-06-14，commit `c4f97da`）**：新增 `db.should_inline_dispatch_for_account(account_id, settings)` —— standalone→恒 inline；否则需 `local_node_inline_dispatch` 开 **且** 账号归属本机 node（`resolve_node_for_account(account_id) or default_node_id == node_id`）才 inline，远程账号一律 enqueue 走 pull。三处全局判断（`messaging.py:dispatch_proactive_text`、`turn_service.py` onboarding 欢迎语、`main.py` 绑定发送）改用该 helper。新增 7 个单测（含远程账号 dispatch 必 enqueue 的回归守卫），standalone 全量回归 559 passed（4 个失败为环境相关、与改动无关、原始代码同样失败）。**已部署 aliyun1**（`ai4all-weixin-backend` + `proactive-scheduler` 17:40:54 重启，后者 `Requires=backend` 级联）：生产实测路由正确——4 个 aliyun2 远程账号判 `inline=False` 走 enqueue/pull，本机账号判 `True`。**至此多机主动消息生产可用。**

**踩到的关键坑（全部已解，详见 [investigation](multi_node_weixin_login_investigation_20260614.md)）**：
1. weixin 插件**配置了 channel 实例才会被网关发现/加载**（npm 装在 `~/.openclaw/npm/projects/`，非 `extensions/`）。
2. 6.5 设备 **scope/pairing 闸**挡 loopback CLI 的 `web.login.start`（`devices approve` 不带 `--url` 走本地信任根批准）。
3. **6.5 core 把 bot `AccountId` 漏出了 `before_agent_reply` hook ctx** → bridge 转发 `channel_account_id="openclaw-weixin"` → 中心 `no_binding` 静默不回复。**已打第 4 个 core 补丁**（`openclaw_patches_maintenance.md` §2.6）。⚠️ **aliyun1 从 5.28 升 6.5 时必打此补丁，否则多机入站全静默**。

**结论**：MVP 的「双机端到端跑通」目标**全部链路已实测**（入站/被动回复/登录/绑定/出站 pull/图片理解 6 条全绿）。上面的 inline 分支非 node-aware bug **已修复并部署 aliyun1（commit `c4f97da`，生产路由实测正确）**，多机主动消息**生产可用**。重构目标达成；剩余仅为运维耐久化与清理项（见下）。

### 当前阶段
- 设计与决策：✅ 完成（本文 + 附录 A/B）。
- 一期代码：✅ **四阶段全部落地，标准回归绿**（分支 `feat/multi-node-access-phase1`，开发于 aliyun2 仓库 + 内存 sqlite 全量回归，不动 live aliyun1）：
  - **Phase 1 ✅**（2026-06-13）：角色/节点配置（`AI4ALL_ROLE` 等 + `has_central_role`/`has_node_role`/`is_inline_dispatch`）；加性 schema（`access_nodes` 表、`accounts.assigned_node_id`、`outbound_messages.node_id/claimed_at`、`binding_intents.node_id`、`ix_outbound_messages_node_dispatch`）；`claim_pending_outbound_by_node` + node helper（`set/resolve_node_for_account`、`upsert/get_access_node`、`pick_node`）；`enqueue_proactive_text` 填 node_id。
  - **Phase 2 ✅**（2026-06-13）：中心 node-facing 端点 `POST /node/outbound/claim|result`、`/node/heartbeat`（bridge secret）；`app/node_gateway.py` 派发层（local 直调 / remote push）；登录链路（start/wait/logout）改经 node_gateway；`create_binding_intent` 定节点 + 写归属。
  - **Phase 3 ✅**（2026-06-13）：`scripts/run_access_node.py` + `app/node_agent.py`（节点 agent：出站 pull loop + 登录 exec 端点 + 心跳）；出站咽喉在 central 改 enqueue（`dispatch_proactive_text` 按 `is_inline_dispatch` 分流、`enqueue_onboarding_welcome`、绑定发送）；图片理解多机走 bridge 内联 base64。
  - **Phase 4 ✅**（2026-06-13）：`.env.example`（27–38 行多机变量齐全）+ `architecture_overview.md` §7 补「多机接入形态」（角色/拓扑/入站出站/中心切换）；全量回归收尾。
  - 回归保障：每阶段末 `standalone` 全量 `tests/` 零回归。**Phase 4 末：556 passed（含 `tests/test_multi_node.py`、`tests/test_node_agent.py`）**。注意 `tests/test_content_invitations.py::...content_invitation_reason` 依赖**真实全局 settings 的 `LLM_API_KEY`**（`account_checks.settings` 未被 conftest patch，是既有测试隔离遗留），跑全量回归需本机 `.env` 或 env 设 `LLM_API_KEY`，否则该用例报 `llm_disabled`（非本期回归）。
- 机器：
  - **aliyun1**（公网 `59.110.40.50` / 内网 `172.24.16.141`）= 线上单机 monolith（systemd：`ai4all-weixin-backend`@`127.0.0.1:8180` + `ai4all-weixin-proactive-scheduler` + nginx + 本机 openclaw，**服务真实用户**）。
  - **aliyun2**（公网 `39.96.70.112` / 内网 `172.24.18.88`）= 本机，纯 node。与 aliyun1 内网已通；**OpenClaw + openclaw-weixin 已装**，**Part 1 跨机被动链路已闭合**（§14 #1）。开发用 Python 3.11 venv 经 uv 装好（系统仅 py3.6，见 runbook B）。
  - **节点→中心地址**：aliyun2 经内网 hosts 别名 `http://aliyun1`（nginx :80）访问 aliyun1，即 §8.2 的「稳定指纹地址」MVP 取值（中心 aliyun1↔aliyun2 切换时改 hosts 指向即可）。

### 关键事实（已核实，指导落地）
- **入站** = OpenClaw bridge 插件 POST `AI4ALL_BACKEND_URL/openclaw/turn`；**被动回复内联在 HTTP 响应里** → 跨机被动链路**零重构代码即可验证**（见 runbook B Part 1）。
- **节点无 DB**：一条主动消息的 claim/mark 在中心、send 在节点（附录 B）。
- 必须在节点物理执行的代码**仅 6 处**（§6）。
- **生产部署** = `git pull` + `scripts/restart_runtime.sh`（systemd restart）；全局 `openclaw gateway restart` 是**风控高危**，禁止当日常操作（会让该机所有微信号同时重连）。

### 决策记录（来自评审问答）
| 维度 | 决策 |
|---|---|
| 驱动 | 单机微信会话容量到顶；多机 = 多出口 IP（降风控，承接上游 §3.1/§4-C） |
| 状态层 | 维持 SQLite 集中；**节点永不碰 DB** |
| 出站 | 混合：**登录 push + 主动消息 pull** |
| 形态 | **aliyun1 = central+node**（保会话不重登），**aliyun2 = node** |
| 账号路由 | 中心动态分配；在线会话钉死登录节点（再均衡需重扫码） |
| 中心切换 aliyun1↔aliyun2 | 设计预留（§8）；一键化 + Litestream 热备留二期 |
| 范围 | MVP 跨机跑通；自动负载调度留二期 |

### 下一步
1. ~~aliyun2 验证跨机被动链路~~ → ✅ 已闭合（§14 #1）。
2. ~~一期编码 Phase 1/2/3/4~~ → ✅ 全部落地（见上「当前阶段」），标准回归 556 passed。
3. ~~aliyun1 原地升级演练~~ / ~~部署 aliyun1=central + aliyun2=node~~ → ✅ **已上线**（见上「🟢 2026-06-14 上线验证」）：双机服务在跑，入站/被动回复/登录 push/绑定 E2E 实测通过。aliyun2 四个补丁全打（QR/登出/图片/**accountId**）。
4. ~~补验出站 pull / 图片理解~~ → ✅ **均已 E2E 验证**（出站 pull：行 26242 中心 enqueue→aliyun2 pull→send 504ms→sent；图片：用户真机发图得正确回复）。
5. ~~修 inline 分支非 node-aware bug~~ → ✅ **已修 + 7 测试 + 559 回归绿 + 已部署 aliyun1**（commit `c4f97da`，生产路由实测正确，见上「已知 bug」注）。
6. ~~耐久化第 4 个补丁~~ → ✅ **已完成**（commit `06a4249`）：抽成 `scripts/patch_openclaw_accountid.sh`（host-agnostic、幂等、自动发现），并折进 `scripts/deploy_image_understanding.sh` 步骤 `[1b/4]`；aliyun2 实跑确认幂等。
7. **延后（非必要）**：aliyun2 空壳 `default` channel 清理——weixin 插件不支持 `--delete`，需手改网关 state，生产节点风险高，暂留（无害，详见 [[openclaw-install-patch-state]]）。
8. **二期**：自动负载调度、一键中心切换、Litestream 热备（§15）。

---

## 0. 一句话目标

线上生产（阿里云）目前一台机器 aliyun1 跑全部服务、挂全部用户。本次重构要：**新增一台机器 aliyun2，让微信接入横向扩展到多机**，过程对在线业务**尽量无损**（aliyun1 上已登录的微信会话不重扫码），并把「中心大脑」与「接入能力」解耦，使 **aliyun1/aliyun2 都能接入**、且**中心节点可在 aliyun1↔aliyun2 之间切换**（可选项，设计预留、一键化留二期）。

**为什么必须多机**（承接上游 §3.1）：第一瓶颈不是 CPU，而是「同一出口 IP 挂大量微信号」触发官方 iLink 的 per-IP 异常检测/配额。**aliyun1、aliyun2 是两台独立 ECS = 两个独立公网出口 IP**，多机的首要价值是**出口 IP 多样化 + 故障域切分**，扩容只是顺带。

---

## 1. 不变量（铁律）

1. **只有「中心」进程能读写 `data/ai4all.sqlite3`。** 接入节点永不直连 SQLite，一律走 HTTP 与中心通信。这保住「SQLite 集中 + WAL 单主机」——`db.py`（约 322KB，全 SQLite 方言 + 北京时间 `strftime`）**几乎不动**。
2. **账号隔离不变。** 所有 DB 查询仍按 `account_id` 约束；新增的 `node_id` 只是「路由属性」，不参与隔离判定。
3. **绕开 OpenClaw 远程能力未知**（上游 §7.5）：`openclaw` CLI 始终在节点本机调用。中心从不直接 RPC 远程 openclaw daemon。

---

## 2. 角色模型：central / node 可组合

一套代码，按 `AI4ALL_ROLE` 选能力。**central 与 node 是两种可独立、也可共存于一台机的能力**：

| 角色 | 含义 | 跑什么 | 碰 SQLite? |
|---|---|---|---|
| `standalone`（默认） | = 今天单机形态 | 全部 + openclaw 本地直发 | 是（本机） |
| `central` | 中心大脑 | FastAPI 大脑、SQLite、画像、三调度器、审核台、admin、web、节点面向 API | 是（本机，唯一写者） |
| `node` | 瘦接入节点 | openclaw + 微信会话、节点 agent（入站转发 + 出站轮询 + 登录 exec） | **否** |

**生产落地形态**：
- **机器 aliyun1 = `central` + `node`（同机共存）**：aliyun1 继续持有它现有的全部微信会话（作为 node「aliyun1」），同时跑中心大脑。**这是无损的关键**——aliyun1 的 openclaw 会话不动、不重登。
- **机器 aliyun2 = `node`（纯节点）**：新登录或再均衡过来的账号落在 aliyun2，用 aliyun2 的出口 IP。

> `standalone` 等价于「central + node 同机 + 出站本机即时直发」，保证**本地开发与现有测试零回归**。

```
                          微信用户
                  ┌──────────┴───────────┐ 微信 iLink 长轮询
          ┌───────┴────────┐    ┌────────┴────────┐
          │ 机器 aliyun2(node)│  │ 机器 aliyun1    │
          │ openclaw+会话   │    │ central + node  │
          │ node-agent      │    │ openclaw+会话    │
          └───┬─────────┬───┘    │ node-agent      │
   ① 入站转发 │  ③ 出站  │        │ FastAPI 大脑     │
   (HTTP→中心)│  轮询认领 │        │ SQLite(唯一写者) │
   ② 登录RPC  │  ④ 结果   │        │ 画像/调度/审核   │
 (中心拨aliyun2)▼  回报   ▼        └─────────────────┘
          ┌───────────────────────────┐
          │  中心面向节点的 HTTP API     │ ← 节点经稳定指纹地址访问(见 §8)
          └───────────────────────────┘
```

---

## 3. 关键决策：出站走「混合」（登录 push + 主动消息 pull）

被动回复**无需跨机**：`handle_openclaw_turn` 的回复放在 `OpenClawTurnResponse.reply` 里，随 `/openclaw/turn` 的 HTTP 响应**原路回到发起入站的那台节点**，本机 openclaw 直接发。

| 出站路径 | 方案 | 理由 |
|---|---|---|
| **被动回复** | 不改 | 回复随响应原路返回，零跨机 |
| **登录/登出/扫码** | **push**（中心 → 节点 agent RPC） | 用户在 web 同步等二维码，要低延迟；aliyun1/aliyun2 双向可达，中心直接调目标节点 agent 跑本机 openclaw，二维码同步回传 |
| **主动消息**（dreaming / 提醒 / 承诺 / onboarding 欢迎语） | **pull**（节点轮询中心队列） | 异步、要可靠；复用既有 `outbound_messages` 表 + `machine_claimed_at` 抢占式 claim 范式（已被 moderation worker 验证）；节点掉线消息留队列，回来续传；中心保持单写者 |

> 双向可达让 push 对主动消息也可行，但 pull 复用面更小、retry/幂等/重启恢复全现成，MVP 选 pull。

---

## 4. 入站路径：MVP 几乎零改动

上游 §1.2 已确认：**入站（OpenClaw → bridge `/openclaw/turn`）是干净的 HTTP POST、天然支持多来源**。多机下：

- **aliyun2 的 openclaw** 配置成 POST 到**中心地址**（而非 localhost）；**aliyun1 的 openclaw**（中心同机）继续 POST localhost。
- **node_id 不必进入入站载荷**：中心可由 `accountId → assigned_node_id` 反查归属节点。入站只需「能到达中心」。
  - （可选增强）节点 agent 在转发时附带 `node_id`，中心据此做「账号漂移检测 / 未知账号自动分配」。MVP 不依赖。

即：**入站的全部改造 = 把 aliyun2 的 openclaw 回调 URL 指向中心稳定地址。** 代码侧不动。

---

## 5. 数据模型变更（最小、全部走 `_ensure_column` 加性迁移）

既有迁移范式 `_ensure_column`（`ALTER TABLE ADD COLUMN`，`db.py:150`）保证以下改动**在 aliyun1 的线上库上加性、向后兼容、可热迁**。

1. **新表 `access_nodes`**：`node_id` PK、`base_url`（中心 push 登录用）、`egress_ip`（运维记录）、`last_heartbeat_at`、`session_count`、`max_sessions`、`status`。MVP 仅作登记 + 心跳。
2. **账号 → 节点归属**：`accounts` 加列 `assigned_node_id TEXT`（或新建 `account_node_assignments`，与 contacts 历史命名脱钩，倾向新列最小改动）。
3. **`outbound_messages` 加列 `node_id TEXT`**：enqueue 时由账号归属解析填入；节点按此过滤认领。
4. **新函数 `claim_pending_outbound_by_node(node_id, batch, claim_timeout)`**：照搬 `claim_queued_content_moderation_tasks`（`db.py:2478`）的「SELECT 候选 → 逐行带条件 UPDATE 抢占」结构，加 `WHERE node_id = ?`，并复用 `machine_claimed_at` 式 stale 超时回收，保证多节点不重复领、节点崩溃后可重领。
5. **默认归属兜底 `DEFAULT_NODE_ID`（配置）**：任何 `assigned_node_id` 为空的账号，enqueue/路由时回落到 `DEFAULT_NODE_ID`（迁移期 = `"aliyun1"`），保证**没有消息被悬空**。

---

## 6. 必须在节点物理执行的代码点（全项目仅 6 处，已实测）

> 出站咽喉改造，需谨慎回归（上游 §5 已警示）。

**出站 `send_weixin_text`（3 处）→ 改为 enqueue 到节点队列：**
- `turn_service.py:492` onboarding 欢迎语（**坑**：turn 在中心跑，中心不持有会话，不能直接发 → 按账号归属 enqueue，节点 pull 后发）
- `main.py:669` 绑定流程内的发送 → 同上 enqueue
- `proactive/messaging.py:232` 主动消息发送 → 拆分：中心只 enqueue，节点 claim + send

**登录/登出（3 处）→ 改为 push RPC 到目标节点 agent：**
- `main.py:703` `start_weixin_qr_login`
- `main.py:580` `wait_weixin_qr_login`
- `main.py:462` `logout_weixin_account`

`openclaw_gateway.py` **逻辑不变**，只是改为「仅 node 角色进程调用」。

---

## 7. 组件与接口

### 7.1 中心新增「节点面向 API」（`Bearer <AI4ALL_BRIDGE_SECRET>` 鉴权）
- `POST /openclaw/turn`（已存在）：节点入站转发到此（可选带 `node_id`）。
- `POST /node/outbound/claim` — body `{node_id, batch}`，返回认领到的待发消息。
- `POST /node/outbound/{id}/result` — `{status, gateway_message_id|error}`，中心复用 `mark_outbound_message_sent/failed`；限速 `ret=-2` 沿用 `OpenClawRateLimited` 语义记录退避。
- `POST /node/heartbeat` — `{node_id, session_count, egress_ip}`，upsert `access_nodes`。

### 7.2 节点服务（新增 `scripts/run_access_node.py`）
瘦服务，只做三件事，**不依赖 SQLite**：
- **入站**：本机 openclaw webhook → POST `CENTRAL_URL/openclaw/turn`（带 bridge secret）→ 取 `reply` → 本机 openclaw 发出。
- **出站 pull 循环**：每 `OUTBOUND_PULL_INTERVAL_SECONDS` 轮询 `/node/outbound/claim` → 逐条本机 `send_weixin_text` → `/result` 回报（限速退避、长回复分块 300ms，承接上游 §2.3/§9.2）。
- **登录 exec 端点**（被中心 push）：`POST /node/exec/login/start|wait`、`/logout` → 跑本机 openclaw → 返回 `qrDataUrl` / 结果。

### 7.3 登录/绑定流程（节点化）
1. 用户在 web 发起绑定 → 中心选节点（MVP：配置指定 / 取 `session_count` 最小者）→ 写 `assigned_node_id`。
2. 中心按 `access_nodes.base_url` **push** 到目标节点 `/node/exec/login/start` → 节点本机起 QR → 二维码回中心 → 中心返回 web。
3. `wait` 同理 push。登录成功后，该账号会话**物理钉死在该节点**（= 钉死在该节点出口 IP，满足上游 §4-C 的 IP 稳定绑定）。

---

## 8. 中心节点可切换（aliyun1↔aliyun2，可选项）

### 8.1 「中心状态包」= 既有备份产物
中心的全部状态 = **SQLite + 画像目录 `user_profiles_dir` + `system_dir`（+ `.env`）**。这正是 `scripts/backup_data.py` 已在快照并支持 `rsync` 异地推送的内容（`_backup_sqlite` 用 SQLite Online Backup API 取一致快照，`_archive_dir` 打包画像/system，`_rsync_offsite` 推异地）。**中心切换直接复用这套，不造新轮子。**

### 8.2 可切换的架构钩子（现在就埋）
- **节点 → 中心方向走「稳定指纹地址」**：`CENTRAL_URL` 不要写 aliyun1 的裸 IP，指向一个可重定向的稳定地址——阿里云**内网 SLB / 私网 DNS 名 / keepalived VIP / floating EIP** 之一。切换中心 = 把该地址指向新机，**节点无需改配置**。
- **中心 → 节点方向**用 `access_nodes.base_url` 直连具体节点（不经指纹地址），切换中心不影响。
- **角色由配置驱动**：`PROACTIVE_SCHEDULER_ENABLED` 等调度开关跟随「谁是 central」启停；aliyun1/aliyun2 跑同一份代码，靠 env 决定角色。

### 8.3 计划内切换流程（MVP 可接受，短暂只读/停写窗口）
1. aliyun1 进入维护：停三调度器、停 enqueue（入站可短暂 503 或缓冲），让在途出站队列收敛。
2. 最终增量同步状态包 aliyun1 → aliyun2（rsync；SQLite 用 Online Backup 取一致拷贝）。
3. aliyun2 起 `central`：`init_db`（加性迁移幂等空跑）+ `PRAGMA integrity_check` 校验。
4. **翻转指纹地址指向 aliyun2** → 节点下一次心跳/入站/出站轮询自动命中 aliyun2。
5. **aliyun1 降级为 `node`**（保留其 openclaw 会话，**不重扫码**）；aliyun2 成为 `central`（可叠加 `node`）。

> **关键性质：切换中心不导致任何微信会话重登**——因为 aliyun1 切换后仍是 node，会话留在 aliyun1，只是「大脑 + DB」搬到了 aliyun2。反向 aliyun2→aliyun1 同流程对称。

### 8.4 分级（按需取用）
- **MVP / 计划内切换**：§8.3，复用备份 + rsync + 指纹地址，分钟级窗口。✅ 本期设计预留、可手动执行。
- **近实时热备（二期）**：用 Litestream 对 aliyun1 的 SQLite 做持续复制到 aliyun2（warm standby），缩短切换窗口与数据丢失边界。**新增依赖**，留二期，§8.2 的钩子已为它铺好路。
- **一键切换脚本（二期）**：把 §8.3 封装成 `scripts/promote_central.py`。

---

## 9. 配置变更（`.env.example`）

```
AI4ALL_ROLE=standalone            # standalone(默认,=今天) | central | node ; aliyun1 设 "central,node" 共存
NODE_ID=                          # 含 node 能力时必填，全局唯一(如 aliyun1 / aliyun2)
DEFAULT_NODE_ID=aliyun1           # 账号未分配节点时的兜底归属(迁移期=aliyun1)
CENTRAL_URL=                      # node→中心 的稳定指纹地址(SLB/DNS/VIP)，不要写裸 IP
NODE_BASE_URL=                    # 本节点基址(中心 push 登录用)，写入 access_nodes
NODE_MAX_SESSIONS=                # 本机会话上限(MVP 仅上报)
OUTBOUND_PULL_INTERVAL_SECONDS=2  # 节点出站轮询间隔
LOCAL_NODE_INLINE_DISPATCH=false  # true: central 同机 node 的出站走同进程即时直发(迁移期降延迟用)
```

> `LOCAL_NODE_INLINE_DISPATCH`：aliyun1 作为 central+node 同机时，可让 aliyun1 自己负责的账号出站走同进程即时发送（零轮询延迟），仅 aliyun2 走 pull。默认 false（统一 pull，代码单路径）；迁移初期若在意 aliyun1 的主动消息延迟可临时置 true。

调度器（`PROACTIVE_SCHEDULER_ENABLED` / `DREAMING_SCHEDULER_ENABLED` / `MODERATION_WORKER_ENABLED`）：**仅在 central 机器开启**，node-only 机器一律关闭。

---

## 10. 无损切换上线 Runbook（aliyun1 单机 → aliyun1+aliyun2）

> 原则：每步独立可回滚；先在 aliyun1 上「原地升级且行为不变」，再引入 aliyun2。

1. **加性迁移上 aliyun1**：部署新代码，`init_db` 自动加 `access_nodes` / `accounts.assigned_node_id` / `outbound_messages.node_id`（加性，老库兼容）。一次性 backfill：所有现存账号 `assigned_node_id="aliyun1"`。
2. **aliyun1 切 `central,node` 形态**：`NODE_ID=aliyun1`、`DEFAULT_NODE_ID=aliyun1`、`CENTRAL_URL=<aliyun1 的指纹地址>`（此刻指向 aliyun1）。aliyun1 的 openclaw 会话不动。验证：入站回复、主动消息、登录、登出全部如常（此时等价 standalone，仅多走了一层本机队列/RPC）。
3. **引入 aliyun2 为 node**：aliyun2 部署代码、起 `run_access_node.py`、`NODE_ID=aliyun2`、`CENTRAL_URL=<指纹地址>`、`NODE_BASE_URL=<aliyun2 内网址>`；aliyun2 的 openclaw 指向中心。aliyun2 心跳上报，`access_nodes` 出现 aliyun2。
4. **灰度**：把 1～2 个新账号登录分配到 aliyun2（或挑非活跃账号在 aliyun2 重扫码迁移），验证 aliyun2 的入站/出站/登录端到端 + aliyun2 的出口 IP 生效。
5. **常态**：新登录按容量分配到 aliyun1/aliyun2；存量账号保持在 aliyun1，按需再均衡（重扫码）。

**回滚**：任一步异常 → aliyun1 改回 `AI4ALL_ROLE=standalone`、停 aliyun2，即恢复今天行为（数据加列不影响旧代码路径）。

---

## 11. 影响文件清单（diff 级别）

| 文件 | 改动 |
|---|---|
| `app/config.py` | +role/node_id/default_node_id/central_url/node_base_url/max_sessions/pull_interval/inline_dispatch |
| `app/db.py` | +`access_nodes` 表&迁移；+`accounts.assigned_node_id`、`outbound_messages.node_id` 加列；+`claim_pending_outbound_by_node`；+节点注册/心跳/归属 helper |
| `app/turn_service.py` | onboarding 欢迎语 `send_weixin_text`→enqueue（解析 node_id）；`/openclaw/turn` 兼容可选 node_id |
| `app/proactive/messaging.py` | 拆分 enqueue / 节点消费；enqueue 时填 node_id |
| `app/proactive/scheduler.py` | central 角色下只 enqueue，不内联发送 |
| `app/main.py` | +节点面向 API（claim/result/heartbeat）；登录/登出/绑定发送（462/580/669/703）改 push RPC / enqueue + 目标节点解析 |
| `app/openclaw_gateway.py` | 逻辑不变，仅 node 调用 |
| `scripts/run_access_node.py` | **新增** 节点服务（入站转发 + 出站 pull + 登录 exec 端点） |
| `scripts/promote_central.py` | **（二期）** 一键中心切换 |
| `.env.example` / 本文 / `architecture_overview.md` | 文档化拓扑、角色、切换流程 |

---

## 12. 测试方案

- **聚焦单测**：node_id 解析与 enqueue 填充（含 `DEFAULT_NODE_ID` 兜底）；`claim_pending_outbound_by_node` 抢占（两节点不重复领、stale 超时可重领、空队列）；`/node/outbound/result` 状态机（sent/failed/rate_limited）。
- **契约测试**：节点↔中心 API 鉴权（bridge secret）；入站远程转发取回 reply。
- **集成测试**（mock `openclaw` CLI + 内存 SQLite）：模拟 aliyun1、aliyun2 两节点领取互斥账号集并各自发送；登录 push 到指定节点。
- **回归**：`standalone` 下跑全量 `tests/`（内存 SQLite），确认零回归；重点 `tests/test_turn_service.py`（欢迎语路径变化）、主动消息相关、`test_*moderation*`（审核仍在中心 enqueue/同步红线前执行）。
- **切换演练**（预发）：按 §8.3 在测试环境跑一次 aliyun1→aliyun2→aliyun1，校验 `integrity_check`、会话不重登、节点零改配。

---

## 13. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 出站咽喉改造回归（上游 §5 警示） | 6 个执行点全部列明；`standalone` 全量回归；`LOCAL_NODE_INLINE_DISPATCH` 可临时退回同进程发送 |
| 中心单点（DB/大脑） | 本期接受（未要求 HA）；§8 提供计划内切换，二期 Litestream 热备 |
| 节点→中心网络抖动 | 入站靠 openclaw 重试 + 中心 `get_duplicate_reply` 去重；出站留队列，节点恢复续领 |
| 主动消息多一跳轮询延迟 | 间隔默认 2s；aliyun1 同机可开 `LOCAL_NODE_INLINE_DISPATCH` 零延迟 |
| 账号漂移（会话出现在非归属节点） | MVP 由 accountId→assigned_node 反查为准；可选入站带 node_id 做漂移检测告警 |
| 切换中心丢数据 | 用 SQLite Online Backup 取一致快照 + 停写窗口；二期 Litestream 收窄丢失边界 |

---

## 14. 待你/评审钉死的开放问题

1. **openclaw 入站回调 URL 是否可配成远程**（aliyun2 → 中心）？上游 §1.2 判定「天然支持多来源」，但需在 aliyun2 上实配验证（openclaw 是打包 dist）。→ **✅ 已验证（2026-06-12）**：在 aliyun2 上直连 `POST http://aliyun1/openclaw/turn`（仿桥接 payload + 真实 bridge secret），返回 **HTTP 200** `{"status":"ignored","no_reply":true,"metadata":{"reason":"no_binding",...}}`，66ms。证明跨机远程 POST + Bearer 鉴权 + 中心 ingest 全通；`no_binding` 收口印证线上 `OPENCLAW_INBOUND_REQUIRE_BINDING=true` 生效。最大未知闭合。详见 [runbook B](multi_node_access_runbook_B.md) Part 1。→ **2026-06-14 进一步：真机微信走完整回环（绑定→入站→被动回复发回微信）E2E 验证通过**，期间发现并修复 6.5 core hook ctx 缺 AccountId 的回归（见 [investigation](multi_node_weixin_login_investigation_20260614.md) §10 + patches §2.6）。
2. **节点 agent 与中心之间**：MVP 仅 bridge secret，还是要内网 + mTLS？（阿里云内网 + 安全组通常 secret 足够）。
3. **中心指纹地址选型**：内网 SLB / 私网 DNS / keepalived VIP / floating EIP —— 取决于你们阿里云现有网络设施。→ **MVP 已选 `/etc/hosts` 主机名别名**（aliyun2 上 `aliyun1`→aliyun1 内网 IP，零设施依赖）；中心 aliyun1↔aliyun2 切换时改各节点 hosts 指向即可。二期若上 SLB/DNS 再平滑替换。
4. **再均衡策略**：存量账号迁到 aliyun2 只能重扫码（会话不可热迁），是否接受「按需手动迁移」直到二期调度器？
5. **多 openclaw 实例 per node**（上游 §4-A）：MVP 一节点一 openclaw；是否需要预留一机多实例的子结构？

---

## 15. 分期

- **一期（本设计，MVP）**：角色拆分 + 加性迁移 + 出站队列/登录 push + 节点 agent + aliyun1 原地升级 + 引入 aliyun2 + 无损 Runbook。目标：**aliyun1/aliyun2 双机端到端跑通**。
- **二期**：自动负载调度器（心跳/容量 → 最闲分配 + 再均衡提示）、一键中心切换脚本、Litestream 热备、一机多 openclaw 实例（承接上游 §4-A）。

---

# 附录 A：登录 push 时序（深挖）

## A.1 当前单机流程（基于代码核实）

登录是**异步**的：web 建 intent → 后台起 QR → web 轮询取二维码 → 后台 wait 阻塞至扫码 → 完成绑定 → 延时发欢迎语。

```
浏览器            中心 FastAPI                      本机 openclaw
  │ POST /web/binding-intents                          │
  │───────────────►│ 建 binding_intent                  │
  │                │ _start_openclaw_qr_for_binding     │
  │                │   start_weixin_qr_login ──subprocess──►│ web.login.start
  │                │◄───────── qrDataUrl ──────────────────│
  │                │ update_binding_intent(qr_created)  │
  │                │ _schedule_binding_wait → 后台 task   │
  │◄── 202 ────────│                                    │
  │ GET /web/binding-intents/{id} (轮询取 qr_data_url)  │
  │◄── qrDataUrl ──│  → 浏览器渲染二维码，用户扫码         │
  │                │ [后台] _wait_for_binding_intent     │
  │                │   wait_weixin_qr_login ──subprocess──►│ web.login.wait (阻塞≤wait_timeout)
  │                │◄──── connected + channel_account_id ──│
  │                │ _complete_binding_intent_from_wait  │
  │                │   upsert_channel_binding            │
  │                │ loop.call_later(5s, 欢迎语)          │
  │ GET .../{id} (轮询取 status=completed)               │
  │◄── completed ──│                                    │
```

涉及的 3 个本机 openclaw 调用点：`start_weixin_qr_login`(`main.py:703`)、`wait_weixin_qr_login`(`main.py:580`)、欢迎语 `send_weixin_text`(`main.py:669`)。

## A.2 多机改造：引入 `node_gateway` 派发层

新增 `app/node_gateway.py`，把 `openclaw_gateway` 的 4 个函数包一层「按 node_id 派发」：

```python
# app/node_gateway.py（新增）—— 中心侧调用，决定「本机直调」还是「push 到远程节点」
from app import openclaw_gateway

def _is_local_node(node_id: str) -> bool:
    # central+node 同机：node_id == settings.node_id 且本进程具备 node 能力
    return settings.has_node_role and node_id == settings.node_id

def node_start_qr(*, node_id, account_id, **kw) -> dict:
    if _is_local_node(node_id):
        return openclaw_gateway.start_weixin_qr_login(account_id=account_id, **kw)
    base = _resolve_node_base_url(node_id)          # access_nodes.base_url
    return _http_post(f"{base}/node/exec/login/start",
                      json={"account_id": account_id, **kw},
                      timeout=kw["gateway_timeout_ms"]/1000 + 5)

def node_wait_qr(*, node_id, account_id, wait_timeout_ms, **kw) -> dict:
    if _is_local_node(node_id):
        return openclaw_gateway.wait_weixin_qr_login(account_id=account_id,
                                                     wait_timeout_ms=wait_timeout_ms, **kw)
    base = _resolve_node_base_url(node_id)
    # 关键：wait 是长轮询，HTTP 客户端超时必须 > wait_timeout_ms（与今天 subprocess 超时同理）
    return _http_post(f"{base}/node/exec/login/wait",
                      json={"account_id": account_id, "wait_timeout_ms": wait_timeout_ms, **kw},
                      timeout=wait_timeout_ms/1000 + 10)

def node_logout(*, node_id, account_id, **kw) -> dict: ...   # 同模式
```

`main.py` 的 3 处改为调 `node_gateway.node_start_qr/node_wait_qr` 并传入目标 `node_id`；欢迎语改为 enqueue（见附录 B），不再直接 send。

**节点 agent 侧**（`scripts/run_access_node.py`）暴露对称的 exec 端点，内部仍调本机 `openclaw_gateway`：

```python
@node_app.post("/node/exec/login/start")   # 鉴权: Bearer <bridge_secret>
def exec_login_start(body): return openclaw_gateway.start_weixin_qr_login(**body)

@node_app.post("/node/exec/login/wait")
def exec_login_wait(body):  return openclaw_gateway.wait_weixin_qr_login(**body)

@node_app.post("/node/exec/logout")
def exec_logout(body):      return openclaw_gateway.logout_weixin_account(**body)
```

## A.3 目标节点的决定时机

`node_id` 在 **binding_intent 创建时**就定好，并写入账号归属：

```
POST /web/binding-intents
  → 选节点 node_id = pick_node()        # MVP: 配置指定 / access_nodes 里 session_count 最小且 status=online
  → create_binding_intent(..., node_id=node_id)
  → set_account_assigned_node(account_id, node_id)   # accounts.assigned_node_id
  → _start_openclaw_qr_for_binding(intent)  # 内部用 intent.node_id 派发
```

登录成功后会话即钉死在该节点（= 钉死在该节点出口 IP，满足上游 §4-C）。`assigned_node_id` 同时成为后续出站路由（附录 B）和入站反查的依据。

## A.4 多机登录时序（aliyun2 为远程节点）

```
浏览器     中心(aliyun1: central)               节点 aliyun2(agent)  aliyun2 本机 openclaw
  │ POST /web/binding-intents                        │                   │
  │─────────►│ pick_node()=aliyun2; 写 assigned_node_id=aliyun2 │        │
  │          │ node_start_qr(node_id=aliyun2)─push──►│ exec_login_start  │
  │          │                                       │── start ─────────►│
  │          │◄──────────── qrDataUrl ───────────────│◄── qrDataUrl ─────│
  │          │ update_binding_intent(qr_created)     │                   │
  │◄─ qr ────│ (web 轮询取二维码，用户扫码)             │                   │
  │          │ [后台] node_wait_qr(node_id=aliyun2)─push─►│ exec_login_wait│
  │          │   (HTTP 长连，超时>wait_timeout)        │── wait(阻塞) ─────►│
  │          │◄────── connected+channel_account_id ───│◄── connected ─────│
  │          │ _complete_binding_intent (写库都在中心) │                   │
  │          │ enqueue 欢迎语(node_id=aliyun2) → 出站队列 │                │
  │◄completed│                                        │ (aliyun2 pull 出站→发欢迎语)
```

## A.5 失败 / 边界处理
- **节点不可达**（push 超时/连接拒绝）：`_start_openclaw_qr_for_binding` 已有 `except` → `set_binding_intent_error(status="failed")`，与今天 openclaw 异常同路径；web 轮询到 failed 提示重试。
- **wait 长轮询**：中心→节点 HTTP 客户端超时必须 > `wait_timeout_ms`，否则中心先断开但节点 openclaw 仍在 wait（语义与今天 subprocess 超时一致，沿用 `openclaw_login_wait_timeout_ms`）。
- **幂等**：登录以 `openclaw_login_session_key` 为键，重复 start 由 openclaw 侧 `force=False` 处理，行为不变。
- **standalone / aliyun1 同机**：`_is_local_node` 命中 → 直调本机，零网络跳，行为与今天完全一致。

---

# 附录 B：出站 `claim_pending_outbound_by_node` 与中心/节点拆分（深挖）

## B.1 核心约束：节点无 DB → 认领与标记都在中心

节点不碰 SQLite，所以一条主动消息的生命周期被切成「中心管状态、节点管发送」：

```
中心: enqueue_proactive_text  →  row(status=pending, node_id=aliyun2)   [DB 写]
节点: POST /node/outbound/claim {node_id:aliyun2}                       
中心:   claim_pending_outbound_by_node → UPDATE status=sending 返回行   [DB 写]
节点:   send_weixin_text(本机 openclaw, 限速退避/长回复分块)             [无 DB]
节点: POST /node/outbound/{id}/result {status:sent, gateway_message_id}
中心:   mark_outbound_message_sent + insert_outbound_delivery_message   [DB 写]
```

即今天 `send_proactive_text` 的「claim+send+mark」三步被一刀切在 send 两侧：**claim 和 mark 留中心**（DB），**send 搬节点**（openclaw）。`enqueue_proactive_text` 本就是干净的独立函数（policy + 同步审核红线 + 建行），**完全不动**，只在建行时多填一个 `node_id`。

## B.2 schema 迁移（加性，走 `_ensure_column`）

```python
_ensure_column(conn, "outbound_messages", "node_id", "TEXT")
_ensure_column(conn, "outbound_messages", "claimed_at", "TEXT")   # stale 回收用
conn.execute("""CREATE INDEX IF NOT EXISTS ix_outbound_messages_node_dispatch
                ON outbound_messages(node_id, status, scheduled_at)""")
```

`node_id` 在 enqueue 时解析：`resolve_node_for_account(account_id) or settings.default_node_id`（迁移期 `default_node_id="aliyun1"`，保证存量/未分配账号不悬空）。

## B.3 `claim_pending_outbound_by_node`（照搬 moderation claim 抢占范式 + node 维度）

```python
def claim_pending_outbound_by_node(
    *, node_id: str, batch_size: int, claim_timeout_seconds: int, max_attempts: int = 5,
) -> List[Dict[str, Any]]:
    """认领某节点的待发主动消息：status=pending，或卡在 sending 且认领超时的（节点崩溃回收）。
    每行用「带条件 UPDATE + rowcount==1」原子抢占，保证同 node 多消费者不重复领。"""
    batch = max(1, min(200, int(batch_size)))
    stale = f"-{max(1, int(claim_timeout_seconds))} seconds"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM outbound_messages
            WHERE node_id = ?
              AND attempts < ?
              AND ( status = 'pending'
                 OR ( status = 'sending'
                      AND claimed_at IS NOT NULL
                      AND claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now','+8 hours', ?)) ) )
              AND ( scheduled_at IS NULL
                 OR scheduled_at <= strftime('%Y-%m-%d %H:%M:%S', datetime('now','+8 hours')) )
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (node_id, max_attempts, stale, batch),
        ).fetchall()
        claimed: List[Dict[str, Any]] = []
        for row in rows:
            cur = conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'sending',
                    attempts = attempts + 1,
                    claimed_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now','+8 hours')),
                    error = NULL,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now','+8 hours'))
                WHERE id = ?
                  AND node_id = ?
                  AND ( status = 'pending'
                     OR ( status = 'sending'
                          AND claimed_at IS NOT NULL
                          AND claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now','+8 hours', ?)) ) )
                """,
                (row["id"], node_id, stale),
            )
            if cur.rowcount != 1:          # 已被并发消费者抢走 → 跳过
                continue
            r = conn.execute("SELECT * FROM outbound_messages WHERE id = ?", (row["id"],)).fetchone()
            if r is not None:
                claimed.append(_decode_outbound_message(r))
    return claimed
```

设计要点：
- **WHERE node_id = ?**：节点只领自己的账号消息，aliyun1/aliyun2 天然互斥（不同 node_id 不可能撞行）。
- **rowcount==1 原子抢占**：防同一 node_id 的「重复进程 / 重启叠跑」双领（与 `claim_queued_content_moderation_tasks` 同构）。
- **stale `sending` 回收**：节点 claim 后崩溃，行卡在 `sending`；超 `claim_timeout_seconds` 可被重领，避免永久卡死。
- **`attempts < max_attempts`**：毒消息（持续失败）达上限后不再重领，进 failed 兜底，防无限重试。
- **`scheduled_at` 过滤**：延时消息未到点不领（出站消费者现在统一在节点，必须自己尊重 schedule）。

## B.4 中心/节点代码拆分

**中心**（`proactive/scheduler` 等只 enqueue）：
```python
# send_proactive_text 在 role=central 且非 inline 时，退化为「只 enqueue」
def dispatch_proactive_text(...):
    outbound = enqueue_proactive_text(..., )       # 已含 node_id 解析
    return outbound                                # 不再 claim/send；交给节点 pull
```
保留旧 `send_proactive_text`（enqueue+claim+本机 send）仅用于 `standalone` 与 `LOCAL_NODE_INLINE_DISPATCH=true` 的同机直发，行为零变化。

**中心节点面向端点**：
```python
@app.post("/node/outbound/claim")   # Bearer bridge_secret
def node_outbound_claim(body):
    return {"messages": claim_pending_outbound_by_node(
        node_id=body["node_id"], batch_size=body.get("batch", 20),
        claim_timeout_seconds=settings.outbound_claim_timeout_seconds)}

@app.post("/node/outbound/{mid}/result")
def node_outbound_result(mid, body):
    if body["status"] == "sent":
        sent = mark_outbound_message_sent(outbound_message_id=mid,
                                          gateway_message_id=body.get("gateway_message_id"))
        if sent: insert_outbound_delivery_message(outbound_message=sent)
        return sent
    return mark_outbound_message_failed(outbound_message_id=mid, error=body.get("error",""))
```

**节点 agent 出站循环**（无 DB，只 openclaw + HTTP）：
```python
while True:
    claimed = http_post(f"{CENTRAL}/node/outbound/claim", {"node_id": NODE_ID, "batch": 20})["messages"]
    for m in claimed:
        try:
            res = send_weixin_text(to_user_id=m["to_user_id"], text=m["text"],
                                   account_id=m["channel_account_id"],
                                   idempotency_key=m["idempotency_key"],   # UNIQUE → 网关幂等
                                   session_key=m["session_key"], channel=m["channel"],
                                   gateway_timeout_ms=GATEWAY_TIMEOUT)      # 内含限速退避/分块
            http_post(f"{CENTRAL}/node/outbound/{m['id']}/result",
                      {"status": "sent", "gateway_message_id": (res or {}).get("messageId")})
        except OpenClawRateLimited as e:
            http_post(f"{CENTRAL}/node/outbound/{m['id']}/result",
                      {"status": "failed", "error": f"rate_limited: {e}"})   # stale 回收或下轮重领
        except Exception as e:
            http_post(f"{CENTRAL}/node/outbound/{m['id']}/result", {"status": "failed", "error": str(e)})
    sleep(OUTBOUND_PULL_INTERVAL_SECONDS)
```

> 限速退避（`ret=-2`）仍在节点本机 `send_weixin_text` 内完成（承接上游 §2.3/§9.2，长回复分块 300ms），最终态才回报；`claimed_at` stale 回收是节点崩溃的安全网，不是常规重试路径。

## B.5 不破坏的既有语义
- **同步审核红线 + policy** 都在 `enqueue_proactive_text` 内、入队前执行（中心），节点只发已放行内容——审核零改动。
- **异步审核抽检** `enqueue_outbound_for_moderation` 仍在中心 enqueue 时触发，与今天一致（非发送门禁）。
- **配额 `quota_date` / 幂等 `idempotency_key`(UNIQUE)** 不变；节点重试复用同 key，网关幂等。
- **`insert_outbound_delivery_message`**（落地交付记录）留中心 `/result`，与今天落点一致。
