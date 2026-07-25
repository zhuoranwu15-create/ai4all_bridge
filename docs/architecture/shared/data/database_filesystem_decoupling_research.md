# 数据库与文件系统解耦调研

> 状态：技术调研与演进建议。
> 日期：2026-06-15。
> 适用范围：AI4ALL 微信个人 AI 陪伴项目，尤其是 `data/ai4all.sqlite3`、`data/user_profiles`、`data/system` 在多服务器部署下的演进。

## 1. 一句话结论

**方向可行：先把文件画像/记忆从本地磁盘解耦到共享存储，再把业务主库从 SQLite 迁到 PostgreSQL。**

但需要区分两件事：

1. **OSS 适合作为对象存储后端**，用于存放画像文件、daily notes、导出物、图片和备份。应用应通过统一 storage adapter 读写对象，并用版本号、ETag 或数据库记录控制并发。
2. **不要把 OSS 挂载成本地共享文件系统后继续按本地文件语义使用**。OSS 是对象存储，不是 POSIX 文件系统。通过 `ossfs` 这类 FUSE 挂载可以降低改造成本，但不应承担高频 read-modify-write、强一致锁、原子追加等核心语义。

如果短期想最小改动，阿里云 NAS/NFS 比 OSS 挂载更接近“多机共享文件系统”。但中长期建议仍然把文件读写收敛到应用层 storage abstraction，避免业务代码继续散落 `Path.read_text()` / `write_text()`。

推荐阶段：

```text
阶段 A：SQLite central 单写 + 文件 storage adapter
阶段 B：文件画像/记忆迁 OSS/NAS，DB 仍 SQLite central
阶段 C：SQLite -> PostgreSQL，文件元数据/版本在 PG
阶段 D：API/worker 多实例，Redis 账号锁 + PG 原子 claim
阶段 E：按账号 home region 做多中心接入，非必要不做数据库多写
```

## 2. 当前事实

### 2.1 数据库

当前主库是 SQLite 文件：

- 配置入口：`app/config.py` 的 `database_path = "data/ai4all.sqlite3"`。
- 连接入口：`app/db.py:connect()`，每次短连接，已设置 `busy_timeout=5000`、`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`。
- schema 初始化：`app/db.py:init_db()` 在 FastAPI startup 执行，包含 `CREATE TABLE IF NOT EXISTS`、`_ensure_column()` 和历史 contacts 迁移。
- 业务访问：绝大多数 SQL 收敛在 `app/db.py`，公共函数基本返回 `dict`、`list[dict]` 或标量，业务层没有大量直接依赖 `sqlite3.Row`。
- 当前多机接入：`accounts.assigned_node_id`、`outbound_messages.node_id/claimed_at`、`access_nodes` 已支持 `central + node` 形态。node 通过 HTTP 调中心，不直接碰 SQLite。

主要风险：

- `init_db()` 启动期 DDL 不适合多 API 实例同时执行。
- SQL 内大量 SQLite 方言，尤其是 `strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))`。
- `turn_service.py` 仍把 `sqlite3.Connection` 作为事务句柄跨模块传给 `insert_message()`、`increment_daily_usage()` 等函数。
- RPM 限流是进程内内存 deque，多实例后不再全局一致。

### 2.2 文件画像与记忆

当前文件状态由本地磁盘承载：

- 路径配置：`app/config.py` 的 `user_profiles_dir = "data/user_profiles"`、`system_dir = "data/system"`。
- 路径 helper：`app/user_profiles.py` 的 `account_profile_dir()`、`context_file_path()`。
- per-account 文件：`SOUL.md`、`IDENTITY.md`、`USER.md`、`MEMORY.md`、legacy `user_profile.md`。
- daily notes：`app/memory_writer.py` 追加写 `data/user_profiles/<account>/memory/YYYY-MM-DD.md`。
- Dreaming：`app/dreaming.py` 读取 daily notes，并改写 `MEMORY.md` / `USER.md` 等长期上下文文件。
- wipe 行为：DB 事务先提交，再由调用方尽力删除账号文件目录。这个顺序是正确的，不能把文件删除放进 DB 事务里。

