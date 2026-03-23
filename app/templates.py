"""Shared Jinja2Templates instance with custom filters."""
from fastapi.templating import Jinja2Templates
from app import settings_cache

_templates: Jinja2Templates | None = None


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian pluralization.

    Usage in template: {{ 5 | plural('устройство', 'устройства', 'устройств') }}
    """
    n = abs(int(n))
    if 11 <= n % 100 <= 19:
        return many
    rem = n % 10
    if rem == 1:
        return one
    if 2 <= rem <= 4:
        return few
    return many


def _get_analytics_globals() -> dict:
    ym_enabled = settings_cache.get_bool("yandex_metrika_enabled")
    ga_enabled = settings_cache.get_bool("google_analytics_enabled")
    return {
        "yandex_metrika_id": settings_cache.get("yandex_metrika_id") or "" if ym_enabled else "",
        "google_analytics_id": settings_cache.get("google_analytics_id") or "" if ga_enabled else "",
    }


def get_templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory="templates")
        _templates.env.filters["plural"] = _plural_ru
        _templates.env.globals["analytics"] = _get_analytics_globals
    return _templates
