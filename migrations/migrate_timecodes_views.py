"""
Миграция: добавляет counted_at и view_count в таблицу timecodes.

  counted_at  — дата последнего засчитанного просмотра (дедупликация: 1 раз в день на item).
  view_count  — сколько раз этот item был засчитан (серия или фильм).

Популярность ("Популярно в NP") считается напрямую из timecodes:
  - Фильм:  SUM(view_count) по всем профилям
  - Сериал: SUM(view_count) / number_of_episodes

При миграции:
  - counted_at = CURRENT_DATE для таймкодов с percent >= 90
  - view_count = 1 для таймкодов с counted_at IS NOT NULL

Идемпотентна — безопасно запускать повторно.

Запуск:
    # Локально:
    poetry run python migrations/migrate_timecodes_views.py

    # Docker:
    docker compose exec app poetry run python migrations/migrate_timecodes_views.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import text
from app.db.database import async_session_maker, init_db


async def main():
    await init_db()
    async with async_session_maker() as session:
        # 1. Добавляем новые колонки в timecodes
        await session.execute(text("""
            ALTER TABLE timecodes ADD COLUMN IF NOT EXISTS counted_at DATE;
        """))
        await session.execute(text("""
            ALTER TABLE timecodes ADD COLUMN IF NOT EXISTS view_count INTEGER NOT NULL DEFAULT 0;
        """))

        await session.commit()
        print("Schema updated.")

        # 2. counted_at = CURRENT_DATE для таймкодов с percent >= 90
        result = await session.execute(text("""
            UPDATE timecodes SET counted_at = CURRENT_DATE
            WHERE counted_at IS NULL
              AND (data::json->>'percent')::float >= 90;
        """))
        print(f"counted_at set for {result.rowcount} timecodes.")

        # 3. view_count = 1 для уже засчитанных таймкодов
        result = await session.execute(text("""
            UPDATE timecodes SET view_count = 1
            WHERE counted_at IS NOT NULL AND view_count = 0;
        """))
        print(f"view_count = 1 set for {result.rowcount} timecodes.")

        await session.commit()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
