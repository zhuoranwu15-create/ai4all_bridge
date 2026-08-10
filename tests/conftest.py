import os
import shutil
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# ambient DATABASE_URL guard：本地/生产 .env 可能指向真实 dev/生产 PG。主 pytest 始终使用
# pytest-postgresql 创建的临时 PG，因此先清空进程内 ambient URL，再由 test_settings 为每个
# DB/integration 测试注入克隆库 DSN，杜绝误连外部数据库。
# ---------------------------------------------------------------------------
import app.config as _app_config  # noqa: E402
_app_config.settings.database_url = ""
# 同类 ambient .env 泄漏：dev/生产 .env 常把 openclaw_cli_path 设成绝对路径（CLI 不在服务 PATH 时），
# 会泄漏进直接用全局 settings 的 openclaw/runtime_health/node 测试——它们断言命令首元素为裸名 "openclaw"，
# 装了 CLI 的开发机上 cmd[0] 变成绝对路径致误判失败（CI/干净环境无 .env 故不暴露）。在此固定回默认裸名，
# 使测试与本机 .env 无关；需要绝对路径的测试自行 monkeypatch 覆盖。
_app_config.settings.openclaw_cli_path = "openclaw"
# init_db 的 PG 迁移闸：session 启动时只对 pytest-postgresql 模板库应用一次迁移。
# 真实库不会被误连——上面的 ambient guard 已清空 URL，测试 DSN 只来自临时 PG fixture。
os.environ["AI4ALL_ALLOW_AUTO_MIGRATE"] = "1"


# ---------------------------------------------------------------------------
# 主测试后端固定为 PostgreSQL。pytest-postgresql 启动一个 session 级临时 PG 进程，先把
# 完整迁移链加载到模板库；postgresql_db 再为每个测试从模板 create/drop 独立数据库。
# 这样同时保留逐测试隔离，并避免约 1290 个 DB/integration 用例重复执行整条迁移链。
# ---------------------------------------------------------------------------
def _resolve_pg_ctl() -> "str | None":
    """定位 ``pg_ctl``；返回 None 表示交回 pytest-postgresql 的默认查找逻辑。

    pytest-postgresql 默认只认 Debian 布局 ``/usr/lib/postgresql/<ver>/bin/pg_ctl``，
    路径不存在时退回 ``pg_config --bindir``。RPM 系（阿里云 Linux/RHEL）把 ``pg_ctl``
    直接装进 ``/usr/bin``，而 ``pg_config`` 属于 ``*-devel`` 包，服务器上通常没装，
    于是整档在 fixture setup 阶段全量 error。这里优先用 PATH 上真实存在的 pg_ctl，
    并允许 ``AI4ALL_TEST_PG_CTL`` 显式指定（多版本共存时用）。
    """
    explicit = os.environ.get("AI4ALL_TEST_PG_CTL", "").strip()
    if explicit:
        return explicit
    return shutil.which("pg_ctl")


def _load_migrated_pg_template(host, port, user, dbname, password):
    """Apply the complete application migration chain once to the PG template DB."""
    import psycopg

    from app.db import init_db
    from app.db._backend import close_pg_pool

    probe = psycopg.connect(
        host=host, port=port, user=user, dbname=dbname, password=password
    )
    try:
        template_dsn = _dsn_from_conn(probe)
    finally:
        probe.close()

    original_url = _app_config.settings.database_url
    close_pg_pool()
    _app_config.settings.database_url = template_dsn
    try:
        init_db()
    finally:
        close_pg_pool()
        _app_config.settings.database_url = original_url


from pytest_postgresql import factories as _pg_factories  # noqa: E402

postgresql_proc = _pg_factories.postgresql_proc(
    executable=_resolve_pg_ctl(), load=[_load_migrated_pg_template]
)
postgresql_db = _pg_factories.postgresql("postgresql_proc")


# 判定为「db 档」的 fixture：请求其一即视为依赖真实 DB（模板克隆）。client 不在此列，
# 因为含 client 的用例统一归为 integration（client 依赖 fresh_db，故须先判 client）。
_DB_FIXTURES = frozenset({
    "fresh_db", "test_settings", "db_dsn", "postgresql_db", "postgresql_proc",
})

