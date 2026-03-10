"""
In-memory rate limiter (per-process).
Stores timestamps in a plain dict — safe for single-process uvicorn workers.
"""
import time
from collections import defaultdict

_windows: dict[str, list[float]] = defaultdict(list)


def _allowed(key: str, max_calls: int, window_sec: int) -> bool:
    """Sliding-window check. Returns True if request is within limits."""
    now = time.monotonic()
    bucket = _windows[key]
    # Prune old entries
    _windows[key] = [t for t in bucket if now - t < window_sec]
    if len(_windows[key]) >= max_calls:
        return False
    _windows[key].append(now)
    return True


def _reset(key: str):
    _windows.pop(key, None)


# ── Public helpers ─────────────────────────────────────────────────────────────

def check_login(ip: str) -> bool:
    """10 failed-login attempts per 15 min per IP."""
    return _allowed(f"login:{ip}", max_calls=10, window_sec=900)


def clear_login(ip: str):
    """Call after a successful login so the counter doesn't punish the user."""
    _reset(f"login:{ip}")


def check_register(ip: str) -> bool:
    """5 registration attempts per hour per IP."""
    return _allowed(f"reg:{ip}", max_calls=5, window_sec=3600)


def check_forgot(ip: str) -> bool:
    """3 forgot-password requests per hour per IP."""
    return _allowed(f"forgot:{ip}", max_calls=3, window_sec=3600)


def check_2fa(ip: str) -> bool:
    """5 попыток TOTP-верификации за 15 минут с IP."""
    return _allowed(f"2fa:{ip}", max_calls=5, window_sec=900)


def clear_2fa(ip: str):
    _reset(f"2fa:{ip}")


def can_import(user_id: int) -> tuple[bool, int]:
    """
    Проверяет доступность импорта без записи попытки.
    Returns (allowed, seconds_until_allowed).
    """
    key = f"import:{user_id}"
    now = time.monotonic()
    cooldown = 86400
    entries = _windows.get(key, [])
    if entries:
        elapsed = now - entries[-1]
        if elapsed < cooldown:
            return False, int(cooldown - elapsed)
    return True, 0


def reset_import(user_id: int) -> None:
    """Сбросить лимит импорта для пользователя (вызывается из админки)."""
    _reset(f"import:{user_id}")


def check_import(user_id: int) -> tuple[bool, int]:
    """
    JSON-импорт для simple-пользователей: 1 раз в 24 часа на аккаунт.
    Returns (allowed, seconds_until_allowed).
    """
    key = f"import:{user_id}"
    now = time.monotonic()
    cooldown = 86400  # 24h
    entries = _windows.get(key, [])
    if entries:
        elapsed = now - entries[-1]
        if elapsed < cooldown:
            return False, int(cooldown - elapsed)
    _windows[key] = [now]
    return True, 0


def check_sync(user_id: int) -> tuple[bool, int]:
    """
    MyShows sync cooldown: 1 sync per 5 minutes per user.
    Returns (allowed, seconds_until_allowed).
    """
    key = f"sync:{user_id}"
    now = time.monotonic()
    cooldown = 300  # 5 min
    entries = _windows.get(key, [])
    if entries:
        elapsed = now - entries[-1]
        if elapsed < cooldown:
            return False, int(cooldown - elapsed)
    _windows[key] = [now]
    return True, 0
