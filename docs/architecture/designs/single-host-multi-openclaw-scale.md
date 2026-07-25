# 单机单 IP 多 OpenClaw 连接微信账号规模问题研究和架构设计讨论

> 状态：讨论稿（用于与外部工程/架构专家评审）
> 最后更新：2026-06-10
> 适用范围：AI4ALL 微信个人 AI 陪伴项目（bridge + OpenClaw）

---

## 0. 一句话问题

**单台机器、单一出口 IP、通过 OpenClaw 挂 1000～5000 个微信账号，能不能扛住？扛不住的话，瓶颈在哪一层，怎么解？**

直觉上大家担心的是 CPU / 内存。但本研究的结论是：**先撞墙的不是机器资源，而是（1）微信侧风控/配额、（2）单 OpenClaw 进程的单点与爆炸半径**。机器资源是第三顺位。因此「单机能不能扛」是个伪命题——真正要设计的是**分片与隔离策略**，以及 bridge 侧支撑分片所需的路由改造。

---

## 1. 背景与当前架构现状

### 1.1 系统组成

- **bridge**（本项目，FastAPI）：每个微信账号完全隔离的 Soul / 对话状态 / 记忆。按 `account_id` 隔离是核心不变量。
- **OpenClaw**：微信侧网关进程，持有微信账号的在线连接，负责收发消息。bridge 与 OpenClaw 是两个独立进程。

### 1.2 两者的对接面（基于代码核实）

> 来源：`app/openclaw_gateway.py`

- **出站（bridge → OpenClaw）**：bridge 每发一条消息都通过 `subprocess.run(["openclaw", "gateway", "call", <method>, ...])` **fork 一个子进程**，去调用本机那个**唯一**的 openclaw daemon。
  - 关键：bridge 代码里**完全没有「哪个 OpenClaw 实例」的概念**——没有 base URL / host / instance / shard，写死「本机 openclaw CLI」。
  - 涉及方法：`web.login.start` / `web.login.wait` / `send` / `channels logout`。
- **入站（OpenClaw → bridge）**：OpenClaw 收到微信消息后 HTTP POST 到 bridge `/turn`，带 `accountId`，bridge 按账号隔离处理。**这个方向是干净的、天然支持多来源。**
- **生命周期**：login / logout / `gateway restart` 全部作用在那个**唯一 daemon** 上。

### 1.3 现状的直接含义

- 当前形态 = **1 个 OpenClaw daemon 持有全部账号的微信连接**。
- 开发机上 `openclaw gateway restart` 后还能继续收发，是因为所有账号都挂在同一个进程上，**一起被重启、一起重连**——在小规模下没事，但这正是规模化时最危险的点（见 §3.2）。
- bridge 对账号物理落在哪个 OpenClaw 进程**零感知**，要做分片必须先补这块。

---

## 2. 关键技术前提：协议与连接模型（决定一切）

2026 年微信 Bot 生态出现了一个重要变化，直接影响本问题的所有量化判断，**必须先对齐**：

### 2.1 官方 iLink 协议（2026 主流）—— 已确认为本项目所用协议

- 腾讯于 2026 年首次为个人微信开放**官方 Bot API**，底层协议称为 **iLink**，域名 `ilinkai.weixin.qq.com`（腾讯官方服务器），3 月 22 日开始灰度。OpenClaw 的 `openclaw-weixin` 渠道即对接此协议。
- **连接模型是长轮询（long-polling）**：客户端 POST `/ilink/bot/getupdates`，服务端最多 hold 35s 直到有新消息。用 `get_updates_buf` cursor 翻页，必须每次更新否则收到重复消息。
- 微信只做消息透传通道，不存储对话内容、不负责 AI 输出。
- Bot 自己发出的消息也会出现在 getupdates（`message_type: 2`），必须过滤，否则自问自答死循环。