# 产品归属先以稳定的文件/module 命名线索推断；后续测试迁移到
# tests/products/<app_id>/ 后，同一规则会自然按目录命中。未能可靠归属
# 某个具体产品的用例归入 shared，避免把共享契约误算进某个产品回归档。
_PRODUCT_MARKER_PATTERNS = (
    ("plum", "plum"),
    ("mingchan", "mingchan"),
    ("zhaoxi", "zhaoxi"),
)
# 历史顶层测试的显式所有权。目录重组完成前，先用这张表让 marker 反映代码
# 所属产品；后续文件迁移到 products/<app_id>/ 后可逐步删除对应条目。
_PRODUCT_FILE_OVERRIDES = {
    "mingchan": (
        "companion_world_",
        "world_content_scheduler",
    ),
    "zhaoxi": (
        "admin_proactive",
        "after_turn_scheduling",
        "commitment",
        "content_invitations",
        "creator_role_template",
        "debug_onboarding_campaign",
        "dreaming",
        "dynamic_reminders",
        "memory_writer",
        "mission_",
        "onboarding",
        "proactive_",
        "reactivation",
        "reminder",
        "relationship_state",
        "session_lifecycle",
        "user_meta",
        "web_campaign",
        "web_onboarding",
        "world_content",
    ),
}
_PLATFORM_MARKER_WORDS = frozenset(
    {
        "moderation", "billing", "quota", "rate_limiter", "product_policy",
        "product_membership", "multi_product", "account_app_id", "layer_boundaries",
        "agent_runtime", "runtime", "db_backend", "migration", "pytest_postgres",
    }
)


def _product_marker_for_item(item):
    """Return exactly one product/platform/shared marker for a collected test."""
    nodeid = item.nodeid.lower().replace("\\", "/")
    stem = nodeid.rsplit("/", 1)[-1].split("::", 1)[0]
    filename = stem.removeprefix("test_")
    for marker, prefixes in _PRODUCT_FILE_OVERRIDES.items():
        if any(filename.startswith(prefix) for prefix in prefixes):
            return marker
    matches = [marker for token, marker in _PRODUCT_MARKER_PATTERNS if token in nodeid]
    if len(set(matches)) > 1:
        # 跨产品隔离/兼容契约属于 shared，不应被任一产品档独占。
        return "shared"
    if matches:
        return matches[0]
    if any(word in stem for word in _PLATFORM_MARKER_WORDS):
        return "platform"
    return "shared"


def pytest_collection_modifyitems(config, items):
    """派生测试层级及互斥的产品归属 marker。"""
    for item in items:
        fx = set(getattr(item, "fixturenames", ()))
        if "client" in fx:
            item.add_marker("integration")
        elif fx & _DB_FIXTURES:
            item.add_marker("db")
        else:
            item.add_marker("unit")
        item.add_marker(_product_marker_for_item(item))


def _dsn_from_conn(conn) -> str:
    """从 pytest-postgresql 的连接推出供业务层直连的 URL 形式 DSN。"""
    info = conn.info
    if info.host and info.host.startswith("/"):
        # 本地 unix socket：host 放进 query，URL 主体留空 host
        return f"postgresql://{info.user}@/{info.dbname}?host={info.host}&port={info.port}"
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture(autouse=True)
def _stub_text_sanitizer_llm(monkeypatch):
    """默认把自由文本清洗器（D-B）的 LLM 调用短路为「原样放行」。

    清洗器按产品决策 fail closed：LLM 不可用即拒绝放行。测试环境没有 provider，若不桩住，
    每个碰到自建角色/称呼的用例都会退化成 503，掩盖真正要断言的行为。
    需要验证改写/硬拒绝/fail-closed 的用例自行 monkeypatch 覆盖本桩。
    """
    def _pass_through(messages, **_kwargs):
        import json as _json

        payload = _json.loads(messages[-1]["content"])
        return _json.dumps(
            {"verdict": "pass", "sanitized_text": payload["text"], "categories": []}
        )

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion", _pass_through
    )


@pytest.fixture
def db_dsn(request):
    """Return the DSN of this test's isolated clone of the migrated PG template."""
    conn = request.getfixturevalue("postgresql_db")
    return _dsn_from_conn(conn)


@pytest.fixture
def empty_pg_database(postgresql_db):
    """Reset this test's clone to schema zero for migration-chain boundary tests."""
    from app.db._backend import close_pg_pool

    close_pg_pool()
    postgresql_db.execute("DROP SCHEMA public CASCADE")
    postgresql_db.execute("CREATE SCHEMA public")
    postgresql_db.commit()
    yield postgresql_db
    close_pg_pool()


