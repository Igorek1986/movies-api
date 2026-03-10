from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    UniqueConstraint,
    Text,
)
from sqlalchemy.sql import func
from app.db.database import Base


# Роли пользователей и лимиты устройств
USER_ROLES = ("simple", "premium", "super")
DEVICE_LIMITS = {
    "simple": 3,
    "premium": 8,
    "super": None,  # без ограничений
}


class User(Base):
    """Модель пользователя — только для веб-авторизации."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    # session_key используется только для cookie-авторизации в веб-интерфейсе.
    # Для доступа к API (Lampa) используется Device.token.
    session_key = Column(String(64), unique=True, nullable=True, index=True)
    # Роль: "simple" (3 уст.), "premium" (8 уст.), "super" (без лимита)
    role = Column(String(20), nullable=False, default="simple", server_default="simple")
    # Флаг администратора сайта: доступ к /admin и /stats без пароля
    is_admin = Column(Boolean, nullable=False, default=False, server_default="false")
    # TOTP 2FA
    totp_secret  = Column(String(64), nullable=True)
    totp_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    backup_codes = Column(Text, nullable=True)   # JSON list of SHA-256 hex digests
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username}, role={self.role})>"


class Device(Base):
    """Устройство пользователя. Каждое устройство имеет уникальный токен для Lampa."""

    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False, default="Основное")
    # Хранится plaintext — нужен для device activation flow.
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<Device(id={self.id}, user_id={self.user_id}, name={self.name})>"


class DeviceCode(Base):
    """Одноразовый код для привязки устройства (Lampa) к Device без ручного ввода токена."""

    __tablename__ = "device_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(6), unique=True, nullable=False, index=True)  # формат: "483921"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<DeviceCode(code={self.code}, linked={self.device_id is not None})>"


class Timecode(Base):
    """Прогресс просмотра — привязан к устройству и опциональному профилю Lampa."""

    __tablename__ = "timecodes"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Опциональный ID профиля из встроенной системы профилей Lampa.
    # Пустая строка означает «без профиля» (дефолт).
    lampa_profile_id = Column(String(100), nullable=False, default="", server_default="")
    card_id = Column(String(100), nullable=False, index=True)  # "{tmdb_id}_movie" или "_tv"
    item = Column(String(100), nullable=False, index=True)     # хэш эпизода/фильма (lampa_hash)
    data = Column(Text, nullable=False)                        # JSON: {duration, time, percent}
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "device_id", "lampa_profile_id", "card_id", "item",
            name="uq_timecode_unique"
        ),
    )

    def __repr__(self):
        return f"<Timecode(device_id={self.device_id}, card_id={self.card_id}, item={self.item})>"


class MediaCard(Base):
    """Базовые TMDB-метаданные для карточек истории просмотра."""

    __tablename__ = "media_cards"

    card_id = Column(String(100), primary_key=True)   # "{tmdb_id}_movie" | "{tmdb_id}_tv"
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(10), nullable=False)   # "movie" | "tv"
    title = Column(String(500), nullable=True)
    original_title = Column(String(500), nullable=True)
    poster_path = Column(String(300), nullable=True)
    year = Column(String(4), nullable=True)
    # Extended TMDB cache fields
    backdrop_path = Column(String(300), nullable=True)
    overview = Column(Text, nullable=True)
    vote_average = Column(Float, nullable=True)
    release_date = Column(String(20), nullable=True)   # release_date (movie) / first_air_date (tv)
    last_air_date = Column(String(20), nullable=True)  # tv only
    number_of_seasons = Column(Integer, nullable=True) # tv only
    seasons_json = Column(Text, nullable=True)          # JSON list of seasons, tv only
    last_ep_season = Column(Integer, nullable=True)    # last_episode_to_air.season_number, tv only
    last_ep_number = Column(Integer, nullable=True)    # last_episode_to_air.episode_number, tv only
    next_ep_air_date = Column(String(20), nullable=True)  # next_episode_to_air.air_date; "" = нет; NULL = не обновлено
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<MediaCard(card_id={self.card_id}, title={self.title})>"


class LampaProfile(Base):
    """Человеческое название для lampa_profile_id внутри устройства."""

    __tablename__ = "lampa_profiles"

    id               = Column(Integer, primary_key=True, index=True)
    device_id        = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    lampa_profile_id = Column(String(100), nullable=False)
    name             = Column(String(100), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("device_id", "lampa_profile_id", name="uq_lampa_profile"),
    )

    def __repr__(self):
        return f"<LampaProfile(device_id={self.device_id}, profile_id={self.lampa_profile_id}, name={self.name})>"


class MyShowsUser(Base):
    """Статистика обращений пользователей MyShows."""

    __tablename__ = "stats_myshows_users"

    id = Column(Integer, primary_key=True, index=True)
    login = Column(String(100), nullable=False, index=True)
    date = Column(String(10), nullable=False, index=True)   # YYYY-MM-DD
    requests = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        UniqueConstraint("login", "date", name="uq_myshows_login_date"),
    )


class ApiUser(Base):
    """Статистика обращений по IP (обычные пользователи API)."""

    __tablename__ = "stats_api_users"

    id = Column(Integer, primary_key=True, index=True)
    ip = Column(String(50), nullable=False, index=True)
    date = Column(String(10), nullable=False, index=True)   # YYYY-MM-DD
    requests = Column(Integer, default=1, nullable=False)
    country = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True)
    region = Column(String(100), nullable=True)
    flag_emoji = Column(String(10), nullable=True)

    __table_args__ = (
        UniqueConstraint("ip", "date", name="uq_api_ip_date"),
    )


class CategoryRequest(Base):
    """Статистика обращений к категориям контента."""

    __tablename__ = "stats_category_requests"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(200), nullable=False, index=True)
    ip = Column(String(50), nullable=False, index=True)
    date = Column(String(10), nullable=False, index=True)   # YYYY-MM-DD
    requests = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        UniqueConstraint("category", "ip", "date", name="uq_category_ip_date"),
    )


class PasswordResetToken(Base):
    """Одноразовый токен для сброса пароля через Telegram."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<PasswordResetToken(user_id={self.user_id})>"


