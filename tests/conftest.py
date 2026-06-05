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
    s.llm_model = "test-model"
    s.llm_context_messages = 100
    s.llm_default_prompt = "你是测试助手"
    s.llm_max_tool_rounds = 3
    s.debug_trace_account_ids = ""
    s.rate_limit_daily = 3
    s.rate_limit_rpm = 10
    s.rate_limit_daily_message = "每日上限"
    s.rate_limit_rpm_message = "每分钟上限"
    s.conversation_session_max_turns = 500
    s.conversation_session_business_day_start_hour = 4
    s.dreaming_scheduler_enabled = False
    s.dreaming_scheduler_interval_seconds = 300.0
    s.dreaming_scheduler_batch_size = 100
    s.openclaw_login_auto_start = False
    s.openclaw_login_start_timeout_ms = 5000
    s.openclaw_login_wait_timeout_ms = 5000
    s.openclaw_gateway_call_timeout_ms = 5000
    s.proactive_outbound_enabled = True
    s.proactive_outbound_daily_limit = 3
    s.proactive_quiet_hours_start = "22:00"
    s.proactive_quiet_hours_end = "08:00"
    s.companion_followup_daily_limit = 1
    s.content_invitation_daily_limit = 1
    s.proactive_avoidance_window_hours = 6
    s.content_invitation_rejection_cooldown_days = 30
    s.content_invitation_expire_hours = 24
    s.proactive_scheduler_enabled = False
    s.proactive_scheduler_interval_seconds = 30.0
    s.proactive_scheduler_batch_size = 20
    s.proactive_scheduler_bypass_quiet_hours = False
    s.proactive_account_check_interval_seconds = 3600
    s.proactive_account_check_context_messages = 12
    s.proactive_account_check_min_confidence = 0.85
    s.proactive_content_invitation_generation_enabled = True
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
    return s


@pytest.fixture
def fresh_db(test_settings):
    """Patch settings modules to use a temp SQLite/profile workspace."""
    patches = [
        patch("app.db.settings", test_settings),
        patch("app.user_profiles.settings", test_settings),
        patch("app.dreaming.settings", test_settings),
        patch("app.session_lifecycle.settings", test_settings),
        patch("app.proactive.policy.settings", test_settings),
    ]
    for p in patches:
        p.start()
    try:
        from app.db import init_db
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
        patch("app.turn_service.settings", fresh_db),
        patch("app.dreaming.settings", fresh_db),
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
