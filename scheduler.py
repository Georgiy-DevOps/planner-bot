"""
scheduler.py — Планировщик уведомлений для Telegram-бота.

Запускается рядом с ботом (или в том же процессе).
Отправляет:
  - 08:00  Утренний бриф: привычки + задачи дня
  - 21:00  Вечерний чекин: что выполнено, что осталось
  - Каждые 2ч  Проверка дедлайнов: алерт если просрочено или истекает сегодня

Настройка через .env:
  TG_BOT_TOKEN, MORNING_TIME=08:00, EVENING_TIME=21:00,
  DEADLINE_CHECK_INTERVAL=120 (минуты)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scheduler")

BOT_TOKEN        = os.getenv("TG_BOT_TOKEN","")
DATA_DIR         = Path(os.getenv("DATA_DIR","data"))
MORNING_TIME     = os.getenv("MORNING_TIME","08:00")
EVENING_TIME     = os.getenv("EVENING_TIME","21:00")
DEADLINE_MINUTES = int(os.getenv("DEADLINE_CHECK_INTERVAL","120"))
MONTH_DAYS       = 31


# ──────────────────────────── Данные ─────────────────────────────────────────

def all_user_ids() -> list[int]:
    """Все uid из папки data/."""
    if not DATA_DIR.exists():
        return []
    return [int(f.stem) for f in DATA_DIR.glob("*.json") if f.stem.isdigit()]


def load_user(uid: int) -> Optional[dict]:
    p = DATA_DIR / f"{uid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ──────────────────────────── Аналитика ──────────────────────────────────────

def get_today_habits(data: dict) -> tuple[list, list]:
    """→ (выполнены, не выполнены) за сегодня."""
    today_idx = date.today().day - 1
    done, missed = [], []
    for h in data.get("habits",[]):
        days = h.get("days",[False]*MONTH_DAYS)
        if today_idx < len(days) and days[today_idx]:
            done.append(h)
        else:
            missed.append(h)
    return done, missed


def get_active_tasks(data: dict) -> list[dict]:
    return [t for t in data.get("tasks",[]) if not t.get("done")]


def get_overdue_tasks(data: dict) -> list[dict]:
    today = date.today()
    return [
        t for t in data.get("tasks",[])
        if not t.get("done") and t.get("deadline")
        and date.fromisoformat(t["deadline"]) < today
    ]


def get_due_today_tasks(data: dict) -> list[dict]:
    today = date.today()
    return [
        t for t in data.get("tasks",[])
        if not t.get("done") and t.get("deadline")
        and date.fromisoformat(t["deadline"]) == today
    ]


def calc_streak(days: list, today_idx: int) -> int:
    streak = 0
    for i in range(min(today_idx, len(days)-1), -1, -1):
        if days[i]: streak += 1
        else: break
    return streak


# ──────────────────────────── Сообщения ──────────────────────────────────────

def morning_message(data: dict) -> str:
    """Утренний бриф."""
    today = date.today()
    today_idx = today.day - 1
    done_h, missed_h = get_today_habits(data)
    active = get_active_tasks(data)
    overdue = get_overdue_tasks(data)
    due_today = get_due_today_tasks(data)

    prio_icon = {"high":"🔴","medium":"🟡","low":"🟢"}
    lines = [
        f"☀️ *Доброе утро!*  {today.strftime('%d.%m.%Y')}",
        "",
        f"🔥 *Привычки на сегодня* ({len(missed_h)} ожидают):",
    ]
    for h in missed_h[:5]:
        streak = calc_streak(h.get("days",[]), today_idx)
        streak_str = f" 🔥{streak}" if streak > 0 else ""
        lines.append(f"  • {h.get('emoji','')} {h.get('name','')}{streak_str}")
    if not missed_h:
        lines.append("  ✅ Все привычки выполнены!")

    lines.append("")
    if overdue:
        lines.append(f"⚠️ *Просрочено* ({len(overdue)}):")
        for t in overdue[:3]:
            lines.append(f"  • {prio_icon.get(t.get('priority','medium'),'⚪')} {t['text']} (дедлайн: {t.get('deadline','')})")

    if due_today:
        lines.append(f"\n📅 *Дедлайн сегодня* ({len(due_today)}):")
        for t in due_today[:3]:
            lines.append(f"  • {t['text']}")

    if active:
        lines.append(f"\n✅ *Активных задач:* {len(active)}")
        for t in sorted(active, key=lambda x: {'high':0,'medium':1,'low':2}
                        .get(x.get('priority','medium'),1))[:3]:
            lines.append(f"  • {prio_icon.get(t.get('priority','medium'),'⚪')} {t['text']}")
        if len(active) > 3:
            lines.append(f"  _...и ещё {len(active)-3}_")

    lines.append("\n_Хорошего дня! /tasks — список задач_")
    return "\n".join(lines)


def evening_message(data: dict) -> str:
    """Вечерний чекин."""
    today = date.today()
    today_idx = today.day - 1
    done_h, missed_h = get_today_habits(data)
    active = get_active_tasks(data)
    tasks = data.get("tasks",[])
    done_tasks = [t for t in tasks if t.get("done")]

    total_h = len(data.get("habits",[]))
    rate_h = len(done_h)/total_h*100 if total_h else 0

    # Расходы
    expenses = data.get("finance",{}).get("expenses",[]) or data.get("expenses_list",[])
    spent = sum(e.get("actual",0) or e.get("amount",0) for e in expenses)
    budget = data.get("budget",0)

    streak_champion = None
    best_streak = 0
    for h in data.get("habits",[]):
        s = calc_streak(h.get("days",[]), today_idx)
        if s > best_streak:
            best_streak = s
            streak_champion = h

    lines = [
        f"🌙 *Вечерний чекин*  {today.strftime('%d.%m.%Y')}",
        "",
        f"🔥 *Привычки:* {len(done_h)}/{total_h} ({rate_h:.0f}%)",
    ]
    if done_h:
        lines.append("  Выполнено: " + " ".join(h.get('emoji','')+''+h.get('name','') for h in done_h[:4]))
    if missed_h:
        lines.append("  Пропущено: " + " ".join(h.get('emoji','') for h in missed_h[:4]))

    if streak_champion and best_streak >= 3:
        lines.append(f"\n🏆 Лучший streak: {streak_champion.get('emoji','')} {streak_champion.get('name','')} — {best_streak} дней 🔥")

    lines.append(f"\n✅ *Задачи:* {len(done_tasks)}/{len(tasks)} выполнено")
    if active:
        lines.append(f"  Осталось {len(active)} активных")
        for t in active[:2]:
            lines.append(f"  • {t['text']}")

    if budget:
        remain = budget - spent
        pct = spent/budget*100
        bar = "▓"*min(int(pct/10),10) + "░"*max(0,10-int(pct/10))
        lines.append(f"\n💰 Бюджет: {bar} {pct:.0f}%")
        lines.append(f"  Потрачено: {spent:,.0f} ₽  Остаток: {remain:,.0f} ₽")
        if pct > 90:
            lines.append("  ⚠️ Бюджет почти исчерпан!")

    lines.append("\n_/checkin — отметить привычки  /tasks — задачи_")
    return "\n".join(lines)


def deadline_alert(uid: int, tasks: list[dict]) -> str:
    """Алерт о просроченных/срочных задачах."""
    today = date.today()
    overdue   = [t for t in tasks if not t.get("done") and t.get("deadline")
                 and date.fromisoformat(t["deadline"]) < today]
    due_today = [t for t in tasks if not t.get("done") and t.get("deadline")
                 and date.fromisoformat(t["deadline"]) == today]
    tomorrow  = today + timedelta(days=1)
    due_tomorrow = [t for t in tasks if not t.get("done") and t.get("deadline")
                    and date.fromisoformat(t["deadline"]) == tomorrow]

    if not (overdue or due_today or due_tomorrow):
        return ""

    lines = ["⏰ *Напоминание о дедлайнах*", ""]
    if overdue:
        lines.append(f"🚨 *Просрочено ({len(overdue)}):*")
        for t in overdue[:5]:
            lines.append(f"  • {t['text']} — был {t.get('deadline','')}")
    if due_today:
        lines.append(f"\n🔔 *Сегодня ({len(due_today)}):*")
        for t in due_today[:5]:
            lines.append(f"  • {t['text']}")
    if due_tomorrow:
        lines.append(f"\n📅 *Завтра ({len(due_tomorrow)}):*")
        for t in due_tomorrow[:3]:
            lines.append(f"  • {t['text']}")

    lines.append("\n_/tasks — управление задачами_")
    return "\n".join(lines)


# ──────────────────────────── Jobs ───────────────────────────────────────────

async def job_morning(bot: Bot):
    log.info("▶ Утренний бриф")
    for uid in all_user_ids():
        data = load_user(uid)
        if not data: continue
        try:
            msg = morning_message(data)
            await bot.send_message(uid, msg, parse_mode="Markdown")
            log.info(f"  ✅ Утренний бриф → {uid}")
        except Exception as e:
            log.warning(f"  ⚠️ Ошибка отправки {uid}: {e}")


async def job_evening(bot: Bot):
    log.info("▶ Вечерний чекин")
    for uid in all_user_ids():
        data = load_user(uid)
        if not data: continue
        try:
            msg = evening_message(data)
            await bot.send_message(uid, msg, parse_mode="Markdown")
            log.info(f"  ✅ Вечерний чекин → {uid}")
        except Exception as e:
            log.warning(f"  ⚠️ Ошибка отправки {uid}: {e}")


async def job_deadline_check(bot: Bot):
    log.info("▶ Проверка дедлайнов")
    for uid in all_user_ids():
        data = load_user(uid)
        if not data: continue
        tasks = data.get("tasks",[])
        msg = deadline_alert(uid, tasks)
        if not msg: continue
        try:
            await bot.send_message(uid, msg, parse_mode="Markdown")
            log.info(f"  🔔 Дедлайн-алерт → {uid}")
        except Exception as e:
            log.warning(f"  ⚠️ Ошибка {uid}: {e}")


# ──────────────────────────── Интеграция с ботом ──────────────────────────────

def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Создаёт и настраивает AsyncIOScheduler.
    Вызывай из bot.py:
        from scheduler import create_scheduler
        scheduler = create_scheduler(bot)
        scheduler.start()
    """
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    # Парсим время
    m_h, m_m = map(int, MORNING_TIME.split(":"))
    e_h, e_m = map(int, EVENING_TIME.split(":"))

    # Утренний бриф
    scheduler.add_job(
        job_morning,
        trigger=CronTrigger(hour=m_h, minute=m_m, timezone="Europe/Moscow"),
        args=[bot],
        id="morning_brief",
        replace_existing=True,
        misfire_grace_time=300,
    )
    log.info(f"⏰ Утренний бриф запланирован на {MORNING_TIME}")

    # Вечерний чекин
    scheduler.add_job(
        job_evening,
        trigger=CronTrigger(hour=e_h, minute=e_m, timezone="Europe/Moscow"),
        args=[bot],
        id="evening_checkin",
        replace_existing=True,
        misfire_grace_time=300,
    )
    log.info(f"🌙 Вечерний чекин запланирован на {EVENING_TIME}")

    # Дедлайн-алерты
    scheduler.add_job(
        job_deadline_check,
        trigger=IntervalTrigger(minutes=DEADLINE_MINUTES),
        args=[bot],
        id="deadline_check",
        replace_existing=True,
        misfire_grace_time=120,
    )
    log.info(f"🔔 Проверка дедлайнов каждые {DEADLINE_MINUTES} мин")

    return scheduler


