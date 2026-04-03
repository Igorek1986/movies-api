"""
Миграция: полная инициализация схемы MyShows (myshows_items + myshows_watching +
myshows_user_status без watching-записей).

Обрабатывает три сценария:
  1. Чистая установка — просто создаёт таблицы через init_db().
  2. Обновление с myshows_user_shows — переносит данные в myshows_items /
     myshows_watching и удаляет старую таблицу.
  3. Обновление с myshows_user_status (watching-записи) — переносит watching
     в myshows_watching, удаляет из user_status, убирает ненужные колонки.

Идемпотентна — безопасно запускать повторно.

Запуск:
    # Локально:
    poetry run python migrations/migrate_myshows_full.py

    # Docker:
    docker compose exec app poetry run python migrations/migrate_myshows_full.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db.database import engine, init_db


async def main():
    # Шаг 1: создаём все таблицы (безопасно пропускает существующие)
    await init_db()
    print("✅ Таблицы созданы (myshows_items, myshows_user_status, myshows_watching)")

    async with engine.begin() as conn:

        # ── Шаг 2: миграция из myshows_user_shows (старый формат) ──────────────
        result = await conn.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'myshows_user_shows'
            )
        """))
        if result.scalar():
            # Переносим уникальные записи в myshows_items
            await conn.execute(text("""
                INSERT INTO myshows_items (myshows_id, tmdb_id, media_type)
                SELECT DISTINCT myshows_id, tmdb_id, 'tv'
                FROM myshows_user_shows
                ON CONFLICT (myshows_id) DO NOTHING
            """))
            print("✅ Данные перенесены в myshows_items")

            # Переносим watching-данные прямо в myshows_watching
            result2 = await conn.execute(text("""
                INSERT INTO myshows_watching
                    (device_id, lampa_profile_id, item_id,
                     unwatched_count, next_episode, progress_marker, updated_at)
                SELECT
                    s.device_id,
                    '',
                    i.id,
                    s.unwatched_count,
                    s.next_episode,
                    s.progress_marker,
                    s.updated_at
                FROM myshows_user_shows s
                JOIN myshows_items i ON i.myshows_id = s.myshows_id
                ON CONFLICT (device_id, lampa_profile_id, item_id) DO NOTHING
            """))
            print(f"✅ Перенесено {result2.rowcount} записей в myshows_watching")

            await conn.execute(text("DROP TABLE myshows_user_shows"))
            print("✅ Таблица myshows_user_shows удалена")
        else:
            print("ℹ️  myshows_user_shows не найдена — пропускаем")

        # ── Шаг 3: перенос watching-записей из myshows_user_status ────────────
        # (актуально для установок, прошедших migrate_myshows_v2 но не watching_table)
        col_exists = await conn.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_name = 'myshows_user_status'
                  AND column_name = 'unwatched_count'
            )
        """))
        if col_exists.scalar():
            result3 = await conn.execute(text("""
                INSERT INTO myshows_watching
                    (device_id, lampa_profile_id, item_id,
                     unwatched_count, next_episode, progress_marker, updated_at)
                SELECT
                    device_id, lampa_profile_id, item_id,
                    unwatched_count, next_episode, progress_marker, updated_at
                FROM myshows_user_status
                WHERE cache_type = 'watching'
                ON CONFLICT (device_id, lampa_profile_id, item_id) DO NOTHING
            """))
            print(f"✅ Перенесено {result3.rowcount} watching-записей в myshows_watching")

            result4 = await conn.execute(text("""
                DELETE FROM myshows_user_status WHERE cache_type = 'watching'
            """))
            print(f"✅ Удалено {result4.rowcount} watching-записей из myshows_user_status")

            await conn.execute(text("""
                ALTER TABLE myshows_user_status
                    DROP COLUMN IF EXISTS unwatched_count,
                    DROP COLUMN IF EXISTS next_episode,
                    DROP COLUMN IF EXISTS progress_marker
            """))
            print("✅ Удалены колонки unwatched_count/next_episode/progress_marker из myshows_user_status")
        else:
            print("ℹ️  Колонка unwatched_count не найдена — шаг 3 не нужен")

    print("\n✅ Миграция завершена")


if __name__ == "__main__":
    asyncio.run(main())
