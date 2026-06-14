# Runbook · 新机 aliyun2（接入 node）

> 配套设计：[`multi_node_access_refactor.md`](multi_node_access_refactor.md)
> 角色：aliyun2 = 纯接入 node（OpenClaw + 微信会话 + 轻 agent），**不跑 backend / SQLite / 调度器 / web**。
> 最后更新：2026-06-12

本文分两部分：
- **Part 1（现在可做，零重构代码）**：验证「跨机被动链路」与最大未知（OpenClaw 能否 POST 远程中心）。
- **Part 2（一期代码落地后）**：aliyun2 作为正式 node 接入主动出站 + 经中心 UI 登录。

---

## 名词与占位

| 占位 | 含义 | 谁提供 |
|---|---|---|
| `<CENTRAL_URL>` | aliyun2 能访问到的 **aliyun1（中心）** 基址。aliyun1 的 backend 监听 `127.0.0.1:8180` 在 nginx 后，所以这里走 **aliyun1 经 nginx 的内网地址**（hosts 别名 `aliyun1`） | 运维确认 |
| `<BRIDGE_SECRET>` | `AI4ALL_BRIDGE_SECRET`，须与 aliyun1 的 `.env` 一致 | 取自 aliyun1 的 `.env`（密钥不入库，见各机 `.env`） |
| `<A_HEALTH>` | `<CENTRAL_URL>/health/live` | — |

### 本环境取值（aliyun1 / aliyun2，2026-06-12 实测）

| 项 | 值 | 说明 |
|---|---|---|
| `aliyun1` | 公网 `59.110.40.50` / 内网 `172.24.16.141` | central+node（线上 monolith，服务真实用户） |
| `aliyun2` | 公网 `39.96.70.112` / 内网 `172.24.18.88` | 本机，纯 node |
| 互访 | 两机 `/etc/hosts` 已互配主机名；内网 TCP 端口已互相打开 | aliyun2 上 `aliyun1`→`172.24.16.141`（内网） |
| `<CENTRAL_URL>` | `http://aliyun1`（nginx :80，内网 hosts 别名） | 不写裸 IP，符合设计 §8.2「稳定指纹地址」 |
| `<A_HEALTH>` | `http://aliyun1/health/live` | — |
| `<BRIDGE_SECRET>` | 已与 aliyun1 的 `.env` 对齐 | 值仅存各机 `.env`（gitignored），不写入本文档 |

> ✅ 可达方式已定：走 **aliyun1 的 nginx**（`http://aliyun1`，内网 hosts 别名，无需 aliyun1 backend 额外监听内网网卡）。443 当前无 TLS，用 80。
>
> ⚠️ **Part 1 前置(aliyun1 侧待修)**：2026-06-12 从 aliyun2 实测 `curl http://aliyun1/health/live` 非 200。**根因已定位(2026-06-12,aliyun1 侧诊断)：不是 backend 故障,而是 nginx 缺面向节点的内网 vhost。**
>   - aliyun1 的 `ai4all-weixin-backend` 健康(本机 `curl 127.0.0.1:8180/health/live` → 200)。
>   - aliyun2 用内网 hosts 别名访问时 Host 头是 `aliyun1`,现有 nginx 无任何 `server_name aliyun1`,请求落进 `governor-game.conf` 的 `listen 80 default_server`(`return 404`);且 `ai4company.top.conf` 主动把 `/openclaw/` 全部 `return 404`,入站路径本就堵死。
>   - **修复(已落地)**：在 aliyun1 新增独立内网 vhost `/etc/nginx/conf.d/ai4all-node.conf`(`server_name aliyun1 172.24.16.141`,`allow 172.24.18.88`,代理 `/health/live` 与 `/openclaw/turn` → `127.0.0.1:8180`),不动现有 vhost。`sudo nginx -t && sudo systemctl reload nginx` 后 `<A_HEALTH>` 返回 200 再继续。

