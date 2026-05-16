# 设计文档：限流 + 运营后台 Web UI

日期：2026-05-16

## 背景

AI4ALL Backend 目前没有任何限流机制，也没有可视化的运营管理界面。本次迭代补齐两块能力：

1. **per-account 限流**：短时频率（每分钟）+ 每日次数，防止单账号滥用和意外消耗 LLM 额度。
2. **运营后台 Web UI**：纯 HTML+JS，FastAPI serve 静态文件，供运营人员管理账号、查看用量、调整配置。

---

## 一、限流

### 1.1 限制维度

| 维度 | 存储 | 默认值 | 说明 |
|------|------|--------|------|
| 每分钟请求数（RPM） | 内存滑动窗口 | 5 | 进程重启清零，可接受 |
| 每日消息数 | SQLite `daily_usage` 表 | 100 | 持久化，重启不丢 |

`0` 表示关闭该维度限制。

### 1.2 Schema 变更

**新表 `daily_usage`**

```sql
CREATE TABLE IF NOT EXISTS daily_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,          -- YYYY-MM-DD
    message_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, date),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);
```

**`accounts` 表新增两列（NULL = 用全局默认）**

```sql
ALTER TABLE accounts ADD COLUMN daily_limit INTEGER;
ALTER TABLE accounts ADD COLUMN rpm_limit INTEGER;
```

### 1.3 配置（`.env` / `config.py`）

```
RATE_LIMIT_DAILY=100
RATE_LIMIT_RPM=5
RATE_LIMIT_DAILY_MESSAGE=今天聊得有点多了，我晚些时候再继续陪你。
RATE_LIMIT_RPM_MESSAGE=消息来得太快了，稍等一下再发我吧。
```

### 1.4 新增模块 `app/rate_limiter.py`

```
RateLimiter（进程级单例）
  _lock: threading.Lock
  _windows: dict[account_id -> deque[float]]  # 时间戳队列

  check_rpm(account_id: str, limit: int) -> bool
    - 清除 60 秒前的时间戳
    - len(window) >= limit → return False（不记录本次）
    - 否则追加当前时间戳 → return True

rate_limiter = RateLimiter()   # 模块级单例，main.py import 使用
```

### 1.5 DB 新增函数（`app/db.py`）

```
get_daily_usage(account_id, date) -> int
increment_daily_usage(account_id, date) -> int   # upsert，返回更新后的 count
```

### 1.6 `openclaw_turn` 执行顺序

```
account disabled?         → no_reply=True（现有）
rpm 超限?                 → reply=rpm_message, status="rate_limited"，不写 DB
daily 超限?               → reply=daily_message, status="rate_limited"，不写 DB
dedup?                    → 返回已有回复（现有）
插入 inbound 消息
日次数 +1（increment_daily_usage）
生成回复（LLM 调用）
插入 outbound 消息
返回回复
```

超限时不写消息记录、不消耗 LLM token。

### 1.7 有效限制值计算

```python
# None = 未设置，用全局默认；0 = 关闭该维度限制
effective_rpm   = settings.rate_limit_rpm   if account["rpm_limit"]   is None else account["rpm_limit"]
effective_daily = settings.rate_limit_daily if account["daily_limit"] is None else account["daily_limit"]
# effective_* == 0 时跳过该维度检查
```

---

## 二、运营后台 Web UI

### 2.1 文件结构

```
app/static/
├── index.html      # 账号列表页
├── account.html    # 账号详情页
├── style.css       # 最小样式，无框架
└── admin.js        # 公共逻辑：fetch wrapper、auth、工具函数
```

**FastAPI 挂载**（`main.py`）

```python
from fastapi.staticfiles import StaticFiles
app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
```

访问 `http://localhost:8000/ui/`。

### 2.2 认证

首次访问弹出 `prompt()` 要求输入 Admin Token，存入 `localStorage["admin_token"]`。
所有 fetch 请求自动带 `Authorization: Bearer <token>`。
API 返回 401 → 清除 token → 重新提示。

### 2.3 index.html — 账号列表

显示字段：account_id（链接到详情页）、状态（active/disabled）、今日消息数/日上限、最近活跃时间、启用/禁用按钮。

数据来源：`GET /admin/accounts` + `GET /admin/accounts/{id}/usage`（今日数据）。

### 2.4 account.html — 账号详情（`?id=<account_id>`）

分四个区块，各自独立保存：

| 区块 | 字段 | API |
|------|------|-----|
| 基本信息 | notes、display_name | `PATCH /admin/accounts/{id}` |
| 限流配置 | daily_limit、rpm_limit（空=全局默认） | `PATCH /admin/accounts/{id}` |
| AI 配置 | style、system_prompt | `PATCH /admin/accounts/{id}/profile` |
| 会话列表 | session_key、消息数、最近时间，展开查看最近 20 条消息 | `GET /admin/accounts/{id}/sessions` + `GET /admin/sessions/{id}` |

### 2.5 新增 API 端点

```
GET /admin/accounts/{account_id}/usage
  → 返回今日 message_count 及近 7 天每日用量
  → 用于列表页今日数量和详情页用量展示
```

`PATCH /admin/accounts/{account_id}` 已有，新增接受 `daily_limit`、`rpm_limit` 字段。

### 2.6 错误处理

- 保存按钮：操作中显示"保存中…"，成功显示"已保存 ✓"，失败显示错误信息（inline，不弹窗）
- 列表/详情加载失败：页面内显示错误信息

---

## 三、文件改动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `app/rate_limiter.py` | 新建 | RateLimiter 单例 |
| `app/db.py` | 修改 | 新表 daily_usage，新函数 get/increment_daily_usage，accounts 新列 |
| `app/config.py` | 修改 | 新增 4 个限流配置项 |
| `app/main.py` | 修改 | mount 静态目录，openclaw_turn 加限流检查，新增 usage API，AccountUpdateRequest 新增限流字段 |
| `app/static/index.html` | 新建 | 账号列表页 |
| `app/static/account.html` | 新建 | 账号详情页 |
| `app/static/admin.js` | 新建 | 公共逻辑 |
| `app/static/style.css` | 新建 | 最小样式 |
| `.env.example` | 修改 | 新增限流配置示例 |
