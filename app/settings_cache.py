"""
In-memory cache for app_settings table.

Load on startup via load(db). Updates via set_setting() apply immediately
to both DB and memory — no restart needed.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default values (used if DB row is missing)
DEFAULTS: dict[str, str] = {
    # ── Simple role ────────────────────────────────────────────────────────────
    "simple_device_limit": "1",
    "simple_profile_limit": "3",
    "simple_timecode_limit": "5000",
    "simple_favorite_limit": "200",
    "simple_import_daily": "1",
    # ── Premium role ───────────────────────────────────────────────────────────
    "premium_device_limit": "3",
    "premium_profile_limit": "5",
    "premium_timecode_limit": "10000",
    "premium_favorite_limit": "500",
    "premium_import_daily": "3",
    "premium_myshows_daily": "1",
    "premium_duration_days": "30",
    # ── Super role (0 = unlimited) ─────────────────────────────────────────────
    "super_device_limit": "0",
    "super_profile_limit": "0",
    "super_timecode_limit": "0",
    "super_favorite_limit": "0",
    "super_import_daily": "0",
    "super_myshows_daily": "0",
    # ── Grace period after premium expiry ──────────────────────────────────────
    "timecode_grace_days": "3",
    "premium_warn_days": "3",
    "premium_extend_all_days": "3",
    # ── Inactive user auto-deletion (0 = disabled) ────────────────────────────
    "inactive_delete_days": "180",
    "inactive_warn_days": "7",
    # ── Notifications ──────────────────────────────────────────────────────────
    "default_timezone": "Europe/Moscow",
    # ── General ────────────────────────────────────────────────────────────────
    "daily_task_hour": "2",
    # ── Episodes refresh ───────────────────────────────────────────────────────
    "episodes_future_threshold": "5",   # мин. кол-во будущих серий; если меньше — обновляем
    "episodes_refresh_batch":    "10",  # размер пачки при обработке шоу
    "episodes_refresh_delay":    "2",   # пауза в секундах между пачками
    "watched_threshold": "90",
    "session_ttl_days": "30",
    "session_renew_days": "15",
    "device_token_ttl_days": "90",
    "device_code_ttl_minutes": "10",
    "telegram_link_ttl_minutes": "10",
    "reset_code_ttl_minutes": "15",
    "pending_2fa_ttl_sec": "600",
    # ── Rate limits ────────────────────────────────────────────────────────────
    "rate_login_max": "10",
    "rate_login_window_sec": "900",
    "rate_register_max": "5",
    "rate_register_window_sec": "3600",
    "rate_forgot_max": "3",
    "rate_forgot_window_sec": "3600",
    "rate_2fa_max": "5",
    "rate_2fa_window_sec": "900",
    "sync_cooldown_sec": "86400",
    "popular_period_days": "30",
    # ── Analytics ──────────────────────────────────────────────────────────────
    "yandex_metrika_enabled": "0",
    "yandex_metrika_id": "",
    "google_analytics_enabled": "0",
    "google_analytics_id": "",
    # ── Legal ──────────────────────────────────────────────────────────────────
    "site_name": "NUMParser",
    "contact_email": "",
    "privacy_policy_url": "/privacy",
    "consent_url": "/consent",
    "privacy_policy_content": "",
    "consent_content": "",
}

# Human-readable labels for the admin UI (key → label)
LABELS: dict[str, str] = {
    "simple_device_limit": "Simple — устройств",
    "simple_profile_limit": "Simple — профилей",
    "simple_timecode_limit": "Simple — таймкодов на профиль",
    "simple_favorite_limit": "Simple — закладок на категорию",
    "simple_import_daily": "Simple — импортов в сутки",
    "premium_device_limit": "Premium — устройств",
    "premium_profile_limit": "Premium — профилей",
    "premium_timecode_limit": "Premium — таймкодов на профиль",
    "premium_favorite_limit": "Premium — закладок на категорию",
    "premium_import_daily": "Premium — импортов в сутки",
    "premium_myshows_daily": "Premium — MyShows синков в сутки",
    "premium_duration_days": "Premium — длительность (дней)",
    "super_device_limit": "Super — устройств (0=∞)",
    "super_profile_limit": "Super — профилей (0=∞)",
    "super_timecode_limit": "Super — таймкодов на профиль (0=∞)",
    "super_favorite_limit": "Super — закладок на категорию (0=∞)",
    "super_import_daily": "Super — импортов в сутки (0=∞)",
    "super_myshows_daily": "Super — MyShows синков в сутки (0=∞)",
    "inactive_delete_days": "Автоудаление неактивных аккаунтов (дней, 0 = выкл)",
    "inactive_warn_days": "Предупреждение об удалении аккаунта (дней до удаления)",
    "timecode_grace_days": "Грейс-период таймкодов (дней)",
    "premium_warn_days": "Предупреждение об истечении Premium (дней)",
    "premium_extend_all_days": "Продлить всем Premium (дней)",
    "default_timezone": "Таймзона по умолчанию (fallback)",
    "daily_task_hour": "Час запуска ежедневной задачи (0–23)",
    "episodes_future_threshold": "Эпизоды: порог будущих серий (меньше — обновляем)",
    "episodes_refresh_batch": "Эпизоды: размер пачки при обновлении",
    "episodes_refresh_delay": "Эпизоды: пауза между пачками (сек)",
    "watched_threshold": "Порог «просмотрено» (%)",
    "session_ttl_days": "Срок сессии (дней)",
    "session_renew_days": "Продление сессии (дней до истечения)",
    "device_token_ttl_days": "Срок запоминания устройства (дней)",
    "device_code_ttl_minutes": "TTL кода устройства (мин)",
    "telegram_link_ttl_minutes": "TTL кода Telegram (мин)",
    "reset_code_ttl_minutes": "TTL кода сброса пароля (мин)",
    "pending_2fa_ttl_sec": "Ожидание 2FA (сек)",
    "rate_login_max": "Rate: login — попыток",
    "rate_login_window_sec": "Rate: login — окно (сек)",
    "rate_register_max": "Rate: register — попыток",
    "rate_register_window_sec": "Rate: register — окно (сек)",
    "rate_forgot_max": "Rate: forgot — попыток",
    "rate_forgot_window_sec": "Rate: forgot — окно (сек)",
    "rate_2fa_max": "Rate: 2FA — попыток",
    "rate_2fa_window_sec": "Rate: 2FA — окно (сек)",
    "sync_cooldown_sec": "MyShows cooldown (сек)",
    "popular_period_days": "Популярное — период (дней)",
    "yandex_metrika_enabled": "Яндекс.Метрика — включена",
    "yandex_metrika_id": "Яндекс.Метрика ID",
    "google_analytics_enabled": "Google Analytics — включена",
    "google_analytics_id": "Google Analytics ID",
    "site_name": "Название сервиса",
    "contact_email": "Контактный email (отображается на юридических страницах)",
    "privacy_policy_url": "URL Политики обработки персональных данных",
    "consent_url": "URL Согласия на обработку персональных данных",
    "privacy_policy_content": "Текст Политики обработки персональных данных (HTML)",
    "consent_content": "Текст Согласия на обработку персональных данных (HTML)",
}

# Ordered groups for the UI
GROUPS: list[tuple[str, list[str]]] = [
    (
        "Лимиты Simple",
        [
            "simple_device_limit",
            "simple_profile_limit",
            "simple_timecode_limit",
            "simple_favorite_limit",
            "simple_import_daily",
        ],
    ),
    (
        "Лимиты Premium",
        [
            "premium_device_limit",
            "premium_profile_limit",
            "premium_timecode_limit",
            "premium_favorite_limit",
            "premium_import_daily",
            "premium_myshows_daily",
            "premium_duration_days",
        ],
    ),
    (
        "Лимиты Super (0 = без ограничений)",
        [
            "super_device_limit",
            "super_profile_limit",
            "super_timecode_limit",
            "super_favorite_limit",
            "super_import_daily",
            "super_myshows_daily",
        ],
    ),
    (
        "Обновление эпизодов",
        [
            "episodes_future_threshold",
            "episodes_refresh_batch",
            "episodes_refresh_delay",
        ],
    ),
    (
        "Общие настройки",
        [
            "inactive_delete_days",
            "inactive_warn_days",
            "timecode_grace_days",
            "premium_warn_days",
            "premium_extend_all_days",
            "watched_threshold",
            "popular_period_days",
            "daily_task_hour",
            "session_ttl_days",
            "session_renew_days",
            "device_token_ttl_days",
            "device_code_ttl_minutes",
            "telegram_link_ttl_minutes",
            "reset_code_ttl_minutes",
            "pending_2fa_ttl_sec",
        ],
    ),
    (
        "Уведомления",
        [
            "default_timezone",
        ],
    ),
    (
        "Аналитика",
        [
            "yandex_metrika_enabled",
            "yandex_metrika_id",
            "google_analytics_enabled",
            "google_analytics_id",
        ],
    ),
    (
        "Юридические",
        [
            "site_name",
            "contact_email",
            "privacy_policy_url",
            "consent_url",
            "privacy_policy_content",
            "consent_content",
        ],
    ),
    (
        "Rate Limits",
        [
            "rate_login_max",
            "rate_login_window_sec",
            "rate_register_max",
            "rate_register_window_sec",
            "rate_forgot_max",
            "rate_forgot_window_sec",
            "rate_2fa_max",
            "rate_2fa_window_sec",
            "sync_cooldown_sec",
        ],
    ),
]

# Keys rendered as <textarea> in admin UI (long text / HTML content)
TEXTAREA_KEYS: set[str] = {"privacy_policy_content", "consent_content"}

# Keys rendered as <input type="checkbox"> in admin UI (stored as "1"/"0")
CHECKBOX_KEYS: set[str] = {"yandex_metrika_enabled", "google_analytics_enabled"}

_cache: dict[str, str] = dict(DEFAULTS)


def get(key: str, default: str | None = None) -> str | None:
    return _cache.get(key, default if default is not None else DEFAULTS.get(key))


def get_bool(key: str) -> bool:
    return get(key) == "1"


def get_int(key: str) -> int:
    val = get(key)
    try:
        return int(val) if val is not None else 0
    except (ValueError, TypeError):
        return 0


def get_role_limit(role: str, resource: str) -> int | None:
    """Return limit for role+resource, or None if unlimited (value == 0).

    resource examples: 'device_limit', 'profile_limit', 'timecode_limit',
                       'import_daily', 'myshows_daily'
    """
    val = get_int(f"{role}_{resource}")
    return None if val == 0 else val


def all_settings() -> dict[str, str]:
    """Return a copy of the current in-memory settings."""
    # Fill missing keys with defaults
    result = dict(DEFAULTS)
    result.update(_cache)
    return result


async def load(db) -> None:
    """Load all settings from DB into memory. Call once on startup."""
    from app.db.models import AppSetting
    from sqlalchemy import select

    result = await db.execute(select(AppSetting))
    rows = result.scalars().all()
    for row in rows:
        _cache[row.key] = row.value
    logger.info(f"Settings loaded: {len(rows)} keys from DB")


async def set_setting(key: str, value: str, db) -> None:
    """Persist a setting to DB and update in-memory cache immediately."""
    from app.db.models import AppSetting
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
    await db.execute(stmt)
    await db.commit()
    _cache[key] = value
    logger.info(f"Setting updated: {key} = {value!r}")