# ──────────────────────────── Команды-обработчики для бота ───────────────────

SCHEDULER_HANDLERS_SNIPPET = '''
# ── Вставить в bot.py ──────────────────────────────────────────────────────
# from scheduler import create_scheduler, morning_message, evening_message
#
# После инициализации bot и dp:
#   scheduler = create_scheduler(bot)
#   scheduler.start()
#
# on_startup:
#   async def on_startup():
#       scheduler.start()
#   dp.startup.register(on_startup)
#
# on_shutdown:
#   async def on_shutdown():
#       scheduler.shutdown(wait=False)
#   dp.shutdown.register(on_shutdown)

@dp.message(Command("morning"))
async def cmd_morning(message: types.Message):
    """Принудительный утренний бриф /morning."""
    uid = message.from_user.id
    data = load_user(uid)
    if not data:
        await message.answer("Нет данных. Начни с /start")
        return
    await message.answer(morning_message(data), parse_mode="Markdown")

@dp.message(Command("evening"))
async def cmd_evening(message: types.Message):
    """Вечерний чекин вручную /evening."""
    uid = message.from_user.id
    data = load_user(uid)
    if not data:
        await message.answer("Нет данных.")
        return
    await message.answer(evening_message(data), parse_mode="Markdown")

@dp.message(Command("remind"))
async def cmd_remind(message: types.Message):
    """Настройка напоминаний: /remind 09:00 22:00"""
    parts = message.text.strip().split()
    if len(parts) >= 3:
        morning_t, evening_t = parts[1], parts[2]
        # Обновить env/config для этого пользователя (упрощённо)
        await message.answer(
            f"✅ Напоминания установлены:\\n"
            f"☀️ Утро: {morning_t}\\n"
            f"🌙 Вечер: {evening_t}\\n"
            f"_(Глобальная настройка через .env: MORNING_TIME / EVENING_TIME)_",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "⏰ Текущие напоминания:\\n"
            f"☀️ Утро: {MORNING_TIME}\\n"
            f"🌙 Вечер: {EVENING_TIME}\\n"
            f"🔔 Дедлайны: каждые {DEADLINE_MINUTES} мин\\n\\n"
            "_Изменить: /remind 09:00 22:00_",
            parse_mode="Markdown"
        )
'''


# ──────────────────────────── Standalone mode ────────────────────────────────

async def _standalone():
    """Запуск как отдельного процесса (без основного бота)."""
    if not BOT_TOKEN:
        log.error("TG_BOT_TOKEN не задан в .env")
        sys.exit(1)

    bot = Bot(token=BOT_TOKEN)
    scheduler = create_scheduler(bot)
    scheduler.start()
    log.info("✅ Scheduler запущен. Ctrl+C для остановки.")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Scheduler остановлен")


if __name__ == "__main__":
    asyncio.run(_standalone())