主要风险：

- 多台 central/API 同时运行时，不同机器的本地文件会分叉。
- daily notes 是追加写，OSS 对象存储不天然等价于本地 append 文件。
- Dreaming 的 read-modify-write 如果多 worker 并发，会有覆盖风险。
- prompt 构建读取本地文件，多实例后同一账号可能看到不同上下文。

## 3. 方案比较

### 3.1 OSS 作为对象存储

适用：

- per-account context 文件对象，例如 `accounts/{account_id}/SOUL.md`。
- daily memory notes 对象，例如 `accounts/{account_id}/memory/2026-06-15.md`。
- moderation export、debug artifact、图片和备份。
- 低频读写、对象整体替换、带版本控制的写入。

不适用：

- 直接当本地文件系统做高频 `open("a")` 追加。
- 依赖本地文件锁、rename 原子性、目录遍历强语义的代码。
- 多 writer 对同一小文件做并发 read-modify-write。

推荐用法：

- 应用不直接访问挂载路径，而是调用 `ContextStorage` / `BlobStorage`。
- 当前版本信息放在 PostgreSQL 或对象 metadata 中。
- 写入采用 compare-and-swap：读取 `version` 或 ETag，写入时检查版本未变；冲突时重读合并。
- 对 `MEMORY.md` / `USER.md` 这类热上下文，小文件内容可以直接放 PostgreSQL，OSS 作为快照/归档。

### 3.2 OSS 通过 ossfs/FUSE 挂载

适用：

- 迁移过渡期，减少路径改造。
- 低频、单 writer、可容忍缓存延迟的文件。
- 运维手工查看对象内容。

不建议承载：

- `memory/YYYY-MM-DD.md` 并发追加。
- Dreaming 自动应用长期记忆。
- 任何要求强一致文件锁的流程。

如果使用该方案，必须约束：

- 每个 account 同一时刻只有一个 writer。
- 所有写入仍经过账号级锁。
- 禁止多个 worker 同时修改同一 context 文件。
- 视为临时兼容层，不作为长期架构接口。

### 3.3 阿里云 NAS/NFS 共享文件系统

适用：

- 最小改动地让多台 ECS 看到同一份 `data/user_profiles`。
- 需要接近 POSIX 文件系统语义的短期方案。
- 在不立刻改 storage adapter 的情况下先消除本地文件分叉。

风险：

- 仍要处理多 writer 并发覆盖。
- 文件锁和缓存行为要经过真实压测。
- 不能解决 SQLite 多机共享问题，SQLite 主库仍应保持 central 单写。
- 对长期云原生化帮助有限，未来仍要抽 storage adapter 或迁结构化表。

### 3.4 PostgreSQL 作为下一步主库

适用：

- 多 API/worker 实例共享业务真源。
- 消息、session、billing、moderation、proactive 队列、scheduler claim。
- 用事务、行锁、`SKIP LOCKED` 或 `UPDATE ... RETURNING` 做 worker 领取。
- 用连接池承接多进程短连接。

必要配套：

- PgBouncer 或应用连接池。
- 版本化迁移，替代启动期 `init_db()` DDL。
- Redis 做全局限流、账号锁、leader election 和短期幂等。
- 时间字段统一策略，建议存 UTC `TIMESTAMPTZ`，展示层转北京时区。

## 4. 推荐目标架构

```text
OpenClaw node / 微信接入节点
        |
        v
Stateless FastAPI API
        |
        +-- Redis
        |     - turn_lock:{account_id}
        |     - rpm/daily rate limit
        |     - scheduler leader election
        |
        +-- PostgreSQL
        |     - accounts/sessions/messages
        |     - billing/moderation/proactive
        |     - context metadata/version
        |     - worker claim state
        |
        +-- OSS/NAS/Object Storage
              - context file blobs or snapshots
              - daily notes objects
              - exports/media/backups
```

