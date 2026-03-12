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

    # Usernames пользователей с доступом к /admin и /stats без пароля (через session_key)
    ADMIN_USERNAMES: str = ""

    TMDB_TOKEN: str
    MYSHOWS_API: str
    MYSHOWS_AUTH_URL: str

    # Базовый URL сайта (для формирования webhook и ссылок)
    BASE_URL: str = "http://localhost:8000"
    # URL плагина np.js (если пусто — используется {BASE_URL}/np.js)
    PLUGIN_URL: str = ""
    # Пароль для доступа к админ-панели /admin
    ADMIN_PASSWORD: str = ""

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_NAME: str = ""  # для ссылок t.me/...
    # JSON-массив telegram_id администраторов, напр. "[123456789]"
    TELEGRAM_ADMIN_IDS: str = "[]"
    # True если сервер за NAT без публичного порта (polling вместо webhook)
    TELEGRAM_USE_POLLING: bool = False

    # Ссылка на донат (показывается в боте; если пусто — кнопки нет)
    DONATE_URL: str = ""

    # HTTP/SOCKS-прокси для загрузки картинок с TMDB (опционально)
    # Формат: http://host:port или socks5://host:port
    IMAGE_PROXY_URL: str = ""
    IMAGE_PROXY_USER: str = ""
    IMAGE_PROXY_PASS: str = ""

    @property
    def admin_username_list(self) -> list[str]:
        return [u.strip() for u in self.ADMIN_USERNAMES.split(",") if u.strip()]

    @property
    def telegram_admin_id_list(self) -> list[int]:
        import json

        try:
            return [int(x) for x in json.loads(self.TELEGRAM_ADMIN_IDS)]
        except Exception:
            return []

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
