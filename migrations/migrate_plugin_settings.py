"""
Миграция: создаёт таблицу plugin_settings для синхронизации настроек плагинов
между устройствами одного пользователя.

Идемпотентна — безопасно запускать повторно на уже обновлённой БД.

Запуск:
    # Локально:
    poetry run python migrations/migrate_plugin_settings.py

    # Docker:
    docker compose exec app poetry run python migrations/migrate_plugin_settings.py
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
            CREATE TABLE IF NOT EXISTS plugin_settings (
                user_id    INTEGER      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                plugin     VARCHAR(100) NOT NULL,
                settings   TEXT         NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, plugin)
            );
        """))
        await session.commit()
    print("Done: plugin_settings table ready")


if __name__ == "__main__":
    asyncio.run(main())
