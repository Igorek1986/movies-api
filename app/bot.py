"""
Telegram-бот NUMParser (aiogram v3).

Привязка устройства — через deep link t.me/BOT?start=CODE.
Восстановление пароля — бот отправляет 6-значный код, пользователь вводит его на сайте.

Команды пользователя:
  /start [CODE]  — приветствие; если передан код — привязывает аккаунт
  /status        — роль и количество устройств

Команды администратора (telegram_id в TELEGRAM_ADMIN_IDS):
  /admin                       — список команд
  /info username               — информация об аккаунте
  /setpremium username         — роль premium
  /setsuper username           — роль super
  /setsimple username          — роль simple
  /broadcast текст             — всем привязанным пользователям
"""

import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    MenuButtonWebApp, MenuButtonDefault,
    WebAppInfo,
)
from sqlalchemy import select, func

from app.db.database import async_session_maker
from app.db.models import TelegramUser, TelegramLinkCode, User, Device, SupportMessage
from app import settings_cache

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_dp: Dispatcher | None = None
_router = Router()


def get_bot() -> Bot | None:
    return _bot


def get_dp() -> Dispatcher | None:
    return _dp


async def _on_startup(bot: Bot) -> None:
    from app.config import get_settings
    settings = get_settings()
    if not settings.TELEGRAM_USE_POLLING:
        webhook_url = f"{settings.BASE_URL}/bot/webhook"
        secret = settings.TELEGRAM_BOT_TOKEN.split(":")[1]
        await bot.set_webhook(
            webhook_url,
            secret_token=secret,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"Telegram webhook set: {webhook_url}")

    # Команды бота (видны в меню «/»)
    await bot.set_my_commands([
        BotCommand(command="start",  description="Главное меню"),
        BotCommand(command="status", description="Статус аккаунта"),
    ])

    # Глобальная кнопка меню — открывает Mini App (для привязанных пользователей)
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="📱 Управление",
            web_app=WebAppInfo(url=f"{settings.BASE_URL}/tg-app"),
        )
    )
    logger.info("Bot commands and menu button set")


async def _on_shutdown(bot: Bot) -> None:
    from app.config import get_settings
    if not get_settings().TELEGRAM_USE_POLLING:
        await bot.delete_webhook()


def init_bot(token: str) -> tuple[Bot, Dispatcher]:
    global _bot, _dp
    _bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    _dp = Dispatcher()
    _dp.include_router(_router)
    _dp.startup.register(_on_startup)
    _dp.shutdown.register(_on_shutdown)
    return _bot, _dp


# ─── Хелперы ──────────────────────────────────────────────────────────────────

def _is_admin(telegram_id: int) -> bool:
    from app.config import get_settings
    return telegram_id in get_settings().telegram_admin_id_list


async def _get_tg_user(db, telegram_id: int) -> TelegramUser | None:
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def _process_link_code(message: types.Message, code: str):
    """Привязывает Telegram-аккаунт к коду из БД."""
    async with async_session_maker() as db:
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(TelegramLinkCode).where(TelegramLinkCode.code == code)
        )
        link_code = result.scalar_one_or_none()

        if not link_code:
            await message.answer("Код не найден. Запросите новый на сайте.")
            return

        if link_code.expires_at.replace(tzinfo=timezone.utc) < now:
            await db.delete(link_code)
            await db.commit()
            await message.answer("Код истёк. Запросите новый на сайте.")
            return

        # Этот Telegram уже привязан к другому аккаунту?
        existing = await _get_tg_user(db, message.from_user.id)
        if existing and existing.user_id != link_code.user_id:
            await message.answer(
                "Этот Telegram уже привязан к другому аккаунту NUMParser.\n"
                "Сначала отвяжите его в настройках того аккаунта."
            )
            return

        # У целевого пользователя уже есть другой Telegram — обновляем
        result2 = await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == link_code.user_id)
        )
        tg_user = result2.scalar_one_or_none()

        username = message.from_user.username
        if tg_user:
            tg_user.telegram_id = message.from_user.id
            tg_user.username = username
        else:
            db.add(TelegramUser(
                user_id=link_code.user_id,
                telegram_id=message.from_user.id,
                username=username,
            ))

        await db.delete(link_code)
        await db.commit()

        user_result = await db.execute(select(User).where(User.id == link_code.user_id))
        user = user_result.scalar_one_or_none()

    await _send_start_menu(message)


# ─── Команды пользователя ─────────────────────────────────────────────────────

@_router.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    # Deep link: t.me/bot?start=CODE → command.args == "CODE"
    if command.args:
        await _process_link_code(message, command.args.strip())
        return

    await _send_start_menu(message)


@_router.message(Command("status"))
async def cmd_status(message: types.Message):
    async with async_session_maker() as db:
        tg = await _get_tg_user(db, message.from_user.id)
        if not tg:
            await message.answer("Telegram не привязан ни к одному аккаунту NUMParser.")
            return

        user_result = await db.execute(select(User).where(User.id == tg.user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer("Аккаунт не найден.")
            return

        device_count = await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == user.id)
        )

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    limit = settings_cache.get_role_limit(user.role, "device_limit") or 3
    limit_str = str(limit) if limit is not None else "∞"

    await message.answer(
        f"<b>Аккаунт:</b> {user.username}\n"
        f"<b>Роль:</b> {role_labels.get(user.role, user.role)}\n"
        f"<b>Устройств:</b> {device_count} / {limit_str}"
    )


