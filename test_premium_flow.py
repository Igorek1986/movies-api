"""
test_premium_flow.py — интерактивная эмуляция цикла истечения Premium.

Шаги:
  1. Сбрасывает состояние: premium_until = сейчас + 2 дня
  2. [Enter] Эмулирует 05:00 дня предупреждения → должно прийти уведомление
  3. [Enter] Эмулирует 05:00 после истечения    → роль → simple, grace установлен
  4. [Enter] Эмулирует 05:00 после grace         → данные удалены

Запуск:
    poetry run python test_premium_flow.py --username igorek1986
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

FMT = "%d.%m.%Y %H:%M UTC"


def ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def sep(title: str = ""):
    line = "─" * 60
    print(f"\n{line}")
    if title:
        print(f"  {title}")
        print(line)


async def get_state(db, user_id: int) -> dict:
    from app.db.models import Device, Timecode, LampaProfile
    from sqlalchemy import select, func

    dev_rows = (await db.execute(
        select(Device.id, Device.name).where(Device.user_id == user_id).order_by(Device.created_at)
    )).all()

    tc_total = (await db.execute(
        select(func.count()).select_from(Timecode)
        .where(Timecode.device_id.in_([r.id for r in dev_rows]))
    )).scalar() or 0

    prof_total = (await db.execute(
        select(func.count()).select_from(LampaProfile)
        .where(LampaProfile.device_id.in_([r.id for r in dev_rows]))
    )).scalar() or 0

    return {
        "devices": [(r.id, r.name) for r in dev_rows],
        "dev_count": len(dev_rows),
        "prof_total": prof_total,
        "tc_total": tc_total,
    }


def print_state(label: str, user, state: dict):
    grace = user.timecode_grace_until.strftime("%d.%m.%Y") if user.timecode_grace_until else "—"
    puntil = user.premium_until.strftime("%d.%m.%Y") if user.premium_until else "—"
    print(f"\n  {'📋 ' + label}")
    print(f"    Роль:          {user.role}")
    print(f"    Premium до:    {puntil}")
    print(f"    Grace до:      {grace}")
    print(f"    warned:        {user.premium_warned}")
    print(f"    Устройств:     {state['dev_count']}  {[n for _, n in state['devices']]}")
    print(f"    Профилей:      {state['prof_total']}")
    print(f"    Таймкодов:     {state['tc_total']:,}")


async def run(username: str):
    import app.db.models  # noqa: F401
    from app.db.database import async_session_maker
    from app.db.models import User
    from app import settings_cache
    from app.tasks import run_premium_expiry_check
    from sqlalchemy import select

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден.")
            sys.exit(1)

    grace_days = settings_cache.get_int("timecode_grace_days")

    # ── Временная шкала ───────────────────────────────────────────────────────
    now_real       = datetime.now(timezone.utc)
    premium_until  = (now_real + timedelta(days=2)).replace(hour=23, minute=59, second=59, microsecond=0)
    t_warn         = premium_until - timedelta(days=2)          # сейчас — в окне предупреждения
    t_expired      = premium_until + timedelta(seconds=1)       # секунда после истечения
    t_grace_end    = premium_until + timedelta(days=grace_days, seconds=1)  # после grace

    sep("ЭМУЛЯЦИЯ ЦИКЛА PREMIUM EXPIRY")
    print(f"  Пользователь:    {username}")
    print(f"  Grace days:      {grace_days}")
    print(f"  Временная шкала:")
    print(f"    premium_until  = {ts(premium_until)}")
    print(f"    t_warn         = {ts(t_warn)}  ← эмулируем первой")
    print(f"    t_expired      = {ts(t_expired)}")
    print(f"    t_grace_end    = {ts(t_grace_end)}")

    # ── ШАГ 0: сброс состояния ────────────────────────────────────────────────
    sep("ШАГ 0 / Сброс состояния пользователя")
    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        user.role = "premium"
        user.premium_until = premium_until
        user.premium_warned = False
        user.timecode_grace_until = None
        user.notify_premium_after = None
        await db.commit()
        await db.refresh(user)
        state = await get_state(db, user.id)

    print_state("После сброса", user, state)
    print(f"\n  ⚠️  Внимание: устройств {state['dev_count']}, таймкодов {state['tc_total']:,}")

    # ── ШАГ 1: проверка в день предупреждения ────────────────────────────────
    sep(f"ШАГ 1 / Эмуляция 05:00 — день предупреждения  [{ts(t_warn)}]")
    input("  Нажмите Enter для запуска проверки...")
    print(f"\n  🕔 now = {ts(t_warn)}")
    await run_premium_expiry_check(_now=t_warn)

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        state = await get_state(db, user.id)

    print_state("Результат", user, state)
    expected = "✅ Уведомление отправлено (premium_warned=True)" if user.premium_warned else "❌ Уведомление НЕ отправлено"
    print(f"\n  {expected}")

    # ── ВЕТКА А: продление до истечения ──────────────────────────────────────
    sep("ВЕТКА А / Продление до истечения premium_until?")
    ans = input("  Тест «продление до истечения»? [y/N] ").strip().lower()
    if ans == "y":
        sep("ВЕТКА А / Продление Premium пока подписка активна")
        from app.admin import extend_premium as _extend  # noqa — прямой вызов логики
        # Вызываем через ORM напрямую, не через HTTP
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            from datetime import timedelta as _td
            now_utc = datetime.now(timezone.utc)
            base = user.premium_until if (user.premium_until and user.premium_until > now_utc) else now_utc
            user.premium_until = (base + _td(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)
            user.premium_warned = False
            await db.commit()
            await db.refresh(user)
            state = await get_state(db, user.id)

        print_state("После продления", user, state)
        puntil = user.premium_until.strftime("%d.%m.%Y") if user.premium_until else "—"
        print(f"\n  ✅ Premium продлён до {puntil} (уведомление отправит бот)")

        # Теперь снова переводим в «истёк» для продолжения теста
        sep("  Сброс обратно в истёкшее состояние для продолжения теста")
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            user.premium_until = premium_until  # исходная дата
            user.premium_warned = True  # предупреждение уже было
            await db.commit()

    # ── ШАГ 2: проверка после истечения ──────────────────────────────────────
    sep(f"ШАГ 2 / Эмуляция 05:00 — после истечения  [{ts(t_expired)}]")
    input("  Нажмите Enter для запуска проверки...")
    print(f"\n  🕔 now = {ts(t_expired)}")
    await run_premium_expiry_check(_now=t_expired)

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        state = await get_state(db, user.id)

    print_state("Результат", user, state)
    if user.role == "simple":
        print(f"  ✅ Роль понижена до simple")
    if user.timecode_grace_until:
        print(f"  ✅ Grace установлен: {ts(user.timecode_grace_until)}")
        print(f"  📊 Данные пока НЕ удалены: {state['dev_count']} устр., {state['tc_total']:,} тк")

    # ── ВЕТКА Б: продление во время grace ────────────────────────────────────
    sep("ВЕТКА Б / Восстановление во время grace-периода?")
    ans = input("  Тест «восстановление во время grace»? [y/N] ").strip().lower()
    if ans == "y":
        sep("ВЕТКА Б / Восстанавливаем Premium из grace-периода")
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            from datetime import timedelta as _td
            now_utc = datetime.now(timezone.utc)
            user.role = "premium"
            user.premium_until = (now_utc + _td(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)
            user.premium_warned = False
            user.timecode_grace_until = None
            await db.commit()
            await db.refresh(user)
            state = await get_state(db, user.id)

        print_state("После восстановления", user, state)
        print(f"\n  ✅ Premium восстановлен, grace сброшен (уведомление отправит бот)")
        print(f"  📊 Данные НЕ удалены: {state['dev_count']} устр., {state['prof_total']} проф., {state['tc_total']:,} тк")
        sep("ВЕТКА Б / ТЕСТ ВОССТАНОВЛЕНИЯ ЗАВЕРШЁН")
        return

    # ── ШАГ 3: проверка после grace ───────────────────────────────────────────
    sep(f"ШАГ 3 / Эмуляция 05:00 — после grace  [{ts(t_grace_end)}]")
    input("  Нажмите Enter для запуска очистки...")
    print(f"\n  🕔 now = {ts(t_grace_end)}")
    await run_premium_expiry_check(_now=t_grace_end)

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        state_after = await get_state(db, user.id)

    print_state("Результат", user, state_after)

    d_dev  = state["dev_count"]  - state_after["dev_count"]
    d_prof = state["prof_total"] - state_after["prof_total"]
    d_tc   = state["tc_total"]   - state_after["tc_total"]
    print(f"\n  🗑  Удалено устройств:  {d_dev}")
    print(f"  🗑  Удалено профилей:   {d_prof}")
    print(f"  🗑  Удалено таймкодов:  {d_tc:,}")
    if user.timecode_grace_until is None:
        print(f"  ✅ grace_until сброшен")

    sep("ТЕСТ ЗАВЕРШЁН")


def main():
    parser = argparse.ArgumentParser(description="Тест flow истечения Premium")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.username))


if __name__ == "__main__":
    main()
