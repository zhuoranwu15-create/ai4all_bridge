# 厚节点改造落地设计（中心控制面 + 计算下沉节点，共享 Postgres）

> 状态：P1–P4 已落地，P5（灰度切换）待线上推进
> 最后更新：2026-06-21
> 适用范围：AI4ALL 微信个人 AI 陪伴项目（bridge + OpenClaw）
> 上游依据：
> - [`multi_node_access_refactor.md`](../access/multi_node_access_refactor.md) —— 已上线的「中心大脑 + 瘦接入节点」一期，本文在其基础上把节点做厚。
> - [`database_filesystem_decoupling_research.md`](database_filesystem_decoupling_research.md) —— SQLite→PostgreSQL、文件画像解耦的方向论证（阶段 A→E）。本文是其阶段 C/D 的**具体落地实现**。
> - [`multi_node_access_retrospective.md`](../access/multi_node_access_retrospective.md) —— 一期复盘与技术债。
>
> 一句话目标：把节点从「瘦接入口」升级为「跑完整 turn 的计算单元」，中心退化为「身份 / 账号分配 / 计费 / 审核 / 管理」的控制面；数据真相统一收敛到**中心 Postgres**。

---

## 1. 背景与动机

一期（`multi_node_access_refactor.md`）的形态是**单中心大脑 + 瘦节点**：每条入站微信消息都要从节点 HTTP 回中心，由中心跑完整个 `turn_service`。节点只有 openclaw（微信会话），零 DB、零业务逻辑。

本次目标：**把核心业务逻辑下沉到节点**。趁当前用户量与压力都很低，以最小迁移代价完成。

### 决策记录（已拍板）

| 决策 | 选择 | 含义 |
|---|---|---|
| 数据放哪 | **共享中心库** | 不做账号分片；数据仍是中心一份真相 |
| 节点如何访库 | **换 Postgres，节点直连** | 中心 DB 从 SQLite 迁到 PG，节点原样跑 `turn_service`、只是连远程库 |
| 计费一致性 | **每轮实时回中心扣费** | 库在中心，`record_chat_usage_charge` 直写中心 PG 即天然满足 |
| profile 文件 | **进 PG** | SOUL/IDENTITY/USER/MEMORY + daily notes 的内容存 PG TEXT 列 |
| 测试基座 | **核心走 ephemeral Postgres** | 保真，避免「测试 SQLite、生产 PG」方言漂移；少量纯逻辑测试可仍用 SQLite |

> 为什么「共享中心库 + 多节点写」就**必须**换 PG：SQLite 是单机单写者（WAL 只放宽并发读，写串行且不能跨主机）。多个节点并发写同一个库，SQLite 扛不住，client-server 数据库是硬需求。

---

## 2. 改造后的目标架构

```
┌──────────────── 中心 Center（控制面）────────────────┐
│  PostgreSQL  ← 唯一数据真相                            │
│    身份/账号/账本/审核/会话/消息/profile 全在这里        │
│  Web 注册 · OTP · 账号开通 · 节点分配 pick_node()       │
│  Admin 管理台 + 审核台（读写 PG，几乎不动）             │
│  节点注册表 access_nodes · 登录编排 node_gateway        │
│  全局：llm_runtime_config · RPM 共享计数               │
│  （不再跑 turn_service / 不再跑业务调度）               │
└───────────────────────────────────────────────────────┘
      ▲ 直连 PG（内网，连接池）      ▲ 登录 push（HTTP，沿用一期）
      │                             │
┌─────┴──────── 节点 Node（运行面，N 台）──┴──────────┐
│  完整 app 栈   AI4ALL_ROLE=node                      │
│  openclaw（微信会话，沿用一期）                       │
│  turn_service · prompt_builder · 工具循环      ← 下沉  │
│  onboarding · memory_writer · session 生命周期 · dreaming │
│  proactive + dreaming 调度（只扫本节点账号）   ← 下沉  │
│  所有 DB I/O 直连中心 PostgreSQL                      │
└──────────────────────────────────────────────────────┘
```

### 2.1 关键流转变化

**入站（每轮消息）**：节点 openclaw → **本地** `/openclaw/turn` → 本地 `turn_service` → 读写中心 PG → 回复本地 openclaw 发出。
**一期里每条消息回中心的 HTTP 往返被消除**——因为 compute 和微信会话本来就同在「归属节点」，无需再绕中心。

