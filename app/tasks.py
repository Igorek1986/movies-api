"""
Background tasks.

run_premium_expiry_check — runs daily at configured hour (settings: daily_task_hour, default 2):
  1. Finds users with expired premium_until → demotes to simple
  2. Finds users whose premium expires within 3 days → schedules warning
  3. Sets notify_premium_after to next allowed delivery time in user's timezone
  4. Cleans up devices / profiles / timecodes after grace_period expires

run_notification_delivery — runs every 10 minutes:
  Sends pending Telegram notifications where notify_premium_after <= now.
  Respects notify_type ("warning" / "expired").
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_check_task: asyncio.Task | None = None
_delivery_task: asyncio.Task | None = None


# ─── Timezone helpers ──────────────────────────────────────────────────────────


def _get_tz(tz_str: str | None) -> ZoneInfo:
    from app import settings_cache

    name = tz_str or settings_cache.get("default_timezone") or "Europe/Moscow"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, Exception):
        return ZoneInfo("Europe/Moscow")


def _next_notify_time(user, now: datetime) -> datetime:
    """Return UTC datetime when to deliver notification to this user.

    Rules (applied in user's local timezone):
    - Current hour in [notify_start, notify_end) → send now
    - Current hour < notify_start              → today at notify_start
    - Current hour >= notify_end               → tomorrow at notify_start
    """
    tz = _get_tz(user.timezone)
    now_local = now.astimezone(tz)
    start = user.notify_start if user.notify_start is not None else 9
    end = user.notify_end if user.notify_end is not None else 22

    hour = now_local.hour
    if start <= hour < end:
        return now  # within window — deliver immediately

    if hour < start:
        candidate = now_local.replace(hour=start, minute=0, second=0, microsecond=0)
    else:
        candidate = (now_local + timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
    return candidate.astimezone(timezone.utc)


def _fmt_date(dt, user) -> str:
    """Format datetime in user's local timezone as dd.mm.YYYY."""
    if dt is None:
        return "—"
    if not hasattr(dt, "hour"):
        # date object — just format as-is
        return dt.strftime("%d.%m.%Y")
    tz = _get_tz(getattr(user, "timezone", None))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d.%m.%Y")


def _seconds_until_next_run() -> float:
    """Seconds until next daily task run (hour from settings, default 2)."""
    from app import settings_cache

    hour = max(0, min(23, settings_cache.get_int("daily_task_hour") or 2))
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ─── Business logic (daily at 05:00) ─────────────────────────────────────────


async def run_premium_expiry_check(_now: datetime | None = None) -> None:
    from app.db.database import async_session_maker
    from app.db.models import User, TelegramUser
    from app import settings_cache
    from sqlalchemy import select, and_

    logger.info("Running premium expiry check...")

    async with async_session_maker() as db:
        now = _now or datetime.now(timezone.utc)
        grace_days = settings_cache.get_int("timecode_grace_days")

        # ── 1. Demote expired premium users ───────────────────────────────────
        result = await db.execute(
            select(User).where(
                and_(
                    User.role == "premium",
                    User.premium_until.isnot(None),
                    User.premium_until <= now,
                )
            )
        )
        for user in result.scalars().all():
            user.role = "simple"
            # premium_until сохраняем — нужен как база при продлении в grace-периоде

            if grace_days == 0:
                user.timecode_grace_until = now
            else:
                user.timecode_grace_until = now + timedelta(days=grace_days)

            if user.notifications_enabled:
                user.notify_premium_after = _next_notify_time(user, now)
                user.notify_type = "expired"
                logger.info(
                    f"User {user.username}: premium expired → simple, notify at {user.notify_premium_after}"
                )
            else:
                logger.info(
                    f"User {user.username}: premium expired → simple (notifications disabled)"
                )

        await db.commit()

        # ── 2. Advance warning N days before expiry ───────────────────────────
        warn_days = settings_cache.get_int("premium_warn_days") or 3
        warn_horizon = now + timedelta(days=warn_days)
        result = await db.execute(
            select(User).where(
                and_(
                    User.role == "premium",
                    User.premium_until.isnot(None),
                    User.premium_until > now,
                    User.premium_until <= warn_horizon,
                    User.premium_warned == False,  # noqa: E712
                )
            )
        )
        for user in result.scalars().all():
            user.premium_warned = True
            if user.notifications_enabled:
                user.notify_premium_after = _next_notify_time(user, now)
                user.notify_type = "warning"
                logger.info(
                    f"User {user.username}: premium warning scheduled at {user.notify_premium_after}"
                )

        await db.commit()

        # ── 3. Inactive simple users: warn and delete ─────────────────────────
        inactive_delete_days = settings_cache.get_int("inactive_delete_days")
        inactive_warn_days = settings_cache.get_int("inactive_warn_days")

        if inactive_delete_days > 0:
            from sqlalchemy import or_

            today = now.date()
            warn_threshold = today - timedelta(
                days=inactive_delete_days - inactive_warn_days
            )
            delete_threshold = today - timedelta(days=inactive_delete_days)

            # Предупреждение
            result = await db.execute(
                select(User).where(
                    User.role == "simple",
                    User.timecode_grace_until.is_(None),
                    User.inactive_warned == False,  # noqa: E712
                    User.last_active_at.isnot(None),
                    User.last_active_at <= warn_threshold,
                    User.last_active_at > delete_threshold,
                )
            )
            for user in result.scalars().all():
                user.inactive_warned = True
                if user.notifications_enabled:
                    user.notify_inactive_after = _next_notify_time(user, now)
                    logger.info(f"User {user.username}: inactive warning scheduled")

            await db.commit()

            # Удаление
            result = await db.execute(
                select(User).where(
                    User.role == "simple",
                    User.timecode_grace_until.is_(None),
                    User.last_active_at.isnot(None),
                    User.last_active_at <= delete_threshold,
                )
            )
            for user in result.scalars().all():
                logger.info(
                    f"Auto-deleting inactive user: {user.username} "
                    f"(last_active: {user.last_active_at})"
                )
                await db.delete(user)

            await db.commit()

        # ── 4. Clean up devices / profiles / timecodes after grace period ─────
        result = await db.execute(
            select(User).where(
                and_(
                    User.timecode_grace_until.isnot(None),
                    User.timecode_grace_until <= now,
                )
            )
        )
        for user in result.scalars().all():
            dev_limit = settings_cache.get_role_limit(user.role, "device_limit")
            prof_limit = settings_cache.get_role_limit(user.role, "profile_limit")
            tc_limit = settings_cache.get_role_limit(user.role, "timecode_limit")
            if dev_limit is not None:
                await _cleanup_devices(db, user.id, dev_limit, user.username)
            if prof_limit is not None:
                await _cleanup_profiles(db, user.id, prof_limit, user.username)
            if tc_limit is not None:
                await _cleanup_timecodes(db, user.id, tc_limit, user.username)
            user.timecode_grace_until = None
            user.premium_until = None  # грейс истёк, база для продления больше не нужна

        await db.commit()

    logger.info("Premium expiry check complete.")


# ─── Notification delivery (every 10 minutes) ─────────────────────────────────


async def run_notification_delivery() -> None:
    from app.db.database import async_session_maker
    from app.db.models import User, TelegramUser
    from sqlalchemy import select, and_, or_

    async with async_session_maker() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(User).where(
                or_(
                    and_(
                        User.notify_premium_after.isnot(None),
                        User.notify_premium_after <= now,
                    ),
                    and_(
                        User.notify_inactive_after.isnot(None),
                        User.notify_inactive_after <= now,
                    ),
                )
            )
        )
        users = result.scalars().all()
        if not users:
            return

        for user in users:
            tg = (
                await db.execute(
                    select(TelegramUser).where(TelegramUser.user_id == user.id)
                )
            ).scalar_one_or_none()

            if tg:
                try:
                    if user.notify_premium_after and user.notify_premium_after <= now:
                        if user.notify_type == "warning":
                            await _send_premium_warning(tg.telegram_id, user)
                        else:
                            await _send_premium_expired(tg.telegram_id, user)
                    if user.notify_inactive_after and user.notify_inactive_after <= now:
                        await _send_inactive_warning(tg.telegram_id, user)
                except Exception as e:
                    logger.warning(
                        f"Failed to deliver notification to {user.username}: {e}"
                    )

            if user.notify_premium_after and user.notify_premium_after <= now:
                user.notify_premium_after = None
                user.notify_type = None
            if user.notify_inactive_after and user.notify_inactive_after <= now:
                user.notify_inactive_after = None

        await db.commit()
        logger.info(f"Delivered {len(users)} deferred notification(s)")


