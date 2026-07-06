"""Текстовий eval голосового агента: ганяє діалогові сценарії проти реального OpenAI.

Використовує ТОЙ САМИЙ системний промпт, ті самі tools і PMS, що й bot.py.
Різниця з живим дзвінком лише в тому, що немає STT/TTS — вхід/вихід текстом.
Тому тут перевіряється ЛОГІКА (дати, ціни, підтвердження, tools), а не голос.
Голосові речі (перебивання T7, шум T9, якість вимови) — тільки вручну через мікрофон.

Використання:
    python eval.py            # всі сценарії
    python eval.py T3 T6      # тільки вибрані

Потребує OPENAI_API_KEY у server/.env (кожен сценарій = кілька центів API).
Тестові дані пишуться в hotel.db з ім'ям «…Тестовий» і чистяться перед запуском.
"""

import json
import re
import sqlite3
import sys

import pms
import bot  # noqa: E402 — тягне load_dotenv і схеми tools
from openai import OpenAI
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter

import os

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_STEPS = 30  # запобіжник від зациклення діалогу

_digits = lambda s: re.sub(r"\D", "", s or "")  # noqa: E731


def _call_tool(name: str, a: dict) -> dict:
    """Ті самі PMS-функції, що викликають хендлери в bot.py."""
    if name == "check_availability":
        return pms.check_availability(
            a.get("check_in_date"), a.get("check_out_date"),
            a.get("room_type"), int(a.get("guests") or 1))
    if name == "create_booking":
        return pms.create_booking(
            a.get("check_in_date"), a.get("check_out_date"),
            a.get("room_type"), int(a.get("guests") or 1),
            a.get("guest_name"), a.get("phone"))
    if name == "take_message":
        return pms.take_message(a.get("guest_name"), a.get("phone"), a.get("topic"))
    return {"error": f"невідомий інструмент {name}"}


def _cleanup() -> None:
    conn = sqlite3.connect(pms.DB_PATH)
    conn.execute("DELETE FROM bookings WHERE guest_name LIKE '%Тестов%'")
    conn.execute("DELETE FROM messages WHERE guest_name LIKE '%Тестов%'")
    conn.commit()
    conn.close()


def _test_bookings() -> list[dict]:
    return [b for b in pms.list_bookings() if "Тестов" in b["guest_name"]]


def _booked(log) -> bool:
    return any(n == "create_booking" and r.get("success") for n, _, r in log)


def run_dialog(client: OpenAI, tools: list, turns: list[str], auto_confirm: bool,
               done=None) -> list[tuple]:
    """Прогнати сценарій: скриптові репліки гостя + авто-«так» на підтвердження.

    Авто-підтвердження потрібне, бо кількість питань агента (телефон по цифрах,
    повний переказ броні) може відрізнятись від кількості скриптових реплік.
    Повертає лог викликів інструментів: [(name, args, result), ...].
    """
    messages = [{"role": "system", "content": bot.build_system_prompt()}]
    tool_log: list[tuple] = []
    queue = list(turns)
    extra = 4 if auto_confirm else 0

    for _ in range(MAX_STEPS):
        last = messages[-1]
        wants_user = last["role"] == "system" or (
            last["role"] == "assistant" and not last.get("tool_calls"))
        if wants_user:
            if done and done(tool_log):
                break
            if queue:
                text = queue.pop(0)
            elif extra > 0:
                text, extra = "Так, все правильно.", extra - 1
            else:
                break
            print(f"  🧑 ГІСТЬ: {text}")
            messages.append({"role": "user", "content": text})

        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools, tool_choice="auto")
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = _call_tool(tc.function.name, args)
                tool_log.append((tc.function.name, args, result))
                print(f"    🔧 {tc.function.name}({args}) -> {result}")
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            print(f"  🤖 ОКСАНА: {msg.content}")
            messages.append({"role": "assistant", "content": msg.content})

    return tool_log


# ---------- Перевірки сценаріїв (повертають [(ok, опис), ...]) ----------

def checks_t3(log) -> list[tuple[bool, str]]:
    bs = _test_bookings()
    out = [(len(bs) == 1, f"рівно 1 бронь у базі (є {len(bs)})")]
    if not bs:
        return out
    b = bs[0]
    out += [
        (b["room_type"] == "superior", f"тип Покращений (є {b['room_type_label']})"),
        (b["check_in"] == "2026-07-14" and b["check_out"] == "2026-07-17",
         f"дати 2026-07-14 → 2026-07-17 (є {b['check_in']} → {b['check_out']})"),
        (b["guests"] == 2, f"2 гостей (є {b['guests']})"),
        (b["total_price"] == 5400, f"сума 5400 грн (є {b['total_price']})"),
        (_digits(b["phone"]) == "0677774567",
         f"телефон 0677774567 (є {_digits(b['phone'])})"),
    ]
    return out


