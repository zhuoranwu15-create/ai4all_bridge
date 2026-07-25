# Campaign 漏斗埋点与统计 技术设计 v1（获客→激活→onboarding 完成）

> 状态：**代码已落地（改动 #1–#6），待运营建活码 + 前端联调**。全量回归 1228 passed（新增 20 用例）。
> 目标：给每个营销活码（campaign）建立一条可监控、近实时的转化漏斗，用于评估各 campaign 的获客与激活效果。
> 关联：[campaign_codes_technical_design](campaign_codes_technical_design.md)、[campaign_persona_v1_technical_design](campaign_persona_v1_technical_design.md)、[campaign_persona_marketing_playbook_v1](../../../products/zhaoxi/capabilities/campaign_persona_marketing_playbook_v1.md)

---

## 0. 范围与既定决策

针对"用户点击带活码的营销链接 → 注册 → onboarding"这条链路，补齐系统内可观测节点的埋点与聚合，让运营能按 campaign 看漏斗。三个高风险点已拍板：

| 决策点 | 结论 | 影响 |
|---|---|---|
| 落地页「点击 PV/UV」怎么测 | **建 beacon 上报端点，测全漏斗**。落地页加载即上报 `campaign_code`，系统内测 PV/UV（含未转化点击） | 需新增 1 个**无鉴权公开端点** + 前端一行上报 + 匿名 `visitor_token` 去重；防刷是主要风险 |
| 统计数据从哪看、什么时效 | **在线 admin 端点，近实时**。查询时对现有表现算聚合，不做离线 ETL | 复用既有表 + 加读/聚合层 + 1 个 admin 端点；不新增 nearline 数仓作业 |
| 漏斗测到哪一层 | **到 onboarding 完成为止**。曝光→注册→扫码→激活→onboarding 各步→完成 | 不接 D1/D7 留存与首次付费；下游质量信号留待 v2 |

**不在本期范围**：留存/付费等下游质量信号；个体级漏斗串联（"这个访客后来注册了"——v1 只做 campaign 聚合计数漏斗，不做单人 stitch）；归因失败的落表（仅记录盲点，见 §8）；跨设备/清缓存 UV 精确去重。

---

## 1. 现状核对（可观测 vs 盲点）

活码 `code` **不从微信 inbound 进系统，而是从 Web 落地页 URL 参数 `campaign_code` 采集**（`app/static/onboarding.html:178` / `home.html:174` 前端 JS 读 `URLSearchParams`），随注册/登录 payload 上送后端（`web.py:858 /web/register-and-binding-intent`、`web.py:973 /web/login`）。因此：

| 链路节点 | 可观测性 | 现有数据源（file:line） |
|---|---|---|
| 站外曝光 / 点击营销链接 | **盲点**（系统外） | 无。只能靠营销平台后台/微信官方扫码统计 |
| 落地页加载（带 `campaign_code`） | **当前盲点**：前端 JS 读 param，纯访问不落后端；只有走到注册请求才随 payload 到后端 | 无后端留痕 → **本设计补 beacon** |
| 手机号 OTP 注册 | 可观测 | `platform_users` 建行（`web.py:842`） |
| 带活码注册成功（账号+归因） | **可观测（强锚点）** | `account_campaign_attribution`（PK `account_id`，`ON CONFLICT DO NOTHING` 天然去重；`campaign_code` + `attributed_at`，索引 `ix_account_campaign_attribution_code`）；写入点 `billing.py:2142` |
| 活码累计转化 | 可观测（但只累计、无时间分桶） | `campaign_codes.used_count`（`campaign.py:287`） |
| 扫码绑定微信成功 | 可观测 | `binding_intents.status='completed'` + `completed_at`（`_core.py:549,555`，索引 `(account_id, created_at)`）；完成点 `web.py:187-213` |
| 激活（首条微信消息 / channel 建立） | 可观测 | `channel_bindings.first_seen_at`（`_core.py:648`）；或首条 `messages`（inbound/user） |
| onboarding 首条欢迎语已发（pending→step1） | 可观测 | `turn_service.py:894-909` + `onboarding_state_changed` 事件 |
| onboarding 各步跃迁 / 完成 | **可观测（现成事件流）** | `analytics_events` 表 `event_name='onboarding_state_changed'`（`accounts.py:1220-1228`）；`persona_selected`（`onboarding.py:444`） |

