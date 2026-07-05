# TDAI 长期记忆 — 上线操作指引

> 状态：上线操作手册（runbook，可直接照抄）
> 日期：2026-06-30
> 关联：`docs/tech_design/tdai_integration_preflight.md`（接入前确认稿，含边界/容量论证）
> 适用：AI4ALL 微信 Bot 接入本地 fork 的 TDAI Gateway 多租户版本，两台厚节点各一套 sidecar。

本文是**可照做的上线步骤**，不重复 preflight 的设计论证。第一阶段范围：被动 `/recall` + after-turn `/capture` + 解绑 `/namespace/wipe`，不接主动搜索工具、不做历史 seed。

---

## 部署坐标（照抄命令前先核对这张表）

| 项 | 值 | 备注 |
|---|---|---|
| TDAI fork 仓库 | `git@github.com:huanshanxiaoyao/TencentDB-Agent-Memory.git`（remote `origin`） | **不要**用 `upstream`（腾讯官方，无多租户） |
| 部署分支 | `custom-multitenant` | 多租户改造分支 |
| **部署 tag（锁版本）** | **`v0.3.6-ai4all-multitenant`** | 已推到 origin；两台节点都 checkout 这个 tag |
| Gateway 监听 | `127.0.0.1:8420` | 只绑本地，AI4ALL 同机调用 |
| 每台节点数据目录 | 跟随**运行 gateway 的用户**：用户级托管（无 sudo，如 aliyun2）用 `~/.local/share/ai4all/tdai`；系统级托管（sudo，如 aliyun1 central）可用 `/var/lib/ai4all/tdai`（须 `chown` 给服务用户） | **两节点各自独立，禁止共享**。`/var/lib` 默认 root 拥有、普通用户不可写，纯 node 机不要照抄 |
| 鉴权 key | 生产用**强随机 secret**，两边完全一致 | 开发机验证时用的是弱 key `tdai1234`，**生产务必替换**。aliyun2 当前临时用 `aliyun2025`（待统一换强 key 时两侧同步） |
| Node | ≥ 22.16（aliyun2 实测系统 `node v24.16` OK；开发机 v22.22 OK） | `package.json engines.node>=22.16.0` |
| 包管理 | **`npm install`**（不是 `npm ci`） | registry 走 `npmmirror`（国内快）。注：`v0.3.6` tag 实际含 `package-lock.json`，但仍用 `npm install` 即可（`@node-rs/jieba` 走预编译二进制，已实测自动拉取） |

> 开发机参考路径（核对/排查用）：TDAI = `/Users/suchong/workspace/TencentDB-Agent-Memory`，AI4ALL = `/Users/suchong/workspace/ai4all/weixin_bot`。生产 AI4ALL 后端是 PostgreSQL（`DATABASE_URL` 非空）。

**两边 key 必须一致**：TDAI 侧 `.env` 的 `TDAI_GATEWAY_API_KEY` == AI4ALL 侧 `.env` 的 `TDAI_GATEWAY_API_KEY`。不一致 → gateway 返回 401 → recall/capture 全部静默降级。

---

## 生产部署进度（按节点）

> 实测记录，aliyun1 照此对照即可。

| 节点 | TDAI 侧（阶段1） | AI4ALL 侧（阶段2） |
|---|---|---|
| **aliyun2**（node 角色，用户级托管） | ✅ **已完成 2026-06-30**：fork@`v0.3.6-ai4all-multitenant`(`1610504`) at `/home/jack/workspace/TencentDB-Agent-Memory`；`npm install` OK；`.env`(DATA_DIR=`~/.local/share/ai4all/tdai`，key=`aliyun2025`)；`systemctl --user tdai-gateway.service`(enabled, Linger=yes)；/health 四项达标、401 负向、官方 smoke ✅ PASS | ✅ **已完成 2026-06-30**：`TDAI_ENABLED=true` + 同 key；recall allowlist=`aid_956326343,aid_806382741`(本机有 956326343)；capture 全量。组织化 E2E 已验证：真实多轮 capture/recall 均 200、注入真实 persona+scene、warm 165–276ms |
| **aliyun1**（central,node，**实测用户级托管**） | ✅ **已完成 2026-06-30**：同 fork/tag at `/home/jack/workspace/TencentDB-Agent-Memory`；`npm install` OK(postinstall 的 OpenClaw `[FAIL]` 属预期无害，已 `\|\| true` 吞掉)；`.env`(DATA_DIR=`~/.local/share/ai4all/tdai`，**强随机 key 48hex**，非弱 key)；`systemctl --user tdai-gateway.service`(enabled, Linger=yes，**单元无代理行=直连**)；/health 四项达标、401 负向、正向 200、官方 smoke ✅ PASS | ✅ **已完成 2026-06-30**：先 `git ff` 到 main(补 3 个 TDAI 提交，FF 前缺 `tdai_client.py`)→`test_tdai_client` 7 passed→`TDAI_ENABLED=true` + 同 key→`systemctl --user restart backend`(干净恢复、/health ok)。⚠️ **recall 在本机暂为「暗」**：allowlist 两账号都不在 aliyun1(956326343 在 aliyun2、806382741 仅 dev SQLite)，故 recall 不触发；capture 全量(61 账号)。组织化 recall 验证待挑一个真实 aliyun1 账号入名单(改真实用户回复行为，需决策) |

