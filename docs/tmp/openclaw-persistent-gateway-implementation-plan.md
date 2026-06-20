# OpenClaw 持久 Gateway 连接实施方案

> 临时实施方案。背景见
> [`docs/tmp/interim-message-latency-investigation.md`](interim-message-latency-investigation.md)。
> 本文把“后端持久化 gateway 连接，替换 per-send CLI”细化到可编码、可测试、可灰度的执行计划。

日期：2026-06-20。范围：AI4ALL 后端与 node agent 的前台/主动发送链路；不改 OpenClaw 插件、不改 turn
契约。

---

## 1. 结论

认可原排查文档推荐的方案 A，但第一版应收敛为：

**仅把 `send_weixin_text()` 的 `send` 传输层从“每条消息 spawn CLI”替换为“进程内复用一条本机 OpenClaw
Gateway WebSocket 长连接”，并保留现有 CLI fallback。**

这样一次性解决通道 B 的发送延迟：

- tool thinking 暂态消息；
- onboarding / 绑定欢迎语；
- proactive / reminder / commitment / reactivation 等主动消息；
- node-only 机器上 `/node/exec/send/text` 和 outbound pull 本机发送。

不建议第一版做流式 turn 响应。流式只能解决暂态消息顺序，不能解决主动消息/欢迎语，且要改 bridge 插件和
turn response 契约，影响面更大。

---

## 2. 已确认的问题模型

### 2.1 快路径：最终回复

最终回复走 OpenClaw bridge 入站请求的同步 HTTP response：

1. OpenClaw bridge 插件请求 AI4ALL `/openclaw/turn`。
2. AI4ALL 处理 turn 后返回 `OpenClawTurnResponse(reply=...)`。
3. 插件在 OpenClaw 进程内使用已有通道发送 reply。

这条链路快的原因不是 HTTP response 本身，而是发送发生在 OpenClaw 进程/插件侧，不需要每条消息重新握手。

### 2.2 慢路径：暂态/主动/欢迎语

这些消息没有入站 response 可搭车，必须由 AI4ALL 后端主动调用 OpenClaw Gateway：

- `app/turn_service.py::_make_tool_thinking_sender()`；
- `app/routers/web.py` 的 onboarding welcome；
- `app/proactive/messaging.py::send_proactive_text()`；
- `app/node_agent.py` 的 `/node/exec/send/text` 和 outbound pull。

这些调用最终都收敛到：

```text
app/openclaw_gateway.py::send_weixin_text()
  -> _run_gateway_call(method="send")
  -> subprocess.run([openclaw, "gateway", "call", "send", ...])
```

当前慢点是每条发送都启动一个 OpenClaw CLI 进程，由 CLI 新建 Gateway client、握手、发一条 request、退出。线上拆
时显示约 10s 花在请求到达 Gateway 前的建连/初始化阶段，真正 Gateway 处理约 100ms。

---

## 3. 目标与非目标

### 3.1 目标

- 将通道 B 单条发送延迟从约 10s 降到百毫秒级。
- 保持 `send_weixin_text()` 对外函数签名和返回/异常语义不变。
- 保持账号隔离不变量：`channel`、`accountId`、`sessionKey`、`to`、`idempotencyKey` 原样传给 Gateway。
- 保留 CLI fallback，确保 OpenClaw 协议或 WS 鉴权异常时可以快速回退。
- 改动集中在 OpenClaw Gateway transport 层，不改 turn 编排、不改 proactive 业务逻辑、不改 node routing。

### 3.2 非目标

- 不改 OpenClaw bridge 插件。
- 不改 `/openclaw/turn` response schema。
- 不把最终回复也改为主动发送；最终回复仍走现有快路径。
- 不在第一版支持远程公网 `ws://` Gateway。第一版只支持本机 loopback 或安全 `wss://`，生产先按本机 node
  进程连接本机 OpenClaw Gateway。
- 不重构 login start/wait/logout；第一版只加速 `send`。登录/登出仍可继续走 CLI，降低风险。

---

## 4. 推荐架构

### 4.1 Transport 分层

新增一个“可选持久 WS transport”，由 `send_weixin_text()` 选择：

