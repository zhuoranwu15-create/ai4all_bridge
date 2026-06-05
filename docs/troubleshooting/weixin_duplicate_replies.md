# 微信重复回复排查（同一条消息被双投递）

**症状**：用户反馈“每次都连续回我两条相似的，是不是坏掉了”。同一条入站消息，bot 连续生成两条内容相近的回复。

**首例**：2026-06-05，账号 `aid_212899436`（OpenClaw 侧 `a24d30815652-im-bot`）。本文据该次排查整理，供后续同类问题快速定位。

---

## TL;DR

- **根因不在本仓库（ai4all_bridge）**，在 OpenClaw 的 `openclaw-weixin` 监听层：health-monitor 重启时把同一账号的 weixin monitor **多启动了一个**，两个并发监听从**同一个 sync 游标**重复拉取同一条微信消息，于是每条消息被投递两次、转发两次、回复两次。
- **一句话判断**：在 OpenClaw 日志里找 **double-start 签名**——同一账号在几秒内出现 ≥2 次 `Monitor started`（且只跟一次 `Monitor ended`）。命中即监听泄漏；所有重复都恰好发生在该账号“≥2 个监听并发”的时间窗内。
- **止血**：`openclaw gateway restart`（systemd `--user` 服务，**无需 sudo**），重启后每账号监听收敛回 1。
- **注意**：重启是**临时止血**，OpenClaw 的 double-start 竞态会复发。根因修复在 OpenClaw 侧。

---

## 一、30 秒定位

OpenClaw 网关日志：`/tmp/openclaw/openclaw-YYYY-MM-DD.log`（每行一个 JSON，字段 `time` 本地时间、`1` 为消息体）。

### 1. 链路定界：重复在 bridge 之前还是之后？

```bash
LOG=/tmp/openclaw/openclaw-$(date +%F).log
echo "channel 层入站收取:"; grep -c "inbound message: from=" "$LOG"
echo "bridge 转发:";        grep -c "ai4all bridge forwarding turn" "$LOG"
```

两者**相等（1:1）** ⇒ bridge 不重复、后端不重复，重复发生在 **openclaw-weixin 监听/接收层**（bridge 上游）。这是与“多 worker / 多插件 / 后端重复处理”区分的关键。

### 2. 确认监听泄漏：double-start 签名（核心判据）

```bash
LOG=/tmp/openclaw/openclaw-$(date +%F).log
python3 - "$LOG" <<'PY'
import json,re,sys
from datetime import datetime
sr=re.compile(r"\[([^\]]+)\] Monitor started:")
def pt(s): return datetime.strptime(s[:23],"%Y-%m-%dT%H:%M:%S.%f")
last={}; hits=[]
for line in open(sys.argv[1],encoding="utf-8"):
    try: r=json.loads(line)
    except: continue
    m=sr.search(str(r.get("1","")))
    if not m: continue
    a=m.group(1); t=r.get("time","")
    if a in last and 0<=(pt(t)-pt(last[a])).total_seconds()<=5:
        hits.append((a,last[a],t))      # 同账号 5s 内连开两次 = 泄漏
    last[a]=t
print(f"double-start 事件: {len(hits)}")
for a,t1,t2 in hits: print(f"  {a}  {t1} -> {t2}")
PY
```

任一命中即说明该次重启把某账号监听**多开了一个**，此后该账号常驻 2 个并发监听、持续双发。

> **不要用累计 `started - ended` 差值判断**：`openclaw gateway restart` 是 SIGKILL 旧进程，旧监听不会打印 `Monitor ended`，会让全天累计差值**虚高**，止血后仍显示“泄漏”。double-start 签名不受重启影响，是可靠判据。
>
> `openclaw channels status` 只显示每账号一条 “running”，**看不到**泄漏的进程内监听副本。

### 3.（可选）逐事件确认“重复落在并发窗内”

把某账号的 `Monitor started/ended` 与 `inbound message` 按时间排序，维护在线计数，凡 `inbound` 发生在 `live>1` 时即为重复风险：

```bash
LOG=/tmp/openclaw/openclaw-$(date +%F).log ACCT=a24d30815652-im-bot
python3 - "$LOG" "$ACCT" <<'PY'
import json,re,sys
LOG,A=sys.argv[1],sys.argv[2]
sr=re.compile(r"\[([^\]]+)\] Monitor started:"); er=re.compile(r"\[([^\]]+)\] Monitor ended")
rr=re.compile(r"\[([^\]]+)\] inbound message: from=")
ev=[]
for line in open(LOG,encoding="utf-8"):
    try: r=json.loads(line)
    except: continue
    msg=str(r.get("1","")); t=r.get("time")
    for rx,k in ((sr,"START"),(er,"end"),(rr,"inbound")):
        m=rx.search(msg)
        if m and m.group(1)==A: ev.append((t,k)); break
ev.sort()
live=0
for t,k in ev:
    if k=="START": live+=1
    elif k=="end": live=max(0,live-1)
    mark="  <== DUP-RISK (live>1)" if (k=="inbound" and live>1) else ""
    print(f"{t}  {k:8} live={live}{mark}")
PY
```

