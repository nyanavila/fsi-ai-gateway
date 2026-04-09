from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Anthropic ────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str

    # Model aliases — update to latest available
    MODEL_SMALL: str = "claude-haiku-4-5-20251001"
    MODEL_LARGE: str = "claude-sonnet-4-6"

    # ── Redis ────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://redis:6379/0"

    # ── Token budgets (daily, per department) ─────────────────────────────────
    BUDGET_CX_DAILY_TOKENS: int = 5_000_000
    BUDGET_IT_DAILY_TOKENS: int = 1_000_000
    BUDGET_FINANCE_DAILY_TOKENS: int = 500_000
    BUDGET_DEFAULT_DAILY_TOKENS: int = 500_000

    # ── App ──────────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    APP_ENV: str = "production"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