> ⚠️ **Part 1 真正的拦路虎(aliyun2 侧):出站 HTTP 代理必须对内网绕过(no_proxy)**。2026-06-12 实测:aliyun2 上 `curl http://aliyun1/health/live`(乃至裸 IP `172.24.16.141`)返回 **502**,响应头带 `Proxy-Connection: keep-alive` 且**无 `Server: nginx`**——502 不是 aliyun1 nginx 发的(aliyun1 nginx 日志里没有任何 aliyun2 的访问记录),而是 **aliyun2 本机的出站 HTTP 代理**发的:该代理路由不到 VPC 内网 IP,自行回 502。`curl --noproxy '*'` 立即 200 即可证实。
>   - **判别**:`curl -sS -i http://aliyun1/health/live` 看响应头有 `Proxy-Connection`/无 `Server: nginx` = 走了代理;`env | grep -i proxy` 看 `http_proxy/https_proxy`。
>   - **范围澄清(重要)**:这个 502 主要咬的是**交互式 shell 里的 `curl`**(curl 读了 shell 的 `http_proxy`)。而 **OpenClaw 是 user systemd 服务 `openclaw-gateway.service`**(`~/.config/systemd/user/`,`systemctl --user` 管,无需 sudo),启动时用 unit 里 `Environment=` 写死的环境,**不继承 shell**。2026-06-12 实测两机该进程环境**均无 proxy 变量** → 插件 POST `http://aliyun1/openclaw/turn` 本来就直连,**通常不需要任何 no_proxy 改动**。
>   - **正确做法:先验证、再决定**。查 OpenClaw 真实进程环境:`cat /proc/$(pgrep -f 'openclaw/dist/index.js')/environ | tr '\0' '\n' | grep -i proxy`。
>     - 输出为空 → 不用改,直接进 Part 1。
>     - 出现 `http_proxy=`(说明该机 user systemd 全局注了代理)→ 才需给服务加 no_proxy:`systemctl --user edit openclaw-gateway.service` 写 drop-in(Node 不认 CIDR,显式列):`Environment=NO_PROXY=aliyun1,172.24.16.141,localhost,127.0.0.1`(再加一行小写 `no_proxy`),然后 `systemctl --user daemon-reload && systemctl --user restart openclaw-gateway.service`,用 `/proc/<新PID>/environ` 复核。用 drop-in 不动主 unit,OpenClaw 升级重生成 unit 也不被覆盖。
>   - 至于你**手动 curl 验证**方便:shell 里 `export no_proxy=aliyun1,172.24.16.141,localhost,127.0.0.1`(或 `curl --noproxy '*'`)即可,与插件无关。

---

## Part 1 · 零代码验证跨机被动链路（现在做）

目标：证明「aliyun2 收微信消息 → 转发到 aliyun1 → aliyun1 出回复 → 回复经 aliyun2 发出」端到端通。**这一步不依赖任何一期代码**，只配 OpenClaw 指向 aliyun1。

### 步骤

**0. 前提**
- aliyun2 上 OpenClaw 已装好、可正常扫码登录一个微信号（先用 openclaw 自带方式，**别用 aliyun1 上的存量真实账号**，用一个一次性测试号）。
- 拿到 `<CENTRAL_URL>` 和 `<BRIDGE_SECRET>`。

**1. 打通网络 aliyun2 → aliyun1**
- 安全组：aliyun2 出 → aliyun1 入已开（两机内网 TCP 已互通，hosts 已配 `aliyun1`）。
- 在 aliyun2 上验证可达：
  ```bash
  curl -fsS http://aliyun1/health/live    # 预期返回 health/live 的 JSON，HTTP 200
  ```
  若非 200,按上方两条 ⚠️ 依次排除:① 响应头带 `Proxy-Connection`/无 `Server: nginx` → 是 **aliyun2 出站代理**作祟,配 `no_proxy`(`curl --noproxy '*'` 能 200 即坐实);② 响应是 nginx 的 404/502 → 是 **aliyun1 nginx** 缺内网 vhost,装 `ai4all-node.conf` 并 reload。拿到 200 再往下走。

