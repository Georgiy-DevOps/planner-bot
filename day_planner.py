"""
day_planner.py — AI-планировщик дня на базе Claude.

Читает данные пользователя, анализирует задачи и привычки,
возвращает структурированный план на день с приоритетами и тайм-блоками.

Использование:
  python day_planner.py --data data/123.json
  python day_planner.py --data data/123.json --energy high --free-hours 6
  python day_planner.py --data data/123.json --format md --out plans/today.md
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL     = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"
TODAY            = date.today()
MONTH_DAYS       = 31
MAX_TOKENS_PLAN  = 2048


# ──────────────────────────── Загрузка данных ─────────────────────────────────

def load_data(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"❌ Файл не найден: {path}")
    return json.loads(p.read_text(encoding="utf-8"))


def get_expenses(data: dict) -> list[dict]:
    if data.get("expenses_list"):
        return data["expenses_list"]
    return [
        {"desc": x.get("name",""), "amount": x.get("actual",0)}
        for x in data.get("finance",{}).get("expenses",[])
    ]


# ──────────────────────────── Анализ данных ───────────────────────────────────

def analyze_data(data: dict) -> dict:
    """Извлекает ключевые метрики для контекста Claude."""
    today_idx  = TODAY.day - 1
    habits     = data.get("habits", [])
    tasks      = data.get("tasks", [])
    expenses   = get_expenses(data)

    # Привычки
    habits_today = []
    habits_behind = []
    for h in habits:
        days = h.get("days", [False]*MONTH_DAYS)
        done_today = today_idx < len(days) and days[today_idx]
        done_cnt   = sum(1 for i in range(today_idx) if i < len(days) and days[i])
        rate       = done_cnt / today_idx if today_idx > 0 else 0

        streak = 0
        for i in range(min(today_idx-1, len(days)-1), -1, -1):
            if days[i]: streak += 1
            else: break

        info = {
            "name":      h.get("name",""),
            "emoji":     h.get("emoji","📌"),
            "done_today": done_today,
            "rate":      round(rate*100),
            "streak":    streak,
        }
        if done_today:
            habits_today.append(info)
        else:
            habits_behind.append(info)

    # Задачи
    overdue = []
    urgent  = []
    active  = []
    done    = []
    for t in tasks:
        if t.get("done"):
            done.append(t)
            continue
        is_overdue = (t.get("deadline") and
                      date.fromisoformat(t["deadline"]) < TODAY)
        info = {
            "text":     t.get("text",""),
            "priority": t.get("priority","medium"),
            "deadline": t.get("deadline",""),
            "category": t.get("category",""),
        }
        if is_overdue:               overdue.append(info)
        elif t.get("priority")=="high": urgent.append(info)
        else:                            active.append(info)

    # Финансы
    spent  = sum(e.get("amount",0) for e in expenses)
    budget = data.get("budget",0)
    fin_alert = spent > budget*0.9 if budget else False

    return {
        "habits_done_today":   habits_today,
        "habits_pending":      habits_behind,
        "tasks_overdue":       overdue,
        "tasks_urgent":        urgent,
        "tasks_active":        active,
        "tasks_done_count":    len(done),
        "tasks_total":         len(tasks),
        "finance_spent":       spent,
        "finance_budget":      budget,
        "finance_alert":       fin_alert,
    }


# ──────────────────────────── Prompt Builder ──────────────────────────────────

SYSTEM_PROMPT = """Ты — AI-планировщик дня. Твоя задача: проанализировать данные пользователя
и создать конкретный, реалистичный план на сегодня.

Правила:
1. Учитывай уровень энергии и доступное время.
2. Просроченные задачи — в первую очередь.
3. Невыполненные привычки — интегрируй в расписание, не отдельным блоком.
4. Финансовый алерт → предложи снизить расходы сегодня.
5. Тайм-блоки: реалистичные (не набивай 8 задач за час).
6. Давай конкретные советы, не общие фразы.
7. Формат ответа — строго JSON (без markdown-обёртки), структура описана ниже.