**既有埋点基建（可复用）**：
- `analytics_events` 表（`_core.py:1070-1087`）：append-only 事件流，列 `account_id / event_name / from_state / to_state / source / properties_json / event_time`，索引 `(event_name,event_time)`、`(account_id,event_time)`。写入原语 `record_analytics_event()`（`accounts.py:1178`，注释明确"仅写元数据，严禁写用户正文"）。
- **关键约束**：`analytics_events.account_id` 是 `TEXT NOT NULL` + `FOREIGN KEY→accounts(id)`（`_core.py:1072,1080`）。**落地页曝光发生在账号创建之前，没有 account_id，因此不能进这张表**——曝光数据必须独立存（见 §3.1）。
- 时间工具：`time_utils.beijing_now()`（`time_utils.py:13`）；北京自然日口径与 usage 端点（`admin_accounts.py:302`）一致。

**现有缺口**：`analytics_events` 有写无读——0 个查询/聚合函数、无 admin 端点消费、无 campaign 维度；`admin_campaigns.py` 只有活码 CRUD、无统计端点；`used_count` 是 campaign 现有唯一数字（无时间分桶、无中间漏斗步骤）。

---

## 2. 漏斗模型（v1 定义）

按 campaign 聚合的**计数漏斗**（不串联个体）。每一阶段是"按 `campaign_code` + 北京日"的计数，漏斗即相邻阶段计数之比：

| 阶段 | 指标 | 口径 | 数据源 |
|---|---|---|---|
| **S0 曝光** | PV / UV | PV=落地页加载次数；UV=去重 `visitor_token` | `campaign_visits`（新表，§3.1） |
| **S1 注册成功** | 带活码注册数 | 去重 `account_id` | `account_campaign_attribution`，按 `attributed_at` 北京日分桶 |
| **S2 扫码成功** | 完成绑定数 | 归因账号中 `binding_intents.status='completed'`，取每账号最早 `completed_at` | `binding_intents` JOIN attribution |
| **S3 激活** | 首条消息 / channel 建立 | 归因账号中有 `channel_bindings` 行，取 `first_seen_at` | `channel_bindings` JOIN attribution |
| **S4 onboarding 进行** | 各步到达数 | 归因账号的 `onboarding_state_changed` 事件按 `to_state` 计数（step1_sent / step2_sent） | `analytics_events` JOIN attribution |
| **S5 onboarding 完成** | 完成数 | `to_state='complete'`（或 `accounts.onboarding_state='complete'`） | `analytics_events` / `accounts` JOIN attribution |

**核心转化率**：S1/S0（落地页转化）、S2/S1（扫码转化）、S5/S1（激活完成率）。

> **设计要点**：S1–S5 全部**可从既有表 join `account_campaign_attribution` 现算得到，无需新增任何事件写入**。唯一缺的是 S0（曝光），它在账号存在前发生，必须新建独立存储。因此本设计的新增写路径**只有一处**（beacon + `campaign_visits`），其余都是读/聚合层。这是最小改动路径。

---

## 3. 数据存储设计

分两层：**账号存在前的匿名曝光**（新表）+ **账号存在后的漏斗**（复用既有表按 `campaign_code` 归组）。

### 3.1 新表 `campaign_visits`（S0 曝光，唯一新增写路径）

匿名、campaign-scoped、无 account_id、无 PII：

```sql
CREATE TABLE IF NOT EXISTS campaign_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_code TEXT NOT NULL,           -- 落地页 URL param，未知/非法则落 '' 或专用 'unknown'
    visitor_token TEXT,                    -- 前端 localStorage 生成的匿名随机 id，用于 UV 去重
    page TEXT,                             -- 'onboarding' | 'home'，区分落地页
    referrer TEXT,                         -- document.referrer（可选，粗粒度来源）
    user_agent TEXT,                       -- 粗粒度 UA（可选，判端/防刷）
    visit_date TEXT NOT NULL,              -- 北京自然日 YYYY-MM-DD，服务端写入，聚合分桶用
    event_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
);
CREATE INDEX IF NOT EXISTS ix_campaign_visits_code_date ON campaign_visits(campaign_code, visit_date);
```