```text
send_weixin_text()
  -> 构造 send params（保持现有逻辑）
  -> _run_send_gateway_call()
       -> if WS enabled: persistent_ws.call("send", params)
       -> if WS disabled or WS failed and fallback enabled: _run_gateway_call("send", params)
  -> _extract_send_result_error(result)（保持现有业务错误识别）
```

现有 `_run_gateway_call()` 保留，继续服务：

- `start_weixin_qr_login()`；
- `wait_weixin_qr_login()`；
- 作为 `send` fallback。

### 4.2 文件范围

建议改动文件：

| 文件 | 改动 |
|---|---|
| `app/config.py` | 增加持久 WS transport 配置项 |
| `.env.example` | 说明新配置和灰度开关 |
| `requirements.txt` | 显式增加已验证的 `websockets` 版本 |
| `app/openclaw_gateway.py` | 接入 WS transport，保留 CLI fallback 与错误解析 |
| `app/openclaw_gateway_ws.py`（建议新增） | 持久 Gateway WS client、配置解析、握手、请求发送、关闭连接 |
| `app/main.py` | 可选：startup warmup，shutdown close |
| `app/node_agent.py` 或 `scripts/run_access_node.py` | 可选：node-only 进程 shutdown close；第一版也可 lazy connect |
| `tests/test_openclaw_gateway.py` | transport 选择、fallback、错误语义回归 |
| `tests/test_openclaw_gateway_ws.py`（建议新增） | fake WS server/client 协议测试 |

---

## 5. 配置设计

### 5.1 建议配置项

新增到 `Settings`：

```python
openclaw_gateway_ws_enabled: bool = False
openclaw_gateway_ws_url: str = ""
openclaw_gateway_ws_token: str = ""
openclaw_gateway_ws_password: str = ""
openclaw_gateway_ws_config_path: str = "~/.openclaw/openclaw.json"
openclaw_gateway_ws_read_openclaw_config: bool = True
openclaw_gateway_ws_fallback_to_cli: bool = True
openclaw_gateway_ws_connect_timeout_ms: int = 3000
openclaw_gateway_ws_request_timeout_ms: int = 5000
openclaw_gateway_ws_protocol_version: int = 4
openclaw_gateway_ws_warmup_on_startup: bool = False
```

默认 `enabled=false`，保证发布后未显式开启时行为完全等同当前 CLI。

### 5.2 连接信息解析优先级

第一版建议按以下顺序解析：

1. `OPENCLAW_GATEWAY_WS_URL` 显式配置。
2. 若 `OPENCLAW_GATEWAY_WS_READ_OPENCLAW_CONFIG=true`，读取 `OPENCLAW_GATEWAY_WS_CONFIG_PATH`：
   - `gateway.tls.enabled=true` → `wss://127.0.0.1:<gateway.port>`；
   - 否则 → `ws://127.0.0.1:<gateway.port>`；
   - 端口解析与 OpenClaw CLI 对齐：`OPENCLAW_GATEWAY_PORT` > `gateway.port` > OpenClaw 默认端口 `18789`。
3. 不建议直接读取 `OPENCLAW_GATEWAY_URL`，避免与 OpenClaw CLI 的远程模式语义混淆；如要复用，应在文档里明确。

当前本机配置里 Gateway 端口不是 OpenClaw client 默认值，所以实现不能硬编码端口。

实现细节：

- `OPENCLAW_GATEWAY_WS_CONFIG_PATH` 默认 `~/.openclaw/openclaw.json`，读取前必须 `os.path.expanduser()`。
- 如果 OpenClaw config 文件不存在、JSON 非法，或读取到的字段类型不符合预期，WS transport 应抛
  `OpenClawGatewayError` 并由 fallback 兜底，不应影响 CLI path。
- 第一版只解析明文 token/password；如果 OpenClaw config 使用 SecretRef，Python 端不自行解析 SecretRef，应记录脱敏
  warning 并 fallback CLI。

### 5.3 鉴权信息解析优先级