class TelegramUser(Base):
    """Привязка Telegram-аккаунта к пользователю сайта."""

    __tablename__ = "telegram_users"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username    = Column(String(100), nullable=True)   # @handle без @
    linked_at   = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<TelegramUser(user_id={self.user_id}, telegram_id={self.telegram_id})>"


class TelegramLinkCode(Base):
    """Одноразовый код для привязки Telegram-аккаунта (TTL 10 мин)."""

    __tablename__ = "telegram_link_codes"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code       = Column(String(6), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<TelegramLinkCode(user_id={self.user_id}, code={self.code})>"


class Session(Base):
    """Веб-сессия пользователя (cookie session_key → Session.key)."""

    __tablename__ = "sessions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key        = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    ip         = Column(String(50), nullable=True)
    user_agent = Column(String(500), nullable=True)

    def __repr__(self):
        return f"<Session(user_id={self.user_id}, ip={self.ip})>"


class Totp2faPending(Base):
    """Временный токен ожидающего 2FA-подтверждения входа (TTL 10 мин)."""

    __tablename__ = "totp_2fa_pending"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token      = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<Totp2faPending(user_id={self.user_id})>"


class SupportMessage(Base):
    """Сообщение в чате поддержки между пользователем и администратором."""

    __tablename__ = "support_messages"

    id               = Column(Integer, primary_key=True, index=True)
    # Telegram пользователя (не обязательно привязанного к аккаунту сайта)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    user_username    = Column(String(100), nullable=True)
    # direction: 'in' = user→admin, 'out' = admin→user
    direction        = Column(String(3), nullable=False)
    text             = Column(Text, nullable=False)
    # ID уведомления в чате конкретного администратора (для маршрутизации ответов)
    admin_telegram_id = Column(BigInteger, nullable=True, index=True)
    admin_msg_id      = Column(Integer, nullable=True)
    is_read           = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at        = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<SupportMessage(id={self.id}, direction={self.direction}, from={self.user_telegram_id})>"