> ✅ **已确认（基于代码核实，非推测）**：ai4all 当前用的就是**官方 iLink**，不是逆向 iPad/Web 协议。证据链：
> 1. `app/db.py:64-66` 注释明写 QR 登录回传的是 "raw **ilink bot id** such as `abc@im.bot`"；账号 id 形态 `@im.bot` / `-im-bot`（`app/db.py:61-79`）即 iLink bot 命名约定。
> 2. 渠道包为 `@tencent-weixin/openclaw-weixin@2.4.4`（腾讯**官方** npm scope，见 `docs/architecture/designs/openclaw_weixin_gateway_logout_patch.md`）。
> 3. 运行时行为：gateway "替账号向 **iLink 长轮询**拉消息"（同上文档），即 §2.1 描述的 getupdates 模型。
> 4. `openclaw_gateway.py` 的 `web.login.start` / `web.login.wait` 是 OpenClaw **通用扫码登录 RPC** 的封装命名，与"Web 协议逆向"无关——不要被 `web.` 前缀误导。
>
> 结论：下文所有量化判断**一律按官方 iLink（长轮询）建模**；逆向 iPad/Web 协议的列仅作对照参考，本项目不适用。

### 2.2 为什么协议模型决定瓶颈

下表左列（逆向 iPad/Web）**本项目不适用，仅作对照**；本项目按右列（官方 iLink）建模。

| 维度 | 逆向 iPad/Web 协议（不适用，仅对照） | 官方 iLink（长轮询）✅ 本项目 |
|---|---|---|
| 单账号连接 | 持久 TCP + 心跳 + synckey 状态 | 一个外挂的长轮询 HTTP 请求（≤35s）循环 + cursor |
| 5000 账号在单进程 | 5000 条持久连接 + 心跳风暴 | 5000 个并发 in-flight HTTP 长轮询 + 5000 份 cursor |
| 风控性质 | 高（腾讯视为外挂，封号风险大） | 较低但有配额（官方授权，但同 IP 多号聚集仍异常） |
| 重启代价 | 全员重连，风控高危 | 全员重新拉起长轮询，cursor 需正确恢复 |

**结论（已锁定官方 iLink）**："单机挂量"的资源瓶颈主要是**并发长轮询 socket 数 + 事件循环调度**，而非持久连接内存；风控不是"会不会被当外挂封号"，而是"同 IP 大量账号是否触发官方异常检测 / per-account / per-IP 配额"。

### 2.3 iLink 配额与限速（已查证，2026-06）

> 结论先行：**腾讯官方未公开任何具体配额/频率数值**，但限速客观存在且会在运行时返回明确错误码。这意味着容量规划不能依赖"官方文档的数字"，只能靠 §8 压测实测 + 运行时埋点反推。

- **发送限速**：`sendmessage` 被限速时返回 **`ret=-2`、`errmsg=rate limited`**（社区已实测到，例如定时任务批量投递时触发）。处理方式是**退避重试**（社区实现常见 backoff 3s）。
- **长回复自我限速**：社区实现普遍把长回复分块（`chunk_size≈1000`），逐块发送、块间 **300ms 间隔**，以规避单账号高频发送触发限速。
- **收消息不是"频率限制"而是长轮询 hold**：`getupdates` 服务端最多 hold 35s，正确用法是"返回后立即发起下一次"，不要高频轮询。
- **连接有效期 24h**：iLink 连接 24 小时过期，需自动续连；运行时另有 `errcode -14 → pause_session`（会话暂停信号）需正确处理。
- **条款层面**：《微信 ClawBot 功能使用条款》明确腾讯保留"对信息收发规模/频率进行识别并采取提示/拦截/阻断"的权利，且"不建议用于核心业务、可随时变更或终止"。即合规红线在腾讯手里，配额可被单方收紧。

> 对本项目的直接含义：
> 1. **出站侧（§3.4 改造时）必须内建退避 + 限速感知**：识别 `ret=-2 / rate limited`，按账号退避，长回复分块 300ms。这条现在就能在 `openclaw_gateway` / 发送链路加固，不必等分片。
> 2. **per-account / per-IP 真实配额只能压测得出**（§8.1），官方不给数字。
> 3. **"不建议核心业务 + 可随时终止"是产品级风险**，强化了 §4-D 企业微信兜底的必要性。

---

## 3. 瓶颈分层分析（按撞墙先后排序）

### 3.1 第一层：微信侧风控 / 配额（最先撞墙，与机器多强无关）