1. `OPENCLAW_GATEWAY_WS_TOKEN` / `OPENCLAW_GATEWAY_WS_PASSWORD`。
2. 若允许读取 OpenClaw config：
   - `gateway.auth.mode == "token"` 读取 `gateway.auth.token`；
   - `gateway.auth.mode == "password"` 读取 `gateway.auth.password`；
   - `gateway.auth.mode == "none"` 不传 auth。
3. 若 token/password 都有值，显式配置优先；若同一来源同时出现 token 与 password，应优先遵循
   `gateway.auth.mode`，无法判断时抛 `OpenClawGatewayError` 并 fallback CLI。
4. 任何日志、异常、测试快照都必须脱敏 token/password。

### 5.4 `.env.example` 文案

建议放在现有 OpenClaw 配置附近：

```bash
# OPENCLAW_GATEWAY_WS_ENABLED=false # true 时 send_weixin_text 优先复用本机 Gateway WS 长连接；默认 false 保持 CLI 行为
# OPENCLAW_GATEWAY_WS_URL=ws://127.0.0.1:18790 # 留空则读取 ~/.openclaw/openclaw.json 的 gateway.port
# OPENCLAW_GATEWAY_WS_TOKEN= # 可显式配置；留空且 READ_OPENCLAW_CONFIG=true 时读取 OpenClaw gateway.auth.token
# OPENCLAW_GATEWAY_WS_PASSWORD= # password auth 模式；留空且 READ_OPENCLAW_CONFIG=true 时读取 OpenClaw gateway.auth.password
# OPENCLAW_GATEWAY_WS_READ_OPENCLAW_CONFIG=true # 是否允许读取 ~/.openclaw/openclaw.json 的 gateway.port/auth
# OPENCLAW_GATEWAY_WS_FALLBACK_TO_CLI=true # WS 失败时回落 openclaw gateway call send，便于灰度回滚
# OPENCLAW_GATEWAY_WS_CONNECT_TIMEOUT_MS=3000
# OPENCLAW_GATEWAY_WS_REQUEST_TIMEOUT_MS=5000
# OPENCLAW_GATEWAY_WS_PROTOCOL_VERSION=4
# OPENCLAW_GATEWAY_WS_WARMUP_ON_STARTUP=false
```

---

## 6. Gateway WS 协议细节

OpenClaw protocol v4 的 WebSocket 帧：

```json
{"type":"req","id":"...","method":"send","params":{...}}
{"type":"res","id":"...","ok":true,"payload":{...}}
{"type":"event","event":"tick","payload":{...}}
```

连接建立时，server 先发 challenge：

```json
{"type":"event","event":"connect.challenge","payload":{"nonce":"..."}}
```

client 必须先消费 `connect.challenge`，拿到非空 `payload.nonce` 后再发 `connect` request；未收到 challenge、nonce
为空或超时，都视为协议错误，关闭 socket 并 fallback CLI。

当前 OpenClaw `gateway-client/backend` + shared token/password 的 loopback path 不需要把 nonce 放进
`auth.nonce`，也不应自造不存在的 `auth.nonce` 字段。nonce 只在 device auth 路径进入 `device.nonce` 并参与签名；
第一版不实现 device auth。

client 随后发 `connect` request：

```json
{
  "type": "req",
  "id": "uuid",
  "method": "connect",
  "params": {
    "minProtocol": 4,
    "maxProtocol": 4,
    "client": {
      "id": "gateway-client",
      "displayName": "ai4all-bridge",
      "version": "ai4all",
      "platform": "linux",
      "mode": "backend",
      "instanceId": "process-uuid"
    },
    "caps": [],
    "role": "operator",
    "scopes": ["operator.write"],
    "auth": {"token": "<redacted>"}
  }
}
```

鉴权 params 形态：

```json
{"auth":{"token":"<redacted>"}}
{"auth":{"password":"<redacted>"}}
{"auth":{}}
```

`auth none` 时可省略 `auth` 或传空对象；实现应保持一种固定写法，便于测试。

握手成功时 `connect` response payload 是 `hello-ok`，需要校验：

- `payload.type == "hello-ok"`；
- `payload.protocol == 4`；
- `payload.features.methods` 包含 `send`；
- `payload.auth.scopes` 包含 `operator.write` 或 `operator.admin`。

