"""Миграция: создание таблицы trusted_devices.

Запуск:
  # Локально (systemd/venv):
  poetry run python migrations/migrate_trusted_devices.py

  # Docker:
  docker compose exec app poetry run python migrations/migrate_trusted_devices.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db.database import engine


async def main():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trusted_devices (
                id           SERIAL PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token        VARCHAR(64) NOT NULL UNIQUE,
                created_at   TIMESTAMPTZ DEFAULT now(),
                last_used_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trusted_devices_user_id ON trusted_devices(user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trusted_devices_token ON trusted_devices(token)"))
    print("Migration trusted_devices: OK")


if __name__ == "__main__":
    asyncio.run(main())
