from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Text,
)
from sqlalchemy.sql import func
from app.db.database import Base


class User(Base):
    """Модель пользователя — только для веб-авторизации."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    # session_key используется только для cookie-авторизации в веб-интерфейсе.
    # Для доступа к API (Lampa) используется Profile.api_key.
    session_key = Column(String(64), unique=True, nullable=True, index=True)
    # email — необязательный, только для восстановления пароля
    email = Column(String(200), unique=True, nullable=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username})>"


class Profile(Base):
    """Профиль пользователя. Каждый профиль имеет собственный API-ключ для Lampa."""

    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False, default="Основной")
    # Хранится plaintext — нужен для device activation flow.
    # Не является паролем, управляет только доступом к спискам фильмов.
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<Profile(id={self.id}, user_id={self.user_id}, name={self.name})>"


class DeviceCode(Base):
    """Одноразовый код для привязки устройства (Lampa) к профилю без ручного ввода токена."""

    __tablename__ = "device_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(8), unique=True, nullable=False, index=True)  # формат: "ABC-123"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    profile_id = Column(Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<DeviceCode(code={self.code}, linked={self.profile_id is not None})>"


class Timecode(Base):
    """Прогресс просмотра — привязан к профилю, не к пользователю напрямую."""

    __tablename__ = "timecodes"

    id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    card_id = Column(String(100), nullable=False, index=True)  # "{tmdb_id}_movie" или "_tv"
    item = Column(String(100), nullable=False, index=True)     # хэш эпизода/фильма (lampa_hash)
    data = Column(Text, nullable=False)                        # JSON: {duration, time, percent}
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("profile_id", "card_id", "item", name="uq_timecode_unique"),
    )

    def __repr__(self):
        return f"<Timecode(profile_id={self.profile_id}, card_id={self.card_id}, item={self.item})>"


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
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<MediaCard(card_id={self.card_id}, title={self.title})>"


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
    """Одноразовый токен для сброса пароля через email."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<PasswordResetToken(user_id={self.user_id})>"
