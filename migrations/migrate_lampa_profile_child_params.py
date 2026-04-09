"""
Миграция: добавляет колонки child (BOOLEAN) и params (JSONB) в таблицу lampa_profiles.

Запуск:
    poetry run python migrations/migrate_lampa_profile_child_params.py

Docker:
    docker compose exec app poetry run python migrations/migrate_lampa_profile_child_params.py
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
            ADD COLUMN IF NOT EXISTS child BOOLEAN NOT NULL DEFAULT false;
        """))
        await session.execute(text("""
            ALTER TABLE lampa_profiles
            ADD COLUMN IF NOT EXISTS params JSONB NOT NULL DEFAULT '{}';
        """))
        await session.commit()
    print("Done: lampa_profiles.child and .params columns added")


if __name__ == "__main__":
    asyncio.run(main())