**中心→节点 push 仅保留登录编排**：Web 注册在中心 → `pick_node()` 选节点 → 中心 push 到目标节点起二维码（沿用一期 `node_gateway`/`node_agent` 的 `/node/exec/login/*`）。登录完成后该账号的会话落在该节点，此后一切节点本地完成。

**主动消息**：proactive/dreaming 调度下沉到节点，每个节点扫描加 `WHERE assigned_node_id = <本节点>`。一期里「全库扫描、多实例重复扫」的隐患（见复盘）随之消失；生成的主动消息由归属节点本地直发，跨节点 outbound pull 队列对**同节点投递**退化为本地直发（保留表做重试/durability，后续可简化）。

### 2.2 中心 / 节点职责对照

| 能力 | 中心 | 节点 |
|---|---|---|
| Web 注册 / OTP / 账号开通 | ✅ | — |
| 节点分配 `pick_node` / 归属写入 | ✅ | — |
| 管理台 / 审核台 | ✅ | — |
| 计费账本 / 钱包（PG） | ✅ 权威 | 节点写入（直连 PG） |
| 登录编排（push QR） | ✅ 发起 | ✅ 执行（openclaw） |
| turn 处理 / prompt / 工具 | — | ✅ |
| onboarding / 记忆 / dreaming | — | ✅ |
| proactive / dreaming 调度 | — | ✅（按本节点账号） |
| RPM 限流 | ✅ 共享计数 | 查询中心 |

---

## 3. 分期实施

把「换库」和「下沉」解耦：**先在单机 standalone 形态把 Postgres 跑通跑绿，再分布计算**。每期独立交付、独立回归。

| Phase | 目标 | 风险 | 可交付验证 | 状态 |
|---|---|---|---|---|
| **P1 DB 层 Postgres 化** | standalone 单机跑在 PG 上、全量测试绿 | **最高**（方言回归面最广） | 真实库快照导入 PG，行为对拍 | ✅ 已落地 |
| **P2 profile 文件进 PG** | 人格/记忆内容入 PG，文件 I/O 收敛 storage 接口 | 中 | 存量文件导入，读写一致 | ✅ 已落地 |
| **P3 RPM/配额共享化** | RPM 从进程内迁中心共享（PG `rpm_hits` 表） | 低 | 多进程并发限流正确 | ✅ 已落地 |
| **P4 计算下沉 + 调度分片** | 节点跑完整 app、直连 PG；调度按节点扫描 | 中 | 节点本地 E2E 回环 | ✅ 已落地 |
| **P5 灰度切换** | 账号逐步迁到节点，全绿铺开 | 中 | 逐账号灰度，可回滚 | ⏳ 待线上推进 |

> P2、P3 与 P1 无强依赖，可并行推进，但都需在 P4 之前完成（节点要能读到 profile、要能共享限流）。

---

## 4. Phase 1 详细方案（DB 层 Postgres 化）

这是整个改造的地基，也是回归面最广的一期。核心策略：**不逐处手改 SQL，用兼容垫片层把方言差异收敛到极少数文件**。

### 4.1 现状盘点（grep 实测，2026-06-20）

| 项 | 数量 | 处理方式 |
|---|---|---|
| `?` 占位符（`app/db/*.py`） | **713 行** | 垫片层翻译 `?`→`%s`，**不手改** |
| `strftime('…','+8 hours')` 北京时间 | **244 处** | DDL 默认值出 PG 版；查询体内的收敛到 dialect helper |
| `sqlite3.` 直接引用（type hint / 异常 / Row） | **150 处** | 别名到 `app/db/_backend.py`，机械替换 |
| `AUTOINCREMENT` | 20 | PG `GENERATED ALWAYS AS IDENTITY` |
| `INSERT OR IGNORE/REPLACE` | 7 | 改写 `ON CONFLICT DO NOTHING/UPDATE` |
| `ON CONFLICT …` | 18 | 多数 PG 兼容，逐处核对目标列 |
| `executescript` | 2（仅迁移） | 拆成多条 `execute` |
| `PRAGMA …` | 13 | 连接期 PRAGMA → PG 无需；`user_version`/`table_info` 见 4.3 |

> 业务层（`turn_service` 等）把 `sqlite3.Connection` 当事务句柄跨模块传（`insert_message(conn=…)` 等）。垫片连接对象需保持同样的「可传递、可显式提交/回滚」语义。

### 4.2 兼容垫片层（新增 `app/db/_backend.py`）