# ─── Хелпер: главное меню ────────────────────────────────────────────────────

async def _send_start_menu(message: types.Message):
    """Отправляет приветствие. Кнопка меню уже установлена глобально на боте."""
    from app.config import get_settings
    base_url = get_settings().BASE_URL
    is_admin = _is_admin(message.from_user.id)

    async with async_session_maker() as db:
        tg = await _get_tg_user(db, message.from_user.id)
        if tg:
            user_result = await db.execute(select(User).where(User.id == tg.user_id))
            user = user_result.scalar_one_or_none()
        else:
            user = None

    if is_admin:
        name = user.username if user else "—"
        text = (
            f"👋 Привет, <b>{name}</b>!\n\n"
            f"Вы администратор NUMParser.\n\n"
            f"<b>Команды:</b>\n"
            f"/status — статус аккаунта\n"
            f"/admin — управление пользователями\n\n"
            f"Нажмите кнопку <b>«📱 Управление»</b> рядом с полем ввода, "
            f"чтобы открыть панель администратора."
        )
    elif tg and user:
        text = (
            f"👋 Привет, <b>{user.username}</b>!\n\n"
            f"Нажмите кнопку <b>«📱 Управление»</b> рядом с полем ввода, "
            f"чтобы управлять устройствами.\n\n"
            f"<b>Команды:</b>\n"
            f"/status — статус аккаунта"
        )
    else:
        text = (
            f"👋 Привет! Я бот <b>NUMParser</b>.\n\n"
            f"Чтобы управлять устройствами через Telegram — "
            f"сначала привяжите аккаунт на сайте:\n"
            f"<a href=\"{base_url}/profiles\">{base_url}/profiles</a>"
        )

    await message.answer(text, disable_web_page_preview=True)


# ─── Команды администратора ───────────────────────────────────────────────────

@_router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Команды администратора:</b>\n\n"
        "/info username — информация об аккаунте\n"
        "/setpremium username — роль premium\n"
        "/setsuper username — роль super\n"
        "/setsimple username — роль simple\n"
        "/broadcast текст — сообщение всем привязанным\n"
    )


@_router.message(Command("info"))
async def cmd_info(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /info username")
        return

    async with async_session_maker() as db:
        user_result = await db.execute(select(User).where(User.username == username))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer(f"Пользователь <b>{username}</b> не найден.")
            return

        device_count = await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == user.id)
        )
        tg_result = await db.execute(
            select(TelegramUser).where(TelegramUser.user_id == user.id)
        )
        tg = tg_result.scalar_one_or_none()

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    limit = settings_cache.get_role_limit(user.role, "device_limit") or 3
    limit_str = str(limit) if limit is not None else "∞"
    tg_str = (f"@{tg.username}" if tg and tg.username else str(tg.telegram_id)) if tg else "не привязан"

    await message.answer(
        f"<b>Аккаунт:</b> {user.username}\n"
        f"<b>Роль:</b> {role_labels.get(user.role, user.role)}\n"
        f"<b>Устройств:</b> {device_count} / {limit_str}\n"
        f"<b>Telegram:</b> {tg_str}\n"
        f"<b>Регистрация:</b> {user.created_at.strftime('%d.%m.%Y') if user.created_at else '—'}"
    )


async def _set_role(message: types.Message, username: str, role: str):
    from app.db.models import USER_ROLES
    if role not in USER_ROLES:
        await message.answer(f"Неизвестная роль: {role}")
        return
    async with async_session_maker() as db:
        user_result = await db.execute(select(User).where(User.username == username))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer(f"Пользователь <b>{username}</b> не найден.")
            return
        old_role = user.role
        user.role = role
        await db.commit()

    role_labels = {"simple": "Базовый", "premium": "Премиум", "super": "Супер"}
    await message.answer(
        f"Роль <b>{username}</b>: "
        f"{role_labels.get(old_role, old_role)} → {role_labels.get(role, role)}"
    )


