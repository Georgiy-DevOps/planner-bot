"""
webapp_handlers.py — Обработчики Telegram Mini App в боте.

Вставить в bot.py:
  from webapp_handlers import register_webapp_handlers
  register_webapp_handlers(dp, bot)

Или подключить router:
  from webapp_handlers import router as webapp_router
  dp.include_router(webapp_router)

Переменные окружения:
  WEBAPP_URL=https://your-domain.com/planner/webapp.html
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                            KeyboardButton, ReplyKeyboardMarkup,
                            WebAppInfo)

log = logging.getLogger("webapp")
router = Router(name="webapp")

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DATA_DIR   = Path(os.getenv("DATA_DIR", "data"))
MONTH_DAYS = 31


# ──────────────────────────── Загрузка/сохранение ────────────────────────────

def load_user(uid: int) -> dict:
    p = DATA_DIR / f"{uid}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"habits":[], "tasks":[], "finance":{"expenses":[]}, "budget":0}


def save_user(uid: int, data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    p = DATA_DIR / f"{uid}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────── Клавиатуры ─────────────────────────────────────

def webapp_keyboard(uid: int) -> InlineKeyboardMarkup:
    """Inline-кнопка открытия WebApp."""
    if not WEBAPP_URL:
        return InlineKeyboardMarkup(inline_keyboard=[])
    url = f"{WEBAPP_URL}?uid={uid}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Открыть планировщик", web_app=WebAppInfo(url=url))
    ]])


def reply_webapp_button() -> ReplyKeyboardMarkup:
    """Reply-кнопка в нижней панели (если WEBAPP_URL задан)."""
    if not WEBAPP_URL:
        return None
    btn = KeyboardButton(text="📱 Планировщик", web_app=WebAppInfo(url=WEBAPP_URL))
    return ReplyKeyboardMarkup(
        keyboard=[[btn]], resize_keyboard=True, one_time_keyboard=False
    )


# ──────────────────────────── Хендлеры ───────────────────────────────────────

@router.message(Command("app"))
async def cmd_app(message: types.Message):
    """Открыть Mini App."""
    if not WEBAPP_URL:
        await message.answer(
            "⚠️ WEBAPP_URL не настроен.\n"
            "Задайте в .env: WEBAPP_URL=https://your-domain.com/webapp.html"
        )
        return
    await message.answer(
        "📱 *Planner Mini App*\n\nОткрой планировщик прямо в Telegram:",
        reply_markup=webapp_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )


@router.message(F.web_app_data)
async def handle_webapp_data(message: types.Message, bot: Bot):
    """
    Получаем данные из WebApp после нажатия MainButton.
    message.web_app_data.data — строка JSON.
    """
    uid = message.from_user.id

    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        await message.answer("❌ Ошибка обработки данных из приложения.")
        return

    # Загружаем текущие данные
    data = load_user(uid)

    # ── Обновляем привычки ──
    if "habits" in payload:
        incoming_habits = payload["habits"]
        existing_map = {h.get("name",""): i for i,h in enumerate(data.get("habits",[]))}
        for inc_h in incoming_habits:
            name = inc_h.get("name","")
            if name in existing_map:
                idx = existing_map[name]
                data["habits"][idx]["days"] = inc_h.get("days", data["habits"][idx].get("days",[]))
            # Новые привычки из webapp не добавляем автоматически (безопасность)

    # ── Обновляем задачи ──
    if "tasks" in payload:
        incoming_tasks = payload["tasks"]
        existing_map = {t.get("text",""): t for t in data.get("tasks",[])}
        for inc_t in incoming_tasks:
            txt = inc_t.get("text","")
            if txt in existing_map:
                existing_map[txt]["done"] = inc_t.get("done", existing_map[txt].get("done"))

    # ── Добавляем новые задачи ──
    new_tasks = payload.get("new_tasks", [])
    if new_tasks:
        existing_texts = {t.get("text","") for t in data.get("tasks",[])}
        added = 0
        for t in new_tasks[:10]:  # лимит 10 новых за раз
            txt = t.get("text","").strip()
            if txt and txt not in existing_texts:
                data.setdefault("tasks",[]).append({
                    "text":     txt,
                    "priority": t.get("priority","medium"),
                    "done":     False,
                    "deadline": t.get("deadline",""),
                    "category": "",
                })
                existing_texts.add(txt)
                added += 1

    # ── Добавляем новые расходы ──
    new_expenses = payload.get("new_expenses", [])
    if new_expenses:
        fin = data.setdefault("finance", {})
        fin.setdefault("expenses", [])
        added_exp = 0
        for e in new_expenses[:20]:  # лимит
            amt = float(e.get("amount", 0))
            if amt > 0:
                fin["expenses"].append({
                    "name":   e.get("name",""),
                    "actual": amt,
                    "plan":   0,
                    "category": e.get("category","Прочее"),
                })
                added_exp += 1

    # ── Сохраняем ──
    save_user(uid, data)
    log.info(f"WebApp sync uid={uid}: +{len(new_tasks)} задач, +{len(new_expenses)} расходов")

    # ── Статистика для ответа ──
    today_idx = __import__("datetime").date.today().day - 1
    habits  = data.get("habits",[])
    tasks   = data.get("tasks",[])
    h_done  = sum(1 for h in habits if today_idx<len(h.get("days",[])) and h["days"][today_idx])
    t_done  = sum(1 for t in tasks if t.get("done"))
    expenses_list = data.get("finance",{}).get("expenses",[])
    spent = sum(e.get("actual",0) for e in expenses_list)
    budget = data.get("budget",0)

    lines = [
        "✅ *Данные сохранены!*",
        "",
        f"🔥 Привычки сегодня: {h_done}/{len(habits)}",
        f"📋 Задачи: {t_done}/{len(tasks)} выполнено",
    ]
    if new_tasks:
        lines.append(f"➕ Добавлено задач: {len(new_tasks)}")
    if new_expenses:
        lines.append(f"💸 Добавлено расходов: {len(new_expenses)}")
    if budget:
        lines.append(f"💰 Бюджет: {spent:,.0f} / {budget:,.0f} ₽")

    await message.answer("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────── Регистрация ────────────────────────────────────

def register_webapp_handlers(dp, bot: Optional[Bot] = None):
    """Зарегистрировать router в Dispatcher."""
    dp.include_router(router)
    log.info(f"WebApp handlers registered. WEBAPP_URL={WEBAPP_URL or '(не задан)'}")
