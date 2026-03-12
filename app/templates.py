"""Shared Jinja2Templates instance with custom filters."""
from fastapi.templating import Jinja2Templates

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


def get_templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory="templates")
        _templates.env.filters["plural"] = _plural_ru
    return _templates
