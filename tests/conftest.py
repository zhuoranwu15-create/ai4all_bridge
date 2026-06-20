import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def test_settings(tmp_path):
    s = MagicMock()
    s.database_path = str(tmp_path / "test.db")
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
    s.llm_model = "test-model"
    s.llm_default_provider_id = "deepseek-v4-pro"
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
    s.debug_trace_account_ids = ""
    s.rate_limit_daily = 3
    s.rate_limit_rpm = 10
    s.rate_limit_rpm_window_seconds = 30.0
    s.rate_limit_daily_message = "每日上限"
    s.rate_limit_rpm_message = "每分钟上限"
    s.conversation_session_max_turns = 500
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
    s.proactive_avoidance_window_hours = 6
    s.content_invitation_rejection_cooldown_days = 30
    s.content_invitation_expire_hours = 24
    s.proactive_scheduler_enabled = False
    s.proactive_scheduler_interval_seconds = 30.0
    s.proactive_scheduler_batch_size = 20
    s.proactive_scheduler_bypass_quiet_hours = False
    s.proactive_planning_interval_seconds = 3600
    s.proactive_account_check_context_messages = 12
    s.proactive_account_check_min_confidence = 0.85
    s.proactive_content_invitation_tool_rounds = 5
    s.reactivation_daily_limit = 1
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
    s.baidu_ai_search_enabled = False
    s.baidu_ai_search_api_key = ""
    s.baidu_ai_search_base_url = "https://qianfan.baidubce.com"
    s.baidu_ai_search_endpoint = "/v2/ai_search/web_search"
    s.baidu_ai_search_source = "baidu_search_v2"
    s.baidu_ai_search_top_k = 5
    s.aliyun_access_key_id = ""
    s.aliyun_access_key_secret = ""
    s.aliyun_sms_sign_name = ""
    s.aliyun_sms_template_code = ""
    s.aliyun_sms_max_per_phone_per_hour = 3
    s.aliyun_captcha_scene_id = ""
    s.aliyun_captcha_prefix = ""
    s.otp_expires_minutes = 10
    s.otp_token_expires_minutes = 10
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
        patch("app.routers.bridge.settings", test_settings),
        patch("app.routers.web.settings", test_settings),
        patch("app.routers.debug.settings", test_settings),
        patch("app.routers.admin_moderation.settings", test_settings),
        patch("app.routers.admin_proactive.settings", test_settings),
        patch("app.routers.admin_dreaming.settings", test_settings),
        patch("app.routers.admin_ops.settings", test_settings),
        patch("app.routers.admin_llm.settings", test_settings),
        patch("app.llm.settings", test_settings),
        patch("app.user_profiles.settings", test_settings),
        patch("app.dreaming.settings", test_settings),
        patch("app.session_lifecycle.settings", test_settings),
        patch("app.proactive.policy.settings", test_settings),
        patch("app.proactive.reactivation.settings", test_settings),
        patch("app.proactive.settings.settings", test_settings),
        patch("app.moderation.policy.settings", test_settings),
        patch("app.moderation.sensitive_words.settings", test_settings),
        patch("app.moderation.service.settings", test_settings),
        patch("app.moderation.worker.settings", test_settings),
        patch("app.moderation.llm_review.settings", test_settings),
        patch("app.moderation.image_review.settings", test_settings),
        patch("app.moderation.aliyun_review.settings", test_settings),
        patch("app.moderation.aliyun_alerting.settings", test_settings),
        patch("app.moderation.export.settings", test_settings),
        patch("app.user_meta_scheduler.settings", test_settings),
    ]
    for p in patches:
        p.start()
    try:
        from app.db import init_db
        from app.db._core import _db_path
        # 护栏：确认 db 层确实路由到临时库，绝不落到生产库 data/ai4all.sqlite3。
        # （拆包后 settings 绑定若失效会静默回落生产库；此断言可第一时间拦截。）
        resolved = str(_db_path())
        assert resolved == str(test_settings.database_path), (
            f"测试 DB 未隔离，疑似指向生产库: {resolved}"
        )
        init_db()
        yield test_settings
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.fixture
def client(fresh_db):
    """FastAPI TestClient with isolated DB, test settings, mocked LLM."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.rate_limiter import RateLimiter

    patches = [
        patch("app.main.settings", fresh_db),
        patch("app.routers.deps.settings", fresh_db),
        patch("app.routers.serializers.settings", fresh_db),
        patch("app.routers.health.settings", fresh_db),
        patch("app.routers.bridge.settings", fresh_db),
        patch("app.routers.web.settings", fresh_db),
        patch("app.routers.debug.settings", fresh_db),
        patch("app.routers.admin_moderation.settings", fresh_db),
        patch("app.routers.admin_proactive.settings", fresh_db),
        patch("app.routers.admin_dreaming.settings", fresh_db),
        patch("app.routers.admin_ops.settings", fresh_db),
        patch("app.routers.admin_llm.settings", fresh_db),
        patch("app.llm.settings", fresh_db),
        patch("app.turn_service.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch("app.moderation.policy.settings", fresh_db),
        patch("app.moderation.sensitive_words.settings", fresh_db),
        patch("app.moderation.service.settings", fresh_db),
        patch("app.moderation.worker.settings", fresh_db),
        patch("app.moderation.llm_review.settings", fresh_db),
        patch("app.moderation.image_review.settings", fresh_db),
        patch("app.moderation.aliyun_review.settings", fresh_db),
        patch("app.moderation.aliyun_alerting.settings", fresh_db),
        patch("app.moderation.export.settings", fresh_db),
        patch("app.dreaming.settings", fresh_db),
        patch("app.user_meta_scheduler.settings", fresh_db),
        patch("app.session_lifecycle.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch("app.turn_service.rate_limiter", RateLimiter()),
        patch("app.turn_service.generate_reply", return_value="mock reply"),
        patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)),
    ]
    for p in patches:
        p.start()
    yield TestClient(app)
    for p in patches:
        p.stop()
