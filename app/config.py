from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "local"
    ai4all_bridge_secret: str = "dev-secret"
    database_path: str = "data/ai4all.sqlite3"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    llm_context_messages: int = 12
    llm_default_prompt: str = (
        "你是 AI4ALL 的个人 AI 陪伴与生活助理。"
        "你要自然、温和、简洁地回应用户，优先提供情绪陪伴、日常建议和生活协助。"
        "不要把自己定位为心理咨询师，不做诊断。"
    )

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