**两节点相对本手册的偏差（实测）：**
- **进程托管**：两机**都是 `systemctl --user`**（用户级，无 sudo），非手册默认的系统级 systemd。aliyun1 经本次实测确认为用户级，**与 `deployment_diff.md` S7「sudo 系统级」的说法不符——以实测为准**（memory `llm-timeout-and-aliyun1-svc` 同此）。gateway 单元随之放 `~/.config/systemd/user/`，并 `loginctl ... Linger=yes`。
- **数据目录**：两机 `/var/lib` 均不可写、无免密 sudo → 都用 `~/.local/share/ai4all/tdai`。
- **密钥来源**：DeepSeek key = AI4ALL `.env` 的 `LLM_API_KEY`（base=`api.deepseek.com`）；DashScope key = AI4ALL `.env` 的 `DASHSCOPE_API_KEY`。直接 shell 内同步，不另找。注意 TDAI 侧 `TDAI_LLM_MODEL` 固定 `deepseek-chat`，不跟随 AI4ALL 的 `LLM_MODEL`（aliyun1 是 `deepseek-v4-pro`）。
- **出网（两机相反，务必实测）**：
  - **aliyun2**：DeepSeek 直连不通 → gateway 单元带 `HTTP(S)_PROXY=http://127.0.0.1:7890` + `NO_PROXY=…,.aliyuncs.com`（DeepSeek 走 clash、DashScope 直连）。
  - **aliyun1**：DeepSeek+DashScope **都能直连且更快**（实测 DeepSeek 直连 0.30s vs 代理 1.55s）→ gateway 单元**不放任何代理行**，systemd user 环境也无 proxy，直连。
  - 结论：部署前用 `curl --noproxy '*'` vs `-x http://127.0.0.1:7890` 实测两个 endpoint 再定单元是否带代理。
- **鉴权 key**：aliyun2 临时弱 key `aliyun2025`；**aliyun1 已用 `openssl rand -hex 24` 强随机 key**（48 hex，仅本机 localhost，TDAI↔AI4ALL 两侧一致）。key 为每节点独立，无需跨机一致。
- **embedding 配置**：仓库自带 `tdai-gateway.yaml` 已是 DashScope `text-embedding-v3` + `recall.strategy: hybrid`，**无需新建/改动**，只要 `.env` 提供 `DASHSCOPE_API_KEY`。
- **代码升级（aliyun1 特有）**：aliyun1 backend 代码 FF 前停在 PR#5、**不含 TDAI 接入**（`app/tdai_client.py` 缺失）。Phase 2 前必须先 `git fetch && git merge --ff-only origin/main` 补齐 3 个 TDAI 提交，并 `pytest tests/test_tdai_client.py` 验证后再重启。aliyun2 当时已在含 TDAI 的代码上，无此步。

---

## 0. 开发机已验证的事实（生产照搬即可）

上线前已在开发机完成配置与全链路验证，结论：

| 验证项 | 结果 |
|---|---|
| gateway 鉴权开启（`TDAI_GATEWAY_API_KEY`） | 无 key→401、错 key→401、正确 key→200；`/health` 仍开放 |
| AI4ALL→gateway 带鉴权调用链 | recall/capture 全部 200，无 `Illegal header value` |
| 端到端真实 turn（测试账号） | recall 命中并注入，回复正确使用召回记忆 |
| 账号隔离 | 所有调用限定 `ai4all:{account_id}`，不串号 |
| 官方 smoke（带 key） | ✅ PASS — vector recall LIVE（无关键词改写也召回） |
| 单元测试 `tests/test_tdai_client.py` | 7 passed |
| recall 降级 | gateway 停止时主对话正常回复，不阻塞 |

