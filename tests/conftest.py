import os
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# ambient DATABASE_URL guard：本地/生产 .env 可能为「运行时」设了 DATABASE_URL（切 PG）。
# 测试后端只由 AI4ALL_TEST_DB 决定、且每测试用独立库；因此在此把进程内 ambient
# database_url 清空，避免它泄漏到未 patch settings 的测试——否则全局 is_postgres()
# 会被误判（SQLite 连接跑出 PG 分支报错），PG 档下甚至会误连真实 dev/生产库。
# 两档都生效：SQLite 档回到空串（→ database_path）；PG 档由 test_settings 各自注入临时库 DSN。
# ---------------------------------------------------------------------------
import app.config as _app_config  # noqa: E402
_app_config.settings.database_url = ""
# 同类 ambient .env 泄漏：dev/生产 .env 常把 openclaw_cli_path 设成绝对路径（CLI 不在服务 PATH 时），
# 会泄漏进直接用全局 settings 的 openclaw/runtime_health/node 测试——它们断言命令首元素为裸名 "openclaw"，
# 装了 CLI 的开发机上 cmd[0] 变成绝对路径致误判失败（CI/干净环境无 .env 故不暴露）。在此固定回默认裸名，
# 使测试与本机 .env 无关；需要绝对路径的测试自行 monkeypatch 覆盖。
_app_config.settings.openclaw_cli_path = "openclaw"


# ---------------------------------------------------------------------------
# 测试后端开关（1e）：默认 SQLite；AI4ALL_TEST_DB=postgres 时整套跑临时 PG。
# PG 档由 pytest-postgresql 提供：postgresql_proc 起一个 session 级 PG 进程，
# postgresql_db 为每个测试 create/drop 一个独立库（与 SQLite 的 tmp_path 每测试隔离对等）。
# ---------------------------------------------------------------------------
_PG_MODE = os.environ.get("AI4ALL_TEST_DB", "").strip().lower() in (
    "pg", "postgres", "postgresql",
)

if _PG_MODE:  # 仅 PG 档注册，SQLite 档完全不引入 pytest-postgresql
    from pytest_postgresql import factories as _pg_factories

    postgresql_proc = _pg_factories.postgresql_proc()
    postgresql_db = _pg_factories.postgresql("postgresql_proc")


# PG 档下应跳过的「SQLite 专有基础设施」测试（按测试函数名匹配）：
# - 临时 DB 文件拷贝隔离（依赖 sqlite 文件 + shutil.copy）
# - WAL checkpoint 截断（依赖 -wal 文件）
# - 迁移用 PRAGMA table_info 内省列（PG 无 PRAGMA）
_SQLITE_ONLY_TESTS = frozenset({
    "test_temporary_database_copy_isolates_candidate_writes",
    "test_temporary_database_copy_isolates_invitation_writes",
    "test_checkpoint_wal_truncates_after_writes",
    "test_migration_adds_node_columns_table_index_idempotent",
})


# 判定为「db 档」的 fixture：请求其一即视为依赖真实 DB（建库+迁移）。client 不在此列，
# 因为含 client 的用例统一归为 integration（client 依赖 fresh_db，故须先判 client）。
_DB_FIXTURES = frozenset({
    "fresh_db", "test_settings", "db_dsn", "postgresql_db", "postgresql_proc",
})


def pytest_collection_modifyitems(config, items):
    """两件事：
    1) 按 fixture 依赖自动派生互斥主档 marker（unit/db/integration）——一处覆盖全部用例，
       新增测试按其请求的 fixture 自动归档，无需逐文件手写 marker。
    2) PG 档下跳过验证 SQLite 专有基础设施的测试（WAL/文件拷贝/PRAGMA 内省）。
    """
    for item in items:
        fx = set(getattr(item, "fixturenames", ()))
        if "client" in fx:
            item.add_marker("integration")
        elif fx & _DB_FIXTURES:
            item.add_marker("db")
        else:
            item.add_marker("unit")

    if _PG_MODE:
        skip = pytest.mark.skip(reason="SQLite 专有基础设施，PG 档不适用")
        for item in items:
            if item.originalname in _SQLITE_ONLY_TESTS or item.name in _SQLITE_ONLY_TESTS:
                item.add_marker(skip)


def _dsn_from_conn(conn) -> str:
    """从 pytest-postgresql 的连接推出 URL 形式 DSN（供 is_postgres 识别 + 业务层直连）。"""
    info = conn.info
    if info.host and info.host.startswith("/"):
        # 本地 unix socket：host 放进 query，URL 主体留空 host
        return f"postgresql://{info.user}@/{info.dbname}?host={info.host}&port={info.port}"
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture
def db_dsn(request):
    """SQLite 档返回空串（→ 走 database_path）；PG 档返回本测试独立临时库的 DSN。"""
    if not _PG_MODE:
        return ""
    conn = request.getfixturevalue("postgresql_db")
    return _dsn_from_conn(conn)