### 4.1 文件状态模型

建议拆成两层：

```text
account_context_files
  account_id
  file_name              -- SOUL.md / IDENTITY.md / USER.md / MEMORY.md
  storage_backend        -- postgres | oss | nas
  object_key             -- oss/nas path, postgres inline 可为空
  content_text           -- 小文件可 inline
  version
  content_hash
  updated_at
  updated_by
  metadata_json
  PRIMARY KEY(account_id, file_name)
```

```text
daily_memory_notes
  account_id
  business_day
  storage_backend
  object_key
  content_text           -- 小规模阶段可 inline
  version
  content_hash
  updated_at
  PRIMARY KEY(account_id, business_day)
```

实践建议：

- `SOUL.md`、`IDENTITY.md`、`USER.md`、`MEMORY.md` 是 prompt 热路径，建议先放 PG inline，必要时异步镜像到 OSS。
- daily notes 可以按天放 OSS，但追加写要通过应用层合并，不能让多个进程直接 append 同一个对象。
- 导出物、图片、备份优先放 OSS。

### 4.2 Storage adapter

最小接口：

```python
class ContextStorage:
    def read_context_file(self, account_id: str, filename: str) -> StoredText: ...
    def write_context_file(
        self,
        account_id: str,
        filename: str,
        content: str,
        expected_version: int | None = None,
    ) -> StoredText: ...
    def append_daily_note(
        self,
        account_id: str,
        business_day: str,
        block: str,
        idempotency_key: str,
    ) -> StoredText: ...
    def delete_account_context(self, account_id: str) -> None: ...
```

关键语义：

- 写入必须带 `account_id`。
- 写入应支持版本检查，防止 Dreaming 覆盖 onboarding 或用户编辑。
- `append_daily_note()` 必须幂等，避免消息重试导致重复追加。
- `delete_account_context()` 只能在 DB wipe 成功后执行，失败要可重试和可告警。

## 5. 推荐迁移路线

### Phase 0：收口访问点

目标：不改存储后端，先把文件访问集中。

- `memory_writer.py` 改为使用 `user_profiles.account_profile_dir()`，避免路径规则重复。
- 新增 `user_profiles.wipe_account_files(account_id)`，替代调用方散落的 `shutil.rmtree()`。
- 新增 `ContextStorage` 接口，先实现 `LocalFilesystemContextStorage`。
- `read_agent_context()`、`write_user_name()`、Dreaming 应用记忆改走 storage 接口。

验证：

- `tests/test_agent_context.py`
- `tests/test_memory_writer.py`
- `tests/test_dreaming.py`
- `tests/test_onboarding.py`
- `tests/test_admin_context_files.py`

### Phase 1：文件后端切换

目标：让多台机器看到同一份画像/记忆。

可选路径：

1. **保守路径：NAS/NFS 共享 `data/user_profiles`**
   - 改动小，适合短期验证多 central/API 实例。
   - 必须配账号级锁，避免多 writer。

2. **推荐路径：PG inline 热上下文 + OSS artifact**
   - `SOUL.md`、`IDENTITY.md`、`USER.md`、`MEMORY.md` 放 PG。
   - daily notes 可先 PG inline，量变大后迁 OSS。
   - OSS 用于快照、导出、图片、备份。

3. **过渡路径：OSS object storage adapter**
   - 不使用 ossfs 挂载。
   - 每次写对象整体替换，并用 version/ETag 做并发保护。

切换方式：

- 本地文件全量导入新后端。
- 打开双写：本地文件仍写，新后端也写。
- 抽样比对 content_hash。
- 按账号灰度读新后端。
- 全量读新后端，保留本地文件只读备份一段时间。

### Phase 2：PostgreSQL 影子库

目标：迁数据库，但不一次性切流。

