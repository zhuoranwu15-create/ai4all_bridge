# 暂态消息延迟排查与方案记录

> 临时文档。背景：`feat: send interim message on tool calls`（901803e）+ 前台发送统一 push
> 改造（91e96f7）上线后，挂在 aliyun2 的账号触发搜索工具时，「稍等我查一下」暂态消息**晚于**最终
> 搜索结果到达。本文沉淀完整排查链路、根因、已排除方案与推荐方案，供后续接手与决策。

日期：2026-06-20。涉及线上：aliyun1（central+node）+ aliyun2（node-only）。

---

## 1. 现象

- 挂 aliyun2 的微信账号（如 aid_956326343）发能触发 `web_search` 的消息。
- 预期：先收到「稍等我查一下」，几秒后收到搜索结果。
- 实际：**先收到搜索结果，暂态消息反而后到**（顺序颠倒，体验失效）。
- 不触发搜索的普通消息：**正常、不慢**。

---

## 2. 两条发送通道（核心模型）

发送快慢不取决于消息类型，而取决于**发送方手里有没有到 openclaw gateway 的常驻连接**。

### 通道 A —— 正常回复（快，~0.1s）
1. openclaw 内的 **openclaw-bridge 插件**发 HTTP 请求到中心，**同步等响应**。
2. 中心跑完 turn，把回复塞进该请求的 **HTTP response**（`OpenClawTurnResponse(reply=...)`）返回。
3. 插件拿到回复后发出——**插件活在 openclaw 进程内**，与 gateway 是**进程内**通信，握着常驻通道，
   发送**不付握手**。
- 要点：回复是**搭入站请求的 response 原路返回**，由插件用进程内常驻连接发。入站这条 HTTP 短连接
  只是"运回复文字"的载体，省时间的是插件端不用握手的常驻连接。

### 通道 B —— 暂态 / 主动 / 欢迎语（慢，~10s）
1. 这些消息**没有正在等响应的入站请求可搭车**：暂态消息必须赶在最终回复**之前**发，等不到
   response；主动消息是中心自己发起。
2. 发起方是**我们的后端进程（FastAPI）**，与 openclaw 是**两个进程**，后端**没有**到 gateway 的
   常驻连接。
3. 后端只能 `subprocess` 起 `openclaw gateway call send` CLI → CLI **每次都新建一条到 gateway 的
   连接**、付 ~10s 初始化、发完退出。

**结论轴**：插件（进程内、常驻连接）→ 快；后端（外部进程、走 CLI、每发冷启一次）→ 慢。

代码落点：
- 通道 A：turn 的 HTTP 响应 `reply` 字段（`app/turn_service.py` 返回 → bridge router → 插件）。
- 通道 B：`app/openclaw_gateway.py` `send_weixin_text()` → `_run_gateway_call(method="send")` →
  `subprocess.run([openclaw_cli_path, "gateway", "call", "send", "--json", "--timeout", ...,
  "--params", ...])`。这个 CLI 子进程就是 ~10s 瓶颈。

---

## 3. 本次「顺序颠倒」的具体根因

1. **第一层（已修，91e96f7）**：暂态消息原走 `should_inline_dispatch_for_account` 闸门——账号归属
   aliyun2、turn 在 aliyun1 跑，闸门返回 False，`_make_tool_thinking_sender` 直接 `return None`，
   消息**根本没装上**。已用对称 push 通道 `node_gateway.node_send_text` 修复（本机直调 / 远程 push
   到 `/node/exec/send/text`），local/remote 行为对齐。

2. **第二层（本次顺序颠倒的直接原因）**：修复后暂态消息确实发出了，但它走**通道 B**（CLI），单次发送
   付 ~10s；而最终搜索结果走**通道 A**（搭 turn response，~0.1s）。`web_search` 本身比 10s 快，于是
   结果先到、暂态后到。**不是网络/路由慢，是通道 B 的 CLI 冷启固定 ~10s。**

