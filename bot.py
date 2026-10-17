"""
bot.py — точка входа планнер-бота.
Polling-режим. Запускается через systemd или напрямую.
"""

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from dotenv import load_dotenv

from webapp_handlers import router as webapp_router
from scheduler import create_scheduler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DATA_DIR   = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)

from aiogram import Router
main_router = Router()

@main_router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я планнер-бот.\n\n"
        "Помогу отслеживать задачи, привычки и финансы.\n\n"
        "Команды:\n"
        "/start — это меню\n"
        "/help — справка"
    )

@main_router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📋 Доступные команды:\n"
        "/start — главное меню\n"
        "/app — открыть планнер\n"
        "/help — эта справка"
    )


async def main():
    if not BOT_TOKEN:
        raise ValueError("TG_BOT_TOKEN не задан в .env")

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())

    # Подключаем роутеры
    dp.include_router(main_router)
    dp.include_router(webapp_router)

    # Запускаем планировщик уведомлений в том же процессе
    scheduler = create_scheduler(bot)
    scheduler.start()

    log.info("Бот запущен (polling)")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
