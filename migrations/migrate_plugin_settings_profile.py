"""
Миграция: добавляем lampa_profile_id в plugin_settings.

Очищает таблицу (конфликтующие данные без профиля) и пересоздаёт
с новым составным PK (user_id, lampa_profile_id, plugin).

Запуск:
    poetry run python migrations/migrate_plugin_settings_profile.py
    docker compose exec app poetry run python migrations/migrate_plugin_settings_profile.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db.database import engine, init_db


async def main():
    async with engine.begin() as conn:
        col_exists = await conn.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_name = 'plugin_settings'
                  AND column_name = 'lampa_profile_id'
            )
        """))
        if col_exists.scalar():
            print("ℹ️  lampa_profile_id уже существует — миграция не нужна")
            return

        print("🗑️  Очищаем plugin_settings (данные без профиля несовместимы)...")
        await conn.execute(text("DROP TABLE IF EXISTS plugin_settings"))
        print("✅ Таблица удалена")

    await init_db()
    print("✅ Таблица plugin_settings пересоздана с lampa_profile_id")
    print("\n✅ Миграция завершена")


if __name__ == "__main__":
    asyncio.run(main())
