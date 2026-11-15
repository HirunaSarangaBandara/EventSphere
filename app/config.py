from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./eventsphere.db"
    secret_key: str = "development-only-change-this-secret"
    access_token_expire_minutes: int = 120
    waitlist_hold_minutes: int = 10
    waitlist_sweep_seconds: int = 30
    checkin_early_minutes: int = 180
    checkin_late_minutes: int = 120
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    csrf_cookie_secure: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
