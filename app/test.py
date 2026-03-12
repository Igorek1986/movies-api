def build_episode_hash_string(season: int, episode: int, original_title: str) -> str:
    """
    Формирует строку для хэширования эпизода (как в плагине).

    Правило: если сезон >= 10, добавляем ':' между сезоном и эпизодом,
    чтобы избежать коллизий: S11E2 ≠ S1E12
    """
    if season >= 10:
        return f"{season}:{episode}{original_title}"
    else:
        return f"{season}{episode}{original_title}"


example1 = build_episode_hash_string(11, 2, "The Simpsons")
example2 = build_episode_hash_string(1, 12, "The Simpsons")

print(example1)
print(example2)


def lampa_hash(s: str) -> str:
    """
    Точная реплика Lampa.Utils.hash()
    Возвращает строку с числом, как в оригинале.
    """
    hash_val = 0
    for c in s:
        # Java-style: hash = 31 * hash + char
        hash_val = (31 * hash_val + ord(c)) & 0xFFFFFFFF

    # Конвертация в signed 32-bit (как в Java)
    if hash_val >= 0x80000000:
        hash_val -= 0x100000000

    # Возвращаем как строку (abs, потому что в JS нет отрицательных индексов)
    return str(abs(hash_val))


# print(lampa_hash(example1))
# print(lampa_hash(example2))
# print(lampa_hash("Wicked: For Good"))
# print(lampa_hash("Shelter"))
# print(lampa_hash("SomeOtherString"))
с

# 11 сезон 2 серия
# Lampa.Utils.hash('11:2The Simpsons')
# '793540481'
# первый сезон 12 серия
# Lampa.Utils.hash('112The Simpsons')
# '294971707'


# Lampa.Utils.hash('Wicked: For Good')
# '1131769645'
# lampa_hash("Shelter")           → 572566331
# lampa_hash("SomeOtherString")   → теоретически тоже может дать 572566331
