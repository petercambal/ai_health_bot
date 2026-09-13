from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    telegram_webhook_base_url: str
    telegram_webhook_secret: str

    gemini_api_key: str
    gemini_model: str = "gemini-3.6-flash"

    # Optional: unauthenticated Semantic Scholar API calls share one very easy-to-hit
    # global rate limit (observed 429 on the very first request in testing). A free
    # key raises that limit substantially - apply at
    # https://www.semanticscholar.org/product/api#api-key-form. Without one,
    # search_scientific_studies just fails gracefully and Gemini answers without
    # citations for that message.
    semantic_scholar_api_key: str | None = None

    database_url: str

    log_level: str = "INFO"

    @property
    def telegram_webhook_path(self) -> str:
        return "/webhook/telegram"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.telegram_webhook_base_url.rstrip('/')}{self.telegram_webhook_path}"


settings = Settings()
