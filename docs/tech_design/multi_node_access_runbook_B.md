# Runbook · 新机 B（接入 node）

> 配套设计：[`multi_node_access_refactor.md`](multi_node_access_refactor.md)
> 角色：B = 纯接入 node（OpenClaw + 微信会话 + 轻 agent），**不跑 backend / SQLite / 调度器 / web**。
> 最后更新：2026-06-12

本文分两部分：
- **Part 1（现在可做，零重构代码）**：验证「跨机被动链路」与最大未知（OpenClaw 能否 POST 远程中心）。
- **Part 2（一期代码落地后）**：B 作为正式 node 接入主动出站 + 经中心 UI 登录。

---

## 名词与占位

| 占位 | 含义 | 谁提供 |
|---|---|---|
| `<CENTRAL_URL>` | B 能访问到的 **A（中心）** 基址。A 的 backend 监听 `127.0.0.1:8180` 在 nginx 后，所以这里通常是 **A 经 nginx 的内网/域名地址**（或 A 把 8180 暴露到内网） | 运维确认 |
| `<BRIDGE_SECRET>` | `AI4ALL_BRIDGE_SECRET`，须与 A 的 `.env` 一致 | 取自 A 的 `.env` |
| `<A_HEALTH>` | `<CENTRAL_URL>/health/live` | — |

> ⚠️ 先确认 A 从 B 的可达方式：A 的 backend 默认只听 `127.0.0.1:8180`（见 `restart_runtime.sh` 的 `READY_URL`），**B 不能直连 127.0.0.1**。两条路二选一，运维定：
> 1. 走 **A 的 nginx**（对外/内网域名，推荐，已有 TLS/路由）；
> 2. 让 A 的 backend 额外监听内网网卡（`--host 0.0.0.0` 或内网 IP）并放安全组——改动更大，不推荐。

---

## Part 1 · 零代码验证跨机被动链路（现在做）

目标：证明「B 收微信消息 → 转发到 A → A 出回复 → 回复经 B 发出」端到端通。**这一步不依赖任何一期代码**，只配 OpenClaw 指向 A。

### 步骤

**0. 前提**
- B 上 OpenClaw 已装好、可正常扫码登录一个微信号（先用 openclaw 自带方式，**别用 A 上的存量真实账号**，用一个一次性测试号）。
- 拿到 `<CENTRAL_URL>` 和 `<BRIDGE_SECRET>`。

**1. 打通网络 B → A**
- 放通安全组：B 出 → A 入（nginx 443/80 或内网 8180）。
- 在 B 上验证可达：
  ```bash
  curl -fsS <A_HEALTH>          # 预期返回 health/live 的 JSON，HTTP 200
  ```
  通不了 → 先解决安全组 / nginx 路由，别往下走。

**2. 配 OpenClaw bridge 插件指向 A**
- 在 B 的 OpenClaw bridge 插件配置里设置：
  - `AI4ALL_BACKEND_URL = <CENTRAL_URL>`（插件会 POST 到 `<CENTRAL_URL>/openclaw/turn`）
  - 鉴权头 `Authorization: Bearer <BRIDGE_SECRET>`（A 的 `/openclaw/turn` 要求该 Bearer，secret 必须与 A 一致）
- 具体配置项位置依 OpenClaw 插件实现而定（见 [`openclaw_bridge_design.md`](openclaw_bridge_design.md) §6）。

**3. 登一个一次性测试微信号**（B 本机 openclaw 扫码）

**4. 发消息验证**
- 用另一个微信给测试号发一句话（如「在吗」）。
- **看 A 的日志**确认收到了转发：
  ```bash
  # 在 A 上
  journalctl -u ai4all-weixin-backend -f | grep -i "openclaw/turn\|turn\|account"
  ```
  预期出现该测试号的入站 turn 记录。
- **看 B**：测试号是否收到了 AI 回复。

### 预期结果与排查

| 现象 | 含义 | 排查 |
|---|---|---|
| 测试号正常收到 AI 回复 | ✅ 跨机被动链路通；§14 #1（远程 POST）验证通过 | — |
| B 发消息但 A 日志无 turn | 插件没 POST 到 A / 网络不通 | 查 `AI4ALL_BACKEND_URL`、安全组、`curl <A_HEALTH>` |
| A 日志有 turn 但返回 401/403 | Bearer secret 不对 | 对齐 `<BRIDGE_SECRET>` 与 A `.env` |
| A 有 turn、B 收不到回复 | 回复回传/openclaw 发送侧问题 | 看 A 返回体 `reply`、B openclaw 发送日志 |
| A 报该号未绑定 → no_reply | `OPENCLAW_INBOUND_REQUIRE_BINDING=true` 拦截未绑定号 | 测试可在 A 临时绑定该号，或评估该开关 |

> 完成后回填设计文档 §14 #1 状态。

### 本阶段限制（重要）
- **B 上这个号的主动消息（提醒 / dreaming / 欢迎语）不会发**——出站队列消费（node agent）属一期代码，尚未实现。这是预期的。
- **不要把 A 上的存量真实账号迁到 B**（会话不可热迁，迁了要重扫码）。Part 1 只用一次性测试号。

---

## Part 2 · B 作为正式 node 接入（一期代码落地后）

前置：一期代码已合并（`run_access_node.py`、`node_gateway`、中心 node-facing 端点、schema 迁移），A 已按 [runbook A](multi_node_access_runbook_A.md) 升级为 central+node。

### 步骤

**1. 部署代码**
- B 上 `git clone`/`pull` 本仓库，建 `.venv`，`pip install -r requirements.txt`。
- **不创建/不依赖** `data/ai4all.sqlite3`；B 不碰 DB。

**2. 配置 `.env`（node 角色最小集）**
```
AI4ALL_ROLE=node
NODE_ID=B
CENTRAL_URL=<CENTRAL_URL>          # 节点→中心 的稳定指纹地址（见设计 §8.2）
NODE_BASE_URL=<B 内网址:端口>       # 中心 push 登录用，写入 access_nodes
AI4ALL_BRIDGE_SECRET=<BRIDGE_SECRET>
OUTBOUND_PULL_INTERVAL_SECONDS=2
# 不开：PROACTIVE_SCHEDULER_ENABLED / DREAMING_SCHEDULER_ENABLED / MODERATION_WORKER_ENABLED
```

**3. 起 node agent**
- `scripts/run_access_node.py`（建议 systemd unit，如 `ai4all-weixin-node`），它负责：出站 pull 循环 + 登录 exec 端点。
- OpenClaw bridge 插件仍按 Part 1 指向 `<CENTRAL_URL>`（入站不变）。
- **不起** `ai4all-weixin-backend` / `-proactive-scheduler` / nginx 业务路由。

**4. 验证**
- 中心 `access_nodes` 出现 B（心跳 `last_heartbeat_at` 刷新）。
- 经**中心 web/admin** 发起一次新绑定，确认中心把它分配到 B、B 起二维码、扫码成功（登录 push 链路，设计附录 A.4）。
- 给该号触发一条主动消息，确认 B 的出站 pull 把它发出去（设计附录 B.4）。

### 回滚
- 停 `ai4all-weixin-node`、把该号在 B `logout`；中心侧该账号 `assigned_node_id` 改回 A 并在 A 重登（如需保留）。B 退回「仅 Part 1 测试」状态不影响 A。
