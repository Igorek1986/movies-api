"""
Очистка таймкодов с неправильными хэшами (артефакты старого Lampac).

Правила валидации:
  - Фильм:   item == lampa_hash(original_title)
  - Сериал:  item присутствует в episodes.hash для данного tmdb_show_id
             (проверяется только если episodes таблица имеет данные для этого шоу)

Запуск:
    # Локально (dry-run — только показывает что будет удалено):
    poetry run python migrations/cleanup_timecode_hashes.py

    # С удалением:
    poetry run python migrations/cleanup_timecode_hashes.py --delete

    # Docker:
    docker compose exec app poetry run python migrations/cleanup_timecode_hashes.py [--delete]
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import text
from app.db.database import async_session_maker, init_db
from app.utils import lampa_hash, build_episode_hash_string

DELETE = "--delete" in sys.argv


async def main():
    await init_db()
    async with async_session_maker() as session:
        bad_items: list[dict] = []  # {card_id, item, reason}

        # ── Фильмы ────────────────────────────────────────────────────────────
        print("Проверяем фильмы...")
        movie_rows = (await session.execute(text("""
            SELECT DISTINCT t.card_id, t.item, mc.original_title
            FROM timecodes t
            JOIN media_cards mc ON mc.card_id = t.card_id
            WHERE t.card_id LIKE '%_movie'
              AND mc.original_title IS NOT NULL
        """))).fetchall()

        for row in movie_rows:
            correct = lampa_hash(row.original_title)
            if row.item != correct:
                bad_items.append({
                    "card_id": row.card_id,
                    "item": row.item,
                    "reason": f"movie: expected {correct} (hash of {row.original_title!r}), got {row.item}",
                })

        # ── Сериалы ───────────────────────────────────────────────────────────
        print("Проверяем сериалы...")

        # Источник 1: episodes таблица (tmdb_show_id → set of valid hashes)
        ep_rows = (await session.execute(text("""
            SELECT tmdb_show_id, hash FROM episodes
            WHERE hash IS NOT NULL
        """))).fetchall()

        valid_hashes: dict[int, set[str]] = {}
        for r in ep_rows:
            valid_hashes.setdefault(r.tmdb_show_id, set()).add(r.hash)

        # Источник 2: вычисляем хэши из seasons_json для шоу без episodes
        mc_rows = (await session.execute(text("""
            SELECT tmdb_id, original_title, seasons_json
            FROM media_cards
            WHERE media_type = 'tv'
              AND original_title IS NOT NULL
              AND seasons_json IS NOT NULL
              AND tmdb_id NOT IN (SELECT DISTINCT tmdb_show_id FROM episodes)
        """))).fetchall()

        import json as _json
        for mc in mc_rows:
            try:
                seasons = _json.loads(mc.seasons_json)
            except Exception:
                continue
            hashes: set[str] = set()
            for season in seasons:
                s_num = season.get("season_number")
                ep_count = season.get("episode_count", 0)
                if s_num is None or ep_count == 0:
                    continue
                for ep_num in range(1, ep_count + 1):
                    h = lampa_hash(build_episode_hash_string(s_num, ep_num, mc.original_title))
                    hashes.add(h)
            if hashes:
                valid_hashes[mc.tmdb_id] = hashes

        tv_rows = (await session.execute(text("""
            SELECT DISTINCT t.card_id, t.item, mc.tmdb_id
            FROM timecodes t
            JOIN media_cards mc ON mc.card_id = t.card_id
            WHERE t.card_id LIKE '%_tv'
        """))).fetchall()

        for row in tv_rows:
            show_hashes = valid_hashes.get(row.tmdb_id)
            if show_hashes is None:
                continue  # нет ни episodes ни seasons_json — не можем валидировать
            if row.item not in show_hashes:
                bad_items.append({
                    "card_id": row.card_id,
                    "item": row.item,
                    "reason": f"tv: item not in valid hashes for tmdb_show_id={row.tmdb_id}",
                })

        # ── Итог ──────────────────────────────────────────────────────────────
        if not bad_items:
            print("Неправильных хэшей не найдено.")
            return

        # Считаем сколько строк в timecodes будет затронуто
        bad_by_card: dict[str, list] = {}
        for b in bad_items:
            bad_by_card.setdefault(b["card_id"], []).append(b)

        # Считаем строки для удаления
        placeholders = ", ".join(
            f"(:card_{i}, :item_{i})" for i in range(len(bad_items))
        )
        params = {}
        for i, b in enumerate(bad_items):
            params[f"card_{i}"] = b["card_id"]
            params[f"item_{i}"] = b["item"]

        total_rows = (await session.execute(text(f"""
            SELECT COUNT(*) FROM timecodes
            WHERE (card_id, item) IN ({placeholders})
        """), params)).scalar()

        print(f"\nНайдено неправильных хэшей: {len(bad_items)} уникальных item")
        print(f"Строк в timecodes для удаления: {total_rows}")
        print(f"Затронуто карточек: {len(bad_by_card)}\n")

        for card_id, items in sorted(bad_by_card.items()):
            print(f"  {card_id}: {len(items)} bad item(s)")
            for b in items:
                print(f"    item={b['item']}  [{b['reason']}]")

        if not DELETE:
            print("\nDRY RUN — для удаления запустите с флагом --delete")
            return

        # ── Удаление ──────────────────────────────────────────────────────────
        deleted = (await session.execute(text(f"""
            DELETE FROM timecodes
            WHERE (card_id, item) IN ({placeholders})
        """), params)).rowcount
        await session.commit()
        print(f"\nУдалено {deleted} строк.")


if __name__ == "__main__":
    asyncio.run(main())