**实测 recall 延迟（开发机，DashScope embedding + hybrid）**：单账号 warm 状态 6 次采样 213/220/265/350/455/863 ms，中位 ~350 ms，约 1/6 超 500 ms。延迟几乎全部是 **DashScope 向量 embedding 网络往返**（`vec=600~750ms` 在冷态、`~200~450ms` 在 warm），抖动大。日志可见 `FTS5 unavailable → skipping keyword`，当前每次 recall 都走纯向量、必打 DashScope。

**含义（务必知晓）**：`TDAI_RECALL_TIMEOUT_SECONDS=0.5` 覆盖中位但会切掉长尾与冷命中，**recall 不是每轮 100% 注入**。这是设计内的安全降级（超时即不注入、绝不阻塞回复），不是 bug。调高超时能提命中率，但 recall 在热路径是**同步阻塞**的——超时越大，DashScope 慢时每条回复等得越久。权衡见 §6。

---

## 1. TDAI 侧上线步骤（先于业务侧就绪）

### 1.1 版本锁定（关键，别上错版本）

- 必须用含多租户改造的本地 fork（`Map<session_key,TdaiCore>` + per-account dataDir 隔离），**不能用上游官方版**（官方版无多租户，会串号 / `/seed` 误用）。
- 已打好并推到 origin 的部署 tag：**`v0.3.6-ai4all-multitenant`**（commit `1610504`）。两台节点都按此 tag 部署，不要直接用分支 HEAD（防后续提交意外带入）。

```bash
# 每台节点：按 tag 克隆（detached HEAD，锁定版本）
git clone git@github.com:huanshanxiaoyao/TencentDB-Agent-Memory.git
cd TencentDB-Agent-Memory
git checkout v0.3.6-ai4all-multitenant
git describe --tags          # 应输出 v0.3.6-ai4all-multitenant，确认版本正确
```

### 1.2 运行环境要求

- Node ≥ 22.16
- 已安装 `@node-rs/jieba`
- `memory.storeBackend=sqlite`（多租户下**不得**用 `tcvdb`）

### 1.3 生产 `.env`（每台机器独立，禁止两进程共享 dataDir）

```env
TDAI_MULTI_TENANT=true
TDAI_DATA_DIR=~/.local/share/ai4all/tdai     # 用户级节点用户可写路径（aliyun2 实采）；系统级托管可用 /var/lib/ai4all/tdai（chown 给服务用户）
TDAI_GATEWAY_HOST=127.0.0.1                  # 只绑本地，不对外
TDAI_GATEWAY_PORT=8420
TDAI_GATEWAY_API_KEY=<生产强随机 secret>      # 必须非空；与业务侧完全一致；开发机用的是 tdai1234，生产务必换强 key（aliyun2 当前临时 aliyun2025）
# 以下两个 key 直接同步自 AI4ALL .env：LLM_API_KEY（其 base=api.deepseek.com 即 DeepSeek）、DASHSCOPE_API_KEY
TDAI_LLM_BASE_URL=https://api.deepseek.com/v1
TDAI_LLM_API_KEY=<= AI4ALL .env 的 LLM_API_KEY>
TDAI_LLM_MODEL=deepseek-chat                  # 或 deepseek-v4-flash + TDAI_LLM_DISABLE_THINKING=deepseek-v4
DASHSCOPE_API_KEY=<= AI4ALL .env 的 DASHSCOPE_API_KEY>

# 容量参数（单台 ~1000 DAU、日均 10 条；论证见 preflight §2.2）
TDAI_MAX_CONCURRENT_EXTRACTIONS=4
TDAI_MAX_RESIDENT_CORES=200
TDAI_CORE_IDLE_TTL_MS=1800000
```

> 鉴权变量名以 gateway 代码 `src/gateway/config.ts` 为准：`TDAI_GATEWAY_API_KEY`。空值=auth 关闭（仅 dev 可接受）。

### 1.4 `tdai-gateway.yaml`

只放 env 不能覆盖的 embedding 配置：

- `memory.embedding.*` → DashScope `text-embedding-v3`（引用 `${DASHSCOPE_API_KEY}`）
- `memory.recall.strategy: hybrid`

### 1.5 安装依赖 + 启动（建议 systemd）