@_router.message(Command("setpremium"))
async def cmd_setpremium(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /setpremium username")
        return
    await _set_role(message, username, "premium")


@_router.message(Command("setsuper"))
async def cmd_setsuper(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /setsuper username")
        return
    await _set_role(message, username, "super")


@_router.message(Command("setsimple"))
async def cmd_setsimple(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    username = (command.args or "").strip().lstrip("@")
    if not username:
        await message.answer("Использование: /setsimple username")
        return
    await _set_role(message, username, "simple")


@_router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if not _is_admin(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /broadcast текст сообщения")
        return

    async with async_session_maker() as db:
        result = await db.execute(select(TelegramUser))
        all_tg = result.scalars().all()

    sent, failed = 0, 0
    for tg in all_tg:
        if await send_message(tg.telegram_id, text):
            sent += 1
        else:
            failed += 1

    await message.answer(f"Отправлено: {sent}, ошибок: {failed}")


# ─── Чат поддержки ────────────────────────────────────────────────────────────

@_router.message(F.text & ~F.text.startswith("/"))
async def handle_user_message(message: types.Message):
    """Любое текстовое сообщение не от команды — пересылается администраторам."""
    from app.config import get_settings
    settings = get_settings()
    admin_ids = settings.telegram_admin_id_list

    # Если это сообщение от администратора — обрабатываем как ответ поддержки
    if _is_admin(message.from_user.id):
        # Ответ на уведомление о сообщении пользователя
        if message.reply_to_message:
            await _handle_admin_reply(message)
        return

    user_id = message.from_user.id
    username = message.from_user.username
    text = message.text

    # Сохраняем входящее сообщение
    async with async_session_maker() as db:
        msg_obj = SupportMessage(
            user_telegram_id=user_id,
            user_username=username,
            direction="in",
            text=text,
            is_read=False,
        )
        db.add(msg_obj)
        await db.flush()
        msg_id = msg_obj.id

        # Пересылаем каждому администратору
        if not admin_ids:
            await db.commit()
            await message.answer("Ваше сообщение получено. Администратор ответит вам здесь.")
            return

        name = f"@{username}" if username else f"#{user_id}"
        forward_text = (
            f"📩 <b>Сообщение от {name}</b> (ID: <code>{user_id}</code>)\n\n"
            f"{text}\n\n"
            f"<i>Ответьте на это сообщение, чтобы написать пользователю.</i>"
        )

        for admin_id in admin_ids:
            try:
                sent = await _bot.send_message(admin_id, forward_text, parse_mode="HTML")
                # Сохраняем привязку msg_id → admin_msg_id для маршрутизации ответа
                db.add(SupportMessage(
                    user_telegram_id=user_id,
                    user_username=username,
                    direction="in",
                    text=text,
                    admin_telegram_id=admin_id,
                    admin_msg_id=sent.message_id,
                    is_read=False,
                ))
            except Exception as e:
                logger.warning(f"Не удалось переслать сообщение поддержки admin {admin_id}: {e}")

        # Удаляем первичную запись без admin_msg_id (заменена точными записями выше)
        await db.delete(msg_obj)
        await db.commit()

    await message.answer("✅ Сообщение отправлено администратору. Ожидайте ответа.")


async def _handle_admin_reply(message: types.Message):
    """Обрабатывает ответ администратора на уведомление о сообщении пользователя."""
    reply_msg_id = message.reply_to_message.message_id
    admin_id = message.from_user.id

    async with async_session_maker() as db:
        result = await db.execute(
            select(SupportMessage).where(
                SupportMessage.admin_telegram_id == admin_id,
                SupportMessage.admin_msg_id == reply_msg_id,
                SupportMessage.direction == "in",
            )
        )
        original = result.scalar_one_or_none()

    if not original:
        # Не найдено — обычное сообщение от администратора, игнорируем
        return

    user_tg_id = original.user_telegram_id
    reply_text = message.text or message.caption or ""

    ok = await send_message(user_tg_id, f"💬 <b>Ответ от поддержки:</b>\n\n{reply_text}")

    if ok:
        # Сохраняем ответ
        async with async_session_maker() as db:
            db.add(SupportMessage(
                user_telegram_id=user_tg_id,
                user_username=original.user_username,
                direction="out",
                text=reply_text,
                admin_telegram_id=admin_id,
                is_read=True,
            ))
            await db.commit()
        await message.reply("✅ Ответ отправлен пользователю.")
    else:
        await message.reply("❌ Не удалось отправить сообщение пользователю.")


# ─── Публичные функции отправки ───────────────────────────────────────────────

async def send_message(telegram_id: int, text: str) -> bool:
    if not _bot:
        return False
    try:
        await _bot.send_message(telegram_id, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Telegram send failed to {telegram_id}: {e}")
        return False


async def send_reset_code(telegram_id: int, username: str, code: str) -> bool:
    """Отправить 6-значный код для сброса пароля."""
    text = (
        f"Запрос на сброс пароля для аккаунта <b>{username}</b>.\n\n"
        f"Ваш код: <code>{code}</code>\n\n"
        "Введите его на странице восстановления пароля. "
        "Действует 15 минут.\n\n"
        "Если вы не запрашивали сброс — проигнорируйте."
    )
    return await send_message(telegram_id, text)


async def send_new_session_notification(telegram_id: int, ip: str, device: str, change_password_url: str) -> bool:
    """Уведомить пользователя о новом входе в аккаунт."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    text = (
        f"🔐 <b>Новый вход в аккаунт</b>\n\n"
        f"🌐 IP: <code>{ip}</code>\n"
        f"📱 Устройство: {device}\n"
        f"🕐 Время: {now}\n\n"
        f"Если это были не вы — <a href=\"{change_password_url}\">смените пароль</a>."
    )
    return await send_message(telegram_id, text)
