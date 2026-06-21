# P5 线上升级 Runbook — 厚节点改造灰度切换

> 适用版本：commit `ed77c70`（P1–P4 落地后）
> 最后更新：2026-06-21
> 依赖文档：[`docs/tech_design/thick_node_postgres_refactor.md`](../tech_design/thick_node_postgres_refactor.md)

> **⚠️ 现状（2026-06-21 登机实测）：阶段一、阶段二均已上线完成**。aliyun1 已是 `central,node` + 本机 PG（无 SQLite 文件），aliyun2 已是 `node` 厚节点直连 aliyun1 PG、inbound 已本地化、承载 14 个账号。**本文以下步骤为历史执行记录 / 回滚与重建参考**，不是待办。两机实测差异见 [`aliyun1_aliyun2_deployment_diff.md`](aliyun1_aliyun2_deployment_diff.md)。**唯一未做的是阶段三**（aliyun1 退化为纯中心）与 PG HA 主备（§10）。

---

## 0. 总体策略

```
aliyun1（中心）
  ├─ 起点：standalone + SQLite
  ├─ ✅ 阶段一：+ PG（已完成；现 AI4ALL_ROLE=central,node，本机 PG）
  └─ ⬜ 阶段三：纯 central（停跑 turn，只跑控制面）← 仍未做，aliyun1 现仍跑 turn

aliyun2（节点）
  ├─ 起点：瘦接入口（一期形态）
  └─ ✅ 阶段二：node 厚节点，直连 aliyun1 PG，跑完整 turn（已完成，承载 14 账号）
```

**每一步都可单独回滚，不强行打包。**

---

## 1. 前置条件

- [ ] aliyun1 已 `git pull`，代码为 `ed77c70` 或更新
- [ ] aliyun2 已 `git pull`（同上）
- [ ] aliyun1 已安装 PostgreSQL 15+（`psql --version` 确认）
- [ ] `data/ai4all.sqlite3` 有最近一次完整备份（`scripts/backup_data.py`）
- [ ] 服务维护窗口已通知（迁移期 app 可继续运行，但建议低峰时段执行）

---

## 2. aliyun1：安装并初始化 PostgreSQL

```bash
# 若尚未安装（Ubuntu 示例）
sudo apt install -y postgresql-15
sudo systemctl enable --now postgresql

# 建库建用户（以 postgres 用户执行）
sudo -u postgres psql <<'SQL'
CREATE USER ai4all WITH PASSWORD 'your-strong-password';
CREATE DATABASE ai4all OWNER ai4all;
\q
SQL

# 验证本地连接
psql "postgresql://ai4all:your-strong-password@localhost:5432/ai4all" -c "SELECT version();"
```

---

## 3. 数据迁移：SQLite → PG

**在 aliyun1 项目目录执行：**

```bash
# 1. 先跑迁移脚本（--truncate 允许目标库有数据时覆盖，初次必须不加；二次验证可加）
.venv/bin/python scripts/migrate_sqlite_to_pg.py \
    --sqlite data/ai4all.sqlite3 \
    --pg "postgresql://ai4all:your-strong-password@localhost:5432/ai4all" \
    --batch 500

# 迁移完成后脚本会输出行数对拍和账本勾稽结果，确认全部 OK 再继续
# 预期输出样例：
#   [OK] accounts: sqlite=423 pg=423
#   [OK] messages: sqlite=89241 pg=89241
#   [OK] ledger reconciliation: all wallets balanced
```

> 脚本会自动：
> - 在 PG 建好完整 schema（`init_db` + `_MIGRATIONS`）
> - 跳过历史遗留表（如 contacts）
> - 重置 IDENTITY 序列到 MAX(id)，避免自增撞号

如果勾稽出现不一致，检查错误输出后修复再重跑（脚本幂等，支持 `--truncate` 重来）。

---

## 4. 迁移 profile 文件进 PG