# ─── Telegram message senders ─────────────────────────────────────────────────


async def _send_premium_expired(telegram_id: int, user) -> None:
    from app.bot import get_bot
    from app import settings_cache

    bot = get_bot()
    if not bot:
        return

    s_dev = settings_cache.get_int("simple_device_limit")
    s_prof = settings_cache.get_int("simple_profile_limit")
    s_tc = settings_cache.get_int("simple_timecode_limit")

    grace_note = ""
    if user.timecode_grace_until:
        deadline = _fmt_date(user.timecode_grace_until, user)
        grace_note = (
            f"\n\n⏳ Лишние данные будут удалены <b>{deadline}</b>.\n"
            f"Продлите подписку до этой даты, чтобы сохранить историю."
        )

    await bot.send_message(
        telegram_id,
        f"❌ <b>Подписка Premium истекла.</b>\n\n"
        f"Аккаунт: <b>{user.username}</b>\n"
        f"Переведён на <b>Simple</b>:\n"
        f"• Синхронизация с MyShows — недоступна\n"
        f"• Лимит устройств: <b>{s_dev}</b>\n"
        f"• Лимит профилей на устройство: <b>{s_prof}</b>\n"
        f"• Лимит таймкодов на профиль: <b>{s_tc}</b>"
        f"{grace_note}",
        parse_mode="HTML",
    )