目标：让 713 处 `?`、150 处 `sqlite3.X`、40 处 Row 访问**不用逐个手改**。

1. **连接 / 游标代理**
   - 驱动选 **psycopg 3（sync）**+ 连接池（`psycopg_pool.ConnectionPool`），与现有"短连接、每次 commit"模型对齐。
   - 游标代理在 `execute(sql, params)` 时把 `?`→`%s`（注意跳过字符串字面量内的 `?`；本仓 SQL 内字面量含 `?` 极少，垫片用「按单引号配对分段、仅段外替换」规避）。
   - Row 工厂用 psycopg `dict_row`，再包一层同时支持 `row["col"]` 和 `row[0]`（兼容现存两种访问）。
2. **后端中立别名**：`_backend.py` 导出 `Connection`、`Row`、`IntegrityError`、`connect_raw()`。把全仓 `sqlite3.Connection`/`sqlite3.Row`/`except sqlite3.IntegrityError` 机械替换为 `_backend.*`，type hint 与异常捕获一次性中立化。
3. **双后端开关**：`connect()` 按 `settings.database_url`（新增配置）选 SQLite / PG；`DATABASE_URL` 缺省回落现有 SQLite 路径，**保证 standalone 与测试零行为变化**直到切换。

```python
# app/config.py 新增（示意）
database_url: str = ""          # 空=用 database_path 的 SQLite；postgresql://… = PG
db_pool_min_size: int = 1
db_pool_max_size: int = 8       # 单节点连接池上限（× 节点数 ≤ PG max_connections）
```

### 4.3 迁移框架适配

现状：`PRAGMA user_version` 跟踪版本，`_MIGRATIONS = [(version, apply_fn), …]`（6 个迁移函数），`init_db()` 顺序应用（`app/db/_core.py:186-199`）。

改造（**已落地**）：
- 版本跟踪：`PRAGMA user_version` → **`schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)`** 表。`_run_migrations` 读 `MAX(version)`、顺序补差，整体在一个事务里；`applied_at` 由 Python 侧 `beijing_now_str()` 写入（引导表不引方言默认值）。历史 SQLite 库首次升级时把旧 `PRAGMA user_version` 回填进新表，避免幂等迁移被重复执行。`_MIGRATIONS` 列表与各 `apply_fn` 结构**完全复用**。
- `_table_exists`（用 `sqlite_master`）/ `_ensure_column`（用 `PRAGMA table_info`）→ 改走 `information_schema` / `to_regclass`。注意：按开发约定，新 schema 一律走**新迁移函数**，`_ensure_column` 仅历史补丁残留，迁移时一并清理。
- 基线 m0001 出 **PG 版 DDL**（见 4.4）。生产不靠 m0001 重建（生产是从 SQLite 数据迁移过来，见 4.5），m0001 仅用于全新库 / 测试库。

### 4.4 DDL 方言差异（基线与各迁移逐表过）

| SQLite | PostgreSQL |
|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| `… DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now','+8 hours')))` | `… DEFAULT (to_char((now() AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD HH24:MI:SS'))` |
| 查询体 `datetime('now','+8 hours'[, <mod>])`（WHERE/VALUES/`ON CONFLICT … SET`，含 `strftime` 包裹） | `to_char((now() AT TIME ZONE 'Asia/Shanghai')[ + (<mod>)::interval],'YYYY-MM-DD HH24:MI:SS')`。`<mod>` 各形态（`'-1 hour'` / `?` / `? \|\| ' minutes'` / f-string 字面）恰好都是合法 PG interval。保持 `to_char` 文本输出，匹配 TEXT 时间戳列字典序=时间序比较（**已落地** `_backend.translate_statement`） |
| 查询体 `date('now','+8 hours')` | `to_char((now() AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD')`（当日零点边界 `\|\| ' 00:00:00'` 原样可用） |
| `(julianday(A)-julianday(B))*86400000`（毫秒差，analytics） | `EXTRACT(EPOCH FROM ((A)::timestamp-(B)::timestamp))*1000` |
| `executescript("…多条…")` | 翻译后按 `;` 切分逐条 `execute`（`_backend.split_sql_statements`，确定性优于依赖 psycopg 多语句） |
| 内联 `FOREIGN KEY(...) REFERENCES ...`（SQLite 不校验建表顺序） | **PG 建表时剥离**（`_backend.translate_statement`，DDL/DML 合一翻译器）。本仓 baseline 存在前向外键引用（如 `tool_invocations` 被先定义的表引用），PG 建表要求引用表已存在。账号隔离由 app 层 `WHERE account_id` 保证、不依赖 DB 级 FK；剥离后建表顺序无关。**后续如需 DB 级 FK，用建表后 `ALTER TABLE ADD CONSTRAINT` 延迟补**（可选优化）。 |
| 布尔存 `INTEGER 0/1` | **保持** `INTEGER`（不改成 boolean，最小改动；业务读写 0/1 不变） |
| `metadata_json TEXT` | **保持** `TEXT`（不改 jsonb，最小改动） |
| 时间戳列 `TEXT` 北京时间串 | **保持** `TEXT`（不改 timestamptz，最小改动；排序/比较语义不变） |
| 部分唯一索引 `… WHERE …` | PG 原生支持，语法一致 |
| `CREATE TABLE/INDEX IF NOT EXISTS` | PG 支持 |