@pytest.fixture
def test_settings(tmp_path, db_dsn):
    s = MagicMock()
    # database_url 必须显式赋值为临时 PG DSN；否则 MagicMock 自动属性会污染后端选择。
    s.database_url = db_dsn
    s.db_pool_min_size = 1
    s.db_pool_max_size = 8
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.system_dir = str(tmp_path / "system")
    s.ai4all_bridge_secret = "test-secret"
    s.admin_token = "test-admin"
    s.admin_staff_token = "test-staff"
    s.admin_reviewer_token = "test-reviewer"
    s.admin_debug_plaintext_enabled = False
    s.admin_debug_plaintext_account_allowlist = ""
    s.feishu_alert_webhook_url = ""
    s.feishu_website_webhook_url = ""
    s.feishu_error_log_alert_enabled = False
    s.feishu_error_log_alert_min_interval_seconds = 300
    s.feishu_error_log_alert_timeout_seconds = 3.0
    s.feishu_error_log_alert_max_chars = 3500
    s.app_env = "test"
    s.llm_api_key = ""
    s.llm_base_url = "http://fake-llm"
    s.llm_active_family = "deepseek"
    s.llm_task_tiers = ""
    s.llm_providers_json = ""
    s.llm_openai_base_url = "https://api.openai.com"
    s.llm_openai_model = "gpt-4o-mini"
    s.llm_openai_api_key = ""
    s.llm_anthropic_base_url = "https://api.anthropic.com"
    s.llm_anthropic_model = "claude-sonnet-4-6"
    s.llm_anthropic_api_key = ""
    s.llm_timeout_seconds = 30.0
    s.llm_connect_timeout_seconds = 5.0
    s.llm_max_retries = 0
    s.llm_force_ipv4 = False
    s.llm_context_messages = 100
    s.llm_default_prompt = "你是测试助手"
    s.llm_max_tool_rounds = 3
    s.llm_request_dump_enabled = False
    # Agent runtime 对齐开关（Batch A-D）：MagicMock 不会自动返回 False，需显式设定。
    s.llm_tool_surface_prompt_enabled = True
    s.llm_external_content_wrapper_enabled = True
    s.llm_skills_prompt_enabled = False     # 测试里不需要 skill catalog
    s.llm_read_tool_enabled = True
    s.llm_tool_evidence_replay_enabled = False  # 避免测试依赖 DB 里的 tool_invocations
    s.llm_current_message_envelope_enabled = False  # 灰度关：不影响现有测试断言
    # 短期上下文裁剪（context_window）：测试默认关，避免 MagicMock 自动属性污染历史组装；
    # 需要验证裁剪的用例在测试内显式置非零。滚动摘要 P3 默认关。
    s.llm_context_token_budget = 0
    s.llm_context_message_max_chars = 0
    s.llm_rolling_summary_enabled = False
    s.debug_trace_account_ids = ""
    s.rate_limit_daily = 3
    s.rate_limit_rpm = 10
    s.rate_limit_rpm_window_seconds = 30.0
    s.rate_limit_daily_message = "每日上限"
    s.rate_limit_rpm_message = "每分钟上限"
    s.conversation_session_business_day_start_hour = 4
    s.dreaming_scheduler_enabled = False
    # dreaming 无 interval 配置项（天级定点扫描）；此处只登记真实 Settings 上存在的字段，
    # 避免替身凭空多出属性、把"读了不存在配置"的 bug 掩盖成测试通过。
    s.proactive_dreaming_scheduler_enabled = True
    s.dreaming_scheduler_batch_size = 100
    s.user_meta_scheduler_enabled = False
    s.user_meta_scheduler_hour = 3
    s.user_meta_scheduler_page_size = 100
    s.user_meta_scheduler_inter_account_sleep = 0.0
    s.creator_role_templates_enabled = False
    s.openclaw_login_auto_start = False
    s.openclaw_login_start_timeout_ms = 5000
    s.openclaw_login_wait_timeout_ms = 5000
    s.openclaw_gateway_call_timeout_ms = 5000
    s.openclaw_cli_path = "openclaw"
    s.openclaw_gateway_ws_enabled = False
    s.openclaw_gateway_ws_url = ""
    s.openclaw_gateway_ws_token = ""
    s.openclaw_gateway_ws_password = ""
    s.openclaw_gateway_ws_config_path = str(tmp_path / "openclaw.json")
    s.openclaw_gateway_ws_read_openclaw_config = False
    s.openclaw_gateway_ws_fallback_to_cli = True
    s.openclaw_gateway_ws_connect_timeout_ms = 3000
    s.openclaw_gateway_ws_request_timeout_ms = 5000
    s.openclaw_gateway_ws_protocol_version = 4
    s.openclaw_gateway_ws_warmup_on_startup = False
    # 默认关闭入站收口，保留现有测试依赖的 session_key 兜底；收口路径由专门测试显式开启。
    s.openclaw_inbound_require_binding = False
    s.proactive_outbound_enabled = True
    s.proactive_outbound_daily_limit = 3
    s.proactive_quiet_hours_start = "22:00"
    s.proactive_quiet_hours_end = "08:00"
    s.companion_followup_daily_limit = 1
    s.new_user_reactivation_daily_limit = 4
    s.new_user_reactivation_window_hours = 24
    s.new_user_reactivation_idle_hours = 2
    s.new_user_reactivation_cooldown_hours = 6
    s.proactive_avoidance_window_hours = 6
    s.content_invitation_rejection_cooldown_days = 30
    s.content_invitation_expire_hours = 24
    s.proactive_scheduler_enabled = False
    s.proactive_scheduler_interval_seconds = 30.0
    s.proactive_scheduler_batch_size = 20
    s.companion_world_app_inbox_enabled = False
    s.companion_world_app_only_human_proactive_enabled = False
    s.companion_world_notification_cleanup_batch_size = 100
    s.companion_world_lifecycle_evaluation_enabled = False
    s.companion_world_lifecycle_commit_enabled = False
    s.companion_world_mailbox_enabled = False
    s.companion_world_lifecycle_inactivity_days = 60
    s.companion_world_lifecycle_evidence_window_days = 30
    s.companion_world_lifecycle_mismatch_min_events = 3
    s.companion_world_lifecycle_mismatch_min_span_days = 14
    s.companion_world_lifecycle_cooldown_days = 7
    s.companion_world_lifecycle_crisis_freeze_days = 30
    s.companion_world_mailbox_delivery_cooldown_days = 30
    s.companion_world_mailbox_letter_ttl_days = 30
    s.companion_world_mailbox_manifest_hmac_secret = ""
    s.companion_world_lifecycle_scheduler_interval_seconds = 300.0
    s.companion_world_lifecycle_scheduler_batch_size = 50
    s.companion_world_visits_enabled = False
    s.companion_world_human_chat_enabled = False
    s.proactive_scheduler_bypass_quiet_hours = False
    s.proactive_planning_interval_seconds = 3600
    s.proactive_account_check_context_messages = 12
    s.proactive_account_check_min_confidence = 0.85
    s.proactive_content_invitation_tool_rounds = 5
    s.content_invitation_daily_limit = 1
    s.proactive_frequency_max_per_day_cap = 3
    s.proactive_frequency_max_per_week_cap = 14
    s.reactivation_send_slots = "12:15,18:15,21:05"
    s.reactivation_avoidance_window_minutes = 60
    s.reactivation_dedupe_days = 3
    s.reactivation_send_jitter_min_seconds = 0
    s.reactivation_send_jitter_max_seconds = 0
    s.reactivation_topic_followup_window_hours = 72
    s.reactivation_topic_followup_context_messages = 100
    s.reactivation_content_invitation_context_messages = 100
    s.proactive_commitment_extraction_enabled = True
    s.proactive_commitment_context_messages = 8
    s.proactive_commitment_min_confidence = 0.9
    s.proactive_commitment_max_days = 14
    # web_search 能力总开关已改为常量 WEB_SEARCH_ENABLED（常开），不再有可 patch 的 settings 字段。
    s.web_search_default_provider = "duckduckgo"
    s.web_search_provider_order = "duckduckgo,bing"
    s.web_search_provider_failover = True
    s.web_search_sync_timeout_seconds = 8.0
    s.web_search_max_results = 5
    s.web_search_trace_raw_response = False
    s.dashscope_api_key = ""
    s.image_understanding_enabled = False
    s.image_understanding_model = "qwen3-vl-plus"
    s.image_understanding_timeout_seconds = 30.0
    s.image_max_bytes = 10_485_760
    s.image_inbound_dir = str(tmp_path / "inbound")
    s.image_understanding_cost_shell_micros = 5_000_000
    s.image_understanding_fallback_text = "这张图我没太看清，你可以说说它，或者再发我一次～"
    s.moderation_enabled = True
    s.moderation_sync_guard_enabled = True
    s.moderation_worker_enabled = False
    s.moderation_worker_batch_size = 50
    s.moderation_worker_interval_seconds = 5.0
    s.moderation_worker_claim_timeout_seconds = 300
    s.moderation_worker_max_attempts = 3
    s.moderation_sensitive_terms_path = str(tmp_path / "moderation" / "sensitive_terms.json")
    s.moderation_short_text_skip_chars = 8
    s.moderation_inbound_sample_percent = 15
    s.moderation_outbound_sample_percent = 30
    s.moderation_proactive_sample_percent = 100
    s.moderation_llm_enabled = False
    s.moderation_llm_base_url = ""
    s.moderation_llm_api_key = ""
    s.moderation_llm_model = ""
    s.moderation_llm_timeout_seconds = 20.0
    s.moderation_llm_prompt_version = "moderation_llm_v1"
    s.moderation_image_safety_enabled = False
    s.moderation_image_safety_model = ""
    s.moderation_export_dir = str(tmp_path / "moderation_exports")
    s.moderation_safe_fallback_text = "这条内容我不能继续发送，我们换个安全的话题吧。"
    s.moderation_blocked_placeholder = "[blocked by moderation]"
    # 第二阶段入站阿里云云审核：默认关闭，保证既有测试走第一阶段异步路径、行为不变。
    s.moderation_aliyun_enabled = False
    s.moderation_aliyun_inbound_sync_enabled = True
    s.moderation_aliyun_endpoint = "green-cip.cn-beijing.aliyuncs.com"
    s.moderation_aliyun_service = "chat_detection_pro"
    s.moderation_aliyun_timeout_ms = 1000
    s.moderation_inbound_blocked_reply_text = "这个话题我不太方便继续，我们换个轻松点的聊聊吧～"
    s.moderation_aliyun_alert_enabled = True
    s.moderation_aliyun_alert_window_seconds = 300
    s.moderation_aliyun_alert_failure_rate = 0.2
    s.moderation_aliyun_alert_min_samples = 5
    s.moderation_aliyun_alert_consecutive = 3
    s.moderation_aliyun_alert_cooldown_seconds = 300
    s.aliyun_web_search_api_key = ""
    s.aliyun_web_search_enabled = False
    s.aliyun_web_search_base_url = "https://cloud-iqs.aliyuncs.com/search/unified"
    s.aliyun_web_search_engine_type = "LiteAdvanced"
    s.aliyun_web_search_model = "qwen-plus"
    s.aliyun_web_search_forced = True
    s.aliyun_web_search_enable_source = True
    s.aliyun_web_search_strategy = ""
    s.aliyun_access_key_id = ""
    s.aliyun_access_key_secret = ""
    s.aliyun_sms_sign_name = ""
    s.aliyun_sms_template_code = ""
    s.aliyun_sms_max_per_phone_per_hour = 3
    s.aliyun_captcha_scene_id = ""
    s.aliyun_captcha_prefix = ""
    s.otp_expires_minutes = 10
    s.otp_token_expires_minutes = 10
    s.asr_provider = "openai_compatible"
    s.asr_base_url = "http://fake-asr/v1"
    s.asr_api_key = ""
    s.asr_model = "whisper-1"
    s.asr_timeout_seconds = 5.0
    s.volcengine_asr_endpoint = "https://fake-volcengine/asr"
    s.volcengine_asr_resource_id = "volc.bigasr.auc_turbo"
    s.volcengine_asr_app_id = ""
    s.volcengine_asr_access_token = ""
    s.volcengine_asr_api_key = ""
    s.asr_ffmpeg_path = ""
    s.asr_transcode_timeout_seconds = 2.0
    s.asr_max_audio_bytes = 10 * 1024 * 1024
    s.asr_max_duration_ms = 60_000
    s.asr_mock_transcript = ""
    s.companion_world_p1_enabled = False
    s.companion_world_l3_background_enabled = True
    s.companion_world_proactive_safety_enabled = True
    s.companion_world_feed_enabled = False
    s.companion_world_feed_morning_start = "09:00"
    s.companion_world_feed_morning_end = "11:00"
    s.companion_world_feed_evening_start = "18:00"
    s.companion_world_feed_evening_end = "21:00"
    s.companion_world_feed_scheduler_interval_seconds = 60.0
    s.companion_world_feed_scheduler_batch_size = 20
    s.companion_world_feed_claim_lease_seconds = 300
    s.companion_world_feed_retry_max_attempts = 3
    s.companion_world_feed_retry_base_seconds = 30
    s.companion_world_outbox_batch_size = 50
    s.companion_world_outbox_claim_lease_seconds = 300
    s.companion_world_outbox_max_attempts = 5
    s.companion_world_visits_enabled = False
    s.companion_world_human_chat_enabled = False
    # v1.5 媒体与许愿四位（FLAG-001）：与其余能力位同样显式置 False——MagicMock 的自动属性
    # 是 truthy，漏一个就会让所有开了 p1 的用例误以为该能力已灰度开启。
    s.companion_world_chat_image_enabled = False
    s.companion_world_chat_voice_enabled = False
    s.companion_world_feed_image_enabled = False
    s.companion_world_wish_daily_max = 10
    s.companion_world_wish_job_lease_seconds = 600
    s.companion_world_wish_retry_seconds = 3600
    # 鸣蝉代码统一读取新命名；旧字段继续供迁移前朝夕测试使用。MagicMock 不会执行
    # Settings 的兼容属性映射，因此两组测试默认值需在替身上显式对齐。
    s.mingchan_p1_enabled = False
    s.mingchan_asset_base_url = ""
    s.mingchan_l3_background_enabled = True
    s.mingchan_proactive_safety_enabled = True
    s.mingchan_feed_enabled = False
    s.mingchan_feed_morning_start = "09:00"
    s.mingchan_feed_morning_end = "11:00"
    s.mingchan_feed_evening_start = "18:00"
    s.mingchan_feed_evening_end = "21:00"
    s.mingchan_feed_scheduler_interval_seconds = 60.0
    s.mingchan_feed_scheduler_batch_size = 20
    s.mingchan_feed_claim_lease_seconds = 300
    s.mingchan_feed_retry_max_attempts = 3
    s.mingchan_feed_retry_base_seconds = 30
    s.mingchan_outbox_batch_size = 50
    s.mingchan_outbox_claim_lease_seconds = 300
    s.mingchan_outbox_max_attempts = 5
    s.mingchan_app_inbox_enabled = False
    s.mingchan_app_only_human_proactive_enabled = False
    s.mingchan_notification_cleanup_batch_size = 100
    s.mingchan_lifecycle_evaluation_enabled = False
    s.mingchan_lifecycle_commit_enabled = False
    s.mingchan_mailbox_enabled = False
    s.mingchan_lifecycle_inactivity_days = 60
    s.mingchan_lifecycle_evidence_window_days = 30
    s.mingchan_lifecycle_mismatch_min_events = 3
    s.mingchan_lifecycle_mismatch_min_span_days = 14
    s.mingchan_lifecycle_cooldown_days = 7
    s.mingchan_lifecycle_crisis_freeze_days = 30
    s.mingchan_mailbox_delivery_cooldown_days = 30
    s.mingchan_mailbox_letter_ttl_days = 30
    s.mingchan_mailbox_manifest_hmac_secret = ""
    s.mingchan_lifecycle_scheduler_interval_seconds = 300.0
    s.mingchan_lifecycle_scheduler_batch_size = 50
    s.mingchan_visits_enabled = False
    s.mingchan_human_chat_enabled = False
    s.mingchan_chat_image_enabled = False
    s.mingchan_chat_voice_enabled = False
    s.mingchan_feed_image_enabled = False
    s.mingchan_wish_daily_max = 10
    s.mingchan_wish_job_lease_seconds = 600
    s.mingchan_wish_retry_seconds = 3600
    # v1.5 媒体地基：数值必须显式给，MagicMock 的 __int__ 恒为 1，否则 /app/config 的限额
    # 会静默变成 1 字节、契约测试也失去意义。签名密钥给固定测试值，与生产的"留空即报错"无关。
    s.media_storage_dir = str(tmp_path / "media")
    s.media_url_signing_secret = "test-media-signing-secret"
    s.media_url_owner_ttl_seconds = 900
    s.media_url_visitor_ttl_seconds = 600
    s.media_pending_ttl_hours = 2
    s.media_reclaim_interval_seconds = 3600.0
    # v1.5 S4 图片机审：公网基址默认留空 = 机审不可用（批处理直接返回 disabled，不读库）。
    # 两个数值同样必须显式给，否则 MagicMock 的 __int__/__float__ 会让批量恒为 1。
    s.media_public_base_url = ""
    s.media_moderation_interval_seconds = 60.0
    s.media_moderation_batch_size = 50
    s.media_image_max_bytes = 8_388_608
    s.media_image_count_max = 4
    s.media_voice_max_bytes = 512_000
    s.media_voice_max_duration_ms = 60_000
    s.voice_message_fallback_text = "这段语音我没听清，你可以打字告诉我，或者再发一次～"
    # 多机接入(默认 standalone:default_node_id 留空 → 出站不写 node_id,行为不变)
    s.ai4all_role = "standalone"
    s.node_id = ""
    s.default_node_id = ""
    s.central_url = ""
    s.node_base_url = ""
    s.node_agent_host = "0.0.0.0"
    s.node_agent_port = 8190
    s.node_max_sessions = 0
    s.outbound_pull_interval_seconds = 2.0
    s.outbound_pull_batch_size = 20
    s.outbound_claim_timeout_seconds = 60
    s.local_node_inline_dispatch = False
    # TDAI 长期记忆 sidecar：测试默认关闭，避免 MagicMock 属性自动为 truthy 触发 capture。
    s.tdai_enabled = False
    s.tdai_gateway_url = "http://127.0.0.1:8420"
    s.tdai_gateway_api_key = ""
    s.tdai_recall_enabled = True
    s.tdai_capture_enabled = True
    s.tdai_recall_timeout_seconds = 0.5
    s.tdai_capture_timeout_seconds = 2.0
    s.tdai_recall_max_chars = 2500
    s.tdai_recall_account_allowlist = ""
    terms_dir = tmp_path / "moderation"
    terms_dir.mkdir(parents=True, exist_ok=True)
    (terms_dir / "sensitive_terms.json").write_text(
        """
{
  "version": "test_terms_v1",
  "terms": [
    {
      "id": "test_block",
      "term": "MODERATION_TEST_BLOCK",
      "level": "block",
      "categories": ["test_block"],
      "match_type": "contains",
      "scopes": ["inbound", "outbound", "internal"],
      "enabled": true
    },
    {
      "id": "test_review",
      "term": "MODERATION_TEST_REVIEW",
      "level": "review",
      "categories": ["test_review"],
      "match_type": "contains",
      "scopes": ["inbound", "outbound", "internal"],
      "enabled": true
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    return s


@pytest.fixture
def fresh_db(test_settings):
    """Patch settings modules to use an isolated clone of the migrated PG template."""
    patches = [
        patch("app.db.settings", test_settings),
        patch("app.db.billing.settings", test_settings),
        patch("app.bootstrap.application.settings", test_settings),
        patch("app.bootstrap.lifecycle.settings", test_settings),
        patch("app.products.zhaoxi.lifecycle.settings", test_settings),
        # main.py 拆出的 router 包：各模块各自绑定 settings，需在此一并路由到临时配置。
        patch("app.routers.deps.settings", test_settings),
        patch("app.routers.serializers.settings", test_settings),
        patch("app.routers.health.settings", test_settings),
        patch("app.products.zhaoxi.api.bridge.settings", test_settings),
        patch("app.routers.web.settings", test_settings),
        patch("app.products.mingchan.api.app.settings", test_settings),
        patch("app.products.mingchan.api.human_chat.settings", test_settings),
        patch("app.products.mingchan.api.media.settings", test_settings),
        patch("app.products.mingchan.api.visits.settings", test_settings),
        patch("app.products.mingchan.api.world.settings", test_settings),
        patch("app.products.mingchan.api.mailbox.settings", test_settings),
        patch("app.products.mingchan.api.resident_wishes.settings", test_settings),
        patch(
            "app.products.mingchan.application.resident_wishes.settings",
            test_settings,
        ),
        # admin 侧 world 路由此前漏登记：它 from app.config import settings，未 patch 时读真实
        # settings，开发/生产机 .env 的 COMPANION_WORLD_LIFECYCLE_COMMIT_ENABLED=true 会泄漏进来，
        # 绕过 503 commit 门控使 approve 走到真实提交路径（本机 409、CI 无 .env 则 503 通过）。
        patch("app.products.mingchan.api.admin_world.settings", test_settings),
        patch("app.products.mingchan.api.notifications.settings", test_settings),
        patch("app.products.zhaoxi.api.creator_role_templates.settings", test_settings),
        patch("app.products.mingchan.infrastructure.app_inbox.settings", test_settings),
        patch("app.products.mingchan.infrastructure.world_repository.settings", test_settings),
        patch("app.products.zhaoxi.proactive.delivery.outbound.settings", test_settings),
        patch("app.platform.media.asr.settings", test_settings),
        # v1.5 媒体：assets 决定落盘根目录（不 patch 会往仓库 data/media 写测试文件），
        # access 决定签名密钥与 TTL。
        patch("app.platform.media.assets.settings", test_settings),
        patch("app.platform.media.access.settings", test_settings),
        # S4 图片机审批处理：节流间隔、批量与公网基址都从这里读。
        patch("app.platform.media.moderation.settings", test_settings),
        patch("app.products.zhaoxi.api.debug.settings", test_settings),
        patch("app.platform.moderation.admin.settings", test_settings),
        patch("app.products.zhaoxi.api.admin_proactive.settings", test_settings),
        patch("app.products.zhaoxi.api.admin_dreaming.settings", test_settings),
        patch("app.routers.admin_ops.settings", test_settings),
        patch("app.routers.admin_llm.settings", test_settings),
        patch("app.agent_runtime.llm.service.settings", test_settings),
        patch("app.agent_runtime.llm.providers.settings", test_settings),
        patch("app.products.zhaoxi.infrastructure.profiles.settings", test_settings),
        patch("app.products.zhaoxi.application.memory.dreaming.settings", test_settings),
        patch("app.products.zhaoxi.application.memory.session_lifecycle.settings", test_settings),
        patch("app.products.mingchan.application.memory.settings", test_settings),
        patch("app.products.mingchan.application.lifecycle.settings", test_settings),
        patch("app.products.mingchan.application.mailbox.settings", test_settings),
        patch("app.products.zhaoxi.proactive.delivery.policy.settings", test_settings),
        patch("app.products.zhaoxi.proactive.orchestration.planning.settings", test_settings),
        patch("app.products.zhaoxi.proactive.store.candidates.settings", test_settings),
        patch("app.products.zhaoxi.proactive.slots.settings", test_settings),
        patch("app.products.zhaoxi.proactive.delivery.dispatch.settings", test_settings),
        patch("app.products.zhaoxi.proactive.recall.manual_companion.settings", test_settings),
        patch("app.products.zhaoxi.proactive.recall.topic_followup.settings", test_settings),
        patch("app.products.zhaoxi.proactive.recall.content_invitation.settings", test_settings),
        patch("app.products.zhaoxi.proactive.recall.hot_topic.settings", test_settings),
        patch("app.products.zhaoxi.proactive.preferences.settings", test_settings),
        patch("app.platform.moderation.policy.settings", test_settings),
        patch("app.platform.moderation.product_policy.settings", test_settings),
        patch("app.platform.moderation.sensitive_words.settings", test_settings),
        patch("app.platform.moderation.service.settings", test_settings),
        patch("app.platform.moderation.worker.settings", test_settings),
        patch("app.platform.moderation.llm_review.settings", test_settings),
        patch("app.platform.moderation.image_review.settings", test_settings),
        patch("app.platform.moderation.aliyun_review.settings", test_settings),
        patch("app.platform.moderation.aliyun_alerting.settings", test_settings),
        patch("app.platform.moderation.export.settings", test_settings),
        patch("app.products.zhaoxi.jobs.user_meta.scheduler.settings", test_settings),
    ]
    for p in patches:
        p.start()
    try:
        # 护栏：主测试后端必须路由到 pytest-postgresql 克隆库，不能继承 .env
        # 中的外部数据库。schema 已在 session 模板中迁移完成。
        assert test_settings.database_url, "PG 临时库 database_url 不应为空"
        yield test_settings
    finally:
        from app.db._backend import close_pg_pool

        close_pg_pool()
        for p in reversed(patches):
            p.stop()


@pytest.fixture
def client(fresh_db):
    """FastAPI TestClient with isolated DB, test settings, mocked LLM."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.platform.quota.rate_limiter import RateLimiter

    patches = [
        patch("app.main.settings", fresh_db),
        patch("app.bootstrap.application.settings", fresh_db),
        patch("app.bootstrap.lifecycle.settings", fresh_db),
        patch("app.products.zhaoxi.lifecycle.settings", fresh_db),
        patch("app.routers.deps.settings", fresh_db),
        patch("app.routers.serializers.settings", fresh_db),
        patch("app.routers.health.settings", fresh_db),
        patch("app.products.zhaoxi.api.bridge.settings", fresh_db),
        patch("app.routers.web.settings", fresh_db),
        patch("app.products.mingchan.api.app.settings", fresh_db),
        patch("app.products.mingchan.api.human_chat.settings", fresh_db),
        patch("app.products.mingchan.api.media.settings", fresh_db),
        patch("app.products.mingchan.api.visits.settings", fresh_db),
        patch("app.products.mingchan.api.world.settings", fresh_db),
        patch("app.products.mingchan.api.mailbox.settings", fresh_db),
        patch("app.products.mingchan.api.resident_wishes.settings", fresh_db),
        patch(
            "app.products.mingchan.application.resident_wishes.settings",
            fresh_db,
        ),
        patch("app.products.mingchan.api.admin_world.settings", fresh_db),
        patch("app.products.mingchan.api.notifications.settings", fresh_db),
        patch("app.platform.media.asr.settings", fresh_db),
        patch("app.products.zhaoxi.api.debug.settings", fresh_db),
        patch("app.platform.moderation.admin.settings", fresh_db),
        patch("app.products.zhaoxi.api.admin_proactive.settings", fresh_db),
        patch("app.products.zhaoxi.api.admin_dreaming.settings", fresh_db),
        patch("app.routers.admin_ops.settings", fresh_db),
        patch("app.routers.admin_llm.settings", fresh_db),
        patch("app.agent_runtime.llm.service.settings", fresh_db),
        patch("app.agent_runtime.llm.providers.settings", fresh_db),
        patch("app.turn_service.settings", fresh_db),
        patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db),
        patch("app.products.zhaoxi.proactive.recall.hot_topic.settings", fresh_db),
        patch("app.platform.moderation.policy.settings", fresh_db),
        patch("app.platform.moderation.product_policy.settings", fresh_db),
        patch("app.platform.moderation.sensitive_words.settings", fresh_db),
        patch("app.platform.moderation.service.settings", fresh_db),
        patch("app.platform.moderation.worker.settings", fresh_db),
        patch("app.platform.moderation.llm_review.settings", fresh_db),
        patch("app.platform.moderation.image_review.settings", fresh_db),
        patch("app.platform.moderation.aliyun_review.settings", fresh_db),
        patch("app.platform.moderation.aliyun_alerting.settings", fresh_db),
        patch("app.platform.moderation.export.settings", fresh_db),
        patch("app.products.zhaoxi.application.memory.dreaming.settings", fresh_db),
        patch("app.products.mingchan.application.lifecycle.settings", fresh_db),
        patch("app.products.mingchan.application.mailbox.settings", fresh_db),
        patch("app.products.zhaoxi.jobs.user_meta.scheduler.settings", fresh_db),
        patch("app.products.zhaoxi.application.memory.session_lifecycle.settings", fresh_db),
        patch("app.products.zhaoxi.infrastructure.profiles.settings", fresh_db),
        patch("app.turn_service.rate_limiter", RateLimiter()),
        patch("app.turn_service.generate_reply", return_value="mock reply"),
        patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)),
    ]
    for p in patches:
        p.start()
    yield TestClient(app)
    for p in patches:
        p.stop()


@pytest.fixture
def mingchan_client(fresh_db):
    """只挂鸣蝉规范 namespace 的启用态测试客户端。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.platform.quota.rate_limiter import RateLimiter

    from app.bootstrap.product_registry import build_test_product_registry
    from app.products.mingchan.manifest import install_admin_routes, install_public_routes

    patches = [
        patch("app.agent_runtime.turns.service.rate_limiter", RateLimiter()),
        patch(
            "app.agent_runtime.turns.service.generate_reply",
            return_value="mock reply",
        ),
        patch(
            "app.agent_runtime.turns.service.generate_reply_with_tools",
            return_value=("mock reply", None),
        ),
    ]
    for item in patches:
        item.start()
    try:
        app = FastAPI()
        install_public_routes(
            app,
            registry=build_test_product_registry(),
            config=fresh_db,
        )
        install_admin_routes(app)
        with TestClient(app) as test_client:
            yield test_client
    finally:
        for item in reversed(patches):
            item.stop()