async def _send_inactive_warning(telegram_id: int, user) -> None:
    from app.bot import get_bot
    from app import settings_cache

    bot = get_bot()
    if not bot:
        return

    delete_days = settings_cache.get_int("inactive_delete_days")
    warn_days = settings_cache.get_int("inactive_warn_days")
    delete_date = _fmt_date(
        user.last_active_at + timedelta(days=delete_days) if user.last_active_at else None,
        user,
    )

    await bot.send_message(
        telegram_id,
        f"⚠️ <b>Ваш аккаунт будет удалён {delete_date}.</b>\n\n"
        f"Аккаунт: <b>{user.username}</b>\n"
        f"Причина: нет активности более {delete_days - warn_days} дней.\n\n"
        f"Чтобы сохранить аккаунт — войдите на сайт или откройте подборку NUMParser в Lampa с привязанного устройства.",
        parse_mode="HTML",
    )


async def _send_premium_activated(telegram_id: int, user) -> None:
    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        return

    expires_str = user.premium_until.strftime("%d.%m.%Y")
    await bot.send_message(
        telegram_id,
        f"🎉 <b>Подписка Premium активирована!</b>\n\n"
        f"Аккаунт: <b>{user.username}</b>\n"
        f"<b>Premium</b> до {expires_str}.",
        parse_mode="HTML",
    )


async def _send_premium_renewed(telegram_id: int, user, was_grace: bool) -> None:
    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        return

    expires_str = user.premium_until.strftime("%d.%m.%Y")

    if was_grace:
        text = (
            f"✅ <b>Подписка Premium восстановлена!</b>\n\n"
            f"Аккаунт: <b>{user.username}</b>\n"
            f"Снова <b>Premium</b> до {expires_str}.\n"
            f"Все данные сохранены — история, устройства, профили."
        )
    else:
        text = (
            f"✅ <b>Подписка Premium продлена.</b>\n\n"
            f"Аккаунт: <b>{user.username}</b>\n"
            f"<b>Premium</b> до {expires_str}."
        )

    await bot.send_message(telegram_id, text, parse_mode="HTML")


async def _send_premium_warning(telegram_id: int, user) -> None:
    from app.bot import get_bot
    from app import settings_cache

    bot = get_bot()
    if not bot:
        return

    expires_str = user.premium_until.strftime("%d.%m.%Y")
    s_dev = settings_cache.get_int("simple_device_limit")
    s_prof = settings_cache.get_int("simple_profile_limit")
    s_tc = settings_cache.get_int("simple_timecode_limit")

    await bot.send_message(
        telegram_id,
        f"⏰ <b>Подписка Premium истекает {expires_str}.</b>\n\n"
        f"Аккаунт: <b>{user.username}</b>\n"
        f"После истечения будет переведён на <b>Simple</b>:\n"
        f"• Синхронизация с MyShows — недоступна\n"
        f"• Лишние устройства будут удалены (оставлено {s_dev} старейших)\n"
        f"• Лишние профили на устройство будут удалены (оставлено {s_prof})\n"
        f"• Лишние таймкоды на профиль будут удалены (лимит {s_tc})\n\n"
        f"Обратитесь к администратору для продления.",
        parse_mode="HTML",
    )


# ─── Cleanup helpers ──────────────────────────────────────────────────────────