- 1000～5000 个号挂一台机 = **一个出口 IP + 一个设备指纹**。即便走官方 iLink，"同 IP 海量账号聚集"仍是异常信号。
  - 本项目（官方 iLink）：腾讯知道你是 bot，封号风险相对下降，但**几乎必然存在 per-account / per-IP 速率与并发配额**，以及对单 IP 账号密度的异常检测——这是本项目的主要风控面。
  - （对照）逆向协议：直接面临封号；社区共识是先用小号测试、用固定 IP、严禁群发营销、频繁换 IP 反触发风控。本项目不走这条路线，列此仅说明 iLink 相对优势。
- **`gateway restart` 是教科书级风控触发点**：全部账号同一秒重连/重新拉起长轮询，从同一 IP 同时上线。
- **企业微信（WeCom）是合规兜底**：官方 WeCom 插件支持私聊/群聊/流式/主动消息/访问控制，需把网关出网 IP 加入「可信 IP 白名单」（否则典型现象是"能收消息不能回复"）。代价是产品形态从"个人微信陪伴"变成"企业微信"，可能不符合本项目定位。

> 这一层直接判定：**"单机单 IP 扛 1000+ 个人微信号"在风控上不成立**，无论硬件多强。出口 IP 多样化是硬约束，不是优化项。

### 3.2 第二层：单 OpenClaw 进程 = 单点 + 爆炸半径全开（第二先撞墙）

- 现在任一账号的协议 bug / 内存泄漏 / OOM / cursor 错乱，拖垮的是**整个 daemon = 全部账号**。
- bridge 做到了按账号隔离，**OpenClaw 这层是零隔离**。
- 一次重启 / 一次崩溃 = 全员掉线 + 全员重连（又叠加 §3.1 的风控）。
- 5000 个号的会话状态、媒体上传、context_token 全在一个进程的生命周期里，**没有故障域切分**。

### 3.3 第三层：单进程资源天花板

- **本项目（官方 iLink）**：5000 个并发长轮询 = 5000 个 in-flight socket + 定时重发 + 每号 cursor/会话状态。OpenClaw 是单线程 event loop（Node 系），海量并发 HTTP 解析/JSON 编解码/回调可吃满一核；连接数本身受 fd 上限、内存（每号几 MB → 10–25GB 量级）约束。
- （对照，不适用）逆向协议：每号持久连接 + 心跳更重，内存与心跳调度更早成为瓶颈。
- 即便是较轻的 iLink 长轮询模型，**单进程也不应该挂到 5000**；这正是"多 OpenClaw 实例"动机的来源。

### 3.4 第四层：bridge 集成层的 subprocess-per-call（独立的扩展性 bug）

- `_run_gateway_call` **每条出站消息 fork 一个 `openclaw` 进程**（`app/openclaw_gateway.py:18-56`）。
- 几十个号时无所谓；到几千号、叠加主动消息调度器（dreaming / heartbeat / 提醒）后，**进程 fork 频率本身**就是 CPU 与延迟瓶颈，且无连接复用。
- 这一层与 OpenClaw 能挂多少号**无关**，是 bridge 自身必须修的债：应改为对目标实例的持久化传输（HTTP / socket / 常驻 IPC）。

---

## 4. 候选解决方案

整体方向只有一个能解 §3.1：**按 `account_id` 分片 + 出口 IP 多样化**。其余都是配套。

### 方案 A：单机多 OpenClaw 实例 + 进程隔离（"多 OpenClaw"本意）

在一台机上跑 N 个 OpenClaw 实例，每实例用独立 `WECLAW_HOME` / `agentDir` / 端口隔离凭证与会话，每实例只挂一批号（如 200–500/实例）。

- ✅ 解决 §3.2 爆炸半径（一个实例挂只影响一片）和 §3.3 单进程资源天花板。
- ❌ **解决不了 §3.1**：仍是单机单 IP，风控/配额墙照旧。
- ❌ 多实例都在同一台机：CPU/内存/fd 仍共享，只是切了故障域，没切资源域。
- 适用：作为横向分片的**单机内子结构**，或账号量其实没那么大、且每号都挂了独立住宅代理时的过渡形态。

> 注意：OpenClaw 官方文档明确——**不要在多个 agent 间复用 `agentDir`**，会导致凭证/会话冲突。多实例隔离必须各自独立 home/dir。

### 方案 B：横向分片（多机 / 多 IP）+ bridge 路由层（推荐主线）