```bash
# 把磁盘存量 profile（SOUL/IDENTITY/USER/MEMORY/user_profile.md 及 memory/*.md daily notes）
# 导入 PG。目标库由 DATABASE_URL 决定（脚本无 --pg 参数）；
# 源目录由 USER_PROFILES_DIR 决定，默认 data/user_profiles（脚本无 --profiles-dir 参数）。
# 脚本默认 dry-run，仅打印将导入/跳过清单；确认无误后加 --apply 才真正写库。

# 1) 先 dry-run 预览（强烈建议）
DATABASE_URL="postgresql://ai4all:your-strong-password@localhost:5432/ai4all" \
    .venv/bin/python scripts/import_profiles_to_db.py

# 2) 确认 summary 后正式写库
DATABASE_URL="postgresql://ai4all:your-strong-password@localhost:5432/ai4all" \
    .venv/bin/python scripts/import_profiles_to_db.py --apply

# 确认输出末尾：written=N（--apply 时）；默认幂等只补缺，需整文件覆盖才加 --overwrite。
```

---

## 5. 阶段一：aliyun1 切换为 standalone + PG

### 5.1 更新 .env

```bash
# 在 aliyun1 的 .env 中，修改/新增以下变量：
DATABASE_URL=postgresql://ai4all:your-strong-password@localhost:5432/ai4all
DB_POOL_MIN_SIZE=2
DB_POOL_MAX_SIZE=10

# AI4ALL_ROLE 保持 standalone（不变）
# AI4ALL_ROLE=standalone
```

### 5.2 重启服务

```bash
sudo systemctl restart ai4all   # 或按实际 systemd 服务名
sudo journalctl -u ai4all -f    # 观察启动日志，确认 init_db 完成，无 ERROR
```

预期日志关键字：
```
ai4all.db _core  INFO  schema_migrations: current_version=5, target=5 — up to date
```

### 5.3 冒烟测试

```bash
# 发一条测试消息，验证 turn 完整走通（含计费、记忆写入）
.venv/bin/python scripts/send_mock_turn.py \
    --url http://127.0.0.1:8180 \
    --text "PG 迁移测试消息" \
    --account 86f866663cf9-im-bot

# 预期：终端回显 AI 回复；DB 中 messages/daily_usage/memories 均有新记录
```

```bash
# 用 psql 确认数据落 PG
psql "postgresql://ai4all:your-strong-password@localhost:5432/ai4all" -c \
    "SELECT COUNT(*) FROM messages WHERE account_id = '86f866663cf9-im-bot' ORDER BY 1 DESC LIMIT 1;"
```

**本步骤完成后，aliyun1 已完全跑在 PG 上，行为与之前一致。可在此稳定观察 1–3 天后再推进阶段二。**

---

## 6. 阶段二：aliyun2 切换为 node 角色

### 6.1 更新 aliyun2 的 .env

```bash
# aliyun2 的 .env 关键变量（其余保持与 aliyun1 同步）：
AI4ALL_ROLE=node
NODE_ID=aliyun2
DATABASE_URL=postgresql://ai4all:your-strong-password@aliyun1-internal-ip:5432/ai4all
DB_POOL_MIN_SIZE=1
DB_POOL_MAX_SIZE=8

# 调度器在节点上同样可开启（只扫 assigned_node_id=aliyun2 的账号）
PROACTIVE_SCHEDULER_ENABLED=false    # 暂时保持 false，灰度验证稳定后再开
DREAMING_SCHEDULER_ENABLED=false

# 节点不需要 Web/Admin 控制面，has_central_role=false，对应路由自动不挂载
```

> 注意：`DATABASE_URL` 里用 aliyun1 的**内网 IP**，不要用公网 IP，避免带宽计费和延迟。

### 6.2 确认 PG 端口从 aliyun2 可达

```bash
# 在 aliyun2 上测试
psql "postgresql://ai4all:your-strong-password@aliyun1-internal-ip:5432/ai4all" -c "SELECT 1;"
```