重要：OpenClaw server 对 device-less shared-token client 的 scope 有本地 backend 旁路条件。Python client 必须满足：

- 连接本机 loopback；
- 无 browser `Origin` header；
- `client.id == "gateway-client"`；
- `client.mode == "backend"`；
- shared token/password 鉴权通过，或 auth none 在本机策略允许。

否则可能握手成功但拿不到 `operator.write`，随后 `send` 会被拒绝。

---

## 7. Python client 设计

### 7.1 依赖

`requirements.txt` 当前未声明 `websockets`；本地 `.venv` 实测为 `websockets==15.0.1`。实现时应显式加入已验证
版本：

```text
websockets==15.0.1
```

如实现前决定升级到 `websockets==16.0`，必须先验证 `websockets.sync.client.connect` API、超时行为和 fake socket
测试，避免依赖升级与业务改动混在一起。

建议使用 `websockets.sync.client.connect`，原因：

- 当前 `send_weixin_text()` 是同步函数；
- tool thinking 已经在后台线程发送，不应额外绑定 FastAPI event loop；
- proactive/node pull 本身也是同步发送；
- 同步 client + lock 的改动最小。

### 7.2 类接口

建议新增：

```python
class OpenClawPersistentGatewayClient:
    def call(self, *, method: str, params: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        ...

    def warmup(self) -> None:
        ...

    def close(self) -> None:
        ...
```

`app/openclaw_gateway.py` 暴露模块级 helper：

```python
def close_persistent_gateway_client() -> None:
    ...

def warmup_persistent_gateway_client() -> None:
    ...
```

便于 `main.py` 和 node 进程生命周期调用。

同文件内增加模块级单例工厂：

```python
_gateway_ws_client: Optional[OpenClawPersistentGatewayClient] = None
_gateway_ws_client_lock = threading.Lock()


def _persistent_gateway_client() -> OpenClawPersistentGatewayClient:
    global _gateway_ws_client
    with _gateway_ws_client_lock:
        if _gateway_ws_client is None:
            _gateway_ws_client = OpenClawPersistentGatewayClient(settings)
        return _gateway_ws_client
```

这个工厂锁只保护懒初始化；`OpenClawPersistentGatewayClient` 内部 `RLock` 保护 socket 生命周期和 request/response
串行，两者是不同锁。

### 7.3 并发模型

第一版建议简单可靠：

- 一个进程一个 client 实例；
- 一个 `threading.RLock` 包住 “connect if needed → send request → recv matching response”；
- 同一时刻只允许一个 WS request in flight；
- 收到 `event/tick` 时忽略并继续等当前 response；
- 收到非当前 id 的 response，视为协议异常，关闭连接并 fallback。

理由：

- 当前暂态/主动发送量不高；
- 串行化后不需要 receiver thread、pending map、复杂取消逻辑；
- 单条发送目标是百毫秒级，串行不会成为第一版瓶颈。

后续若主动消息 QPS 上升，再升级为：

- 单独 receiver thread；
- pending request map；
- 多 request 并发复用同一 socket。

### 7.4 连接与重连

`call()` 流程：

1. 若 WS disabled，直接抛 `OpenClawGatewayWsDisabled` 或返回 fallback。
2. 获取 lock。
3. 若 socket 不存在或已关闭，建立连接并握手。
4. 发送 request frame：

   ```json
   {"type":"req","id":"uuid","method":"send","params":{...}}
   ```

5. 在 `min(timeout_ms, OPENCLAW_GATEWAY_WS_REQUEST_TIMEOUT_MS)` 或业务传入 timeout 内等 response。
6. `ok=true` 返回 `payload`；payload 必须是 dict，否则抛 `OpenClawGatewayError`。
7. `ok=false` 转成 `OpenClawGatewayError`，保留 `error.message`。
8. 任意 socket/JSON/protocol/timeout 错误：
   - close 当前 socket；
   - 记录脱敏 warning；
   - 统一转换为 `OpenClawGatewayError`；
   - 若 fallback enabled，走 CLI；
   - 否则抛 `OpenClawGatewayError`。

重要：`call()` 不应把 `ConnectionRefusedError`、`OSError`、`TimeoutError`、`websockets` 底层异常、
JSON decode 异常或协议校验异常裸抛给 `send_weixin_text()`；否则 `_run_send_gateway_call()` 无法触发 CLI fallback。

