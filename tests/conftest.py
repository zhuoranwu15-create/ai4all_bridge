import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def test_settings(tmp_path):
    s = MagicMock()
    s.database_path = str(tmp_path / "test.db")
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.ai4all_bridge_secret = "test-secret"
    s.admin_token = "test-admin"
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
    s.openclaw_login_auto_start = False
    s.openclaw_login_start_timeout_ms = 5000
    s.openclaw_login_wait_timeout_ms = 5000
    s.openclaw_gateway_call_timeout_ms = 5000
    return s


@pytest.fixture
def fresh_db(test_settings):
    """Patch app.db.settings to use a temp SQLite file."""
    with patch("app.db.settings", test_settings):
        from app.db import init_db
        init_db()
        yield test_settings


@pytest.fixture
def client(fresh_db):
    """FastAPI TestClient with isolated DB, test settings, mocked LLM."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.rate_limiter import RateLimiter

    patches = [
        patch("app.main.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch("app.main.rate_limiter", RateLimiter()),
        patch("app.main.generate_reply", return_value="mock reply"),
    ]
    for p in patches:
        p.start()
    yield TestClient(app)
    for p in patches:
        p.stop()
