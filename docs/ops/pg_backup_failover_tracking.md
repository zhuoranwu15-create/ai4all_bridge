# PG 备份 → aliyun2 & aliyun1 故障切换 — 工作追踪

> 状态：Phase 0 异地 `pg_dump` 与 Phase 1 流复制热备已于 2026-07-05 上线；Phase 2 手工切换/
> 回切尚未完成低峰演练。本文保留当日实测基线和执行记录，最新状态以进度日志及对应勾选项为准。
> 目标：① PG 有异地备份落在 aliyun2；② aliyun1 挂时 aliyun2 能顶上服务（至少保住数据与 aliyun2 本机账号）。

## 0. 切换前基线（2026-07-05 实测）

| 项 | 值 |
|---|---|
| PG 主库 | aliyun1 `/var/lib/pgsql/data`，监听 `172.24.16.141:5432` + localhost |
| PG 版本 | **13.23** |
| db 大小 | **73 MB**（写入量低） |
| 复制相关参数 | `wal_level=replica` ✅、`max_wal_senders=10` ✅、`max_replication_slots=10` ✅、`archive_mode=off` |
| 连接 | `max_connections=100`，当前 ~16，active ~1 |
| aliyun2 依赖方式 | backend 直连 `postgresql://ai4all@172.24.16.141:5432/ai4all` |
| aliyun2 本地 PG | **无**（客户端+服务端都没装），磁盘 22G 空闲 |
| 现有导出 | aliyun1 cron 4:17 `export_pg_to_sqlite.py` → 同盘 `nearline/data/source_snapshot.sqlite3`（**同机、非异地、SQLite 形态**） |
| 账号分布 | aliyun1 61 + aliyun2 14 = 共 75，全在这一个中心 PG |

**风险定性**：中心 PG 是两机硬单点；仅有的"备份"与 PG 同盘同机。aliyun1 磁盘 89%（排查脚本 `scripts/disk_audit.sh`，结论见文末）。

**切换范围的诚实说明**：aliyun1 宕机时，其 61 账号的入站（aliyun1 上的 node/gateway/nginx）本就不可用，切换**主要保住数据 + aliyun2 自己的 14 账号继续服务**，不等于 aliyun1 账号无缝续跑。

---

## Phase 0 — 异地逻辑备份（RPO ≤ 24h，低风险，先做）✅ 基本完成 2026-07-05

从 aliyun2 定时 `pg_dump` 拉 aliyun1 的库到 aliyun2 本地，得到**可恢复的异机副本**。

- [x] **0.1** aliyun2 安装 PG 客户端 → `pg_dump 13.23`（与主库版本精确一致，Alibaba Cloud Linux 3 仓库自带）
- [x] **0.2** pg_hba **已放行**：aliyun2 backend 本就用 `ai4all` 账号每天直连主库 → Phase 0 复用该凭据，零改动零 sudo。（新建只读 `bkp` 账号列为后续加固，见 §决策 D待定）
- [x] **0.3** 写 `scripts/pg_backup_pull.sh`：从 `.env` 解析 `DATABASE_URL`，`pg_dump -Fc --no-owner --no-privileges` → `~/pgbackups/ai4all_<stamp>.dump`，14 天轮转，密码走 `PGPASSWORD`(不进 ps/日志)，落 `~/pgbackups/backup.log`
- [x] **0.4** 手动验证：**13M / 298 对象 / 3 秒**；`pg_restore --list` 通过；含 accounts/messages/daily_usage/moderation 等 54 张表 DATA ✅
- [x] **0.5** aliyun2 cron 已挂：`40 4 * * *`（错开 aliyun1 的 4:17），crond active
- [ ] **0.6** 完整"从 dump 恢复"演练 → **推迟到 Phase 1**（需本地 PG 作恢复目标；当前仅做了 `--list` 结构校验）

**已达成：aliyun1 整机丢失时，数据最多回退到上一晚 04:40，副本落在 aliyun2 异机、可 `pg_restore` 恢复。**

---

## Phase 1 — aliyun2 流复制热备（RPO ~秒级，可提升）

aliyun2 起一个 PG13 standby，持续从 aliyun1 流式同步。db 仅 73MB，成本低；主库参数已就绪。

**已定参数（2026-07-05）**：主库 `password_encryption=md5`→hba 用 `md5`；主库数据目录 `/var/lib/pgsql/data`；主库 `listen_addresses=localhost,172.24.16.141`；aliyun2 内网 IP=`172.24.18.88`（aliyun1 视角一致）；复制角色 `repl`；复制槽 `aliyun2_slot`；aliyun2 已装 `postgresql-server 13.23`。

- [x] **1.1** aliyun2 安装 **PG13 server**（`postgresql-server 13.23-3.0.1.al8`，与主库精确一致）
- [x] **1.2** aliyun1 建复制用户 `repl` + 复制槽 `aliyun2_slot` + pg_hba 放行 `172.24.18.88/32 md5` + reload（Jack 执行，幂等守卫）。**注**：验证握手须用**物理复制模式** `replication=1`；`replication=database` 会被当普通库连接、误报 `no pg_hba.conf entry`（踩过坑）
- [x] **1.3** aliyun2 `pg_basebackup -h 172.24.16.141 -U repl -D ~/pgstandby/data -Fp -Xs -P -R -S aliyun2_slot`（161MB，含 password 的 primary_conninfo + standby.signal）
- [x] **1.4** 启动并验证：`pg_is_in_recovery()=t`、slot `active=t`、lag_bytes=0；**端到端实测**主库插入→2s 后 standby 只读可见 ✅
- [x] **1.5** 固化为 `systemctl --user pg-standby.service`（`Type=simple` 前台 postgres，`Restart=always`，enabled，配合 Linger=yes 开机自启）