### 7.5 TLS 与安全限制

第一版建议在 URL 校验上保守：

- `ws://127.0.0.1:*`、`ws://localhost:*`、`ws://[::1]:*` 允许；
- `wss://...` 允许；
- 非 loopback 的 `ws://` 默认拒绝；
- 如未来要支持内网明文，需要单独 break-glass 配置，不在第一版默认打开。

如果读取到 `gateway.tls.enabled=true` 并生成本机 `wss://127.0.0.1:<port>`，需要二选一明确实现：

1. 首版实现最小 TLS 兼容：读取 OpenClaw gateway TLS cert，做 pinned fingerprint/自签证书校验；
2. 首版暂不支持 local TLS：检测到 local `wss://` 后抛 `OpenClawGatewayError` 并 fallback CLI。

不能直接用 Python 默认 TLS 校验硬连本机自签 `wss://`，否则灰度时会表现为 WS 总是失败。

---

## 8. 接入 `send_weixin_text()`

现有参数构造必须保持：

```python
params = {
    "channel": resolved_channel,
    "to": target,
    "message": message,
    "idempotencyKey": resolved_idempotency_key,
}
if account_id: params["accountId"] = account_id.strip()
if session_key: params["sessionKey"] = session_key.strip()
```

新增内部 helper：

```python
def _run_send_gateway_call(*, params: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
    if settings.openclaw_gateway_ws_enabled:
        try:
            return _persistent_gateway_client().call(
                method="send",
                params=params,
                timeout_ms=timeout_ms,
            )
        except OpenClawGatewayError:
            if not settings.openclaw_gateway_ws_fallback_to_cli:
                raise
            logger.warning("persistent OpenClaw Gateway send failed; falling back to CLI")
    return _run_gateway_call(method="send", params=params, timeout_ms=timeout_ms)
```

然后 `send_weixin_text()` 改为调用 `_run_send_gateway_call()`。

业务错误识别继续放在 `send_weixin_text()` 末尾：

- 有 `messageId` 视为成功；
- `ret=-2` 或 `rate limited` 文案抛 `OpenClawRateLimited`；
- 其它非零业务码抛 `OpenClawGatewayError`。

这样 proactive 的限速重试逻辑不需要改。

---

## 9. 生命周期接入

### 9.1 Central / standalone FastAPI

在 `app/main.py`：

- startup：
  - 若 `openclaw_gateway_ws_warmup_on_startup=true`，调用 `warmup_persistent_gateway_client()`；
  - 因 `websockets.sync.client.connect()` 会阻塞，必须在 async startup hook 中用
    `await asyncio.get_running_loop().run_in_executor(None, warmup_persistent_gateway_client)` 包裹；
  - warmup 失败只 warning，不阻止服务启动，因为 CLI fallback 可用。
- shutdown：
  - 调用 `close_persistent_gateway_client()`。

### 9.2 node-only agent

node-only 由 `scripts/run_access_node.py` 启动，当前 `create_node_agent_app()` 没有显式 shutdown hook。

第一版可接受 lazy connect + 进程退出自然关闭 socket；更完整的做法：

- 在 `create_node_agent_app()` 内注册 shutdown event；
- 或在 `scripts/run_access_node.py` 的 `finally` 中调用 `openclaw_gateway.close_persistent_gateway_client()`。

推荐后者，改动小，并覆盖 pull loop 与 exec app 共用的同一进程。

`scripts/run_access_node.py` 已有 `finally`，实现时只需在其中补关闭调用；关闭失败只 warning，不应阻止进程退出。

---

## 10. 测试计划

### 10.1 单元测试：openclaw_gateway

扩展 `tests/test_openclaw_gateway.py`：

1. `WS disabled`：
   - patch `subprocess.run`；
   - 断言仍走 CLI；
   - 现有测试应基本不变。

2. `WS enabled success`：
   - patch `_persistent_gateway_client().call` 返回 `{"messageId": "m-1"}`；
   - 断言不调用 `subprocess.run`；
   - 断言 send params 与旧 CLI params 完全一致。