> 原则：**类型与语义尽量不动**（TEXT 时间戳、INTEGER 布尔、TEXT json 全保留），只解决"跑不起来"的硬方言差异。把"顺手优化成 jsonb/timestamptz"留到改造稳定之后，避免回归面爆炸。

时间戳默认值（实际落地＝方案 A，见 §7）：DDL **保留** SQLite 的 `strftime/datetime` 字面，由 `_backend.translate_statement` 在 PG 执行前统一翻译为上表 PG 片段，**不**在 DDL 侧引入独立 helper（早期设想的 `_now_bj_sql()` 未实现，已废弃）。查询体内少量 `strftime/now`（WHERE 条件/计算列）同样由垫片翻译；个别确需 Python 侧生成的时间戳走 `app/time_utils.py`。

### 4.5 数据迁移（SQLite → PG）（**已落地**）

- 工具 `scripts/migrate_sqlite_to_pg.py`：复用 `app.db.init_db` 按 `_MIGRATIONS` 在空 PG 建好全量 schema（含 json_patch 函数），再逐表 `SELECT *` from SQLite → 批量 `INSERT` 到 PG（时间戳/JSON/布尔原值搬运）。只迁移源库与目标库**同时存在**的表，历史遗留表（如 contacts）自动跳过；`schema_migrations` 由 init_db 建立、不从源库复制。
- IDENTITY 列（PG `GENERATED ALWAYS`）插入原始 id 须 `OVERRIDING SYSTEM VALUE`，迁移后 `setval(pg_get_serial_sequence(...), MAX(id))` 对齐序列，后续自增不撞号。
- 一致性校验（`reconcile`）：逐表行数对拍；账本按 wallet 勾稽 `SUM(entitlement_ledger.amount_shell_micros)` 与 `entitlement_wallets.balance_shell_micros`；行数不一致则 CLI 退出码非 0。
- 测试 `tests/test_migrate_sqlite_to_pg.py`（真 PG）：覆盖行数对拍、账本勾稽、大额 micros 不溢出（BIGINT）、IDENTITY 序列重置后续号从 MAX+1 继续。

### 4.6 并发与连接（PG 的正题）

- PG 原生多写者：一期为规避 SQLite 单写者而设计的 claim/原子 UPDATE（moderation worker、outbound claim）在 PG 下用 `UPDATE … WHERE … RETURNING` 保持原子，`rowcount` 语义 psycopg 一致，逻辑不变。
- 事务隔离：默认 `READ COMMITTED` 即可；claim 类竞争靠 `WHERE status=… ` 条件 + 行锁（必要处 `FOR UPDATE SKIP LOCKED`）保证不重复领。
- 连接池：每节点一池；`max_connections` 容量规划 = Σ(各节点 `db_pool_max_size`) + 中心自身 + 余量。
- `init_db()` 多实例并发：DDL 迁移只应在**中心**启动时跑一次；节点启动**不跑迁移**（`AI4ALL_ROLE=node` 跳过 `init_db` 的迁移段，或加 PG advisory lock 串行化）。

### 4.7 测试基座（**已落地**）