**standby 关键落点（aliyun2）**：datadir `~/pgstandby/data`、监听 `127.0.0.1:5432`、socket `~/pgstandby/sockets`。
覆盖项写在 `postgresql.auto.conf`：`listen_addresses='localhost'`（主库那份含 172.24.16.141，本机无法 bind）、`unix_socket_directories='/home/jack/pgstandby/sockets'`（默认 /var/run/postgresql root 不可写）。
**与业务无冲突**：backend 仍直连远端 aliyun1:5432 跑业务；本地 standby 只读、独立端口进程。

> ✅ **至此 "自动备份到 aliyun2" 有两层**：① 每日 `pg_dump` 冷备（Phase 0）；② 持续流复制热备、RPO~秒（Phase 1）。**故障切换（Phase 2）按决策分步、暂不自动化**，流程见下、随时可演练。

---

## Phase 2 — 故障切换 & 回切流程（**分步待做，暂不自动化**；流程已就绪可演练）

> 决策 D3：手动切换。以下步骤已就绪，等择期低峰演练，不急于一次做完。

- [ ] **2.1** 触发判据：aliyun1 PG 不可达 > N 分钟 / 整机失联（人工确认，避免误切脑裂）
- [ ] **2.2** 切换步骤（已就绪）：
  1. `systemctl --user stop ...`? 否——standby 已在跑；直接升主：`/usr/bin/pg_ctl -D ~/pgstandby/data promote`（或 `SELECT pg_promote();`）→ standby 变可写主库
  2. 改 aliyun2 backend `.env`：`DATABASE_URL` 的 host `172.24.16.141` → `127.0.0.1`
  3. `systemctl --user restart ai4all-weixin-backend` → `/health` 200 验证
  - 说明：只恢复 aliyun2 自身 14 账号 + 保住数据；aliyun1 的 61 账号入站在 aliyun1 恢复前仍不可用
- [ ] **2.3** standby 也不可用时：从最近异机 `pg_dump` 恢复到新的 PostgreSQL 实例，核对
  schema、关键表计数与账本后再切应用连接。禁止清空 `DATABASE_URL` 回落历史 SQLite 快照。
- [ ] **2.4** **回切（failback）**：aliyun1 恢复后，避免脑裂的次序——先确认旧主不再被写、以新主为准重建复制方向（旧主转 standby 或 pg_rewind），再把 backend 指回。**演练前务必细化，双写是最大风险**
- [ ] **2.5** 低峰演练一次，实测 RTO
- [ ] **2.6**（可选）复制延迟 + 主库可达性监控告警（配合前述"运维缺监控"项一起做）

**已就绪的运维命令速查**：
- 看复制状态(主库侧)：`pg_replication_slots.active` / `pg_stat_replication`
- 看 standby 延迟(备库侧)：`psql -h127.0.0.1 -Uai4all -dai4all -c "select pg_is_in_recovery(), pg_last_wal_replay_lsn()"`
- standby 生命周期：`systemctl --user {status,restart,stop} pg-standby.service`

---

## 决策记录（Decisions）

- **D1**：Phase 0 先行落地（低风险、马上有异地副本）；Phase 1 视精力再上。
- **D2**：aliyun2 装 PG 必须 = **13.x**（匹配主库 13.23，流复制要求主备大版本一致）。
- **D3**：切换为**手动**（judgement call），不做自动 failover（避免脑裂；两机规模小，人工可控）。
- **D待定**：pg_dump 用现有 `ai4all` 账号还是新建只读 `bkp` 账号？（倾向新建只读，最小权限）

## 需要 sudo 的动作清单（Jack 执行）

1. aliyun2：`sudo yum install -y postgresql13`（Phase 0）/ `postgresql13-server`（Phase 1）
2. aliyun1：改 `pg_hba.conf`、reload PG、建复制槽/角色（Phase 0.2 / 1.2）
3. aliyun2：PG server 初始化、开机自启（Phase 1）

## 演练 / 恢复速查（随 Phase 完成补全）

- 从 dump 恢复：`pg_restore -d <target> ~/pgbackups/ai4all_<date>.dump`（待 0.6 补全并实测）
- standby 升主：`pg_ctl promote -D <datadir>`（待 2.2 补全并实测）

## 进度日志

- 2026-07-05：创建追踪文件；采集现状基线；确认主库已 `wal_level=replica`、参数就绪；aliyun2 无 PG。
- 2026-07-05：**Phase 0 落地**。装 `pg_dump 13.23`；写 `scripts/pg_backup_pull.sh` 并手动验证(13M/298对象/3s，54表)；挂 aliyun2 cron `40 4 * * *`。异地备份已生效。
- 2026-07-05：**Phase 1 落地（准实时热备 LIVE）**。aliyun1 建 repl 角色/槽/放行；aliyun2 `pg_basebackup`→`~/pgstandby/data`，`systemctl --user pg-standby.service` 托管，`127.0.0.1:5432`。slot active、lag 0、端到端(主库插入→2s standby 可见)验证通过。**按用户要求：准实时备份到此完成即停，Phase 2 故障切换暂不做**（流程已文档化，随时可分步演练）。
