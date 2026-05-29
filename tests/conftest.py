import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def test_settings(tmp_path):
    s = MagicMock()
    s.database_path = str(tmp_path / "test.db")
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.ai4all_bridge_secret = "test-secret"
    s.admin_token = "test-admin"
    s.admin_staff_token = "test-staff"
    s.app_env = "test"
    s.llm_api_key = ""
    s.llm_model = "test-model"
    s.llm_context_messages = 12
    s.llm_default_prompt = "你是测试助手"
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
    s.proactive_scheduler_enabled = False
    s.proactive_scheduler_interval_seconds = 30.0
    s.proactive_scheduler_batch_size = 20
    s.proactive_scheduler_bypass_quiet_hours = False
    s.proactive_account_scan_interval_seconds = 3600
    s.proactive_heartbeat_candidate_context_messages = 12
    s.proactive_heartbeat_candidate_min_confidence = 0.85
    s.proactive_commitment_extraction_enabled = True
    s.proactive_commitment_context_messages = 8
    s.proactive_commitment_min_confidence = 0.9
    s.proactive_commitment_max_days = 14
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