```bash
cd /path/to/TencentDB-Agent-Memory   # 已 checkout v0.3.6-ai4all-multitenant

# 1. 装依赖：用 npm install（不是 npm ci——lockfile 被 gitignore，新 clone 无 lockfile）。
#    @node-rs/jieba 是原生模块，必须在目标机（Linux x64）本地安装，不能从 macOS 拷 node_modules。
npm install

# 2. 放置 .env（§1.3）到仓库根目录并 chmod 600（含密钥）。
#    tdai-gateway.yaml 仓库已自带且配置正确（DashScope text-embedding-v3 + hybrid），无需新建/改动。

# 3. 启动
node --env-file=.env --import tsx src/gateway/server.ts
```

systemd 服务建议：`Restart=always`、`WorkingDirectory=/path/to/TencentDB-Agent-Memory`、
`ExecStart=/usr/bin/node --env-file=.env --import tsx src/gateway/server.ts`；
业务侧 AI4ALL 服务设 `After=tdai-gateway.service`（启动顺序：先 gateway 后 app）。

**用户级托管（纯 node，如 aliyun2 — 实采）**：单元放 `~/.config/systemd/user/tdai-gateway.service`，
`WantedBy=default.target`；启用 `systemctl --user enable --now tdai-gateway.service`；
确认 `loginctl show-user <user> -p Linger` 为 `Linger=yes`（否则登出即停）。
单元内按本机代理策略加 `Environment=HTTP_PROXY=…/HTTPS_PROXY=…/NO_PROXY=…,.aliyuncs.com`
（DeepSeek 走代理、DashScope 直连）。**若某机为系统级托管**（走 `sudo systemctl`），
DATA_DIR 可用 `/var/lib/ai4all/tdai` 并 `chown` 给服务用户。aliyun1 走哪种以**实测其 backend 托管层级**为准（见 §生产部署进度 的偏差说明）。

### 1.6 启动后健康检查 + smoke（必须 PASS 才放行业务）

```bash
# 健康：status=ok / multi_tenant=true / embedding.configured=true / recallStrategy=hybrid
curl -fsS http://127.0.0.1:8420/health

# 鉴权负向（无 key 应 401）
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8420/recall \
  -H "Content-Type: application/json" -d '{"query":"x","session_key":"ai4all:probe"}'   # 期望 401

# 官方 smoke（带 key，必须 ✅ PASS）
TDAI_GATEWAY_URL=http://127.0.0.1:8420 \
TDAI_GATEWAY_API_KEY=<key> \
node scripts/smoke-recall.mjs --timeout 120
```

---

## 2. 业务侧 AI4ALL 上线步骤（在 TDAI 就绪、smoke PASS 之后）

### 2.1 生产 `.env`

```env
TDAI_ENABLED=true
TDAI_GATEWAY_URL=http://127.0.0.1:8420            # 同机调用
TDAI_GATEWAY_API_KEY=<与 TDAI 侧完全一致>          # 开发机为 tdai1234；生产换强 key
TDAI_RECALL_ENABLED=true
TDAI_CAPTURE_ENABLED=true
TDAI_RECALL_ACCOUNT_ALLOWLIST=aid_956326343,aid_806382741   # 灰度账号运行时主键，逗号分隔；见 §2.3；空=recall 对所有账号关闭
TDAI_RECALL_TIMEOUT_SECONDS=0.5                   # 热路径同步超时；权衡见 §6
TDAI_CAPTURE_TIMEOUT_SECONDS=2.0
TDAI_RECALL_MAX_CHARS=2500
```

### 2.2 灰度策略

- `capture` **全量**（仅 onboarding 完成后的正常轮次）→ 让所有账号后台先攒 L0/L1/L2/L3。
- `recall` 只对 `TDAI_RECALL_ACCOUNT_ALLOWLIST` 内账号生效 → 先验证记忆质量，再逐步扩大名单。
- `allowlist` 只控 recall，**不控 capture**。

### 2.3 allowlist 必须用运行时真实主键（易错点）

- 生产经 binding 解析的真实微信账号是 **DB 短 id**（如 `aid_806382741`）。
- 本地 `send_mock_turn.py --sender X` 无 binding 时回落为 `openclaw-weixin:local:X`，与真实账号**是不同账号**。
- 生产 allowlist 填短 id 形态；别把本地 mock 形态带上去，否则永远不命中。

### 2.4 重启（配置不可热切换）

`tdai_enabled` 是**进程级配置**；`--reload` 不读 `.env`。改完必须**完全重启** app：

