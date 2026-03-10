"""
In-memory rate limiter (per-process).
Stores timestamps in a plain dict — safe for single-process uvicorn workers.
"""
import time
from collections import defaultdict
from app.constants import (
    RATE_LOGIN_MAX, RATE_LOGIN_WINDOW_SEC,
    RATE_REGISTER_MAX, RATE_REGISTER_WINDOW_SEC,
    RATE_FORGOT_MAX, RATE_FORGOT_WINDOW_SEC,
    RATE_2FA_MAX, RATE_2FA_WINDOW_SEC,
    SYNC_COOLDOWN_SEC,
)

_windows: dict[str, list[float]] = defaultdict(list)

_IMPORT_WINDOW_SEC = 86400  # 24 ч — окно для подсчёта импортов


def _allowed(key: str, max_calls: int, window_sec: int) -> bool:
    """Sliding-window check. Returns True if request is within limits."""
    now = time.monotonic()
    bucket = _windows[key]
    _windows[key] = [t for t in bucket if now - t < window_sec]
    if len(_windows[key]) >= max_calls:
        return False
    _windows[key].append(now)
    return True


def _reset(key: str):
    _windows.pop(key, None)


def _import_peek(key: str, max_calls: int) -> tuple[bool, int, int]:
    """Проверить лимит импорта без записи попытки. Returns (allowed, wait_sec, remaining)."""
    now = time.monotonic()
    bucket = [t for t in _windows.get(key, []) if now - t < _IMPORT_WINDOW_SEC]
    used = len(bucket)
    if used >= max_calls:
        return False, max(1, int(_IMPORT_WINDOW_SEC - (now - bucket[0]))), 0
    return True, 0, max_calls - used


def _import_check(key: str, max_calls: int) -> tuple[bool, int]:
    """Записать попытку импорта и вернуть (allowed, wait_sec)."""
    now = time.monotonic()
    bucket = [t for t in _windows.get(key, []) if now - t < _IMPORT_WINDOW_SEC]
    if len(bucket) >= max_calls:
        return False, max(1, int(_IMPORT_WINDOW_SEC - (now - bucket[0])))
    bucket.append(now)
    _windows[key] = bucket
    return True, 0


# ── Public helpers ──────────────────────────────────────────────────────────────

def check_login(ip: str) -> bool:
    return _allowed(f"login:{ip}", max_calls=RATE_LOGIN_MAX, window_sec=RATE_LOGIN_WINDOW_SEC)


def clear_login(ip: str):
    _reset(f"login:{ip}")


def check_register(ip: str) -> bool:
    return _allowed(f"reg:{ip}", max_calls=RATE_REGISTER_MAX, window_sec=RATE_REGISTER_WINDOW_SEC)


def check_forgot(ip: str) -> bool:
    return _allowed(f"forgot:{ip}", max_calls=RATE_FORGOT_MAX, window_sec=RATE_FORGOT_WINDOW_SEC)


def check_2fa(ip: str) -> bool:
    return _allowed(f"2fa:{ip}", max_calls=RATE_2FA_MAX, window_sec=RATE_2FA_WINDOW_SEC)


def clear_2fa(ip: str):
    _reset(f"2fa:{ip}")


def can_import(user_id: int, max_daily: int) -> tuple[bool, int, int]:
    """Проверить доступность импорта без записи. Returns (allowed, wait_sec, remaining)."""
    return _import_peek(f"import:{user_id}", max_daily)


def check_import(user_id: int, max_daily: int) -> tuple[bool, int]:
    """Записать попытку импорта. Returns (allowed, wait_sec)."""
    return _import_check(f"import:{user_id}", max_daily)


def reset_import(user_id: int) -> None:
    """Сбросить лимит импорта (вызывается из админки)."""
    _reset(f"import:{user_id}")


def check_sync(user_id: int) -> tuple[bool, int]:
    """MyShows sync cooldown. Returns (allowed, seconds_until_allowed)."""
    key = f"sync:{user_id}"
    now = time.monotonic()
    entries = _windows.get(key, [])
    if entries:
        elapsed = now - entries[-1]
        if elapsed < SYNC_COOLDOWN_SEC:
            return False, int(SYNC_COOLDOWN_SEC - elapsed)
    _windows[key] = [now]
    return True, 0