- **PV** = `COUNT(*)`；**UV** = `COUNT(DISTINCT visitor_token)`，按 `(campaign_code, visit_date)`。
- `visitor_token`：前端首次加载生成 UUID 存 `localStorage`，同一微信内置浏览器内复用即 UV 去重。局限见 §8。
- 通过**新增迁移函数追加到 `_MIGRATIONS`**（下一个版本号 20，遵循 CLAUDE.md schema 规范），不用启动期 `_ensure_column`。

> **账号隔离不变量说明**：CLAUDE.md 的"按 `account_id` 隔离"针对**账号级用户数据**。`campaign_visits` 是**账号创建之前**的匿名营销遥测，天然无 account、无用户正文/PII，按 `campaign_code` 归组是其正确 scope，**不构成隔离违规**。该表永不与具体用户/账号 join（v1 不做个体串联）。此例外在此显式声明。

### 3.2 账号存在后（S1–S5）：复用既有表，`campaign_code` 现算归组

不新增表、不新增事件。归组锚点是 `account_campaign_attribution`（`account_id → campaign_code`）：

- **S1 注册**：`account_campaign_attribution` 直接按 `campaign_code` GROUP BY，`attributed_at` 取北京日。
- **S2 扫码**：`binding_intents` WHERE `status='completed'`，JOIN attribution ON `account_id`，每账号取 `MIN(completed_at)`。
- **S3 激活**：`channel_bindings` JOIN attribution ON `account_id`，取 `MIN(first_seen_at)`。
- **S4/S5 onboarding**：`analytics_events` WHERE `event_name='onboarding_state_changed'` JOIN attribution ON `account_id`，按 `to_state` + `event_time` 北京日计数。（S5 亦可用 `accounts.onboarding_state='complete'` 做即时快照校验。）

聚合查询范式参考 `ops.py:475-503`（一次 SQL 条件 SUM 出多桶）。

### 3.3 是否落 campaign×day 聚合表？——v1 不落

按决策②（在线近实时），漏斗端点**查询时现算**。数据量级（单 campaign 注册数、事件数）在活码规模下现算足够快，且 `account_campaign_attribution.ix_..._code`、`analytics_events(event_name,event_time)`、`binding_intents(account_id,created_at)` 索引已支撑。若未来 campaign 数或历史窗口增大导致慢查询，再引入按天聚合表或走 nearline ETL（见 §8 延后项）。

---

## 4. 埋点与采集点（改动落点，供后续实现）

**新增写路径只有 A；B 是纯读聚合；C 是出口端点。**

### A. 落地页 beacon（S0，唯一新写入）

- **前端**（`app/static/onboarding.html`、`home.html`）：页面加载时，若 URL 带 `campaign_code`，生成/读取 `localStorage` 的 `visitor_token`，`fetch('POST /web/campaign-visit', {campaign_code, visitor_token, page})`。`keepalive: true`，失败静默（不阻塞落地页）。
- **后端**（`app/routers/web.py` 新 handler `POST /web/campaign-visit`）：
  - **无鉴权**（落地页公开访问）。
  - 校验 `campaign_code` 命中现存活码（`validate_campaign_code` / 轻量存在性查询）；非法 code 落 `'unknown'` 或直接丢弃，避免脏数据与刷量放大。
  - 服务端计算 `visit_date = beijing_now().date().isoformat()`（不信任前端时间）。
  - 写 `campaign_visits`；**fail-open**，异常只 log 不抛。
  - 基础防刷：按 `visitor_token`/IP 轻量限频（复用 `rate_limiter` 思路或简单内存窗口），至少防单一来源刷爆。

### B. S1–S5：无新增埋点，新增读/聚合层

- 新增 `app/products/zhaoxi/infrastructure/persistence/campaign.py`（或新 `app/products/zhaoxi/infrastructure/persistence/campaign_analytics.py`）聚合函数，例如：
  - `get_campaign_visit_stats(campaign_code, date_from, date_to) -> {pv, uv, by_day[]}`（读 `campaign_visits`）。
  - `get_campaign_funnel(campaign_code, date_from, date_to) -> {registered, scanned, activated, onboarding_step1, step2, completed, by_day[]}`（现算 join 既有表）。
