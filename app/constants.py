# app/constants.py
# Все бизнес-константы приложения в одном месте.
# Редактируй здесь — изменения подхватываются везде.

# ── Лимиты устройств и профилей по ролям ─────────────────────────────────────
# super — без ограничений (None)
DEVICE_LIMITS: dict[str, int | None] = {"simple": 3, "premium": 8, "super": None}
PROFILE_LIMITS: dict[str, int | None] = {"simple": 3, "premium": 8, "super": None}

# ── Веб-сессии ────────────────────────────────────────────────────────────────
SESSION_TTL_DAYS = 30  # срок жизни сессии
SESSION_RENEW_DAYS = 15  # продлевать если осталось меньше N дней

# ── Таймкоды ─────────────────────────────────────────────────────────────────
WATCHED_THRESHOLD = 90  # % для пометки «просмотрено»

# ── TTL временных кодов (минуты) ──────────────────────────────────────────────
DEVICE_CODE_TTL_MINUTES = 10  # код активации устройства в Lampa
TELEGRAM_LINK_CODE_TTL_MINUTES = 10  # код привязки Telegram
RESET_CODE_TTL_MINUTES = 15  # код восстановления пароля
PENDING_2FA_TTL_SEC = 600  # ожидание подтверждения 2FA (10 мин)

# ── Rate limits ───────────────────────────────────────────────────────────────
# JSON-импорт: кол-во раз в сутки по ролям (super — без ограничений)
IMPORT_DAILY_LIMITS: dict[str, int | None] = {"simple": 1, "premium": 3, "super": None}

# Cooldown MyShows sync (секунды)
SYNC_COOLDOWN_SEC = 300  # 5 мин

# Прочие лимиты (попытки / окно в секундах)
RATE_LOGIN_MAX = 10
RATE_LOGIN_WINDOW_SEC = 900  # 10 попыток за 15 мин с IP
RATE_REGISTER_MAX = 5
RATE_REGISTER_WINDOW_SEC = 3600  # 5 за час с IP
RATE_FORGOT_MAX = 3
RATE_FORGOT_WINDOW_SEC = 3600  # 3 за час с IP
RATE_2FA_MAX = 5
RATE_2FA_WINDOW_SEC = 900  # 5 за 15 мин с IP