def checks_t4(log) -> list[tuple[bool, str]]:
    bs = _test_bookings()
    premature = any(
        n == "create_booking" and pms.normalize_room_type(a.get("room_type", "")) == "superior"
        for n, a, _ in log)
    out = [
        (len(bs) == 1, f"рівно 1 бронь у базі (є {len(bs)})"),
        (not premature, "Покращений НЕ створювався передчасно"),
    ]
    if bs:
        b = bs[0]
        out += [
            (b["room_type"] == "deluxe", f"тип Делюкс (є {b['room_type_label']})"),
            (b["total_price"] == 8400, f"сума 8400 грн (є {b['total_price']})"),
        ]
    return out


def checks_t6(log) -> list[tuple[bool, str]]:
    created = [x for x in log if x[0] == "create_booking" and x[2].get("success")]
    availability_errors = [
        x for x in log
        if x[0] == "check_availability" and x[2].get("error")]
    caught_without_tool = not any(x[0] == "check_availability" for x in log)
    return [
        (not created, f"бронь НЕ створено (створено {len(created)})"),
        (caught_without_tool or bool(availability_errors),
         "помилку дат зловлено (LLM сам або PMS)"),
    ]


def checks_t8(log) -> list[tuple[bool, str]]:
    msgs = [m for m in pms.list_messages() if "Тестов" in m["guest_name"]]
    out = [
        (len(msgs) == 1, f"рівно 1 запит на дзвінок у базі (є {len(msgs)})"),
        (not _test_bookings(), "бронь НЕ створювалась"),
    ]
    if msgs:
        m = msgs[0]
        out += [
            (_digits(m["phone"]) == "0671234567",
             f"телефон 0671234567 (є {_digits(m['phone'])})"),
            (any(w in m["topic"].lower() for w in ("собак", "спа", "тварин")),
             f"суть питання збережено (є: {m['topic']!r})"),
        ]
    return out


SCENARIOS = {
    "T3": {
        "title": "Happy path: Покращений 14–17 липня, 2 гостей, повний flow",
        "turns": [
            "Доброго дня! Хочу забронювати покращений номер з чотирнадцятого "
            "по сімнадцяте липня, на двох.",
            "Мене звати Тарас Тестовий, телефон нуль шістдесят сім, "
            "сімсот сімдесят сім, сорок п'ять, шістдесят сім.",
        ],
        "auto_confirm": True,
        "done": _booked,
        "checks": checks_t3,
    },
    "T4": {
        "title": "Зміна номера до підтвердження: бронь тільки Делюкс, без передчасної",
        "turns": [
            "Доброго дня! Хочу забронювати покращений номер з чотирнадцятого "
            "по сімнадцяте липня, на двох.",
            "Тарас Тестовий, телефон нуль шістдесят сім, сімсот "
            "сімдесят сім, сорок п'ять, шістдесят сім.",
            "Ні, зачекайте, давайте краще делюкс на ті самі дати.",
        ],
        "auto_confirm": True,
        "done": _booked,
        "checks": checks_t4,
    },
    "T6": {
        "title": "Погані дати: виїзд раніше заїзду — бронь не створюється",
        "turns": [
            "Хочу забронювати стандартний номер на двох. Заїзд сімнадцятого "
            "липня, а виїзд чотирнадцятого липня.",
        ],
        "auto_confirm": False,
        "done": None,
        "checks": checks_t6,
    },
    "T8": {
        "title": "Off-topic: питання про собаку/спа → take_message, без вигадок",
        "turns": [
            "Доброго дня! Скажіть, чи можна у вас зупинитися з собакою? "
            "І чи є спа-центр?",
            "Тарас Тестовий, телефон нуль шістдесят сім, сто двадцять три, "
            "сорок п'ять, шістдесят сім.",
        ],
        "auto_confirm": True,
        "done": lambda log: any(
            n == "take_message" and r.get("success") for n, _, r in log),
        "checks": checks_t8,
    },
}


def main() -> int:
    names = [n.upper() for n in sys.argv[1:]] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"Невідомі сценарії: {unknown}. Доступні: {list(SCENARIOS)}")
        return 2

    pms.init_db()
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tools = OpenAILLMAdapter().from_standard_tools(ToolsSchema(standard_tools=[
        bot.CHECK_AVAILABILITY_SCHEMA, bot.CREATE_BOOKING_SCHEMA, bot.TAKE_MESSAGE_SCHEMA]))

    results: dict[str, list[tuple[bool, str]]] = {}
    for name in names:
        sc = SCENARIOS[name]
        print(f"\n{'=' * 60}\n{name} — {sc['title']}\n{'=' * 60}")
        _cleanup()
        log = run_dialog(client, tools, sc["turns"], sc["auto_confirm"], sc["done"])
        results[name] = sc["checks"](log)

    print(f"\n{'=' * 60}\nПІДСУМОК\n{'=' * 60}")
    failed = 0
    for name, checks in results.items():
        ok = all(c[0] for c in checks)
        failed += not ok
        print(f"{'✅' if ok else '❌'} {name} — {SCENARIOS[name]['title']}")
        for passed, desc in checks:
            print(f"    {'✓' if passed else '✗ FAIL:'} {desc}")

    _cleanup()
    print(f"\n{len(results) - failed}/{len(results)} сценаріїв пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