如果不通，检查 aliyun1 防火墙和 `postgresql.conf` 的 `listen_addresses`、`pg_hba.conf`：

```
# /etc/postgresql/15/main/postgresql.conf
listen_addresses = 'localhost,aliyun1-internal-ip'

# /etc/postgresql/15/main/pg_hba.conf
host    ai4all  ai4all  aliyun2-internal-ip/32  scram-sha-256
```

### 6.3 重启 aliyun2 服务

```bash
sudo systemctl restart ai4all
sudo journalctl -u ai4all -f
```

预期日志：
- 不出现 `init_db`（节点跳过 DDL，只执行 `SELECT 1` 验证连接）
- 不挂载 `/web`、`/admin` 路由（节点不需要）
- 挂载 `/health` 和 `/openclaw` 路由

> 此时 aliyun2 backend 已起、能连 PG，但**还没有任何真实流量进来**——aliyun2 的 openclaw 仍把 inbound 转发到 aliyun1（一期形态）。下面 6.4 先做不切流量的本地验证，6.5 才正式把 inbound 切到本地。

### 6.4 本地验证（不切真实流量，可反复做、零风险）

```bash
# 在 aliyun2 上直接打本地 backend，验证 turn 在本地处理 + 读写 aliyun1 PG。
# 选一个 aliyun2 归属账号；此调用绕过 openclaw，不影响线上真实消息。
.venv/bin/python scripts/send_mock_turn.py \
    --url http://127.0.0.1:8180 \
    --text "节点本地直连验证" \
    --account <aliyun2归属账号>
```

预期：aliyun2 日志显示 turn 本地完成；在 aliyun1 用 psql 能看到该账号新 message/usage 落 PG。期间真实 inbound 仍走 aliyun1，互不影响。

### 6.5 重指向 aliyun2 的 openclaw-bridge 到本地后端 ⚠️（关键步骤，缺这步 inbound 永远回中心）

inbound 的处理位置由 **openclaw-bridge 插件的 `AI4ALL_BACKEND_URL`** 决定（每台机一个设置），**不是**账号级的 `assigned_node_id`——`turn_service` 收到 turn 即无条件本地处理，代码内没有"按归属节点转发 inbound"的逻辑。因此：

```text
# aliyun2 的 openclaw-bridge 插件配置（见 openclaw 插件 env / 安装副本）：
AI4ALL_BACKEND_URL: http://aliyun1   →   http://127.0.0.1:8180
# 改完重启 openclaw gateway 使插件重载。
```

> **⚠️ 这是机器级、非逐账号的切换**：一旦重指向，**aliyun2 openclaw 上承载的全部账号会同时切到本地处理**（当前约 14 个 `assigned_node_id=aliyun2` 的账号）。这是真正的 inbound cutover 时刻，需重点盯。
> **回滚**：把 `AI4ALL_BACKEND_URL` 指回 `http://aliyun1` 并重启 gateway，即刻退回一期（inbound 重新回中心处理），无数据迁移。
> **灰度单元**：因为是机器级，inbound 的最小灰度单元 = "aliyun2 这台机的全部账号"。要更细只能靠"先只把少量账号登录/分配到 aliyun2"来缩小这台机承载量，不能在同机内逐账号切 inbound。

---

## 7. 灰度：assigned_node_id 的作用范围（调度 + 出站，**不含 inbound**）

> **重要**：`assigned_node_id` 控制的是 ①节点调度器扫描范围（proactive/dreaming 各自只扫 `assigned_node_id=本节点`）②主动消息出站归属（`resolve_node_for_account`）。它**不**决定 inbound turn 在哪台机处理——inbound 已在 §6.5 由 bridge 指向机器级切换。正常情况下账号在哪台 openclaw 登录、其 `assigned_node_id` 就该等于哪台机（登录时 `pick_node` 一并写入），两者保持一致。下面的分配操作用于核对/纠正这层一致性，以及在开启节点调度器前确认扫描范围正确。