- `pytest-postgresql` 提供 ephemeral PG：`conftest.py` 加 `AI4ALL_TEST_DB=postgres` 开关，`postgresql_proc`（session 级 PG 进程）+ `postgresql_db`（每测试 create/drop 独立库），经 `db_dsn` fixture 把 `test_settings.database_url` 指向临时库；SQLite 档（默认）完全不引入 pytest-postgresql。
- **现状：PG 档 762 passed / 4 skipped、SQLite 档 766 passed，两档全绿。** 4 个 skipped 是 SQLite 专有基础设施测试（WAL checkpoint 截断、临时 DB 文件拷贝隔离、迁移用 `PRAGMA table_info` 内省），由 conftest 的 `pytest_collection_modifyitems` 按测试名在 PG 档自动跳过。
- 长尾方言修复（运行 PG 档实测暴露 → 收敛到垫片层为主）：
  - `json_extract/json_valid/json_patch` → jsonb 运算（json_patch 用 init 时建的 RFC 7396 递归 PG 函数，保留「null 即删键」语义）；
  - `lastrowid`→`lastval()`、`INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`、`changes()`→`rowcount`、`PRAGMA` 维护函数按后端分支；
  - `CASE WHEN ?` 整型布尔→`= 1`、建表 `INTEGER`→`BIGINT`（SQLite 64 位，防 micros 溢出 int4）、`datetime/date/julianday` 查询体函数；
  - GROUP BY 跨表列补主键入组、ORDER BY 用底层表达式替别名、DO UPDATE 自引用列加表限定（excluded 歧义）；
  - 「catch IntegrityError 后继续用连接」在 PG 会中止整笔事务：高频路径（faq 点赞）改 `INSERT OR IGNORE`+rowcount，id 生成重试循环加 `_savepoint` 子事务隔离。

### 4.8 Phase 1 验收口径

1. `DATABASE_URL=postgresql://…` 下 standalone 启动、`init_db` 建表成功。
2. 真实 `data/ai4all.sqlite3` 快照经迁移脚本导入 PG，行数/账本对拍一致（脚本 + 真 PG 测试**已落地**；待对真实快照实跑一次最终确认）。
3. PG 档全量回归绿（**已达成**：762 passed / 4 SQLite 专有跳过）。
4. `send_mock_turn.py` 对 PG 库完成一轮收发、计费、记忆写入正确。
5. SQLite 档仍可运行（开关回落），保证可灰度、可回滚。

---

## 5. 后续阶段提要（P2–P5，细化留各自小节）

- **P2 profile 进 PG**：新表 `account_profile_files(account_id, filename, content TEXT, version, updated_at)`；`user_profiles.py`/`memory_writer.py` 的文件读写换成 `app/profile_storage.py` 接口（`read_file/write_file/append_file/delete_file/list_filenames/delete_account`，调用点签名不变、按 `account_id` 隔离、接受可选 `conn`）；存量目录一次性导入；wipe 改为删行（不再删目录）。
- **P3 RPM 共享化**：进程内 deque → 中心共享（Redis 滑动窗口，或 PG 计数表）；`daily_usage` 已在 PG，天然共享。
- **P4 计算下沉**：节点 `AI4ALL_ROLE=node` 跑完整 app 直连 PG；入站改本地 `/openclaw/turn`；proactive/dreaming 调度下沉 + 扫描加 `assigned_node_id` 过滤；中心停跑业务调度。
- **P5 灰度**：aliyun1 先 standalone→PG；aliyun2 转 node 直连 PG，挑少量账号 `assigned_node_id` 指过去灰度；全绿逐步铺开，保留回滚到一期中心处理的开关。

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 方言迁移回归面广（713 占位符 / 244 时间戳 / 150 引用） | 垫片层收敛，不手改；PG 档全量回归；真实快照对拍 |
| 账本/钱包一致性在多写者下出错 | 保留 idempotency_key 唯一约束；迁移后勾稽校验；claim 用 `FOR UPDATE SKIP LOCKED` |
| 中心 PG 成为单点 | 一期可接受（低负载）；二期上 PG 流复制热备（替代调研里的 Litestream） |
| 节点直连中心 PG 的网络抖动 | 连接池 + 重试；内网低延迟；turn 失败回落"稍后再发"既有兜底 |
| `init_db` 多实例并发跑迁移 | 仅中心跑迁移；节点跳过；必要时 PG advisory lock |
| 测试变慢（ephemeral PG） | 纯逻辑测试保留 SQLite；PG 档 session 级复用容器 |

---

## 7. 待确认子决策

