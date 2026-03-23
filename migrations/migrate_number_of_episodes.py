"""
Миграция: добавляет колонку number_of_episodes (INTEGER) в таблицу media_cards.

Запуск:
    # Локально:
    poetry run python migrations/migrate_number_of_episodes.py

    # Docker:
    docker compose exec app poetry run python migrations/migrate_number_of_episodes.py
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
            ADD COLUMN IF NOT EXISTS number_of_episodes INTEGER;
        """))
        await session.commit()
    print("Done: media_cards.number_of_episodes column added")


if __name__ == "__main__":
    asyncio.run(main())