JSON-структура ответа:
{
  "summary": "Одна строка: общий тон дня и ключевой фокус",
  "energy_tip": "Совет с учётом уровня энергии (1-2 предложения)",
  "blocks": [
    {
      "time": "09:00–10:30",
      "title": "Название блока",
      "tasks": ["задача 1", "задача 2"],
      "notes": "Необязательный комментарий"
    }
  ],
  "habits_schedule": [
    { "time": "08:00", "habit": "💪 Зарядка", "tip": "Короткий совет" }
  ],
  "priorities": [
    { "rank": 1, "text": "Текст задачи", "reason": "Почему первый приоритет" }
  ],
  "evening_review": "Что проверить вечером (1-2 пункта)",
  "finance_tip": "Совет по финансам на сегодня (или null)"
}"""


def build_user_prompt(analysis: dict, energy: str, free_hours: float,
                      wake_time: str, notes: str) -> str:
    a = analysis
    lines = [
        f"📅 Дата: {TODAY.strftime('%d.%m.%Y')} ({TODAY.strftime('%A')})",
        f"⚡ Уровень энергии: {energy}",
        f"⏱ Свободных часов: {free_hours}",
        f"🌅 Время подъёма: {wake_time}",
    ]
    if notes:
        lines.append(f"📝 Заметки: {notes}")
    lines.append("")

    # Привычки
    if a["habits_pending"]:
        lines.append("🔥 Невыполненные привычки (вставь в план):")
        for h in a["habits_pending"]:
            lines.append(f"  • {h['emoji']} {h['name']} — {h['rate']}% за месяц, streak {h['streak']} дн")

    if a["habits_done_today"]:
        lines.append(f"✅ Уже выполнено сегодня: {', '.join(h['name'] for h in a['habits_done_today'])}")
    lines.append("")

    # Задачи
    if a["tasks_overdue"]:
        lines.append(f"⚠️ Просроченные задачи ({len(a['tasks_overdue'])}):")
        for t in a["tasks_overdue"][:5]:
            lines.append(f"  • [{t['priority']}] {t['text']} (дедлайн: {t['deadline']})")

    if a["tasks_urgent"]:
        lines.append(f"\n🔴 Срочные задачи ({len(a['tasks_urgent'])}):")
        for t in a["tasks_urgent"][:5]:
            dl = f" → {t['deadline']}" if t["deadline"] else ""
            lines.append(f"  • {t['text']}{dl}")

    if a["tasks_active"]:
        lines.append(f"\n📋 Активные задачи ({len(a['tasks_active'])}):")
        for t in a["tasks_active"][:8]:
            lines.append(f"  • [{t['priority']}] {t['text']}")

    lines.append(f"\n✅ Выполнено за месяц: {a['tasks_done_count']} из {a['tasks_total']}")

    # Финансы
    if a["finance_budget"]:
        pct = round(a["finance_spent"]/a["finance_budget"]*100) if a["finance_budget"] else 0
        lines.append(f"\n💰 Бюджет: {a['finance_spent']:,.0f} / {a['finance_budget']:,.0f} ₽ ({pct}%)")
        if a["finance_alert"]:
            lines.append("  ⚠️ АЛЕРТ: бюджет почти исчерпан!")

    lines.append("\nСоздай план на день в формате JSON.")
    return "\n".join(lines)


# ──────────────────────────── Claude API ──────────────────────────────────────

def call_claude(system: str, user: str, api_key: str) -> dict:
    """Вызов Claude API, возвращает распарсенный JSON."""
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS_PLAN,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = httpx.post(CLAUDE_API_URL, json=body, headers=headers, timeout=60)
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()

    # Убираем markdown-обёртку если вдруг есть
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()

    return json.loads(raw)


# ──────────────────────────── Форматирование ──────────────────────────────────

def format_plan_text(plan: dict, analysis: dict) -> str:
    lines = []
    lines.append(f"╔══════════════════════════════════════╗")
    lines.append(f"  📅 ПЛАН НА {TODAY.strftime('%d.%m.%Y')}")
    lines.append(f"╚══════════════════════════════════════╝")
    lines.append("")
    lines.append(f"💡 {plan.get('summary','')}")
    if plan.get("energy_tip"):
        lines.append(f"⚡ {plan['energy_tip']}")
    lines.append("")

    # Приоритеты
    if plan.get("priorities"):
        lines.append("🏆 ТОП ПРИОРИТЕТЫ")
        for p in plan["priorities"]:
            lines.append(f"  {p['rank']}. {p['text']}")
            if p.get("reason"):
                lines.append(f"     → {p['reason']}")
        lines.append("")

    # Привычки
    if plan.get("habits_schedule"):
        lines.append("🔥 ПРИВЫЧКИ В РАСПИСАНИИ")
        for h in plan["habits_schedule"]:
            lines.append(f"  {h['time']}  {h['habit']}")
            if h.get("tip"):
                lines.append(f"         💬 {h['tip']}")
        lines.append("")

    # Тайм-блоки
    if plan.get("blocks"):
        lines.append("📆 РАСПИСАНИЕ ДНЯ")
        for b in plan["blocks"]:
            lines.append(f"\n  ⏰ {b['time']}  —  {b['title']}")
            for t in b.get("tasks",[]):
                lines.append(f"     • {t}")
            if b.get("notes"):
                lines.append(f"     💬 {b['notes']}")
        lines.append("")

    # Вечерний чекин
    if plan.get("evening_review"):
        lines.append(f"🌙 ВЕЧЕРНИЙ ЧЕКИН: {plan['evening_review']}")
        lines.append("")

    # Финансы
    if plan.get("finance_tip"):
        lines.append(f"💰 ФИНАНСЫ: {plan['finance_tip']}")
        lines.append("")

    lines.append(f"─────────────────────────────────────")
    lines.append(f"Сгенерировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)


def format_plan_md(plan: dict, analysis: dict) -> str:
    lines = [f"# 📅 План на {TODAY.strftime('%d.%m.%Y')}", ""]
    lines.append(f"> {plan.get('summary','')}")
    if plan.get("energy_tip"):
        lines.append(f"> ⚡ {plan['energy_tip']}")
    lines.append("")

    if plan.get("priorities"):
        lines.append("## 🏆 Приоритеты")
        for p in plan["priorities"]:
            lines.append(f"{p['rank']}. **{p['text']}** — {p.get('reason','')}")
        lines.append("")

    if plan.get("habits_schedule"):
        lines.append("## 🔥 Привычки")
        for h in plan["habits_schedule"]:
            tip = f" _{h['tip']}_" if h.get("tip") else ""
            lines.append(f"- `{h['time']}` {h['habit']}{tip}")
        lines.append("")

    if plan.get("blocks"):
        lines.append("## 📆 Расписание")
        for b in plan["blocks"]:
            lines.append(f"\n### ⏰ {b['time']} — {b['title']}")
            for t in b.get("tasks",[]):
                lines.append(f"- [ ] {t}")
            if b.get("notes"):
                lines.append(f"> 💬 {b['notes']}")
        lines.append("")

    if plan.get("evening_review"):
        lines.append(f"## 🌙 Вечерний чекин\n{plan['evening_review']}\n")
    if plan.get("finance_tip"):
        lines.append(f"## 💰 Финансовый совет\n{plan['finance_tip']}\n")

    lines.append(f"---\n*{datetime.now().strftime('%d.%m.%Y %H:%M')}*")
    return "\n".join(lines)


def format_plan_json(plan: dict) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2)


# ──────────────────────────── Demo mode ───────────────────────────────────────

def make_demo_plan(analysis: dict) -> dict:
    """Если нет API-ключа — генерируем детерминированный демо-план."""
    a = analysis
    overdue_texts = [t["text"] for t in a["tasks_overdue"]]
    urgent_texts  = [t["text"] for t in a["tasks_urgent"]]
    pending_habits = [f"{h['emoji']} {h['name']}" for h in a["habits_pending"]]

    priorities = []
    for i, t in enumerate(a["tasks_overdue"][:3], 1):
        priorities.append({"rank":i, "text":t["text"], "reason":"Просрочена"})
    for i, t in enumerate(a["tasks_urgent"][:2], len(priorities)+1):
        priorities.append({"rank":i, "text":t["text"], "reason":"Высокий приоритет"})

    blocks = [
        {"time":"08:00–09:00", "title":"Утро и привычки",
         "tasks": pending_habits[:3] or ["Подготовиться к дню"],
         "notes": "Включи таймер, не проверяй телефон"},
        {"time":"09:00–11:00", "title":"Глубокая работа",
         "tasks": (overdue_texts + urgent_texts)[:3] or ["Главная задача дня"],
         "notes": "Режим фокуса, без уведомлений"},
        {"time":"13:00–14:30", "title":"Второй блок",
         "tasks": [t["text"] for t in a["tasks_active"][:3]] or ["Рутинные задачи"],
         "notes": None},
        {"time":"20:00–20:30", "title":"Чекин дня",
         "tasks": ["Отметить привычки", "Обновить задачи"],
         "notes": "5 минут — план на завтра"},
    ]
    blocks = [b for b in blocks if b["tasks"]]

    return {
        "summary": f"Фокус дня: {len(a['tasks_overdue'])} просроченных + {len(pending_habits)} привычек",
        "energy_tip": "Демо-режим: задайте CLAUDE_API_KEY для персонального плана.",
        "blocks": blocks,
        "habits_schedule": [
            {"time":"07:30", "habit": h, "tip":"Сразу после подъёма"}
            for h in pending_habits[:3]
        ],
        "priorities": priorities or [{"rank":1,"text":"Нет активных задач","reason":"День свободен"}],
        "evening_review": "Сколько привычек выполнено? Что перенести?",
        "finance_tip": f"Потрачено {a['finance_spent']:,.0f} ₽ из {a['finance_budget']:,.0f} ₽" if a['finance_budget'] else None,
    }


# ──────────────────────────── main ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI-планировщик дня")
    parser.add_argument("--data",       required=True, help="Путь к data/{uid}.json")
    parser.add_argument("--energy",     default="medium",
                        choices=["low","medium","high"],
                        help="Уровень энергии (default: medium)")
    parser.add_argument("--free-hours", type=float, default=6.0,
                        help="Свободных часов на продуктивную работу (default: 6)")
    parser.add_argument("--wake",       default="08:00", help="Время подъёма (default: 08:00)")
    parser.add_argument("--notes",      default="",      help="Доп. заметки (строка)")
    parser.add_argument("--format",     default="text", choices=["text","md","json"],
                        help="Формат вывода")
    parser.add_argument("--out",        default="",  help="Сохранить в файл (опционально)")
    args = parser.parse_args()

    print(f"📂 Загружаем данные: {args.data}")
    data     = load_data(args.data)
    analysis = analyze_data(data)

    print(f"🔥 Привычек ожидает: {len(analysis['habits_pending'])}")
    print(f"⚠️  Просрочено задач: {len(analysis['tasks_overdue'])}")
    print(f"🔴 Срочных задач:    {len(analysis['tasks_urgent'])}")
    print(f"📋 Активных задач:   {len(analysis['tasks_active'])}")
    print()

    if CLAUDE_API_KEY:
        print("🤖 Генерируем план через Claude...")
        user_prompt = build_user_prompt(
            analysis, args.energy, args.free_hours, args.wake, args.notes
        )
        try:
            plan = call_claude(SYSTEM_PROMPT, user_prompt, CLAUDE_API_KEY)
            print("✅ План получен от Claude\n")
        except Exception as e:
            print(f"⚠️  Claude API ошибка: {e}\n   Переключаюсь на демо-режим\n")
            plan = make_demo_plan(analysis)
    else:
        print("ℹ️  CLAUDE_API_KEY не задан — демо-режим\n")
        plan = make_demo_plan(analysis)

    # Форматирование
    if args.format == "md":
        output = format_plan_md(plan, analysis)
    elif args.format == "json":
        output = format_plan_json(plan)
    else:
        output = format_plan_text(plan, analysis)

    print(output)

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(output, encoding="utf-8")
        print(f"\n💾 Сохранено: {p}")


if __name__ == "__main__":
    main()
