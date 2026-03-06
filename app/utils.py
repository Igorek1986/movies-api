import secrets
import hashlib
import string
import bcrypt
import re


def hash_password(password: str) -> str:
    """Хэширует пароль через bcrypt"""
    password_bytes = password.encode("utf-8")
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=12))
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет пароль против хэша"""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except Exception:
        return False


def validate_name(name: str) -> tuple[bool, str]:
    """Проверяет имя пользователя или профиля: мин. 3 символа, не начинается с цифры."""
    if len(name) < 3:
        return False, "Имя должно быть не менее 3 символов"
    if name[0].isdigit():
        return False, "Имя не должно начинаться с цифры"
    return True, ""


def validate_password(password: str) -> tuple[bool, str]:
    """Проверяет сложность пароля"""
    if len(password) < 8:
        return False, "Пароль должен быть не менее 8 символов"

    if not re.search(r"[A-Z]", password):
        return False, "Пароль должен содержать хотя бы одну заглавную букву"

    if not re.search(r"[a-z]", password):
        return False, "Пароль должен содержать хотя бы одну строчную букву"

    if not re.search(r"\d", password):
        return False, "Пароль должен содержать хотя бы одну цифру"

    return True, ""


def hash_api_key(api_key: str) -> str:
    """Хэширует API-ключ через SHA-256"""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Генерирует случайный session key для веб-авторизации (44 символа)"""
    key = secrets.token_urlsafe(32)
    return key.upper().replace("_", "")[:44]


def generate_profile_api_key() -> str:
    """Генерирует читаемый API-ключ профиля для Lampa.
    Формат: XXXX-XXXX-XXXX-XXXX (16 символов, группы по 4).
    Хранится в БД в открытом виде — нужен для device activation flow.
    """
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "-".join(parts)


def generate_device_code() -> str:
    """Генерирует короткий числовой код для привязки устройства.
    Формат: 6 цифр (например: 483921).
    """
    return "".join(secrets.choice(string.digits) for _ in range(6))


def lampa_hash(s: str) -> str:
    """Lampa.Utils.hash() — Java-style hashCode с множителем 31"""
    hash_val = 0
    for c in s:
        hash_val = (31 * hash_val + ord(c)) & 0xFFFFFFFF

    if hash_val >= 0x80000000:
        hash_val -= 0x100000000

    return str(abs(hash_val))


def build_episode_hash_string(season: int, episode: int, original_title: str) -> str:
    """Формирует строку для хэширования эпизода"""
    if season >= 10:
        return f"{season}:{episode}{original_title}"
    else:
        return f"{season}{episode}{original_title}"