3. `WS enabled fallback`：
   - WS call 抛 `OpenClawGatewayError("closed")`；
   - fallback enabled；
   - 断言最终走 CLI 并成功。

4. `WS enabled no fallback`：
   - fallback disabled；
   - 断言直接抛 WS error。

5. `业务错误语义不变`：
   - WS 返回 `{"ret": -2, "errmsg": "rate limited"}`；
   - 断言仍抛 `OpenClawRateLimited`。

### 10.2 单元测试：WS client

新增 `tests/test_openclaw_gateway_ws.py`，优先用 fake socket 对象测试，不依赖真实 OpenClaw：

- 收到 `connect.challenge` 后发送 `connect` frame；
- `connect.challenge.payload.nonce` 必须被消费：缺失、空值或超时应关闭 socket 并抛 `OpenClawGatewayError`；
- `connect` params 包含 `gateway-client/backend/operator.write/protocol=4`；
- `connect` params 的 `minProtocol/maxProtocol` 使用 `openclaw_gateway_ws_protocol_version`；
- token/password/none 三种 auth params 形态都覆盖；
- `hello-ok` 缺 `operator.write` 时失败；
- `features.methods` 不含 `send` 时失败；
- `send` response `ok=true` 返回 payload；
- `send` response `ok=false` 抛 `OpenClawGatewayError` 且错误文案来自 Gateway；
- JSON 非法、未知 frame、timeout 时关闭 socket；
- `tick` event 在等待 response 时被忽略。

如 fake socket 覆盖不足，再补一个轻量 fake WS server 集成测试，但避免让测试依赖本机真实 OpenClaw。

### 10.3 现有链路回归

聚焦运行：

```bash
.venv/bin/pytest tests/test_openclaw_gateway.py tests/test_openclaw_gateway_ws.py -v
.venv/bin/pytest tests/test_node_agent.py tests/test_tool_thinking.py tests/test_proactive_outbound.py -v
```

如果改了 `main.py` startup/shutdown，再补跑相关 FastAPI client 测试。

### 10.4 线上验证

在单个 node 灰度：

1. 设置：

   ```bash
   OPENCLAW_GATEWAY_WS_ENABLED=true
   OPENCLAW_GATEWAY_WS_FALLBACK_TO_CLI=true
   OPENCLAW_GATEWAY_WS_WARMUP_ON_STARTUP=false
   ```

2. 重启 node agent / backend。
3. 用测试账号触发 web_search。
4. 观察日志：
   - `tool_thinking_dispatch` 到 `tool_thinking_sent`；
   - `node_send_text received` 到 `node_send_text done`；
   - 新增 WS transport 日志。
5. live smoke 时额外打印一次 WS `send` 原始 response payload，并用同一 params 跑一次 CLI：

   ```bash
   openclaw gateway call send --json --params '<same-json>'
   ```

   对比 `messageId`、`ret`、`errmsg`、`error` 等字段是否与 `_extract_send_result_error()` 兼容。
6. 预期：
   - 暂态消息先于最终搜索结果；
   - `openclaw_ms` 从约 10,000ms 降到约 100-300ms；
   - 无 fallback warning。

---

## 11. 灰度与回滚

### 11.1 灰度顺序

1. 本地开发环境：fake WS tests + CLI fallback tests。
2. aliyun2 node-only：只打开 WS transport，保留 fallback。
3. aliyun1 central+node：打开 WS transport，保留 fallback。
4. 观察 24 小时：
   - 暂态消息顺序；
   - proactive 发送成功率；
   - fallback 次数；
   - OpenClaw rate limit 错误是否仍能被识别。
5. 稳定后可考虑将 `OPENCLAW_GATEWAY_WS_ENABLED=true` 作为生产默认配置，但代码默认仍建议保持 false。

### 11.2 回滚

最快回滚：

```bash
OPENCLAW_GATEWAY_WS_ENABLED=false
```

或保留 enabled 但强制 fallback：

```bash
OPENCLAW_GATEWAY_WS_FALLBACK_TO_CLI=true
```

如果 WS 连接异常但 fallback 正常，用户影响应退回到当前“慢但可发”的状态。

