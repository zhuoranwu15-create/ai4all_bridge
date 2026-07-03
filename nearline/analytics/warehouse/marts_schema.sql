-- marts.sqlite3：agg_ 日聚合 mart（可删重建，gitignore，不入备份）。
-- 任何时候都能从 facts.sqlite3 重算；写入用 INSERT OR REPLACE 保证幂等。

-- 分析域1：用户增长与留存（每日一行）。
CREATE TABLE IF NOT EXISTS agg_daily_users (
    date              TEXT PRIMARY KEY,             -- 北京自然日
    new_users         INTEGER NOT NULL DEFAULT 0,   -- 当日注册账号数（非 debug）
    dau               INTEGER NOT NULL DEFAULT 0,   -- 当日有用户入站的 distinct 账号
    inbound_messages  INTEGER NOT NULL DEFAULT 0,   -- 当日用户入站消息条数
    d1_cohort_size    INTEGER NOT NULL DEFAULT 0,   -- 当日首聊 cohort 账号数
    d1_retained       INTEGER NOT NULL DEFAULT 0,   -- cohort 中次日仍活跃数
    d1_retention_rate REAL,                         -- 比率；样本不足/窗口未闭合为 NULL
    d1_status         TEXT,                         -- ok | observe_only | window_open
    computed_at       TEXT
);

-- 分析域2：主动消息（每日）。回复列已在 fct_ 归因，这里直接读列聚合。
CREATE TABLE IF NOT EXISTS agg_daily_proactive (
    date                  TEXT PRIMARY KEY,            -- 北京自然日（按 sent_date）
    total_sent            INTEGER NOT NULL DEFAULT 0,
    blocked_count         INTEGER NOT NULL DEFAULT 0,  -- 有 policy_reason 的策略拦截
    failed_count          INTEGER NOT NULL DEFAULT 0,  -- 下游拒收/错误（status='failed' 且无 policy_reason，含 ret:-2 限速）
    covered_accounts      INTEGER NOT NULL DEFAULT 0,  -- 当日收到 sent 的 distinct 账号
    replied_total         INTEGER NOT NULL DEFAULT 0,  -- 窗口已闭合 sent 中 replied 数
    resolved_sent         INTEGER NOT NULL DEFAULT 0,  -- 当日 sent 中窗口已闭合数（回复率分母）
    reply_rate_overall    REAL,                        -- replied_total / resolved_sent（无样本为 NULL）
    reply_latency_p50_sec INTEGER,
    reply_window_hours    INTEGER,
    by_category_json      TEXT,                        -- {category: {sent, resolved, replied, reply_rate}}
    computed_at           TEXT
);

-- 分析域2：主动消息分时段（日期+小时）。
CREATE TABLE IF NOT EXISTS agg_hourly_proactive (
    date       TEXT NOT NULL,
    hour       INTEGER NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    resolved   INTEGER NOT NULL DEFAULT 0,
    replied    INTEGER NOT NULL DEFAULT 0,
    reply_rate REAL,
    PRIMARY KEY (date, hour)
);

-- 分析域3：Dreaming 运行（每日）。token NULL 安全；partial 独立列；scheduler 为当前快照。
CREATE TABLE IF NOT EXISTS agg_daily_dreaming (
    date                  TEXT PRIMARY KEY,            -- 按 run_date
    runs_total            INTEGER NOT NULL DEFAULT 0,
    runs_succeeded        INTEGER NOT NULL DEFAULT 0,
    runs_partial          INTEGER NOT NULL DEFAULT 0,
    runs_failed           INTEGER NOT NULL DEFAULT 0,
    accounts_covered      INTEGER NOT NULL DEFAULT 0,
    avg_duration_sec      REAL,
    tokens_input          INTEGER,                     -- NULL 安全汇总
    tokens_output         INTEGER,
    items_generated       INTEGER NOT NULL DEFAULT 0,
    items_applied         INTEGER NOT NULL DEFAULT 0,
    items_skipped         INTEGER NOT NULL DEFAULT 0,
    items_applied_rate    REAL,
    items_by_skip_reason_json TEXT,
    scheduler_status      TEXT,                        -- dreaming_scheduler 当前 heartbeat
    scheduler_last_success_at TEXT,
    computed_at           TEXT
);

-- 分析域4：Onboarding 漏斗（每日快照）。当前态分布 + 注册 cohort 完成。人设待埋点。
CREATE TABLE IF NOT EXISTS agg_onboarding_funnel_daily (
    date              TEXT PRIMARY KEY,            -- 快照日
    cnt_pending       INTEGER NOT NULL DEFAULT 0,
    cnt_step1_sent    INTEGER NOT NULL DEFAULT 0,
    cnt_step2_sent    INTEGER NOT NULL DEFAULT 0,
    cnt_step3_sent    INTEGER NOT NULL DEFAULT 0,
    cnt_complete      INTEGER NOT NULL DEFAULT 0,
    cnt_timed_out     INTEGER NOT NULL DEFAULT 0,
    completion_rate   REAL,                        -- complete / (complete + timed_out)
    cohort_registered INTEGER NOT NULL DEFAULT 0,  -- 当日注册账号数
    cohort_completed  INTEGER NOT NULL DEFAULT 0,  -- 其中当前已 complete 数
    style_distribution_json TEXT,                  -- 占位，待 onboarding_events 落库
    computed_at       TEXT
);
