from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "local"
    ai4all_bridge_secret: str = "dev-secret"
    admin_token: str = "dev-admin-token"
    database_path: str = "data/ai4all.sqlite3"
    user_profiles_dir: str = "data/user_profiles"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    llm_connect_timeout_seconds: float = 5.0
    llm_max_retries: int = 1
    llm_force_ipv4: bool = True
    llm_context_messages: int = 12
    llm_default_prompt: str = (
        "你是 AI4ALL 的个人 AI 陪伴与生活助理。"
        "你要自然、温和、简洁地回应用户，优先提供情绪陪伴、日常建议和生活协助。"
        "不要把自己定位为心理咨询师，不做诊断。"
    )

    rate_limit_daily: int = 100
    rate_limit_rpm: int = 5
    rate_limit_daily_message: str = "今天聊得有点多了，我晚些时候再继续陪你。"
    rate_limit_rpm_message: str = "消息来得太快了，稍等一下再发我吧。"
    debug_trace_account_ids: str = ""

    openclaw_login_auto_start: bool = True
    openclaw_login_start_timeout_ms: int = 35000
    openclaw_login_wait_timeout_ms: int = 480000
    openclaw_gateway_call_timeout_ms: int = 60000

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