---

## 12. 日志与可观测性

建议新增低噪声日志：

- WS connect success：
  - gateway url 脱敏；
  - protocol；
  - granted scopes；
  - methods 是否含 send。
- WS send result：
  - method；
  - elapsed_ms；
  - transport=`ws` 或 `cli_fallback`；
  - account_id/to_user_id 可按现有日志习惯保留，但不记录消息正文。
- WS failure：
  - error type；
  - close code/reason；
  - fallback 是否发生；
  - 不输出 token/password。

后续如接入 metrics，可加：

- `openclaw_gateway_send_transport_total{transport="ws|cli|fallback"}`；
- `openclaw_gateway_send_latency_ms`；
- `openclaw_gateway_ws_reconnect_total`；
- `openclaw_gateway_ws_auth_failure_total`。

---

## 13. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| OpenClaw protocol drift | WS client 握手失败或 send 失败 | 固定 protocol v4，握手校验，失败 fallback CLI；升级 OpenClaw 前跑 fake/live smoke |
| 鉴权 scope 被降级 | `send` 被拒绝 | 必须使用 `gateway-client/backend` + loopback；校验 `operator.write`，否则不进入发送 |
| Gateway 重启 | socket 断开，本轮发送失败 | close socket，fallback CLI；下次 call 重连 |
| token 轮换 | 持久连接继续可用但新连接失败 | 失败时重新读取 config/env；记录脱敏错误；运维重启进程可刷新 |
| SecretRef 鉴权配置 | Python 端无法解析 OpenClaw SecretRef | 第一版只解析明文/env；SecretRef 命中时脱敏 warning 并 fallback CLI |
| local TLS / 自签证书 | Python 默认 TLS 校验失败，WS 总是 fallback | 首版明确支持 pinned cert/fingerprint，或检测 local `wss://` 后直接 fallback CLI |
| 并发发送 | 单 socket 多线程读写错乱 | 第一版用全局 lock 串行 request/response |
| proactive QPS 上升 | 串行发送可能成为瓶颈 | 第二版引入 receiver thread + pending map |
| 生产环境依赖缺失 | import websockets 失败 | requirements 显式 pin 已验证版本 `websockets==15.0.1`，启动/测试覆盖 |
| 非 loopback 明文 WS | 凭证和消息可能泄露 | 默认拒绝，远程只允许 wss 或后续单独 break-glass |

---

## 14. 建议实施顺序

1. 新增配置与 `.env.example`，不改变运行行为。
2. 新增 `app/openclaw_gateway_ws.py`，完成配置解析、握手、`call()`、`close()`。
3. 给 WS client 写 fake socket 单测。
4. 在 `openclaw_gateway.py` 中只对 `send` 接入 WS-first/CLI-fallback。
5. 扩展 `tests/test_openclaw_gateway.py` 覆盖 transport 选择与 fallback。
6. 可选接入 `main.py` shutdown/warmup。
7. 接入 node-only 退出关闭连接。
8. 跑聚焦测试。
9. 本地或测试机用真实 OpenClaw 做 live smoke：

   ```bash
   .venv/bin/python - <<'PY'
   from app.openclaw_gateway import send_weixin_text
   # 使用测试账号/测试 peer，避免误发真实用户。
   PY
   ```

   live smoke 必须保留一次 WS 原始 payload 与 CLI `openclaw gateway call send --json --params '<same-json>'`
   的字段比对结果，确认 `messageId`、`ret`、`errmsg`、`error` 等字段仍被现有错误识别逻辑覆盖。

10. aliyun2 灰度，观察日志与消息顺序。

---

## 15. 暂不做但应记录

- 不在第一版支持 `web.login.start` / `web.login.wait` 的 WS transport。它们不是本次 10s 暂态顺序问题的主路径，
  且 wait 是长轮询，放进持久连接会增加复杂度。
- 不做 Node sidecar。sidecar 可复用 OpenClaw 官方 GatewayClient，协议漂移风险更低，但要新增进程管理和 IPC，
  第一版不划算。
- 不做流式 turn。除非未来希望 bridge 插件统一承载“最终回复前的多个事件”，否则本问题用持久后端连接更直接。
