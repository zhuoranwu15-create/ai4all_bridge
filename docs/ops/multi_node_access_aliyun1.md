# Runbook · 线上 aliyun1（central + node）无损升级

> 配套设计：[`../architecture/shared/access/multi_node_access_refactor.md`](../architecture/shared/access/multi_node_access_refactor.md)
> 角色：aliyun1 从「单机 monolith」原地升级为 **central + node 同机共存**——继续持有现有微信会话（**不重扫码**），同时承担中心大脑。
> 最后更新：2026-06-12

aliyun1 现状（systemd）：`ai4all-weixin-backend`@`127.0.0.1:8180`（nginx 后）+ `ai4all-weixin-proactive-scheduler` + `ai4all-monitor-health.timer` + `ai4all-backup.timer` + 本机 openclaw（持有全部存量微信会话）。部署 = `git pull` + `scripts/restart_runtime.sh`。

> 🔴 **铁律**：升级全程**不要执行 `openclaw gateway restart` / `restart_runtime.sh --restart-openclaw`**。全局重启 = 该机所有微信号同一秒重连 = 风控高危 + 会话中断。升级只重启 backend/scheduler（Python 进程），openclaw 进程与会话**原地不动**。

---

## Part 1 · aliyun1 升级为 central+node（一期代码落地后）

前置：一期代码已在仓库内开发完成，并通过 `standalone` 全量回归（`.venv/bin/pytest tests/`）。

### 步骤

**0. 先备份（强制）**
```bash
.venv/bin/python scripts/backup_data.py     # DB + 画像 + system + .env，含异地 rsync（若配置）
```
这同时是后续「中心切换」要用的「中心状态包」（设计 §8.1）。

**1. 拉代码**
```bash
git pull            # 切到含一期改动的提交
```

**2. 配置 `.env`（新增项）**
```
AI4ALL_ROLE=central,node       # central 与 node 同机共存
NODE_ID=aliyun1
DEFAULT_NODE_ID=aliyun1        # 未分配账号兜底归属（迁移期=aliyun1，保证不悬空）
CENTRAL_URL=<aliyun1 的稳定指纹地址>   # 见设计 §8.2：内网 SLB / 私网 DNS / VIP，勿写裸 IP
NODE_BASE_URL=http://127.0.0.1:8180   # 同机，中心 push 登录到本机 node
OUTBOUND_PULL_INTERVAL_SECONDS=2
# LOCAL_NODE_INLINE_DISPATCH=true     # 可选：aliyun1 自有账号出站走同进程即时直发，零轮询延迟（迁移初期降延迟用）
```
调度开关保持现状（aliyun1 是 central，照常开 proactive scheduler 等）。

**3. 重启（加性迁移自动执行）**
```bash
scripts/restart_runtime.sh --install-deps        # 切勿加 --restart-openclaw
```
- `startup()` → `init_db()` 自动跑 `_ensure_column` 加性迁移（`access_nodes` 表、`accounts.assigned_node_id`、`outbound_messages.node_id` / `claimed_at`），**老库兼容、可热迁**。
- 健康检查：`curl -fsS http://127.0.0.1:8180/health/ready` 通过。

**4. 一次性 backfill 存量账号归属**
- 把所有现存账号 `assigned_node_id` 置为 `aliyun1`（一次性脚本或受控 SQL），确保存量账号出站路由到 aliyun1 本机。
- （迁移期即使漏置，`DEFAULT_NODE_ID=aliyun1` 也会兜底，不会悬空。）

**5. 验证「行为与今天一致」**（此刻 = standalone 等价，仅多走一层本机队列/RPC）
- 被动回复：给 aliyun1 上某存量号发消息，回复正常。
- 主动消息：触发一条提醒/dreaming，确认 aliyun1 本机 node 出站把它发出（若开 `LOCAL_NODE_INLINE_DISPATCH` 则同进程即时发）。
- 登录/登出：经中心 UI 走一次绑定（目标节点 = aliyun1），二维码与完成正常。
- `access_nodes` 出现 aliyun1 的心跳。

### 回滚
```
# .env 改回
AI4ALL_ROLE=standalone
# 然后
scripts/restart_runtime.sh        # 不要 --restart-openclaw
```
加列是加性的，旧代码路径不读这些列，回滚安全；openclaw 会话不受影响。

---

## Part 2 · 引入 aliyun2 后的常态

- aliyun2 按 [runbook B](multi_node_access_aliyun2.md) Part 2 上线为 node 后，中心 `pick_node()` 对**新登录**按容量分配到 aliyun1 或 aliyun2。
- **存量账号保持在 aliyun1**；如需迁到 aliyun2 释放 aliyun1 的会话密度，只能在 aliyun2 重扫码（会话不可热迁），按需手动进行直到二期调度器。
- aliyun1 的出口 IP 与 aliyun2 不同 → 两批账号天然走不同 egress（承接上游 §3.1/§4-C）。

---

## Part 3 · 中心切换 aliyun1↔aliyun2（可选，二期一键化）

详见设计 [§8](../architecture/shared/access/multi_node_access_refactor.md)。要点：
- 切换 = 搬「中心状态包」（DB + 画像 + system，= `backup_data.py` 产物）到目标机 + **翻转 `CENTRAL_URL` 指纹地址**，节点零改配。
- **不导致任何会话重登**：aliyun1 切换后仍是 node，会话留在 aliyun1，只是大脑+DB 搬到 aliyun2。
- MVP 为计划内手动切换（分钟级停写窗口）；近实时热备（Litestream）+ 一键脚本 `scripts/promote_central.py` 留二期。

> 切换前必须 `PRAGMA integrity_check` 校验目标机 DB，并确认调度开关只在「当前 central」机器开启、node-only 机器关闭。
