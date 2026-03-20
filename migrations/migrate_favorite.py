"""
Миграция: добавляет колонку favorite (TEXT) в таблицу lampa_profiles.

Запуск:
    poetry run python migrations/migrate_favorite.py
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
            ALTER TABLE lampa_profiles
            ADD COLUMN IF NOT EXISTS favorite TEXT;
        """))
        await session.commit()
    print("Done: lampa_profiles.favorite column added")


if __name__ == "__main__":
    asyncio.run(main())