首例输出：`06:08 START live=1`（单监听窗，零重复，对照组）→ `09:58:47` 一个 `end` 却跟着**两个 `START`** → `live=2`，此后整天卡在 2，所有 inbound 均 `live=2`。

---

## 二、根因机制

1. 触发点：`09:58:46` 全机群 `[openclaw-weixin:<acct>] health-monitor: restarting (reason: stopped)`。
2. 该重启对每个账号打出**两轮** `starting weixin provider` → `weixin monitor started`（间隔约 0.2s），却只有**一条** `Monitor ended` ⇒ 净泄漏 1 个监听。
3. 两个新监听都打印 `[weixin] resuming from previous sync buf (N bytes)`，即**从同一个微信 sync 游标续传**。
4. 此后两个并发监听各自独立轮询（`Monitor timeoutMs=35000`）同一账号，每条微信消息被**两个监听各取一次** ⇒ 双投递。
5. 后续每次重启都是平衡的 `end`+`START`（2→1→2），泄漏的那 1 个始终在，直到整进程重启。

可变的重复间隔（实测 0.2–28s）正由两个监听的轮询相位错开多少决定。

---

## 三、为什么后端没兜住（现状）

真实微信流量入库的 `messages.message_id` **恒为 NULL**，导致后端两道幂等全部空转：

- bridge 在 `before_agent_reply` 用 `message_id = ctx.runId`，但该 hook 的 `ctx` **没有 runId**（实测只有 `agentId/channelId/messageProvider/sessionId/sessionKey/trigger/workspaceDir`），`event` 只有 `cleanedBody`。
- `app/turn_service.py` `get_duplicate_reply()`：`reply_to_message_id` 为空直接 `return None`。
- `app/db.py` 唯一索引 `ux_messages_account_message` 是**部分索引**（`WHERE message_id IS NOT NULL AND != ''`），NULL 不约束，`insert_message` 永不冲突。

> 推论：即便部署 bridge 的 ID 诊断日志（`ids=...`），因 `event` 只有 `cleanedBody`，这些字段也只会是 `undefined`——能**证明** id 缺失，但**不能发现** monitor 双启。发现双启只能靠第一节的 started/ended 计数。

---

## 四、处置

### 止血（运维，立即）

```bash
# 网关为 systemd --user 服务（openclaw-gateway.service），无需 sudo
openclaw gateway restart
sleep 12
openclaw gateway status        # 期望 Runtime: running、Connectivity probe: ok
# 复跑第一节第 3 步的逐事件 live 追踪，确认止血后每账号 live 收敛到 1（最新事件为单个 START）
# 并复跑第 2 步 double-start 检测，确认重启那一刻没有再次 double-start
```

重启会短暂断开全部账号几秒；weixin provider 用持久化 sync buf 续传，消息基本不丢。

> double-start 是**竞态、间歇性**的：首例当天 `15:55` 手动重启后，`16:06` health-monitor 又触发了一次全机群重启却**没有**double-start。所以一次干净重启通常能止血，但不能保证不复发——根因仍需在 OpenClaw 侧修。

### 根因修复（OpenClaw 侧，未在本仓库）

- 修 `openclaw-weixin` health-monitor 重启路径的 **double-start 竞态**：重启必须“先确保旧监听 teardown、再 start”，并为每账号监听加**唯一性互斥**（同账号同时只允许 1 个 monitor）。
- 监控建议：对 `Monitor started` 累计 > `Monitor ended` 即告警（无需改业务代码，纯日志巡检）。

### 后端兜底（本仓库，已评估，暂缓）

可在 `turn_service` 入站对“缺 `message_id` 的流量”做短窗口内容指纹幂等（`account_id+规范化文本+message_type+时间桶`）。**当前判断**：根因在上游、且内容指纹有误杀合法重复的风险，性价比不高，暂不实现。若上游短期内无法修复或重复频发，再考虑落地（建议带 `dry_run` 灰度）。

---

## 五、本次排查边界与产物

- 排查阶段：未改仓库代码、未重启服务、数据库以 `mode=ro&immutable=1` 只读打开。
- 处置阶段：仅执行 `openclaw gateway restart`（经用户授权），收敛监听到每账号 1 个。
- 仍未直接拿到的证据：微信原始单条消息 ID（INFO 日志不打印、`contextToken` 被截断、入库 `event` 只有 `cleanedBody`）。但“并发监听窗 ↔ 重复”一一对应已构成闭环，无需该 ID 即可定论。
- 相关代码锚点：`app/turn_service.py`（`message_id` 计算、`get_duplicate_reply`、`insert_message`）、`app/db.py:435`（部分唯一索引）、`openclaw-bridge/index.js`（`before_agent_reply` 转发与 `buildIdDiagnostics` 诊断日志）。