- [x] **PG 部署形态：中心同机主库 + 一台备机热待命随时切换。** 详见 [§8](#8-pg-部署与切换)。
- [x] **PG 备份：流复制热备（秒级切换硬件宕机）+ 备机每日 `pg_dump`/WAL 归档（PITR 防逻辑事故）。** 详见 §8。
- [x] **时间戳：本期保 DDL 默认值（方案 A），不收敛到 Python 侧。** 理由：① 把 P1 回归面压到最小（改动集中在 DDL，业务层不动）；② DB 单一时钟天然契合多节点——所有节点写同一张 `messages`，时间统一由主库发，避免节点间时钟漂移导致跨节点乱序；③ 方案 B 的「引擎无关」价值已被「定死 PG」抵消，仅剩「测试可冻结时间」一个真实好处，可留作后续独立小重构，不绑进 P1。注意：查询体内少量 `strftime/now`（WHERE 条件/计算列，非列默认值）仍需翻译，跑不掉。
- [x] **RPM 共享存储：PG 计数表（`rpm_hits`，原子 DELETE+COUNT+INSERT，P3 已落地）。** 选择理由：无新依赖（已有 PG），事务语义自然，低 QPS 场景性能足够；Redis 留作后续高并发优化选项。

---

## 8. PG 部署与切换

中心同机主库 + 一台备机热待命，是标准的 **PG 主库 + 流复制热备（streaming replication）**，正好复用一期（`multi_node_access_refactor.md` §8）预留的「中心可切换」能力。

```
┌─ 中心主机 (center, 现 aliyun1) ─┐    流式复制 (WAL)    ┌─ 备机 (standby) ──────┐
│  center app (控制面)            │  ─────────────────▶ │  PG standby (热备)      │
│  PG primary (同机, app 走本地)   │   async, 亚秒级延迟   │  + 每日 pg_dump          │
└────────────────────────────────┘                     │  + WAL 归档 (PITR)       │
        ▲ 节点直连(内网连接池)                            │  + 待命的 center app(温中心)│
        │                                               └─────────────────────────┘
   多节点(node) ── 连接串列两台主机 + target_session_attrs=read-write
```

### 8.1 主备形态

- **物理流复制**（streaming replication），低负载下用 **async**：主库提交不等备库，延迟亚秒级，主库可用性不被备库拖累。代价是极端崩溃瞬间可能丢最后几条已提交事务。
- **账本可选混合同步**：若要账本零丢失，可只对账本相关事务设 `synchronous_commit=on`（按事务粒度），其余保持 async。低额场景下默认全 async 亦可接受（有 `idempotency_key` 兜底）。
- 备机 `hot_standby=on`，将来可把 admin/analytics 只读查询分流到备机（本期不必）。

### 8.2 客户端如何找主库（不靠翻 DNS）

节点与中心统一用多主机连接串，由 libpq 选「可写」那台：

```
postgresql://ai4all@center-host:5432,standby-host:5432/ai4all?target_session_attrs=read-write
```

切换后备库被提升为可写，客户端重连自动落到新主库，**无需手动改 hosts 别名**，比一期 `CENTRAL_URL` 翻别名更省事。

### 8.3 热备 ≠ 备份，备机担双角色

- 流复制 standby **不是备份**——删除/误操作/逻辑损坏会一并同步过去。
- 备机**额外**跑每日 `pg_dump` + WAL 归档，提供 **PITR（时间点恢复）**，专防 standby 救不了的逻辑事故。
- 一句话：**standby 防硬件宕机（秒级切换），pg_dump/PITR 防逻辑事故（回到任意时刻）。** 这取代了调研文档里给 SQLite 用的 Litestream。

### 8.4 切换流程（手动 + runbook，不上自动故障转移）

Patroni/repmgr 对当前负载是过度工程。手动流程：

1. `SELECT pg_promote();` 提升备库为主库。
2. 客户端靠 `target_session_attrs=read-write` 连接串自动重连到新主库。
3. 在备机起 center app（控制面），翻一期的中心 hosts 别名指向备机（与一期「aliyun1↔aliyun2 中心切换」同一套心智）。
4. 老主机修复后用 `pg_rewind` 作为新 standby 重新挂回，恢复主备拓扑。

> 备机 = **温中心**：standby PG + 待命 center app。切换 = 提升 PG 主库 + 起控制面 + 翻中心端点。

### 8.5 注意点

- **同机资源争用**：PG 与 center app 同机，低负载无碍；需监控连接数/内存。
- **迁移只在中心跑**：`init_db` 的 DDL 迁移仅中心启动执行；节点 `AI4ALL_ROLE=node` 跳过迁移段（见 §4.6），避免多实例并发建表。
- **容量规划**：`max_connections` ≥ Σ(各节点 `db_pool_max_size`) + 中心自身 + 余量。