多台机（或多容器）各跑 OpenClaw，每片绑**独立出口 IP**，每片挂有上限的号；bridge 增加 `account_id → 实例` 路由。

- ✅ 同时解 §3.1（IP 多样化）/ §3.2（故障域）/ §3.3（资源域）。
- ✅ 入站方向天然支持（OpenClaw 已带 accountId POST 回 bridge）。
- 代价：bridge 必须做 §5 的改造；运维复杂度上升（多实例编排、健康检查、再均衡）。

### 方案 C：出口 IP 多样化（A/B 的必备配套）

- 手段：多 NIC / 弹性公网 IP 池 / 住宅代理池 / 多小规格 VM。每片或每组账号走不同 egress。
- 关键设计点：**账号与出口 IP 的绑定要稳定**（同一个号长期固定从同一 IP 出去），频繁换 IP 本身触发风控。
- 这是把"单 IP"约束解开的唯一办法，必须与 A 或 B 组合。

### 方案 D：官方企业微信路线（合规兜底，备选）

- 用 WeCom 官方插件 + 可信 IP 白名单 + `accountId`/`binding` 路由，从根上规避个人微信封号风险。
- ❌ 产品形态改变（企业微信 ≠ 个人微信陪伴），需产品决策是否可接受。
- 定位：作为"如果个人微信协议路线被风控逼到墙角"的 Plan B。

### 方案对比

| 方案 | 解风控(§3.1) | 解爆炸半径(§3.2) | 解资源(§3.3) | bridge 改造量 | 运维复杂度 |
|---|---|---|---|---|---|
| A 单机多实例 | ✗ | ✓ | 部分 | 中（需路由到实例） | 低 |
| B 横向分片+路由 | ✓ | ✓ | ✓ | 大 | 高 |
| C 出口 IP 多样化 | ✓（配套） | — | — | 小 | 中 |
| D 企业微信 | ✓（换协议） | ✓ | ✓ | 中 | 中 |

**推荐组合**：**B + C** 为主线；A 作为单机内的故障域切分子结构；D 作为产品兜底。

---

## 5. bridge 侧需要的改造（支撑分片的最小集）

无论 A 还是 B，bridge 都得从"写死本地单 OpenClaw"升级为"按账号寻址到指定实例"。核心两件事：

1. **新增 `account_id → 实例` 路由表**
   - 账号本就按 `account_id` 隔离，这是自然延伸。
   - 需考虑：登录时如何分配实例（负载均衡 / 容量上限）、实例下线时账号如何迁移（re-login 到新实例）。
2. **替换 `_run_gateway_call` 的传输层**
   - 从「写死 `subprocess.run(["openclaw", ...])` 调本地」改为「按账号路由到目标实例的 HTTP/socket 端点」。
   - 顺带干掉 subprocess-per-call（§3.4），改为持久连接 + 复用。
   - 涉及：`send` / `web.login.start` / `web.login.wait` / `channels logout` 全部需要带"目标实例"维度。
3. **生命周期操作实例化**
   - restart / login / logout 必须作用到正确实例，不能再"全局重启"。
   - 入站方向无需改（已带 accountId）。

> 这是把"按账号隔离"这个已有不变量，从 bridge 内部延伸到 OpenClaw 物理拓扑。改动集中、边界清晰，但 `_run_gateway_call` 是全项目出站咽喉，需谨慎回归（参考 `turn_service.py`、`proactive/messaging.py`、`main.py` 的所有调用点）。

---

## 6. 竞品参考：Clawinlink（闭源收费方案）

> ⚠️ **可信度说明**：截至本稿，公开渠道**未检索到名为 "Clawinlink" 的确切产品资料**。该名称大概率是 `Claw + iLink` 的合成命名。以下分为「已知生态事实」（可信）与「对 Clawinlink 的合理推测」（待核实），请勿当作确证。

### 6.1 已知生态事实（来自公开资料）