### 计时实测（aliyun2）
- 检测触发 → aliyun2 收到 push 请求 = **51ms**（我们的代码、跨机网络都很快）。
- openclaw 发送本身 = **10,251ms**。
- 进一步拆 CLI 内部：`start → gateway_ts = 9673ms`，`gateway_ts → end = 118ms`
  —— **~10s 全部花在「建连/初始化」，在请求到达 gateway 之前**；gateway 真正处理只占 ~100ms。
- 纯 `openclaw --version` = 0.065s → **不是 node 启动慢，是连接 gateway 的握手/初始化慢**。

---

## 4. 对齐问题确认（local vs remote）

架构对齐**正确**：本机账号与远程账号现在只差一次 ~51ms 网络跳。那 ~10s 的 CLI 冷启成本对
**本机账号同样存在**（aliyun1 本机发暂态/主动也走同一 CLI 路），只是本机的暂态/主动此前没人盯
延迟、没被注意到。即：**慢是通道 B 的共性，与 local/remote 无关**，不是对齐改造引入的回归。

---

## 5. 已排除的方案 / 试过没用的旋钮

- **`--timeout` 调大/调小**（试 2000 / 30000ms）：仍 ~9.9s。该参数管 gateway 调用超时上限，不管冷启。
- **`OPENCLAW_HANDSHAKE_TIMEOUT_MS`**（试 1500 / 500ms）：仍 ~9.9s，**无效**。名字最像，但它管的不是
  这段等待（握手其实成功了，调到 500ms 都没触发超时）。
- 结论：**openclaw CLI 没有暴露可调的 ready-wait 旋钮**来消掉这 ~10s。继续挖 minified dist 源码找确切
  常量收益低、不确定，已停止。
- **把所有消息都改走通道 A**：不可行。通道 A 依赖「有一个正在等响应的入站请求」；暂态消息要赶在最终
  回复前发、主动消息无入站请求，天然搭不上车。（除非改 turn 契约做流式响应，见方案 B。）

---

## 6. 推荐方案

### 方案 A（推荐）：后端持久化 gateway 连接，替换 per-send CLI
- 让**后端进程自己握一条到 gateway 的常驻 WS 长连接**（像 openclaw-bridge 插件在进程内那样），
  握手只在建连时付一次（甚至后端自写的轻量 WS 客户端可能根本不触发 CLI 那套重初始化）。
- 之后每条发送 ~0.1s。**一次性修好通道 B 上的所有发送**：暂态、主动消息、提醒、commitment、
  reactivation、onboarding/绑定欢迎语。
- 不改 turn 契约、不改 openclaw、不动账号隔离。
- 改造点集中在 `app/openclaw_gateway.py` 的发送传输层：CLI-spawn → 常驻网关连接（连接/auth/call
  帧格式需先摸清 gateway WS 协议）。需进 plan mode 调研协议后再动手。
- 风险：需维护连接生命周期（重连、keepalive、断线降级回 CLI 兜底）；并发发送的连接复用/串行化。

### 方案 B（仅治暂态，不推荐作为唯一解）
- 把 turn 响应改成**流式**，暂态消息作为首块先于最终结果下发，仍由插件（通道 A）发。
- 只解决暂态消息顺序，**不治主动消息/欢迎语**的 10s；且要改 turn 契约与 bridge 插件，面更大。

---

## 7. 当前代码状态

- 已部署提交：`39e7fa8`（aliyun1、aliyun2 两节点一致）。
- 前台发送统一 push 改造（`node_send_text` + `/node/exec/send/text` + 三处前台调用切换）已上线，
  暂态消息现能发出、local/remote 对齐；**剩下的就是通道 B 的 ~10s CLI 冷启**，由方案 A 解决。
- 尚未对方案 A 动任何代码，等决策。

---

## 8. 决策待定

1. 是否进 plan mode 调研 gateway WS 协议、规划方案 A（后端常驻连接）？
2. 还是先维持现状（暂态消息晚到但不致命），方案 A 另起一轮排期？
