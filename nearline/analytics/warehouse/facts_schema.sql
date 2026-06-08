-- facts.sqlite3：dim_/fct_ 基础表（durable 基线，只追加，纳入备份）。
-- 所有建表幂等；禁止 DROP/DELETE。口径见 analytics_foundation_design.md。

-- 各 fct_ 表的增量装载位点（watermark）。
CREATE TABLE IF NOT EXISTS etl_watermark (
    table_name  TEXT PRIMARY KEY,
    last_id     INTEGER NOT NULL DEFAULT 0,  -- 已装载的最大源主键
    last_run_at TEXT
);

-- 日历维度：一行一日（北京自然日）。生成器填充，不读操作库。
CREATE TABLE IF NOT EXISTS dim_date (
    date        TEXT PRIMARY KEY,  -- YYYY-MM-DD
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    day         INTEGER NOT NULL,
    iso_week    TEXT NOT NULL,     -- ISO YYYY-Www
    day_of_week INTEGER NOT NULL,  -- 1=周一 .. 7=周日
    is_weekend  INTEGER NOT NULL   -- 周六/周日为 1
);

-- 账号维度：一行一账号（SCD-1 当前态，主键 UPSERT）。
-- 把历史 contacts/sender_id 命名归一为 account_id，对分析侧隐藏技术债。
CREATE TABLE IF NOT EXISTS dim_account (
    account_id               TEXT PRIMARY KEY,
    channel                  TEXT,
    is_debug                 INTEGER NOT NULL DEFAULT 0,
    registered_at            TEXT,  -- accounts.created_at
    registered_date          TEXT,  -- DATE(registered_at)，注册 cohort 键
    registered_week          TEXT,  -- ISO 周，周 cohort 键
    first_inbound_at         TEXT,  -- 首条 role=user 入站消息时间
    first_active_date        TEXT,  -- 首聊日（首聊 cohort 键）
    last_active_date         TEXT,
    current_onboarding_state TEXT,
    updated_at               TEXT
);

-- 会话消息事实：一行一条 messages（watermark 增量，只追加）。
-- 支撑 DAU、消息量、首聊留存。is_account_first_* 仅对 role=user 入站消息置 1。
CREATE TABLE IF NOT EXISTS fct_message (
    message_pk              INTEGER PRIMARY KEY,  -- = messages.id
    account_id              TEXT NOT NULL,
    session_id              INTEGER,
    direction               TEXT,
    role                    TEXT,
    message_type            TEXT,
    created_at              TEXT,
    event_date              TEXT,     -- DATE(created_at)，北京自然日
    event_hour              INTEGER,  -- 0-23
    business_day            TEXT,     -- 来自 sessions.business_day（可空）
    is_account_first_ever   INTEGER NOT NULL DEFAULT 0,  -- 该账号史上首条用户入站
    is_account_first_of_day INTEGER NOT NULL DEFAULT 0   -- 该账号当日首条用户入站
);
CREATE INDEX IF NOT EXISTS idx_fct_message_event_date ON fct_message(event_date);
CREATE INDEX IF NOT EXISTS idx_fct_message_account ON fct_message(account_id);
CREATE INDEX IF NOT EXISTS idx_fct_message_first_ever ON fct_message(is_account_first_ever);

-- 主动外发事实：一行一条 outbound_messages。回复归因在 ETL 阶段算清并落列，
-- agg_ 层直接读列。category 直接沿用 product_category（8 类枚举，见 ANALYTICS_PLAN §二），不二次归一。
-- 装载两段：新行先 resolution_status='pending'；每次 ETL 对窗口已闭合的 pending 回填后置 'resolved'。
CREATE TABLE IF NOT EXISTS fct_proactive_message (
    id                INTEGER PRIMARY KEY,  -- = outbound_messages.id
    account_id        TEXT NOT NULL,
    category          TEXT,                 -- product_category
    source            TEXT,
    status            TEXT,                 -- sent / cancelled / blocked / ...
    policy_reason     TEXT,                 -- 策略拦截原因（quiet_hours/日上限/冷却）
    created_at        TEXT,
    created_date      TEXT,                 -- DATE(created_at)，用于 blocked 按日归属（无 sent_at）
    scheduled_at      TEXT,
    sent_at           TEXT,
    sent_date         TEXT,                 -- DATE(sent_at)，北京自然日
    sent_hour         INTEGER,              -- 0-23
    replied           INTEGER NOT NULL DEFAULT 0,  -- 窗口内是否有 role=user 入站
    reply_message_id  INTEGER,              -- 归因到的首条入站 messages.id
    reply_latency_sec INTEGER,             -- sent_at 到首条入站秒数
    reply_window_hours INTEGER,            -- 本行采用的归因窗口（默认 24，便于敏感性分析）
    resolution_status TEXT NOT NULL DEFAULT 'pending'  -- pending / resolved / not_applicable
);
CREATE INDEX IF NOT EXISTS idx_fct_proactive_sent_date ON fct_proactive_message(sent_date);
CREATE INDEX IF NOT EXISTS idx_fct_proactive_resolution ON fct_proactive_message(resolution_status);

-- Dreaming 运行事实：一行一次 dreaming_runs（watermark 增量，只追加）。
CREATE TABLE IF NOT EXISTS fct_dreaming_run (
    id           INTEGER PRIMARY KEY,  -- = dreaming_runs.id
    account_id   TEXT NOT NULL,
    source_type  TEXT,
    status       TEXT,                 -- succeeded / partial / failed
    error        TEXT,
    started_at   TEXT,
    completed_at TEXT,
    run_date     TEXT,                 -- DATE(started_at)
    duration_sec INTEGER,             -- completed_at - started_at（可空）
    token_input  INTEGER,             -- 可能为空，聚合按 NULL 安全处理
    token_output INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fct_dreaming_run_date ON fct_dreaming_run(run_date);

-- Dreaming 记忆产出事实：一行一条 dreaming_memory_items（watermark 增量，只追加）。
CREATE TABLE IF NOT EXISTS fct_dreaming_memory_item (
    id              INTEGER PRIMARY KEY,  -- = dreaming_memory_items.id
    dreaming_run_id INTEGER,
    account_id      TEXT NOT NULL,
    operation       TEXT,   -- add / update
    apply_status    TEXT,   -- applied / skipped
    skip_reason     TEXT,   -- low_importance / low_confidence / sensitive_item / duplicate_or_noop
    created_at      TEXT,
    event_date      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fct_memory_event_date ON fct_dreaming_memory_item(event_date);

-- Onboarding 旅程累积快照：一行一账号，随状态推进回填里程碑（COALESCE 保留首次捕捉值）。
-- 历史中间里程碑（step1/2/3）受 schema 局限只能上线后逐日捕捉，历史值为空。
CREATE TABLE IF NOT EXISTS fct_onboarding_journey (
    account_id          TEXT PRIMARY KEY,
    registered_at       TEXT,   -- accounts.created_at
    step1_sent_at       TEXT,
    step2_sent_at       TEXT,
    step3_sent_at       TEXT,
    completed_at        TEXT,
    timed_out_at        TEXT,
    current_state       TEXT,
    time_to_complete_sec INTEGER,
    is_complete         INTEGER NOT NULL DEFAULT 0,
    is_timed_out        INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT
);