- **协议底座**：2026 年微信官方开放 iLink Bot 协议（`ilinkai.weixin.qq.com`），长轮询 `getupdates`（≤35s）+ cursor，`context_token` 作为会话关联凭证回传，`message_type` 过滤防自环。
- **OpenClaw 原生多账号**：单网关内多 agent + 多渠道账号，`session.dmScope` 按 `account-channel-peer` 隔离，binding 把入站分发给 agentId；多账号必须独立 `agentDir`。
- **OpeniLink Hub**（开源 self-hosted relay）：扫码绑定多账号、自动续期会话、消息全链路 tracing、WebSocket/Webhook/AI 自动回复、Web 面板管理——**形态上最接近"多账号微信 Bot 管理平台"**。
- **容器路线（WechatOnCloud）**：每个微信实例 = 一个跑 Xvfb + 官方微信的容器，KasmVNC 投屏，docker.sock 按需创建销毁——重隔离、重资源，规模化成本高。

### 6.2 对 Clawinlink 的合理推测（待核实）

基于"闭源收费 + 2026 年 5 月新出 + 主打微信多账号规模"，其差异化大概率落在我们正要解的同一批难点上：

- **托管式多 IP / 代理池**：替客户解决 §3.1 的出口 IP 多样化（最可能的收费卖点）。
- **实例编排 + 账号路由 + 自动再均衡**：即我们 §4-B / §5 要自建的能力，做成开箱即用。
- **风控规避策略**：登录节奏打散、IP-账号稳定绑定、心跳/长轮询参数调优、重启不全员重连等运营 know-how。
- **可观测性**：消息 tracing、账号在线率、掉线告警（对标 OpeniLink Hub 的 tracing）。

### 6.3 对我们的启示

- 竞品收费点反推：**真正难且值钱的不是"挂上号"，而是"规模化下不被风控 + 故障域可控 + 可运维"**——正是本文 §3.1/§3.2。
- 我们的优势：bridge 已有**强账号隔离 + 完整 Soul/记忆/主动消息**业务层。补齐"分片路由 + 出口 IP 策略"即可，不必从零造网关。
- 决策点：**自建 §4-B/C，还是采购 Clawinlink 这类托管层**——取决于团队对 IP 资源运营、风控对抗的投入意愿（这恰是闭源方案的护城河）。

---

## 7. 待验证的关键未知（评审前必须钉死）

1. ~~**协议确认**：当前是官方 iLink 还是逆向协议？~~ ✅ **已确认为官方 iLink**（见 §2.1 证据链：`@tencent-weixin/openclaw-weixin` 官方包 + `app/db.py` ilink bot id + 运行时 iLink 长轮询）。§2/§3 量化已据此锁定。
2. **账号性质**：真人微信号 vs 注册小号？（风控策略与可挂密度完全不同）
3. **目标规模与消息密度**：1000 还是 5000？单号日均消息量？（决定 §3.3 事件循环是否瓶颈）
4. **当前实测水位**：开发机/线上现在实际挂了几个号？跑了多久？掉线率？（确定离墙还有多远）→ **采集工具已落地**（§9.3 `monitor_health --record-water-level`），接入定时跑一段时间后即可回答。
5. **OpenClaw 能力边界**：能否一机多实例并存？能否给实例/账号绑定独立出口 IP/代理？（决定 A/B 可行性，是最大未知，因 OpenClaw 是打包 dist）
6. **产品红线**：企业微信路线（方案 D）产品上是否可接受？

---

## 8. 建议的下一步（压测 + 决策）

1. **单实例压测**：在受控环境下，单个 OpenClaw 实例逐步加号，测出**单实例安全上限**（在线率、内存、事件循环延迟、风控告警的拐点）。这是所有容量规划的基数。
2. **重启爆炸半径实验**：测 N 个号同时重连/重新拉长轮询的风控反应，验证 §3.2 假设，指导"分批重连/错峰"策略。
3. **出口 IP 实验**：同号固定 IP vs 换 IP 的风控差异，验证 §4-C 的绑定稳定性要求。
4. **bridge 路由层 PoC**：先做 `account_id → 实例` 路由表 + `_run_gateway_call` 寻址化的最小实现（§5），用 2 个实例验证端到端。
5. **竞品尽调**：直接接触 Clawinlink 团队拿到架构/计费/IP 资源细节，对比自建 ROI。

---

## 9. 现在就执行的运维红线（不依赖重构，立即降风险）

重构方案（§4/§5）定稿前，以下措施**零或极小改动、可立即落地**，用于在现有单 daemon 形态下先把自伤风险压住。

