from pydantic import model_validator
from pydantic_settings import BaseSettings


# 非生产环境集合：这些环境允许使用 dev 默认密钥，不触发启动 fail-fast。
_NON_PRODUCTION_ENVS = {"local", "development", "test"}


class Settings(BaseSettings):
    app_env: str = "local"
    ai4all_bridge_secret: str = "dev-secret"
    admin_token: str = "dev-admin-token"
    admin_staff_token: str = ""
    admin_reviewer_token: str = ""
    admin_debug_plaintext_enabled: bool = False
    admin_debug_plaintext_account_allowlist: str = ""
    feishu_alert_webhook_url: str = ""
    feishu_website_webhook_url: str = ""
    feishu_error_log_alert_enabled: bool = True
    feishu_error_log_alert_min_interval_seconds: int = 300
    feishu_error_log_alert_timeout_seconds: float = 3.0
    feishu_error_log_alert_max_chars: int = 3500
    database_path: str = "data/ai4all.sqlite3"
    user_profiles_dir: str = "data/user_profiles"
    system_dir: str = "data/system"

    # ===== 数据库后端（厚节点改造，见 docs/tech_design/thick_node_postgres_refactor.md）=====
    # 空(默认)=用 database_path 的 SQLite，行为逐字节不变；postgresql://user:pwd@host:5432/db = PG 后端。
    database_url: str = ""
    db_pool_min_size: int = 1            # PG 连接池下限(仅 database_url 为 PG 时生效)
    db_pool_max_size: int = 8            # PG 连接池上限；Σ(各节点上限)+中心自身 ≤ PG max_connections

    # ===== 数据备份（scripts/backup_data.py）=====
    backup_dir: str = "data/backups"          # 备份产物根目录
    backup_retention_count: int = 14          # 保留最新份数，更旧的自动轮转删除
    backup_rsync_target: str = ""             # 异地 rsync 目标（如 user@host:/path）；空=不启用异地

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    # 两层模型选择（family × tier）。family=厂商家族(deepseek/openai/anthropic)，tier=pro(综合强)/flash(快)。
    # llm_active_family: 默认生效家族；主对话走该家族 pro、后台任务走 flash（可被下方 task 路由改写）。
    llm_active_family: str = "deepseek"
    # llm_task_tiers: 可选 JSON，覆盖 task→tier 默认表（默认 main_reply=pro、后台任务=flash）。
    # 例：{"moderation":"pro"} 把审核改回 pro，其余不变。见 app/llm_providers.py::tier_for_task。
    llm_task_tiers: str = ""
    llm_providers_json: str = ""
    llm_openai_base_url: str = "https://api.openai.com"
    llm_openai_model: str = "gpt-4o-mini"
    llm_openai_api_key: str = ""
    llm_anthropic_base_url: str = "https://api.anthropic.com"
    llm_anthropic_model: str = "claude-sonnet-4-6"
    llm_anthropic_api_key: str = ""
    llm_timeout_seconds: float = 50.0
    llm_connect_timeout_seconds: float = 5.0
    llm_max_retries: int = 1
    llm_max_tool_rounds: int = 4
    llm_force_ipv4: bool = True
    llm_context_messages: int = 100
    # 短期对话历史裁剪（仅作用于对话 history，不含 system prompt；详见
    # docs/tech_design/context_window_token_budget_design.md）。
    # 历史 token 预算：>0 时在条数窗口基础上再按 token 从最旧端裁剪、保留尾部最近；0=关闭（退回纯条数）。
    llm_context_token_budget: int = 3000
    # 单条历史消息字符硬上限：>0 时超长单条截断加 ...[已截断]（只改喂 LLM 副本，不改落库）；0=关闭。
    llm_context_message_max_chars: int = 2000
    # 历史 user 轮加绝对时间戳前缀 `[周一 2026-07-06 11:39]`（只改喂 LLM 副本，不改落库；assistant 与
    # 当前轮不加）。解决长间隔消息被误当成连续上下文的问题；默认开，可灰度回滚。
    llm_history_timestamp_enabled: bool = True
    # Token 压力滚动摘要总开关：统一编排后转为核心路径（carryover 亦作为其 seed），默认开；
    # 保留开关作灰度回滚闸（涉及后台 LLM 调用成本）。
    llm_rolling_summary_enabled: bool = True
    # 后台压缩每次压掉的最老 chunk 目标 token（按整条消息截断，实际可能略高/略低）。超预算时
    # 一次压 max(chunk, 溢出量)，压完留出 headroom，避免每轮都调摘要 LLM。见统一编排设计 §5.2。
    rolling_summary_chunk_tokens: int = 1500
    # 最近「硬底」：距 now ≤ N 分钟 且 ≤ M 轮 的原文永不被摘要/丢弃（硬底优先于 token 预算，
    # kept 可能短暂超预算）。见 docs/tech_design/context_orchestration_unified_design.md §5.1。
    llm_context_floor_minutes: int = 15
    llm_context_floor_turns: int = 10
    llm_request_dump_enabled: bool = False
    llm_request_dump_dir: str = "tmp/llm_request_bodies/ai4all"
    # Agent runtime 对齐开关（Batch A）
    llm_tool_surface_prompt_enabled: bool = True   # 本轮可用工具 block 进 system prompt
    llm_external_content_wrapper_enabled: bool = True  # web_search/web_fetch 结果加 untrusted wrapper
    # Agent runtime 对齐开关（Batch B）
    llm_skills_prompt_enabled: bool = True         # skills catalog 进 system prompt
    llm_read_tool_enabled: bool = True             # read 工具常驻默认集（registry 控制）
    web_fetch_timeout_seconds: float = 8.0
    # Agent runtime 对齐开关（Batch C）
    llm_tool_evidence_replay_enabled: bool = True  # 最近 K 轮工具证据回灌进 history
    llm_tool_evidence_turns: int = 2               # 回灌的最近 turn 数上限
    llm_tool_evidence_max_result_chars: int = 1500 # 单条 tool result 截断字符数
    # Agent runtime 对齐开关（Batch D）
    llm_current_message_envelope_enabled: bool = False  # 当前 user 消息加 typed envelope（灰度关）
    web_fetch_connect_timeout_seconds: float = 3.0
    web_fetch_max_response_bytes: int = 524288     # 512KB
    web_fetch_max_chars: int = 60000
    web_fetch_max_redirects: int = 3
    llm_default_prompt: str = (
        "你是 AI4ALL 的个人 AI 陪伴与生活助理。"
        "你要自然、温和、简洁地回应用户，优先提供情绪陪伴、日常建议和生活协助。"
        "不要把自己定位为心理咨询师，不做诊断。"
    )

    rate_limit_daily: int = 5000
    rate_limit_rpm: int = 10
    rate_limit_rpm_window_seconds: float = 30.0
    rate_limit_daily_message: str = "今天聊得有点多了，我晚些时候再继续陪你。"
    rate_limit_rpm_message: str = "消息来得太快了，稍等一下再发我吧。"
    debug_trace_account_ids: str = ""
    conversation_session_business_day_start_hour: int = 4
    dreaming_scheduler_enabled: bool = False
    dreaming_scheduler_batch_size: int = 100
    # 统一编排 P4：4 点 dreaming 扫描默认挂在 proactive-scheduler 单例进程（见
    # scripts/run_proactive_scheduler.py）。与上面的 FastAPI in-process 开关互斥使用，避免重复扫描；
    # 若改由 FastAPI 进程承担 dreaming，可将本项设为 false。
    proactive_dreaming_scheduler_enabled: bool = True
    user_meta_scheduler_enabled: bool = False
    user_meta_scheduler_hour: int = 3
    user_meta_scheduler_page_size: int = 100
    user_meta_scheduler_inter_account_sleep: float = 0.5

    openclaw_login_auto_start: bool = True
    openclaw_login_start_timeout_ms: int = 35000
    openclaw_login_wait_timeout_ms: int = 480000
    openclaw_gateway_call_timeout_ms: int = 60000
    openclaw_cli_path: str = "openclaw"             # openclaw CLI 名或绝对路径;不在服务 PATH 时填绝对路径(见 .env.example)
    openclaw_gateway_ws_enabled: bool = False
    openclaw_gateway_ws_url: str = ""
    openclaw_gateway_ws_token: str = ""
    openclaw_gateway_ws_password: str = ""
    openclaw_gateway_ws_config_path: str = "~/.openclaw/openclaw.json"
    openclaw_gateway_ws_read_openclaw_config: bool = True
    openclaw_gateway_ws_fallback_to_cli: bool = True
    openclaw_gateway_ws_connect_timeout_ms: int = 3000
    openclaw_gateway_ws_request_timeout_ms: int = 5000
    openclaw_gateway_ws_protocol_version: int = 4
    openclaw_gateway_ws_warmup_on_startup: bool = False
    # 主动消息发送被限速（ret=-2 / rate limited）时的退避重试：仅作用于后台主动消息链路，
    # 不影响用户同步回复。max_retries=0 表示不重试，限速即落 failed。
    proactive_send_rate_limit_max_retries: int = 2
    proactive_send_rate_limit_backoff_seconds: float = 3.0
    # 入站收口开关：True 时，远端 channel 入站若找不到 completed binding 直接 no_reply，
    # 不再用 session_key 兜底创建账号（防解绑/未绑定账号被重新激活）。
    # 本地用 send_mock_turn 调试需置 false。
    openclaw_inbound_require_binding: bool = True

    proactive_outbound_enabled: bool = True
    # 已废弃：原 legacy_proactive 分类日上限，分类合并后不再使用，保留字段仅为兼容历史 .env。
    proactive_outbound_daily_limit: int = 3
    proactive_quiet_hours_start: str = "22:00"
    proactive_quiet_hours_end: str = "08:00"
    companion_followup_daily_limit: int = 1
    new_user_reactivation_daily_limit: int = 4
    new_user_reactivation_window_hours: int = 24
    new_user_reactivation_idle_hours: int = 2
    new_user_reactivation_cooldown_hours: int = 6
    # 内容邀请日上限（含原拉活内容唤回，已合并入本分类）。原名 reactivation_daily_limit。
    content_invitation_daily_limit: int = 1
    proactive_avoidance_window_hours: int = 6
    content_invitation_rejection_cooldown_days: int = 30
    content_invitation_expire_hours: int = 24
    proactive_scheduler_enabled: bool = False
    proactive_scheduler_interval_seconds: float = 30.0
    proactive_scheduler_batch_size: int = 20
    proactive_scheduler_bypass_quiet_hours: bool = False
    proactive_planning_interval_seconds: int = 3600
    proactive_account_check_context_messages: int = 12
    proactive_account_check_min_confidence: float = 0.85
    proactive_content_invitation_tool_rounds: int = 5
    reactivation_dispatch_enabled: bool = False
    reactivation_dispatch_dry_run: bool = True
    # 用户自定义频次的系统硬顶：用户通过主动消息设定 tool 设的 max_per_day/week
    # 超过即封顶到此（用户可在硬顶内放宽/收紧，但不能突破）。
    proactive_frequency_max_per_day_cap: int = 3
    proactive_frequency_max_per_week_cap: int = 14
    reactivation_send_slots: str = "12:15,18:15,21:05"
    reactivation_avoidance_window_minutes: int = 60
    reactivation_dedupe_days: int = 3
    # Per-send random jitter (seconds) added on top of the slot time so sends
    # spread out instead of all firing at the exact slot minute (matters at scale).
    # 拉活消息无强时效性：把窗口拉到 ~5 分钟，让"到点"节奏匹配调度器 ~20 条/轮的 drain
    # 能力，单 IP 发送更平滑、降低同 IP 瞬时聚集的风控信号。
    reactivation_send_jitter_min_seconds: int = 60
    reactivation_send_jitter_max_seconds: int = 300
    reactivation_topic_followup_window_hours: int = 72
    reactivation_topic_followup_context_messages: int = 100
    reactivation_content_invitation_context_messages: int = 100
    # 近期热点（hot_topic）全局召回：搜索近 24h 热点 → 抽主题成无主候选池 → 每账号 LLM 打分
    # 选择 → 多样性打散 → top1 拉活。默认关；产品目标同拉活，复用 companion_followup 分类/配额。
    hot_topic_recall_enabled: bool = False               # 全局召回总开关（fail-closed）
    hot_topic_recall_query: str = "过去24小时国内外热点新闻话题"  # 全局召回使用的搜索 query
    hot_topic_pool_size: int = 8                         # 单日全局池最多抽取的主题数
    hot_topic_select_top_k: int = 3                      # 每账号相关性排序后保留、参与多样性打散的候选数
    hot_topic_history_dedupe_days: int = 1               # 全局历史去重回看天数（近 24h 已入池主题不再入池）
    hot_topic_ttl_hours: int = 12                        # 全局候选池条目存活时长
    hot_topic_min_score: float = 0.3                     # 每账号 LLM 相关性分数下限，低于则不选
    hot_topic_profile_context_messages: int = 50         # 打分时喂入的近期聊天条数（近 30 天内）
    # 全局热点池刷新触发点（绝对时钟点，逗号分隔 HH:MM，北京时间）：每天到点各刷一次。
    # 允许补跑——只要求"今日该档未生成过"，不卡窄时间窗，服务在档口之间重启也能补上。
    hot_topic_pool_refresh_slots: str = "10:00,17:00"
    # 账号级 hot_topic 候选储备（3 条已改写候选）的有效期；命中未过期未用条目时 0 次 LLM 调用，
    # 全部过期/用完才重新生成（1 次排序 + 1 次批量改写，共 2 次 LLM 调用）。
    hot_topic_account_reserve_ttl_hours: int = 6
    # 热榜数据来源（逗号分隔，按顺序抓取合并）；支持 toutiao / zhihu。
    # 非空时优先用热榜，全部失败才降级 web search（需 web_search_enabled=true）。
    hot_topic_sources: str = "toutiao,zhihu"
    hot_topic_toutiao_url: str = "https://60s.viki.moe/v2/toutiao"
    hot_topic_zhihu_url: str = "https://60s.viki.moe/v2/zhihu"
    hot_topic_zhihu_backup_url: str = ""           # 备用 URL；空=不启用
    hot_topic_fetch_timeout_seconds: float = 5.0   # 每个来源 HTTP 超时
    hot_topic_max_items_per_source: int = 20        # 每个来源最多取条数
    # hot_topic 发送独立 dry_run 开关（fail-closed）：True 时 hot_topic 候选只记录 would_send，
    # 不真实发送，其他类型（topic_followup / content_invitation）不受影响。
    # 须与 reactivation_dispatch_dry_run=false 配合才能区分拦截；两者均 false 才真发热点。
    hot_topic_dispatch_dry_run: bool = True
    proactive_commitment_extraction_enabled: bool = True
    proactive_commitment_context_messages: int = 8
    proactive_commitment_min_confidence: float = 0.9
    proactive_commitment_max_days: int = 14

    web_search_enabled: bool = False
    web_search_default_provider: str = "duckduckgo"
    web_search_provider_order: str = "duckduckgo,bing"
    web_search_provider_failover: bool = True
    web_search_sync_timeout_seconds: float = 3.0
    web_search_max_results: int = 5
    web_search_trace_raw_response: bool = False

    dashscope_api_key: str = ""

    # ===== 图片理解（DashScope qwen3-vl-plus）=====
    # 总开关：关闭时图片轮直接走兜底，不调 VL、不扣图片费。
    image_understanding_enabled: bool = False
    image_understanding_model: str = "qwen3-vl-plus"
    image_understanding_timeout_seconds: float = 30.0
    # 单张图片读取上限，超过则放弃理解（防止超大文件拖垮请求）。
    image_max_bytes: int = 10_485_760
    # OpenClaw 入站图片落地目录；只允许读取该目录内的本地文件（防路径穿越）。
    image_inbound_dir: str = "~/.openclaw/media/inbound"
    # 单次图片理解固定扣费（micros，1 贝壳=1_000_000）。独立成本事件，带总开关。
    image_understanding_cost_shell_micros: int = 5_000_000
    # VL 超时/失败时的兜底话术（红线：禁止让主模型在无描述时瞎猜图片内容）。
    image_understanding_fallback_text: str = "这张图我没太看清，你可以说说它，或者再发我一次～"

    # ===== 内容审核与人工复核（Phase A：本地规则 + 任务记录）=====
    moderation_enabled: bool = True
    moderation_sync_guard_enabled: bool = True
    moderation_worker_enabled: bool = False
    moderation_worker_batch_size: int = 50
    moderation_worker_interval_seconds: float = 5.0
    moderation_worker_claim_timeout_seconds: int = 300
    moderation_worker_max_attempts: int = 3
    moderation_sensitive_terms_path: str = "data/moderation/sensitive_terms.json"
    moderation_short_text_skip_chars: int = 8
    moderation_inbound_sample_percent: int = 15
    moderation_outbound_sample_percent: int = 30
    moderation_proactive_sample_percent: int = 100
    moderation_llm_enabled: bool = False
    # 审核 LLM 开启后复用全局 active provider；以下 moderation_llm_* provider 字段仅保留旧配置兼容。
    moderation_llm_base_url: str = ""
    moderation_llm_api_key: str = ""
    moderation_llm_model: str = ""
    moderation_llm_timeout_seconds: float = 20.0
    moderation_llm_prompt_version: str = "moderation_llm_v1"
    moderation_image_safety_enabled: bool = False
    moderation_image_safety_model: str = ""
    moderation_export_dir: str = "data/moderation_exports"
    moderation_safe_fallback_text: str = "这条内容我不能继续发送，我们换个安全的话题吧。"
    moderation_blocked_placeholder: str = "[blocked by moderation]"
    # ===== 第二阶段：入站用户内容阿里云云审核（同步）=====
    # 入站阿里云文本审核 PLUS 总开关；关闭则入站回退第一阶段本地规则异步审核。
    moderation_aliyun_enabled: bool = False
    # 入站同步筛查开关；关闭则即使云审核开启也不在主链路同步拦截（仅异步记录）。
    moderation_aliyun_inbound_sync_enabled: bool = True
    # 阿里云内容安全 endpoint（默认北京节点）。
    moderation_aliyun_endpoint: str = "green-cip.cn-beijing.aliyuncs.com"
    # 文本审核 PLUS service 场景码。
    moderation_aliyun_service: str = "chat_detection_pro"
    # 阿里云调用 connect/read 超时（毫秒）；超时按降级本地规则处理。
    moderation_aliyun_timeout_ms: int = 1000
    # 入站命中风险、停止本轮回复时返回给用户的固定安全话术。
    moderation_inbound_blocked_reply_text: str = "这个话题我不太方便继续，我们换个轻松点的聊聊吧～"
    # 阿里云审核失败率告警（复用 feishu_alert_webhook_url）。
    moderation_aliyun_alert_enabled: bool = True
    # 失败率统计滑动窗口（秒）。
    moderation_aliyun_alert_window_seconds: int = 300
    # 窗口内失败率超过该比例即告警（0~1）。
    moderation_aliyun_alert_failure_rate: float = 0.2
    # 失败率告警的最小样本量，避免极少量调用误报。
    moderation_aliyun_alert_min_samples: int = 5
    # 连续失败次数达到该值即告警。
    moderation_aliyun_alert_consecutive: int = 3
    # 告警冷却时间（秒），避免持续抖动刷屏。
    moderation_aliyun_alert_cooldown_seconds: int = 300

    aliyun_web_search_api_key: str = ""
    aliyun_web_search_enabled: bool = False
    aliyun_web_search_base_url: str = "https://cloud-iqs.aliyuncs.com/search/unified"
    aliyun_web_search_engine_type: str = "LiteAdvanced"
    aliyun_web_search_model: str = "qwen-plus"
    aliyun_web_search_forced: bool = True
    aliyun_web_search_enable_source: bool = True
    aliyun_web_search_strategy: str = ""

    # Aliyun SMS
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_max_per_phone_per_hour: int = 5

    # Aliyun Captcha 2.0
    aliyun_captcha_scene_id: str = ""
    aliyun_captcha_prefix: str = ""  # public frontend config exposed via /web/config

    # OTP TTL (minutes)
    otp_expires_minutes: int = 10
    otp_token_expires_minutes: int = 10

    # App V1 batch ASR. The provider must expose an OpenAI-compatible
    # POST {base_url}/audio/transcriptions endpoint.
    asr_base_url: str = "https://api.openai.com/v1"
    asr_api_key: str = ""
    asr_model: str = "whisper-1"
    asr_timeout_seconds: float = 30.0
    asr_max_audio_bytes: int = 10 * 1024 * 1024
    asr_max_duration_ms: int = 60_000
    asr_mock_transcript: str = ""

    # ===== 朝夕相伴 Companion World P1 =====
    # 默认关闭；只有四模板预检、存量 backfill 与支持 account:null 的客户端均就绪后才可开启。
    companion_world_p1_enabled: bool = False
    # 后台安全开关与 API/auth flag 正交：默认保持已交付行为，事故时可独立停 L3 或恢复旧 proactive。
    companion_world_l3_background_enabled: bool = True
    companion_world_proactive_safety_enabled: bool = True
    # M3 Feed API + 后续 world-content scheduler；与 P1/L3/proactive 开关正交。
    companion_world_feed_enabled: bool = False
    companion_world_feed_morning_start: str = ""
    companion_world_feed_morning_end: str = ""
    companion_world_feed_evening_start: str = ""
    companion_world_feed_evening_end: str = ""
    companion_world_feed_scheduler_interval_seconds: float = 60.0
    companion_world_feed_scheduler_batch_size: int = 20
    companion_world_feed_claim_lease_seconds: int = 300
    companion_world_feed_retry_max_attempts: int = 3
    companion_world_feed_retry_base_seconds: int = 30
    companion_world_outbox_batch_size: int = 50
    companion_world_outbox_claim_lease_seconds: int = 300
    companion_world_outbox_max_attempts: int = 5
    # M3 App 拉取式收件箱；读取/visible adapter default-off，已有数据 cleanup 独立运行。
    companion_world_app_inbox_enabled: bool = False
    # 真人级 App-only 触达的第二道独立闸；必须与 inbox flag 同时开启。
    companion_world_app_only_human_proactive_enabled: bool = False
    companion_world_notification_cleanup_batch_size: int = 100
    # M4 Lifecycle/Mailbox：evaluation、不可逆 commit 与 mailbox 三个独立 default-off 闸。
    companion_world_lifecycle_evaluation_enabled: bool = False
    companion_world_lifecycle_commit_enabled: bool = False
    companion_world_mailbox_enabled: bool = False
    companion_world_lifecycle_inactivity_days: int = 60
    companion_world_lifecycle_evidence_window_days: int = 30
    companion_world_lifecycle_mismatch_min_events: int = 3
    companion_world_lifecycle_mismatch_min_span_days: int = 14
    companion_world_lifecycle_cooldown_days: int = 7
    companion_world_lifecycle_crisis_freeze_days: int = 30
    companion_world_mailbox_delivery_cooldown_days: int = 30
    companion_world_mailbox_letter_ttl_days: int = 30
    # 仅供离线 catalog manifest HMAC-SHA256 校验；空值时 apply 必须 fail-closed。
    companion_world_mailbox_manifest_hmac_secret: str = ""
    companion_world_lifecycle_scheduler_interval_seconds: float = 300.0
    companion_world_lifecycle_scheduler_batch_size: int = 50

    # ===== 多机接入(central 大脑 + 瘦 node;见 docs/tech_design/multi_node_access_refactor.md)=====
    # 角色 standalone(默认,=今天单机) | central | node | "central,node"(同机共存)。
    # standalone 下所有新路径不触发,行为逐字节不变。
    ai4all_role: str = "standalone"
    node_id: str = ""                              # 含 node 能力时必填,全局唯一(如 aliyun1/aliyun2)
    default_node_id: str = ""                       # 账号未分配节点时兜底归属(迁移期 .env 设 aliyun1)
    central_url: str = ""                           # node→中心 稳定指纹地址(MVP=http://aliyun1),不写裸 IP
    node_base_url: str = ""                         # 本节点基址(中心 push 登录用),写入 access_nodes
    node_agent_host: str = "0.0.0.0"                # node agent HTTP 监听地址
    node_agent_port: int = 8190                     # node agent HTTP 端口
    node_max_sessions: int = 0                      # 本机会话上限(MVP 仅上报,0=未设)
    outbound_pull_interval_seconds: float = 2.0     # 节点出站轮询间隔
    outbound_pull_batch_size: int = 20              # 节点单轮认领条数
    outbound_claim_timeout_seconds: int = 60        # sending 卡死回收阈值(节点崩溃安全网)
    local_node_inline_dispatch: bool = False        # true: central 同机 node 出站同进程即时发(迁移期降延迟)

    # ===== TDAI 长期记忆 sidecar（docs/tech_design/tdai_multitenant_design.md）=====
    # 总开关：默认 false；生产启用时显式设 true。capture/recall 均受此控制。
    tdai_enabled: bool = False
    tdai_gateway_url: str = "http://127.0.0.1:8420"
    tdai_gateway_api_key: str = ""
    tdai_recall_enabled: bool = True
    tdai_capture_enabled: bool = True
    # 热路径严格超时（秒）：超时直接降级，不阻主回复。
    # 0.5 覆盖 DashScope embedding 往返的 warm 长尾（实测中位 ~350ms）；
    # 0.2 会让约 30% warm 召回误降级。
    tdai_recall_timeout_seconds: float = 0.5
    tdai_capture_timeout_seconds: float = 2.0
    # 每段 recall 注入内容最大字符数（prepend_context / context 各自截断）。
    tdai_recall_max_chars: int = 2500
    # 逗号分隔的 account_id，空 = recall 对所有账号关闭。capture 不受此控制（全量）。
    tdai_recall_account_allowlist: str = ""
    # ----- TDAI 主动检索工具（tdai_memory_search / tdai_conversation_search）-----
    # 两个 search 工具总开关，默认关；独立于 recall，可单独灰度。
    tdai_search_enabled: bool = False
    # 模型 tool loop 内调用，非开口热路径，可比 recall(0.5) 宽松。
    tdai_search_timeout_seconds: float = 2.0
    # 每轮两个 search 工具合计调用上限（TDAI 后端未实现，由本侧 TurnContext 计数强制）。
    tdai_search_max_calls_per_turn: int = 3
    # 逗号分隔的 account_id，空 = search 对所有账号关闭（独立于 recall allowlist）。
    tdai_search_account_allowlist: str = ""
    # recall + search 共用的自然灰度阈值：账号累计 inbound(用户)消息数 ≥ 此值即视为
    # "已积累足够记忆"，对 recall 与两个 search 工具开放（与各自 allowlist 取并集）。
    # 0 = 关闭阈值通道，只认 allowlist。生产建议先设较高值观察，再按需下调。
    tdai_memory_min_messages: int = 50

    # ----- 动态提醒 / 例行简报（dynamic reminder）-----
    # 见 docs/tech_design/dynamic_reminder_scheduled_content_design.md。
    # 总开关：为假时到期履约不走 dynamic 分支、创建工具不接受 fulfillment=dynamic。
    dynamic_reminder_enabled: bool = False
    # 每账号活跃（pending）dynamic 提醒数量上限，创建时校验。
    dynamic_reminder_max_active_per_account: int = 5
    # 单次调度扫描处理的 dynamic 到期提醒批量上限。
    dynamic_reminder_batch_size: int = 5
    # 履约失败（搜索/生成失败）的重试次数（配合 30 分钟窗口，由 obligation 控制）。
    dynamic_reminder_max_retries: int = 2
    # 履约合成轮次可用的工具集（逗号分隔工具名）。v1 只带 web_search；后续放开只改此项。
    dynamic_reminder_fulfillment_tools: str = "web_search"
    # 首轮工具选择约束（逗号分隔）；空 = 不强制（tool_choice=auto）。
    # 默认置空：当前 PRO 档为 thinking 模型（deepseek-v4-pro），其 API 只接受 tool_choice="auto"，
    # 指定函数或 "required" 都会 400（Thinking mode does not support this tool_choice）。
    # 「必须先搜索」改由履约提示词硬性要求 + search_ok 后置校验保证（搜不到不发编造内容）。
    # 若将来换用非 thinking 的 provider 需要 API 层硬强制，再把此项设为 web_search。
    dynamic_reminder_force_first_tool: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"

    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> "Settings":
        """生产环境拒绝使用 dev 默认密钥/空密钥，启动即 fail-fast。

        仅在 app_env 非 {local,development,test} 时生效，避免运维漏配时 dev-secret /
        dev-admin-token 直接对外暴露 admin 全权限。health/ready 的软提示作为双保险保留。
        """
        env = str(self.app_env or "").strip().lower()
        if env in _NON_PRODUCTION_ENVS:
            return self
        insecure = []
        if not self.ai4all_bridge_secret or self.ai4all_bridge_secret == "dev-secret":
            insecure.append("AI4ALL_BRIDGE_SECRET")
        if not self.admin_token or self.admin_token == "dev-admin-token":
            insecure.append("ADMIN_TOKEN")
        if insecure:
            raise ValueError(
                f"app_env={self.app_env!r} 拒绝以不安全的默认/空密钥启动: "
                f"{', '.join(insecure)}。请通过环境变量设置强随机值。"
            )
        return self

    @property
    def role_set(self) -> set:
        """解析 ai4all_role 为角色集合(支持逗号分隔的 "central,node")。"""
        return {r.strip().lower() for r in (self.ai4all_role or "").split(",") if r.strip()}

    @property
    def has_central_role(self) -> bool:
        """是否具备中心能力(读写 DB / 大脑 / 节点面向 API)。standalone 视为含中心。"""
        roles = self.role_set
        return "central" in roles or "standalone" in roles

    @property
    def has_node_role(self) -> bool:
        """是否具备接入节点能力(本机 openclaw 直发 / exec)。standalone 视为含 node。"""
        roles = self.role_set
        return "node" in roles or "standalone" in roles

    @property
    def is_inline_dispatch(self) -> bool:
        """出站是否走同进程即时直发。standalone 恒 True(行为不变);
        central+node 同机可由 local_node_inline_dispatch 选开。"""
        if "standalone" in self.role_set:
            return True
        return bool(self.local_node_inline_dispatch)


settings = Settings()
