from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # === Обязательные поля ===
    DB_USER: str = Field(...)
    DB_PASSWORD: str = Field(...)
    DB_NAME: str = Field(...)

    # === Опциональные поля ===
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432

    SUPERUSER_USERNAME: str = ""
    SUPERUSER_PASSWORD: str = ""

    TMDB_TOKEN: str
    MYSHOWS_API: str
    MYSHOWS_AUTH_URL: str

    # Email / SMTP (для восстановления пароля; оставьте пустыми если не нужно)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TLS: bool = True
    # Базовый URL сайта (для формирования ссылки сброса пароля)
    BASE_URL: str = "http://localhost:8000"

    # === Pydantic v2 Config ===
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore"
    )

    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