- 全部只读，按 `campaign_code` 约束；不写、不改既有业务表。

### C. 出口：admin 统计端点

- `app/products/zhaoxi/api/admin_campaigns.py` 新增：
  - `GET /admin/campaign-codes/{code}/stats?from=YYYY-MM-DD&to=YYYY-MM-DD` → 返回该 code 的 S0–S5 计数、核心转化率、按天 breakdown。
  - （可选）`GET /admin/campaign-codes/stats` → 全活码总览（各 code 一行）。
- 鉴权沿用 admin/staff 依赖（`Depends` 现有 `verify_admin_auth`）。
- 新 router 若 `from app.config import settings`，按 CLAUDE.md 约定在 `tests/conftest.py` 的 `fresh_db`/`client` 两 fixture 各补 per-module patch（本例复用现有 `admin_campaigns` 已在则无需重复）。

---

## 5. 数据流

```
[站外] 用户点营销链接（?campaign_code=XXX）        ← 盲点（系统外）
   │
[落地页] onboarding.html/home.html 加载
   ├─(A) beacon → POST /web/campaign-visit → campaign_visits(campaign_code, visitor_token, visit_date)   ← S0 PV/UV
   │
[注册] POST /web/register-and-binding-intent | /web/login  （payload.campaign_code）
   └─ get_or_create_default_ai4all_account_for_user(campaign_code)
        └─ create_ai4all_account_for_user → billing.py:2142 apply_campaign_code_attribution(increment_usage=True)
             └─ account_campaign_attribution(account_id, campaign_code, attributed_at)   ← S1 注册
   │
[扫码] _wait_for_binding_intent → _complete_binding_intent_from_wait_result(web.py:187)
   └─ binding_intents.status='completed', completed_at；upsert channel_bindings                ← S2 扫码 / S3 激活锚点
   │
[首条消息] turn_service.py:894 发 ONBOARDING_WELCOME_TEXT；channel_bindings.first_seen_at        ← S3 激活
   │
[onboarding] set_account_onboarding_state → analytics_events 'onboarding_state_changed'           ← S4/S5
        step1_sent → step2_sent → complete
   │
[出口] GET /admin/campaign-codes/{code}/stats
        └─ campaign_visits（S0）+ attribution/binding/channel/analytics_events 现算 join（S1–S5）
```

---

## 6. 账号隔离 / 隐私 / 合规

- **账号隔离**：S1–S5 所有查询均以 `account_campaign_attribution.campaign_code` → `account_id` 为约束路径，不引入跨账号写入；聚合是只读、按 campaign 归组的计数，不返回单账号明细。`campaign_visits` 无 account 维度（§3.1 已声明例外）。
- **隐私**：`campaign_visits` 只存匿名 `visitor_token`（随机、非 PII）、粗粒度 UA/referrer、campaign_code；**不存手机号、微信 id、正文**。沿用 `record_analytics_event` 的"仅元数据"红线。
- **无鉴权端点**：`/web/campaign-visit` 公开可写是刻意的（落地页匿名访问），因此**不接受任何账号/用户标识入参**，只接受 campaign_code + 匿名 token，把它当作可被污染的低可信遥测（刷量只影响 PV/UV 观感，不影响 S1+ 强锚点）。

---

## 7. 改动清单（供后续实现，本期不写代码）

总览：

| # | 文件 | 动作 | 类型 | 依赖 |
|---|---|---|---|---|
| 1 | `app/db/_core.py` `_MIGRATIONS` | 追加迁移 v20：建 `campaign_visits` 表 + 索引 | schema 迁移 | — |
| 2 | 新 `app/products/zhaoxi/infrastructure/persistence/campaign_analytics.py`（`app/db/__init__.py` 重导出） | `record_campaign_visit()` + `get_campaign_visit_stats()` + `get_campaign_funnel()` | 数据访问层 | #1 |
| 3 | `app/routers/web.py` | 新 `POST /web/campaign-visit`（无鉴权、fail-open、校验 code、服务端算 visit_date、轻量防刷） | router | #2 |
| 4 | `app/static/onboarding.html`、`home.html` | 加载时上报 beacon（visitor_token via localStorage） | 前端 | #3 |
| 5 | `app/products/zhaoxi/api/admin_campaigns.py` | `GET /admin/campaign-codes/{code}/stats`（+ 可选总览） | router | #2 |
| 6 | `tests/` | 迁移/写入/聚合/端点用例 | 测试 | #1–#5 |