- 引入 `DATABASE_URL`，保留 `DATABASE_PATH`。
- 把生产 DDL 从 `init_db()` 迁到版本化 migration。
- 建 PostgreSQL schema。
- SQLite -> PostgreSQL 全量导入。
- 关键写路径双写：messages、sessions、daily_usage、outbound_messages、ledger、moderation、context metadata。
- 建一致性校验脚本：行数、关键索引、最近 N 条 hash、按 account 抽样。

### Phase 3：PostgreSQL 切主

目标：API/worker 主读写 PG。

- 选少量 debug account 读写 PG。
- 后台 worker 先切 PG claim，例如 moderation/outbound。
- 主 turn 链路切 PG。
- SQLite 保留只读快照和回滚窗口。
- 删除双写后，备份策略改为 PG 快照 + OSS/NAS 快照。

### Phase 4：多实例与多中心

目标：API/worker 横向扩容。

- Redis 账号锁保护同账号 turn 顺序。
- Redis 全局限流替代进程内 RPM limiter。
- scheduler 用 leader election 或所有 worker 原子 claim。
- 多中心先用 home region，不做数据库多写。

## 6. 并发与一致性规则

必须保留的不变量：

1. 任何业务查询和文件写入都必须以 `account_id` 为作用域。
2. 同一账号同一时刻最多一个主 turn writer。
3. Dreaming 写 `MEMORY.md` / `USER.md` 必须版本检查。
4. daily note 追加必须幂等，幂等键建议用 `account_id + assistant_message_id`。
5. 文件 wipe 永远在 DB wipe 提交后执行，失败可重试，不回滚 DB。
6. outbox/worker 任务使用数据库原子 claim，不依赖进程内状态。

## 7. 风险清单

| 风险 | 影响 | 建议 |
|---|---|---|
| 把 OSS 挂载成本地 FS 后直接复用现有代码 | 并发覆盖、追加丢失、缓存不一致 | 仅短期低频读写可用；长期走 storage adapter |
| 多 worker 同时 Dreaming 同一账号 | `MEMORY.md` 覆盖 | 账号锁 + version/ETag |
| 多实例启动同时执行 `init_db()` | DDL 竞争 | 迁移系统部署期执行 |
| SQLite 文件放共享盘多机访问 | 锁和 WAL 语义不成立 | SQLite 保持 central 单写，下一步迁 PG |
| PG 连接数爆炸 | RDS 连接耗尽 | PgBouncer / 连接池 |
| 文件与 DB 不一致 | prompt 看到旧上下文或 wipe 残留 | DB 存 metadata/version，文件操作可重试、可审计 |
| 多中心直接多写 | 冲突复杂、成本高 | 先按 account home region 路由 |

## 8. 建议近期任务

1. 新增 `ContextStorage` 接口和本地文件实现。
2. 把 `user_profiles.py`、`memory_writer.py`、`dreaming.py` 的文件访问改走接口。
3. 建 `account_context_files` / `daily_memory_notes` schema 草案，先不切流。
4. 做本地文件 -> 新后端导入脚本，输出 hash manifest。
5. 引入 Redis 账号锁和全局 RPM limiter。
6. 启动 PostgreSQL adapter 设计，优先解决事务 unit-of-work、时间戳策略和迁移系统。

## 9. 参考资料

- SQLite WAL 官方文档：https://www.sqlite.org/wal.html
- PostgreSQL 高可用、负载均衡与复制：https://www.postgresql.org/docs/current/high-availability.html
- PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` 文档：https://www.postgresql.org/docs/current/sql-select.html
- PgBouncer 使用文档：https://www.pgbouncer.org/usage.html
- 阿里云 NAS NFS 挂载文档：https://www.alibabacloud.com/help/en/nas/user-guide/mount-an-nfs-file-system-on-a-linux-ecs-instance
- 阿里云 OSS ossfs 文档：https://www.alibabacloud.com/help/en/oss/user-guide/ossfs
- 阿里云 OSS 条件请求文档：https://www.alibabacloud.com/help/en/oss/user-guide/conditional-requests

