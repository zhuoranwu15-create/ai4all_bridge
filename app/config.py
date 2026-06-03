from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "local"
    ai4all_bridge_secret: str = "dev-secret"
    admin_token: str = "dev-admin-token"
    admin_staff_token: str = ""
    admin_debug_plaintext_enabled: bool = False
    admin_debug_plaintext_account_allowlist: str = ""
    feishu_alert_webhook_url: str = ""
    feishu_website_webhook_url: str = ""
    database_path: str = "data/ai4all.sqlite3"
    user_profiles_dir: str = "data/user_profiles"
    system_dir: str = "data/system"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 40.0
    llm_connect_timeout_seconds: float = 5.0
    llm_max_retries: int = 1
    llm_max_tool_rounds: int = 3
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
    conversation_session_max_turns: int = 500
    conversation_session_business_day_start_hour: int = 4
    dreaming_scheduler_enabled: bool = False
    dreaming_scheduler_interval_seconds: float = 300.0
    dreaming_scheduler_batch_size: int = 100

    openclaw_login_auto_start: bool = True
    openclaw_login_start_timeout_ms: int = 35000
    openclaw_login_wait_timeout_ms: int = 480000
    openclaw_gateway_call_timeout_ms: int = 60000

    proactive_outbound_enabled: bool = True
    proactive_outbound_daily_limit: int = 3
    proactive_quiet_hours_start: str = "22:00"
    proactive_quiet_hours_end: str = "08:00"
    companion_followup_daily_limit: int = 1
    content_invitation_daily_limit: int = 1
    proactive_avoidance_window_hours: int = 6
    content_invitation_rejection_cooldown_days: int = 30
    content_invitation_expire_hours: int = 24
    proactive_scheduler_enabled: bool = False
    proactive_scheduler_interval_seconds: float = 30.0
    proactive_scheduler_batch_size: int = 20
    proactive_scheduler_bypass_quiet_hours: bool = False
    proactive_account_check_interval_seconds: int = 3600
    proactive_account_check_context_messages: int = 12
    proactive_account_check_min_confidence: float = 0.85
    proactive_content_invitation_generation_enabled: bool = True
    proactive_commitment_extraction_enabled: bool = True
    proactive_commitment_context_messages: int = 8
    proactive_commitment_min_confidence: float = 0.9
    proactive_commitment_max_days: int = 14

    web_search_enabled: bool = False
    web_search_default_provider: str = "duckduckgo"
    web_search_provider_order: str = "duckduckgo,bing"
    web_search_provider_failover: bool = True
    web_search_sync_timeout_seconds: float = 8.0
    web_search_max_results: int = 5
    web_search_trace_raw_response: bool = False

    dashscope_api_key: str = ""
    aliyun_web_search_api_key: str = ""
    aliyun_web_search_enabled: bool = False
    aliyun_web_search_base_url: str = "https://cloud-iqs.aliyuncs.com/search/unified"
    aliyun_web_search_engine_type: str = "LiteAdvanced"
    aliyun_web_search_model: str = "qwen-plus"
    aliyun_web_search_forced: bool = True
    aliyun_web_search_enable_source: bool = True
    aliyun_web_search_strategy: str = ""

    baidu_ai_search_enabled: bool = False
    baidu_ai_search_api_key: str = ""
    baidu_ai_search_base_url: str = "https://qianfan.baidubce.com"
    baidu_ai_search_endpoint: str = "/v2/ai_search/web_search"
    baidu_ai_search_source: str = "baidu_search_v2"
    baidu_ai_search_top_k: int = 5

    # Aliyun SMS
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_max_per_phone_per_hour: int = 5

    # Aliyun Captcha 2.0
    aliyun_captcha_scene_id: str = ""
    aliyun_captcha_prefix: str = ""  # frontend only, documented here for reference

    # OTP TTL (minutes)
    otp_expires_minutes: int = 10
    otp_token_expires_minutes: int = 10

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