**无需改动**：`turn_service.py`、`onboarding.py`、`billing.py`、`agent_self_state.py` 等主链路——S1–S5 全靠现有落库数据 join，零侵入。

> **双后端 SQL 约定**：所有列存的是北京墙钟裸串 `'YYYY-MM-DD HH:MM:SS'`。按天分桶统一用 **`substr(col, 1, 10)`**（SQLite/PG 通用），**不要**用 `strftime`（PG 无）或 `date()`（语义不一致）。日期区间入参 `:date_from` / `:date_to` 为 `'YYYY-MM-DD'`，用 `BETWEEN`（含右端当天）。绑定参数用命名占位符，具体风格随 `_backend` 垫片。

### 7.1 任务①——迁移 v20：`campaign_visits` 表

- **步骤**
  1. 在 `app/db/_core.py` 定义 `def _migration_0020_campaign_visits(conn):`，`conn.executescript` 建表 + 索引（DDL 见 §3.1，双后端通用；`INTEGER PRIMARY KEY AUTOINCREMENT` 在 PG 垫片下已有等价处理，参考现有表写法）。
  2. 追加 `(20, _migration_0020_campaign_visits)` 到 `_MIGRATIONS`（末尾；上一个是 19）。
  3. 迁移函数写文档字符串：说明这是 campaign-scoped 匿名曝光表、无 account_id 的理由（§3.1 例外）。
- **自审点**：不触碰基线 m0001；`IF NOT EXISTS` 保证幂等；不使用启动期 `_ensure_column`（建整表用迁移函数）。
- **验证**：`init_db()` 后 `PRAGMA user_version` = 20；`campaign_visits` 表与 `ix_campaign_visits_code_date` 存在。

### 7.2 任务②——数据访问层 `campaign_analytics.py`

新建独立模块（避免把只读分析逻辑塞进已偏大的 `campaign.py`），在 `app/db/__init__.py` 重导出，保持 `from app.db import X` 接口惯例。三个函数，全部只读/只追加，全部按 `campaign_code` 约束：

- `record_campaign_visit(*, campaign_code, visitor_token, page, referrer=None, user_agent=None) -> None`
  - 服务端算 `visit_date = beijing_now().date().isoformat()`（不信前端时间）。
  - `INSERT INTO campaign_visits(...)`；**调用方 fail-open**（本函数可抛，端点吞异常）。
- `get_campaign_visit_stats(*, campaign_code, date_from, date_to) -> {"pv": int, "uv": int, "by_day": [{"date","pv","uv"}]}`
  - SQL 见 §7.7-A。
- `get_campaign_funnel(*, campaign_code, date_from, date_to) -> FunnelResult`
  - 内部跑 S1–S5 各段查询（§7.7-B…E），在 Python 组装成漏斗结构 + 转化率。分段查询比单条大 join 可读、可测、可复用。
  - 返回结构建议：
    ```python
    {
      "campaign_code": "...",
      "range": {"from": "...", "to": "..."},
      "totals": {
        "pv": int, "uv": int,
        "registered": int, "scanned": int, "activated": int,
        "onboarding_step1": int, "onboarding_step2": int, "completed": int,
      },
      "rates": {                       # 分母为 0 时返回 None，不做除零
        "visit_to_register": float|None,   # registered / uv
        "register_to_scan": float|None,    # scanned / registered
        "register_to_complete": float|None # completed / registered
      },
      "by_day": [ {"date", "pv","uv","registered","scanned","activated","step1","step2","completed"} ],
    }
    ```
- **自审点**：所有查询带 `campaign_code = ?` 谓词；不返回任何单账号明细（只出计数）；除零保护；`by_day` 以日期为键做 outer-merge（某天可能只有部分阶段有数）。

