from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    ANTHROPIC_API_KEY: str
    MODEL_SMALL: str  = "claude-haiku-4-5-20251001"
    MODEL_MEDIUM: str = "claude-sonnet-4-6"
    MODEL_LARGE: str  = "claude-opus-4-6"
    REDIS_URL: str = "redis://redis:6379/0"
    BUDGET_CX_DAILY_TOKENS: int = 5_000_000
    BUDGET_IT_DAILY_TOKENS: int = 1_000_000
    BUDGET_FINANCE_DAILY_TOKENS: int = 500_000
    BUDGET_DEFAULT_DAILY_TOKENS: int = 500_000
    LOG_LEVEL: str = "INFO"
    APP_ENV: str = "production"

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