**2. 配 OpenClaw bridge 插件指向 aliyun1**
- 在 aliyun2 的 OpenClaw bridge 插件配置里设置：
  - `AI4ALL_BACKEND_URL = http://aliyun1`（插件会 POST 到 `http://aliyun1/openclaw/turn`）
  - 鉴权头 `Authorization: Bearer <BRIDGE_SECRET>`（值取自 aliyun1 的 `.env` 的 `AI4ALL_BRIDGE_SECRET`，必须与 aliyun1 一致；不写进本文档）
- 具体配置项位置依 OpenClaw 插件实现而定（见 [`openclaw_bridge_design.md`](openclaw_bridge_design.md) §6）。

**3. 登一个一次性测试微信号**（aliyun2 本机 openclaw 扫码）

**4. 发消息验证**
- 用另一个微信给测试号发一句话（如「在吗」）。
- **看 aliyun1 的日志**确认收到了转发：
  ```bash
  # 在 aliyun1 上
  journalctl -u ai4all-weixin-backend -f | grep -i "openclaw/turn\|turn\|account"
  ```
  预期出现该测试号的入站 turn 记录。
- **看 aliyun2**：测试号是否收到了 AI 回复。

### 预期结果与排查

| 现象 | 含义 | 排查 |
|---|---|---|
| 测试号正常收到 AI 回复 | ✅ 跨机被动链路通；§14 #1（远程 POST）验证通过 | — |
| aliyun2 发消息但 aliyun1 日志无 turn | 插件没 POST 到 aliyun1 / 网络不通 | 查 `AI4ALL_BACKEND_URL`、安全组、`curl <A_HEALTH>` |
| aliyun1 日志有 turn 但返回 401/403 | Bearer secret 不对 | 对齐 `<BRIDGE_SECRET>` 与 aliyun1 `.env` |
| aliyun1 有 turn、aliyun2 收不到回复 | 回复回传/openclaw 发送侧问题 | 看 aliyun1 返回体 `reply`、aliyun2 openclaw 发送日志 |
| aliyun1 报该号未绑定 → no_reply | `OPENCLAW_INBOUND_REQUIRE_BINDING=true` 拦截未绑定号 | 测试可在 aliyun1 临时绑定该号，或评估该开关 |

> 完成后回填设计文档 §14 #1 状态。

#### ✅ A 层实测结果（2026-06-12，已闭合 §14 #1）

不依赖扫码、不依赖微信登录，直接在 aliyun2 上仿桥接 payload 打中心，即可验证「最大未知=远程 POST」：

```bash
# aliyun2 上执行；SECRET 取自本机 .env 的 AI4ALL_BRIDGE_SECRET（与 aliyun1 一致）
curl -sS --noproxy aliyun1 -X POST http://aliyun1/openclaw/turn \
  -H "Authorization: Bearer ${SECRET}" -H "Content-Type: application/json" \
  -d '{"channel":"openclaw-weixin","channel_account_id":"conn-test","account_id":"conn-test","chat_type":"private","session_key":"openclaw-weixin:conn-test","message_type":"text","text":"在吗"}'
```

实测返回 **HTTP 200**：`{"status":"ignored","reply":null,"no_reply":true,"metadata":{"reason":"no_binding",...}}`（66ms）。

- 非 401 → **bridge secret 对齐、鉴权通过**。
- 拿到中心 ingest 后的 JSON（metadata 原样回显 channel/account/session）→ **跨机远程 POST 成立、中心已受理并走到绑定闸门**。
- `reason: no_binding` → 线上 `OPENCLAW_INBOUND_REQUIRE_BINDING=true` 生效（合成测试号未绑定被收口）。

副作用：仅两行中心日志，**不建号 / 不调 LLM / 不写记忆 / 不落消息**（`resolve_account_id_for_inbound_channel_identity` 返回 None 即收口，见 `app/turn_service.py`）。

> **B 层（完整回环：微信收消息→桥接 claim turn→回复经微信发回）的「机器间」部分已随此 200 一并证明**（中心跨机回传了 response body）。剩余仅 OpenClaw 本机出站，与单机生产同一份代码。若要眼见完整回环：注册桥接插件指向 `http://aliyun1` + 扫码登测试号 + 在 aliyun1 给该号插一条 binding + 发「在吗」即可，**无需 web 注册端**（web 端按设计仅在中心）。