### 7.3 任务③——beacon 端点 `POST /web/campaign-visit`

- **步骤**
  1. 在 `app/routers/web.py` 定义请求模型 `WebCampaignVisitRequest(campaign_code: str, visitor_token: Optional[str], page: Optional[str])`。
  2. handler **不加** `Depends(_require_session)` / 任何鉴权（落地页公开）。
  3. 逻辑：① 轻量存在性校验 `campaign_code`（命中现存活码则原样落；否则落 `'unknown'` 或直接 `return {"status":"ignored"}`，避免脏数据放大）；② `request.headers` 取粗粒度 `user_agent`、`referer`；③ 调 `record_campaign_visit(...)`；④ **整体 try/except 吞异常**，永远返回 `{"status":"ok"}`（beacon 不因后端错误影响落地页）。
  4. 防刷：按 `visitor_token` 或客户端 IP 做轻量限频（复用 `app/rate_limiter.py` 的窗口思路，或简单内存计数）；超限静默丢弃。
- **自审点**：无鉴权端点绝不接受账号/用户标识入参；不写用户正文；fail-open；限频不阻塞。
- **测试 patch 约定**：若该 handler 新 `from app.config import settings` 引用，`tests/conftest.py` 的 `fresh_db`/`client` 两 fixture 已 patch `app.routers.web.settings`（web router 已在册则无需新增）。

### 7.4 任务④——前端 beacon 上报

- `app/static/onboarding.html`、`home.html` 各加一小段（复用已有 `initCampaignCodeFromUrl` 拿到的 `campaign_code`）：
  ```js
  (function reportCampaignVisit() {
    var code = new URLSearchParams(location.search).get('campaign_code');
    if (!code) return;
    var vt = localStorage.getItem('ai4all_vt');
    if (!vt) { vt = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
               localStorage.setItem('ai4all_vt', vt); }
    fetch('/web/campaign-visit', {
      method: 'POST', keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ campaign_code: code, visitor_token: vt, page: 'onboarding' })
    }).catch(function () {});   // 静默失败，绝不阻塞落地页
  })();
  ```
  `home.html` 里 `page: 'home'`。
- **自审点**：失败静默；`keepalive` 防页面跳转丢包；不采集任何 PII。

### 7.5 任务⑤——admin 统计端点

- 在 `app/products/zhaoxi/api/admin_campaigns.py`：
  - `GET /admin/campaign-codes/{code}/stats?from=YYYY-MM-DD&to=YYYY-MM-DD` → 调 `get_campaign_visit_stats` + `get_campaign_funnel`，合并返回。缺省区间给最近 14 天（服务端算北京日）。
  - 鉴权沿用本文件现有 admin 依赖。
  - （可选）`GET /admin/campaign-codes/stats` 全活码总览：对 `list_campaign_codes()` 每个 code 出 `totals`（注意 N+1，活码量大时限制或分页）。
- **自审点**：入参日期格式校验；区间上限（如 ≤92 天）防全表扫；只出聚合。

### 7.6 任务⑥——测试

- `tests/test_db_campaign_analytics.py`（新）：
  - 迁移后表存在；`record_campaign_visit` 写入 + `get_campaign_visit_stats` 的 PV/UV（造同 `visitor_token` 多行验 UV 去重、跨天验 `by_day`）。
  - `get_campaign_funnel`：造 `account_campaign_attribution` + `binding_intents(status='completed')` + `channel_bindings` + `analytics_events('onboarding_state_changed')` 数据，断言 S1–S5 计数与转化率；验 `campaign_code` 隔离（另一个 code 的数据不串）；验除零保护（无 UV 时 `visit_to_register=None`）。
- `tests/test_web_campaign_visit.py`（新）：beacon 端点合法 code 落库、非法 code 被忽略、异常 fail-open 返回 ok、限频。
- `tests/test_admin_campaigns.py`（扩展）：`/stats` 端点鉴权 + 返回结构 + 日期区间校验。
- 触及共享持久化（新表 + 迁移），提交前跑全量回归。

### 7.7 SQL 聚合草稿（双后端中立，`substr(col,1,10)` 按北京日分桶）