@pytest.fixture
def test_settings(tmp_path, db_dsn):
    s = MagicMock()
    s.database_path = str(tmp_path / "test.db")
    # database_url 必须显式赋值：SQLite 档为空串（→ database_path）；PG 档为临时库 DSN。
    # 否则 MagicMock 自动属性会让 is_postgres() 误判为 True。
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
    s.dreaming_scheduler_interval_seconds = 300.0
    s.dreaming_scheduler_batch_size = 100
    s.user_meta_scheduler_enabled = False
    s.user_meta_scheduler_hour = 3
    s.user_meta_scheduler_page_size = 100
    s.user_meta_scheduler_inter_account_sleep = 0.0
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
    s.web_search_enabled = False
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
    s.asr_base_url = "http://fake-asr/v1"
    s.asr_api_key = ""
    s.asr_model = "whisper-1"
    s.asr_timeout_seconds = 5.0
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
    """Patch settings modules to use a temp SQLite/profile workspace."""
    patches = [
        patch("app.db.settings", test_settings),
        # main.py 拆出的 router 包：各模块各自绑定 settings，需在此一并路由到临时配置。
        patch("app.routers.deps.settings", test_settings),
        patch("app.routers.serializers.settings", test_settings),
        patch("app.routers.health.settings", test_settings),
        patch("app.products.zhaoxi.api.bridge.settings", test_settings),
        patch("app.routers.web.settings", test_settings),
        patch("app.routers.app_api.settings", test_settings),
        patch("app.products.zhaoxi.api.companion_world.settings", test_settings),
        patch("app.products.zhaoxi.api.app_notifications.settings", test_settings),
        patch("app.products.zhaoxi.infrastructure.app_inbox.settings", test_settings),
        patch("app.products.zhaoxi.infrastructure.repositories.companion_world.settings", test_settings),
        patch("app.products.zhaoxi.proactive.contract.common.settings", test_settings),
        patch("app.products.zhaoxi.proactive.delivery.outbound.settings", test_settings),
        patch("app.platform.media.asr.settings", test_settings),
        patch("app.products.zhaoxi.api.debug.settings", test_settings),
        patch("app.products.zhaoxi.api.admin_moderation.settings", test_settings),
        patch("app.products.zhaoxi.api.admin_proactive.settings", test_settings),
        patch("app.products.zhaoxi.api.admin_dreaming.settings", test_settings),
        patch("app.routers.admin_ops.settings", test_settings),
        patch("app.routers.admin_llm.settings", test_settings),
        patch("app.agent_runtime.llm.service.settings", test_settings),
        patch("app.agent_runtime.llm.providers.settings", test_settings),
        patch("app.products.zhaoxi.infrastructure.profiles.settings", test_settings),
        patch("app.products.zhaoxi.application.memory.dreaming.settings", test_settings),
        patch("app.products.zhaoxi.application.memory.session_lifecycle.settings", test_settings),
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
        from app.db import init_db
        from app.db._backend import is_postgres
        from app.db._core import _db_path
        # 护栏：确认 db 层确实路由到临时库，绝不落到生产库。
        # （拆包后 settings 绑定若失效会静默回落生产库；此断言可第一时间拦截。）
        if is_postgres():
            # PG 档：临时库由 pytest-postgresql 每测试 create/drop 隔离，DSN 非空即可。
            assert test_settings.database_url, "PG 档下 database_url 不应为空"
        else:
            resolved = str(_db_path())
            assert resolved == str(test_settings.database_path), (
                f"测试 DB 未隔离，疑似指向生产库: {resolved}"
            )
        init_db()
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
        patch("app.routers.deps.settings", fresh_db),
        patch("app.routers.serializers.settings", fresh_db),
        patch("app.routers.health.settings", fresh_db),
        patch("app.products.zhaoxi.api.bridge.settings", fresh_db),
        patch("app.routers.web.settings", fresh_db),
        patch("app.routers.app_api.settings", fresh_db),
        patch("app.products.zhaoxi.api.companion_world.settings", fresh_db),
        patch("app.platform.media.asr.settings", fresh_db),
        patch("app.products.zhaoxi.api.debug.settings", fresh_db),
        patch("app.products.zhaoxi.api.admin_moderation.settings", fresh_db),
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
        patch("app.platform.moderation.sensitive_words.settings", fresh_db),
        patch("app.platform.moderation.service.settings", fresh_db),
        patch("app.platform.moderation.worker.settings", fresh_db),
        patch("app.platform.moderation.llm_review.settings", fresh_db),
        patch("app.platform.moderation.image_review.settings", fresh_db),
        patch("app.platform.moderation.aliyun_review.settings", fresh_db),
        patch("app.platform.moderation.aliyun_alerting.settings", fresh_db),
        patch("app.platform.moderation.export.settings", fresh_db),
        patch("app.products.zhaoxi.application.memory.dreaming.settings", fresh_db),
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