### 本阶段限制（重要）
- **aliyun2 上这个号的主动消息（提醒 / dreaming / 欢迎语）不会发**——出站队列消费（node agent）属一期代码，尚未实现。这是预期的。
- **不要把 aliyun1 上的存量真实账号迁到 aliyun2**（会话不可热迁，迁了要重扫码）。Part 1 只用一次性测试号。

---

## Part 2 · aliyun2 作为正式 node 接入（一期代码落地后）

前置：一期代码已合并（`run_access_node.py`、`node_gateway`、中心 node-facing 端点、schema 迁移），aliyun1 已按 [runbook A](multi_node_access_runbook_A.md) 升级为 central+node。

> ⚠️ **OpenClaw patch 前置（2026-06-12 aliyun2 实测：3 个 patch 一个都没打，是上游全新装的 weixin/openclaw）**：
> | Patch（`patches/`） | 作用 | Part 2 是否硬前置 |
> |---|---|---|
> | `openclaw-weixin-gateway-methods-runtime` | 暴露 `web.login.start/wait` 为 gateway 方法 | **是** —— 中心 push 扫码登录（附录 A.2）直接依赖,不打则登录 exec 失败 |
> | `openclaw-weixin-logout-account-runtime` | 单账号 `logoutAccount` runtime | 多账号登出/再均衡前需要 |
> | `openclaw-before-agent-reply-media` | 入站图片本地路径透出给 before_agent_reply | 图片理解前需要;纯文本回环不依赖。**多机配套**:已选「bridge 传字节/内联 base64」(见 [补丁维护 §2.5](openclaw_patches_maintenance.md)),node 还须部署字节改造后的 bridge + aliyun1 nginx 调 `client_max_body_size 12m`,否则中心读不到 node 本地图、base64 大图会 413 |
>
> 起 node agent 前先把 patch 应用到 aliyun2 的安装包（weixin 在 `~/.openclaw/npm/projects/.../@tencent-weixin/openclaw-weixin`，openclaw core 在 `~/.npm-global/lib/node_modules/openclaw`）。**别等扫码登录失败才回头查。**
>
> **2026-06-13 实测落地（aliyun2）**：QR + 登出两个插件补丁**已应用并验证**（`logoutAccount`=4、`gatewayMethods`=1、`WEIXIN_GATEWAY_METHODS`=2，`node --check` 通过，gateway 重启健康、无 `unsupported channel`）。两个实操坑：
> 1. **`gateway-methods` 不能 `patch -p1`**：该 patch 的 hunk 头是裸 `@@`（无行号），GNU patch 报 "Only garbage"。按文档约定**做等价手动编辑**：在 weixin 插件 `dist/src/channel.js`（运行态）+ `src/channel.ts`（留档）的 `MEDIA_OUTBOUND_TEMP_DIR` 行后加 `export const WEIXIN_GATEWAY_METHODS = ["web.login.start","web.login.wait"];`，并在 `capabilities` 同级加 `gatewayMethods: WEIXIN_GATEWAY_METHODS,`。`logout` patch 可正常 `patch -p1`（offset 容忍）。
> 2. **冒烟 `openclaw gateway call web.login.start` 会被 gateway 设备配对拦住**（报 `pairing required: scope upgrade pending`，**不是** `provider not available`，所以不是补丁问题）。aliyun2 的 loopback CLI 未做 scope 配对授权；真机登录冒烟应走**中心 push 扫码登录**链路（附录 A.4）来验证，而非本机 CLI。
>
> **2026-06-13 图片理解 + 字节 bridge 落地（aliyun2，本次）**：
> - **核心 dist 图片补丁已打**：`get-reply-BpFiu3Nn.js` 手术注入 `hookCleanedBody`（备份 `.bak.imageunderstanding`，`node --check` 通过）。目标串在 v2026.6.5 精确匹配 1 次、依赖符号齐全，**未跑 deploy 脚本**（那是中心机用的，会改 backend `.env`/重启 backend，node 上手动做 dist 那一步即可）。
> - **字节版 bridge 已装+注册**：`openclaw plugins install ./openclaw-bridge --force` → `~/.openclaw/extensions/ai4all-openclaw-bridge`；`openclaw config set ...config.backendUrl http://aliyun1` + secret 从 `.env` 同步（64 字符，已与 aliyun1 对齐，curl `/openclaw/turn` 得 200 `no_binding` 验证通过）。
> - **⚠️ v2026.6.5 新坑（aliyun1 旧版 v2026.5.28 没有）**：非内置(path)插件的会话钩子默认被挡，inspect 报 `typed hook "before_agent_reply" blocked ... must set ...hooks.allowConversationAccess=true`。须 `openclaw config set plugins.entries.ai4all-openclaw-bridge.hooks.allowConversationAccess true` 再重启，钩子才生效。
> - **~~仍待办① aliyun1 nginx~~ → ✅ 已落地（2026-06-14，中心机）**：`/etc/nginx/conf.d/ai4all-node.conf` 已加 `client_max_body_size 12m`（默认 1m，base64 图会 413）并放行 `location ^~ /node/`（节点心跳/出站认领&回报/登录 push）；`nginx -t` 通过、`reload` 已生效。本机实测 `Host: aliyun1` 下 `/health/live`→200、`/node/heartbeat` 无凭据→401（backend 鉴权闸，非 nginx 404，证明 `/node/` 已正确反代）。备份 `ai4all-node.conf.bak.prenode`。
> - **仍待办（端到端发图前）**：aliyun2 **扫码登一个测试微信号** + 中心给该号插 binding，才能真机发图验证。
> - aliyun2 当前**无任何微信号登录**（仅 weixin 插件 v2.4.4 loaded 可用），故本次 gateway 重启零冲击。