async def _cleanup_devices(db, user_id: int, device_limit: int, username: str) -> int:
    from app.db.models import Device
    from sqlalchemy import select, delete

    all_ids = (
        (
            await db.execute(
                select(Device.id)
                .where(Device.user_id == user_id)
                .order_by(Device.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    if len(all_ids) <= device_limit:
        return 0

    delete_ids = all_ids[device_limit:]
    await db.execute(delete(Device).where(Device.id.in_(delete_ids)))
    logger.info(
        f"User {username}: deleted {len(delete_ids)} devices (limit={device_limit})"
    )
    return len(delete_ids)


async def _cleanup_profiles(db, user_id: int, profile_limit: int, username: str) -> int:
    from app.db.models import Device, LampaProfile, Timecode
    from sqlalchemy import select, delete

    dev_ids = (
        (await db.execute(select(Device.id).where(Device.user_id == user_id)))
        .scalars()
        .all()
    )

    total_deleted = 0
    for device_id in dev_ids:
        profiles = (
            (
                await db.execute(
                    select(LampaProfile)
                    .where(LampaProfile.device_id == device_id)
                    .order_by(LampaProfile.id.asc())
                )
            )
            .scalars()
            .all()
        )

        if len(profiles) <= profile_limit:
            continue

        to_delete = profiles[profile_limit:]
        del_profile_ids = [lp.lampa_profile_id for lp in to_delete]
        del_lp_ids = [lp.id for lp in to_delete]

        await db.execute(
            delete(Timecode).where(
                Timecode.device_id == device_id,
                Timecode.lampa_profile_id.in_(del_profile_ids),
            )
        )
        await db.execute(delete(LampaProfile).where(LampaProfile.id.in_(del_lp_ids)))
        total_deleted += len(to_delete)

    if total_deleted:
        logger.info(
            f"User {username}: deleted {total_deleted} profiles (limit={profile_limit}/device)"
        )
    return total_deleted


async def _cleanup_timecodes(db, user_id: int, limit: int, username: str) -> None:
    from app.db.models import Device, Timecode
    from sqlalchemy import select, func, delete

    dev_ids = (
        (await db.execute(select(Device.id).where(Device.user_id == user_id)))
        .scalars()
        .all()
    )

    if not dev_ids:
        return

    total_deleted = 0
    for device_id in dev_ids:
        profiles = (
            (
                await db.execute(
                    select(Timecode.lampa_profile_id)
                    .where(Timecode.device_id == device_id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

        for profile_id in profiles:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(Timecode)
                    .where(
                        Timecode.device_id == device_id,
                        Timecode.lampa_profile_id == profile_id,
                    )
                )
            ).scalar() or 0

            excess = count - limit
            if excess <= 0:
                continue

            oldest_ids = (
                (
                    await db.execute(
                        select(Timecode.id)
                        .where(
                            Timecode.device_id == device_id,
                            Timecode.lampa_profile_id == profile_id,
                        )
                        .order_by(Timecode.updated_at.asc())
                        .limit(excess)
                    )
                )
                .scalars()
                .all()
            )

            if oldest_ids:
                await db.execute(delete(Timecode).where(Timecode.id.in_(oldest_ids)))
                total_deleted += len(oldest_ids)

    if total_deleted:
        logger.info(
            f"User {username}: deleted {total_deleted} old timecodes (limit={limit}/profile)"
        )


# ─── Task loops ───────────────────────────────────────────────────────────────


async def _check_loop() -> None:
    while True:
        wait = _seconds_until_next_run()
        from app import settings_cache

        hour = max(0, min(23, settings_cache.get_int("daily_task_hour") or 2))
        logger.info(
            f"Next premium check in {wait / 3600:.1f}h (at {hour:02d}:00 server time)"
        )
        await asyncio.sleep(wait)
        try:
            await run_premium_expiry_check()
        except Exception as e:
            logger.error(f"Premium expiry check failed: {e}", exc_info=True)


async def _delivery_loop() -> None:
    while True:
        await asyncio.sleep(600)  # every 10 minutes
        try:
            await run_notification_delivery()
        except Exception as e:
            logger.error(f"Notification delivery failed: {e}", exc_info=True)


def start_tasks() -> None:
    global _check_task, _delivery_task
    _check_task = asyncio.create_task(_check_loop())
    _delivery_task = asyncio.create_task(_delivery_loop())
    logger.info("Background tasks started")


def stop_tasks() -> None:
    global _check_task, _delivery_task
    for t in (_check_task, _delivery_task):
        if t:
            t.cancel()
    _check_task = _delivery_task = None
