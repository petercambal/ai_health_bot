from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    telegram_webhook_base_url: str
    telegram_webhook_secret: str

    gemini_api_key: str
    gemini_model: str = "gemini-3.6-flash"

    # Optional: identifies requests to OpenAlex (search_scientific_studies) for its
    # "polite pool" - more reliable service, not an API key/no signup or approval
    # needed, just a courtesy contact. Works fine without it too.
    openalex_email: str | None = None

    database_url: str

    log_level: str = "INFO"

    @property
    def telegram_webhook_path(self) -> str:
        return "/webhook/telegram"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.telegram_webhook_base_url.rstrip('/')}{self.telegram_webhook_path}"


settings = Settings()