### 步骤

**1. 部署代码**
- aliyun2 上 `git clone`/`pull` 本仓库，建 `.venv`，`pip install -r requirements.txt`。
- **不创建/不依赖** `data/ai4all.sqlite3`；aliyun2 不碰 DB。

**2. 配置 `.env`（node 角色最小集）**
```
AI4ALL_ROLE=node
NODE_ID=aliyun2
CENTRAL_URL=<CENTRAL_URL>          # 节点→中心 的稳定指纹地址（见设计 §8.2）
NODE_BASE_URL=<aliyun2 内网址:端口>  # 中心 push 登录用，写入 access_nodes
AI4ALL_BRIDGE_SECRET=<BRIDGE_SECRET>
OUTBOUND_PULL_INTERVAL_SECONDS=2
# 不开：PROACTIVE_SCHEDULER_ENABLED / DREAMING_SCHEDULER_ENABLED / MODERATION_WORKER_ENABLED
```

**3. 起 node agent**
- `scripts/run_access_node.py`（建议 systemd unit，如 `ai4all-weixin-node`），它负责：出站 pull 循环 + 登录 exec 端点。
- OpenClaw bridge 插件仍按 Part 1 指向 `<CENTRAL_URL>`（入站不变）。
- **不起** `ai4all-weixin-backend` / `-proactive-scheduler` / nginx 业务路由。

**4. 验证**
- 中心 `access_nodes` 出现 aliyun2（心跳 `last_heartbeat_at` 刷新）。
- 经**中心 web/admin** 发起一次新绑定，确认中心把它分配到 aliyun2、aliyun2 起二维码、扫码成功（登录 push 链路，设计附录 A.4）。
- 给该号触发一条主动消息，确认 aliyun2 的出站 pull 把它发出去（设计附录 B.4）。

### 回滚
- 停 `ai4all-weixin-node`、把该号在 aliyun2 `logout`；中心侧该账号 `assigned_node_id` 改回 aliyun1 并在 aliyun1 重登（如需保留）。aliyun2 退回「仅 Part 1 测试」状态不影响 aliyun1。
