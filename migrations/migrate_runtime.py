"""
Миграция: добавляет колонки runtime и episode_run_time в таблицу media_cards.

    runtime          — фильм: продолжительность в минутах (TMDB runtime)
    episode_run_time — сериал: продолжительность серии в минутах (первый элемент episode_run_time[])

Запуск:
    # Локально:
    poetry run python migrations/migrate_runtime.py

    # Docker:
    docker compose exec app poetry run python migrations/migrate_runtime.py
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
        await session.execute(text("""
            ALTER TABLE media_cards
            ADD COLUMN IF NOT EXISTS runtime INTEGER,
            ADD COLUMN IF NOT EXISTS episode_run_time INTEGER;
        """))
        await session.commit()
    print("Done: media_cards.runtime and episode_run_time columns added")


if __name__ == "__main__":
    asyncio.run(main())