**A. S0 曝光 PV/UV（`campaign_visits`）**
```sql
-- 汇总
SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor_token) AS uv
FROM campaign_visits
WHERE campaign_code = :code
  AND visit_date BETWEEN :date_from AND :date_to;

-- 按天
SELECT visit_date AS day,
       COUNT(*) AS pv,
       COUNT(DISTINCT visitor_token) AS uv
FROM campaign_visits
WHERE campaign_code = :code
  AND visit_date BETWEEN :date_from AND :date_to
GROUP BY visit_date
ORDER BY day;
```

**B. S1 注册（`account_campaign_attribution`；`account_id` 是 PK，`COUNT(*)` 即去重账号）**
```sql
SELECT substr(attributed_at, 1, 10) AS day, COUNT(*) AS registered
FROM account_campaign_attribution
WHERE campaign_code = :code
  AND substr(attributed_at, 1, 10) BETWEEN :date_from AND :date_to
GROUP BY substr(attributed_at, 1, 10)
ORDER BY day;
```

**C. S2 扫码成功（每账号取最早 `completed_at`，再按天计账号数）**
```sql
WITH scanned AS (
  SELECT a.account_id, MIN(bi.completed_at) AS scanned_at
  FROM account_campaign_attribution a
  JOIN binding_intents bi ON bi.account_id = a.account_id
  WHERE a.campaign_code = :code
    AND bi.status = 'completed'
    AND bi.completed_at IS NOT NULL
  GROUP BY a.account_id
)
SELECT substr(scanned_at, 1, 10) AS day, COUNT(*) AS scanned
FROM scanned
WHERE substr(scanned_at, 1, 10) BETWEEN :date_from AND :date_to
GROUP BY substr(scanned_at, 1, 10)
ORDER BY day;
```

**D. S3 激活（`channel_bindings.first_seen_at`，每账号取最早）**
```sql
WITH activated AS (
  SELECT a.account_id, MIN(cb.first_seen_at) AS activated_at
  FROM account_campaign_attribution a
  JOIN channel_bindings cb ON cb.account_id = a.account_id
  WHERE a.campaign_code = :code
  GROUP BY a.account_id
)
SELECT substr(activated_at, 1, 10) AS day, COUNT(*) AS activated
FROM activated
WHERE substr(activated_at, 1, 10) BETWEEN :date_from AND :date_to
GROUP BY substr(activated_at, 1, 10)
ORDER BY day;
```

**E. S4/S5 onboarding 各步到达（`analytics_events` JOIN attribution，去重账号）**
```sql
SELECT e.to_state,
       substr(e.event_time, 1, 10) AS day,
       COUNT(DISTINCT e.account_id) AS accounts
FROM analytics_events e
JOIN account_campaign_attribution a ON a.account_id = e.account_id
WHERE a.campaign_code = :code
  AND e.event_name = 'onboarding_state_changed'
  AND e.to_state IN ('step1_sent', 'step2_sent', 'complete')
  AND substr(e.event_time, 1, 10) BETWEEN :date_from AND :date_to
GROUP BY e.to_state, substr(e.event_time, 1, 10)
ORDER BY day, e.to_state;
```

**E'. S5 完成的即时快照（可选，交叉校验；无历史分桶）**
```sql
-- onboarding_updated_at 只存最后一次变更，故快照只能给"当前完成数"，不能按历史日分桶。
-- 历史按天完成数以 E（to_state='complete' 的事件流）为准；本查询仅用于对当前总数做 sanity check。
SELECT COUNT(*) AS completed_now
FROM account_campaign_attribution a
JOIN accounts ac ON ac.id = a.account_id
WHERE a.campaign_code = :code
  AND ac.onboarding_state = 'complete';
```

**组装说明**：`get_campaign_funnel` 分别执行 A–E，按 `day` 做 outer-merge 成 `by_day`，并对各阶段求和成 `totals`，最后算 `rates`（分母 0 → `None`）。S4/S5 的 `to_state` 结果按 `step1_sent→onboarding_step1`、`step2_sent→onboarding_step2`、`complete→completed` 映射。转化率分母口径：`visit_to_register` 用 `uv`（非 pv），`register_to_scan`、`register_to_complete` 用 `registered`。