```bash
# 示例（生产按你的进程管理方式）
kill $(pgrep -f "uvicorn app.main:app")
.venv/bin/uvicorn app.main:app --port <prod-port>   # 或 systemctl restart
```

---

## 3. 上线顺序（硬依赖，不可颠倒）

```
1. 部署 TDAI fork（锁版本）→ 起 gateway → /health ok → 鉴权 401 负向 → smoke ✅ PASS
2. 才设置 AI4ALL TDAI_ENABLED=true + 同一 key → 完全重启 app
3. 用灰度账号验证（§4）
```

两台节点各自独立完成 1→2→3；节点 A、B 的 dataDir 互不共享。用户在哪台节点服务，记忆就落哪台。

---

## 4. 上线后验证清单

- [ ] `/health` 四项正常（status/multi_tenant/embedding.configured/recallStrategy）
- [ ] 无 key 调 `/recall` 返回 401（证明鉴权真的生效）
- [ ] 灰度账号真实对话一轮，gateway 日志见 `recall` 与 `capture` 均 200，无 401
- [ ] **降级兜底**：停掉 gateway，灰度账号仍能正常收到回复（recall/capture 失败不阻塞）
- [ ] 非 allowlist 账号不触发 recall（capture 仍全量）
- [ ] AI4ALL 日志无 `Illegal header value` / `tdai recall failed`

---

## 5. 回滚

按风险从小到大，任选其一即可，互不依赖：

1. **只关 recall**：`TDAI_RECALL_ENABLED=false`（或清空 allowlist）→ 重启 app。capture 继续攒料。
2. **整体关 TDAI**：`TDAI_ENABLED=false` → 重启 app。AI4ALL 回到接入前形态，无任何 TDAI 调用。
3. TDAI store 是**派生缓存**（权威数据在 AI4ALL `messages`）；某节点 dataDir 丢失可从新 turn 重新积累，后续可补 per-account seed 脚本从 `messages` 重建，无数据单点风险。

> TDAI gateway 本身故障不需要回滚 AI4ALL：recall/capture 失败均 best-effort 降级，主对话不受影响。

---

## 6. 已知特性与调参（务必周知）

### 6.1 recall 延迟与命中率

- 延迟主要是 DashScope embedding 往返，抖动大（warm ~200–450ms，冷命中/长尾 600–860ms）。
- `0.5s` 超时：覆盖中位、切掉长尾与冷命中，命中率非 100%，但**安全降级不阻塞回复**。
- 提命中率的两个方向：
  - **业务侧**：调高 `TDAI_RECALL_TIMEOUT_SECONDS`（如 0.8）。代价：recall 同步阻塞热路径，DashScope 慢时每条回复多等最多该时长。
  - **TDAI 侧（更优）**：让 `FTS5` 关键词检索可用，使命中关键词时不必每次都打 embedding；或加 query embedding 缓存。属第二阶段优化。

### 6.2 冷命中

账号 core 被 LRU 淘汰或刚重启后，首轮 recall 触发 SQLite 重载 + 首次 persona 编译（DeepSeek，秒级），必然超时降级，warm 后恢复。频繁冷命中影响体验时调高 `TDAI_MAX_RESIDENT_CORES` / `TDAI_CORE_IDLE_TTL_MS`。

### 6.3 监控指标

- **AI4ALL 侧**：recall/capture/wipe 的 status、latency、account_id、memory_count/strategy（recall 降级日志为 DEBUG 级，排查时需调低日志级别才可见）。
- **TDAI 侧**：`/health.extraction`（`waiting` 长期 >0 才加 `MAX_CONCURRENT_EXTRACTIONS`）、`/health.resident`、进程 RSS / CPU / 文件句柄。

### 6.4 隐私边界（第一阶段）

被 capture 的记忆文本会发往 DeepSeek（extraction/persona）与 DashScope（embedding）。AI4ALL 侧保守边界已落地：审核拦截、红线替换、生成失败、特殊命令、图片理解失败、onboarding 轮次均**不** capture。

---

## 7. 不在第一阶段范围

- 主动搜索工具 `tdai_memory_search` / `tdai_conversation_search`。
- 历史 seed 回灌（HTTP `/seed` 在多租户下已禁用；后续走 `scripts/seed_tdai_memory.py` 按账号从 `messages` 直灌 per-account dataDir）。
- session end / shutdown flush。
- 降低 `MEMORY.md` 在 prompt 中权重以避免与 TDAI recall 重复。