### 7.1 选一个低风险账号，写入 assigned_node_id

通过 Admin API（在 aliyun1 中心执行，因为账号数据在中心 PG）：

```bash
# 使用 Admin API 分配账号到 aliyun2
curl -X PATCH http://aliyun1:8180/admin/accounts/<account_id>/node \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"assigned_node_id": "aliyun2"}'
```

或直接 SQL（慎用，仅在 Admin API 不存在时）：

```sql
UPDATE accounts SET assigned_node_id = 'aliyun2' WHERE id = '<account_id>';
```

### 7.2 验证灰度账号在 aliyun2 上完整跑通

```bash
# 在 aliyun2 上发一条 mock turn（使用该账号）
.venv/bin/python scripts/send_mock_turn.py \
    --url http://127.0.0.1:8180 \
    --text "节点灰度测试" \
    --account <account_id>
```

预期：
- turn 在 aliyun2 本地 turn_service 处理
- 消息/记忆写入中心 PG（可在 aliyun1 psql 确认）
- aliyun1 不再收到该账号的 turn 请求

### 7.3 观察指标

- 飞书报警：无新 ERROR
- aliyun2 日志：turn 正常完成，无 DB 连接错误
- aliyun1 PG 负载：正常（连接数/TPS 在预期范围内）

### 7.4 逐步扩大

```bash
# 稳定后，批量将账号迁移到 aliyun2
# 使用 psql 或 Admin API 更新 assigned_node_id
# 建议每批 20-50 个账号，观察 30 分钟后继续
```

---

## 8. 阶段三（后续）：aliyun1 退化为纯中心

当 aliyun2 节点稳定承载所有/大部分活跃账号后，可将 aliyun1 切换为 `AI4ALL_ROLE=central`，停止跑 turn_service（届时补充本节）。

---

## 9. 回滚方案

### 回滚节点（aliyun2）→ 一期瘦节点形态

```bash
# aliyun2 .env：恢复一期配置
AI4ALL_ROLE=node   # 或一期的配置值
# 去掉 DATABASE_URL（回用一期转发到中心的方式）
sudo systemctl restart ai4all
```

### 回滚 aliyun1 → SQLite

```bash
# aliyun1 .env：注释掉 DATABASE_URL
# DATABASE_URL=
sudo systemctl restart ai4all
```

SQLite 数据库 `data/ai4all.sqlite3` 未被删除或修改，直接回落即可。  
迁移后 PG 上的数据（新增消息等）不会自动同步回 SQLite——如需保留，需手动导出相关记录。

---

## 10. PG 主备（参考设计文档 §8）

当 aliyun1 PG 稳定运行后，参考 [`thick_node_postgres_refactor.md §8`](../tech_design/thick_node_postgres_refactor.md#8-pg-部署与切换) 配置流复制热备 + pg_dump PITR 备份，进一步降低单点风险。节点 `DATABASE_URL` 使用多主机连接串：

```
postgresql://ai4all:pwd@aliyun1-internal:5432,standby-internal:5432/ai4all?target_session_attrs=read-write
```

---

## 快速检查表

| 检查项 | 命令 / 方式 |
|---|---|
| PG 连接可达 | `psql <DATABASE_URL> -c "SELECT 1;"` |
| schema 版本正确 | `psql ... -c "SELECT * FROM schema_migrations ORDER BY version;"` |
| 行数对拍 | 已内建于 `migrate_sqlite_to_pg.py` 主流程（复制后自动逐表对拍，`--no-reconcile` 才关闭）；无独立 reconcile-only 模式 |
| 服务健康 | `curl http://<host>:8180/health` |
| 一条 turn 走通 | `scripts/send_mock_turn.py --text "test"` |
| 飞书无新报警 | 飞书群 ai4all-alerts 观察 |