> **边界与注意**：
> - **CTE 兼容**：SQLite ≥3.8.3 与 PG 均支持 `WITH`；本项目基线满足。
> - **强制人设活码的跳步**：campaign 若强制了 AI 名字+人设，onboarding 可能 `step1_sent→complete` 跳过 `step2_sent`（`onboarding.py:507-508`）。漏斗中 `onboarding_step2 < onboarding_step1` 属正常，非数据缺失。
> - **归因失败不入 S1**：用失效码注册的账号无 attribution 行，S1 及其后全部段都不计入该 campaign（§8.2 盲点）。
> - **索引利用**：B 走 `ix_account_campaign_attribution_code`；C 走 `ix_binding_intents_account_created`；D 走 `ix_channel_bindings_account_seen`；E 走 `ix_analytics_events_account`/`_name_time`；A 走 `ix_campaign_visits_code_date`。均无全表扫。

---

## 8. 已知盲点与延后项（v1 记录，不修）

1. **站外曝光/点击不可归因**：链接点击→落地页加载这段在系统外。S0 只能从"落地页加载 beacon"算起；真正的曝光/点击数需营销平台后台 + 微信官方扫码统计对照。
2. **归因失败不落表**：活码过期/disabled/账号已归因时，`apply_campaign_code_attribution` 只 `log.warning`、不写 `account_campaign_attribution`（`campaign.py:379,413`）。后果：**用了失效码的用户会注册成功但不计入该 campaign 的 S1**，使 campaign 看起来转化差，实为码配置错。v1 只记录此盲点；若需监控失效码，v2 可加一张 `campaign_attribution_failures` 轻量计数表或从日志采集。
3. **UV 去重局限**：`visitor_token` 基于 `localStorage`——清缓存/换设备/换浏览器会重复计 UV；微信内置浏览器分享打开一般可保持。v1 接受 campaign 级近似 UV。
4. **个体级串联未做**：v1 是聚合计数漏斗，不追"某访客后来注册了哪个账号"。如需精确 S0→S1 单人转化，需把 `visitor_token` 透传进注册请求并落到 attribution，涉及隐私与主链路改动，留待 v2。
5. **onboarding `timed_out` 无自动写入方**：流失只能靠 `onboarding_updated_at` 超时推断，无显式"放弃"事件（`onboarding.py:526` 的 `check_onboarding_timeout` 无生产调用方）。v1 漏斗用"未到达 complete 且停在 stepN"表达流失即可。
6. **在线现算的规模上限**：campaign 数/历史窗口显著增大后，`/stats` 现算可能变慢，届时再引入 campaign×day 聚合表或 nearline ETL（决策②的升级路径）。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| `/web/campaign-visit` 无鉴权被刷 | 只影响 S0 观感，不污染 S1+ 强锚点；校验 code 命中活码 + 轻量限频 + 丢弃非法 code |
| beacon 阻塞/拖慢落地页 | 前端 `keepalive` 异步上报、失败静默；后端 fail-open |
| 新表迁移影响双后端 | `campaign_visits` 用双后端通用 DDL（AUTOINCREMENT/TEXT/默认值均 SQLite+PG 通用），随 `_MIGRATIONS` 幂等应用 |
| 现算聚合慢查询 | 依赖已有索引；§8.6 升级路径 |
| 统计误读（失效码致 S1 偏低） | §8.2 明确记录；运营建码 SOP 强调核对有效期/status |
| 统计端点泄露单账号数据 | 端点只返回 campaign 聚合计数，不返回单账号明细 |

**回滚**：beacon 与统计端点均为旁路、只读/只追加，无主链路耦合；下线只需移除端点与前端上报，`campaign_visits` 表留存不影响任何业务。

---

## 10. 交付顺序建议

1. 先落 #1 迁移 + #2 `record_campaign_visit`/聚合函数 + #6 对应测试（无外部依赖，纯数据层，可先合）。
2. 再落 #3 beacon 端点 + #4 前端上报（S0 打通）。
3. 最后落 #5 admin 统计端点（S0–S5 出口）。
4. 建 1 个测试活码，走 beacon→注册→扫码→onboarding 全流程，核对 `/admin/campaign-codes/{code}/stats` 各阶段计数与转化率。