### 9.1 运维纪律（零代码）

1. **禁止把全局 `gateway restart` 当日常操作**。这是 §3.1/§3.2 教科书级风控触发点——所有账号同一秒从同一 IP 重新上线/重新拉长轮询。
   - 必须重启时：**错峰、分批**，避开活跃时段；能用更细粒度操作（单账号 logout/重登）就不要全局重启。
2. **出口 IP 稳定绑定**：同一账号长期固定从同一 IP 出去，**不要频繁换 IP**（频繁换 IP 本身是风控信号，见 §4-C）。
3. **登录节奏打散**：批量登录/重登时分批错峰，不要同 IP 短时间集中上号。

### 9.2 发送侧加固（小改动，对应 §2.3 限速）

- 出站链路识别 `ret=-2 / rate limited`，按账号**退避重试**；长回复**分块发送 + 块间 300ms**。可在 `openclaw_gateway` / 发送链路就地加固，不必等 §5 分片改造。

### 9.3 水位基线采集（已落地 ✅）

- 监控脚本 `scripts/monitor_health.py` 新增 `--record-water-level`（env `MONITOR_RECORD_WATER_LEVEL`），每次运行把账号水位快照以 JSONL 追加到 `--water-level-file`（默认 `data/water_level.jsonl`）。
- 快照来自 bridge 自有数据 `db.get_account_water_level()`：**已绑定账号总数 + 各时间窗（15/60/1440 分钟）内仍活跃（有入站）的去重账号数**，作为在线率/掉线率 proxy。重复采样即得趋势。
- 这是测量旁路，**不进健康告警流**、失败只打 stderr，不影响既有监控。
- 用途：直接回答 §7.4「当前实测水位」——挂了几个号、活跃率多少、掉线趋势如何，为 §8 压测与容量规划提供基线。
- 建议接入现有定时监控（如每 5–10 分钟一次），跑一段时间后用这批 JSONL 画出水位曲线。

> 这三项做完，§7 的关键未知（协议=已确认 iLink、配额=已查证无公开数值、水位=开始采集）就只剩"账号性质/目标规模/OpenClaw 多实例能力"需要团队和压测回答。

---

## 附：参考来源

- [多智能体路由 - OpenClaw 官方文档](https://docs.openclaw.ai/zh-CN/concepts/multi-agent)
- [微信 - OpenClaw 官方文档](https://docs.openclaw.ai/zh-CN/channels/wechat)
- [微信Bot API 技术解析：腾讯 iLink 协议首次合法开放](https://github.com/hao-ji-xing/cc-weixin/blob/main/weixin-bot-api.md)
- [openclaw-weixin/weixin-bot-api.md（iLink 协议细节：getupdates / cursor / context_token）](https://github.com/hao-ji-xing/openclaw-weixin/blob/main/weixin-bot-api.md)
- [OpeniLink Hub — 开源微信 Bot 管理平台（多账号/tracing）](https://github.com/openilink/openilink-hub)
- [WechatOnCloud — 容器化多微信实例方案](https://github.com/Gloridust/WechatOnCloud)
- [从微信 ClawBot 到 iLink：解析官方 Bot 协议](https://yarrow.ren/posts/wechat-ilink-llm-bridge-guide/)
- [OpenClaw 多 Agent、多账户怎么配（binding/隔离）](https://blog.csdn.net/weixin_45653525/article/details/158924197)
- [OpenClaw 接入微信实测（2026 最新版，iLink）](https://blog.csdn.net/u011831527/article/details/161201252)
- [微信 ClawBot：首个官方个人号 Bot API iLink 协议拆解与实战 - 字节笔记本](https://www.bytenote.net/article/wechat-ilink-bot-api)（含 `ret=-2 rate limited`、24h 连接、`errcode -14` pause、分块 300ms 等运行时约束）
- [插件速率异常案例 - 微信开放社区](https://developers.weixin.qq.com/community/develop/doc/0008a620c2ca4045e51551a1d61400)（社区实测 `iLink sendmessage rate limited: ret=-2`）
- 内部代码：`app/openclaw_gateway.py`、`app/db.py:get_account_water_level`、`scripts/monitor_health.py`、`CLAUDE.md` 模块地图
