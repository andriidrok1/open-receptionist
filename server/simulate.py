"""simulate.py — симулятор дзвінків: LLM-клієнт розмовляє з нашим агентом.

Навіщо окремо від eval.py. Там сценарії скриптові: заздалегідь відомі репліки,
заздалегідь відомий результат. Це ловить регресії, але не ловить того, що
ламається в реальному дзвінку — коли людина мимрить, передумує, називає дату,
яка вже минула, або мовчить про адресу, доки не спитають двічі.

Тут клієнта грає окрема модель зі своїм характером і набором фактів про себе.
Вона НЕ знає, як влаштований наш агент, і поводиться як людина на телефоні.

Головне не діалог, а перевірки. Вони не залежать від сценарію і ловлять клас
помилок «агент щось вигадав»:

    - записав дані, яких клієнт не називав (адресу з прикладу в промпті);
    - запропонував час, якого не повертав інструмент;
    - створив запис на слот, що не проходив успішної перевірки;
    - після вдалого запису ще й завів заявку на дзвінок;
    - зациклився: питає те саме втретє.

Кожна з них — реальний баг, спійманий у живому дзвінку. Тепер вони ловляться
без голосу, за копійки й за хвилину.

Використання:
    python simulate.py                       # усі персони активного закладу
    python simulate.py --business cleaning   # інший заклад
    python simulate.py mumbler past_date     # тільки вибрані
    python simulate.py --list                # перелік персон
    python simulate.py --show                # друкувати діалоги повністю

Потребує OPENAI_API_KEY. Один прогін персони — кілька центів.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import date, timedelta

_ARGV = sys.argv[1:]
if "--business" in _ARGV:
    i = _ARGV.index("--business")
    os.environ["BUSINESS"] = _ARGV[i + 1]
    del _ARGV[i : i + 2]
SHOW = "--show" in _ARGV
if SHOW:
    _ARGV.remove("--show")

import agent_tools  # noqa: E402
import crm  # noqa: E402
from business import BUSINESS, build_system_prompt  # noqa: E402
from openai import OpenAI  # noqa: E402
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CALLER_MODEL = os.getenv("SIMULATE_CALLER_MODEL", "gpt-4o-mini")
MAX_TURNS = 14

BOOK = BUSINESS.booking
_digits = lambda s: re.sub(r"\D", "", s or "")  # noqa: E731


# ------------------------------- персони -------------------------------

def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


FACTS = {
    "name": "Тарас Мельник",
    "phone": "0632096974",
    "address": "вулиця Січових Стрільців, будинок сім, квартира дванадцять",
}

_service = BOOK.resources[0].label if BOOK.resources else "послуга"
_service_alt = BOOK.resources[-1].label if len(BOOK.resources) > 1 else _service

PERSONAS = {
    "plain": {
        "title": "Звичайний клієнт: приходить, домовляється, дає дані",
        "style": "Ти спокійний і конкретний. Відповідаєш коротко, по суті.",
        "goal": f"Записатися на «{_service}» на {_future(3)}, приблизно на 11:00.",
    },
    "mumbler": {
        "title": "Мимрить, тягне «е-е-е», формулює з другого разу",
        "style": (
            "Ти говориш нечітко, з паузами і словами-паразитами («е-е-е», «ну»,"
            " «як його»). Перше речення в тебе виходить незрозумілим, уточнюєш"
            " лише коли перепитають. Ніколи не називаєш усе одразу."
        ),
        "goal": f"Записатися на «{_service}», десь наступного тижня, зранку.",
    },
    "past_date": {
        "title": "Просить дату, яка вже минула",
        "style": "Ти впевнений у собі й не одразу розумієш, що помилився з датою.",
        "goal": (
            f"Записатися на «{_service}» на {(date.today() - timedelta(days=30)).isoformat()}"
            " (так, це вже минуле — ти переплутав). Якщо скажуть, що дата минула —"
            f" погодься і назви {_future(5)}."
        ),
    },
    "changes_mind": {
        "title": "Передумує: інша послуга, потім інший час",
        "style": "Ти нерішучий. Двічі змінюєш рішення, вже після того як назвав перше.",
        "goal": (
            f"Спершу просиш «{_service}» на {_future(4)} о 10:00. Потім передумуєш"
            f" і хочеш «{_service_alt}». Потім просиш перенести на 15:00."
        ),
    },
    "bad_phone": {
        "title": "Диктує номер із помилкою, потім виправляє",
        "style": "Ти квапишся і плутаєшся в цифрах.",
        "goal": (
            f"Записатися на «{_service}» на {_future(6)} о 12:00. Коли спитають"
            " телефон — назви спершу нуль шість три два нуль дев'ять шість дев'ять"
            " сім (це лише дев'ять цифр). Якщо перепитають — назви правильний."
        ),
    },
    "offtopic": {
        "title": "Питає те, чого заклад не оголошував",
        "style": "Ти допитливий, ставиш питання поза стандартним прайсом.",
        "goal": (
            "Дізнатися те, чого немає у прайсі закладу (щось незвичне для цієї"
            " галузі). Записуватись не хочеш. Якщо запропонують передзвонити —"
            " погодься і дай свої дані."
        ),
    },
    "silent_address": {
        "title": "Не називає адресу, доки не спитають прямо",
        "style": (
            "Ти небалакучий. Даєш рівно те, про що спитали, і нічого зверху."
            " Адресу називаєш ТІЛЬКИ якщо спитають про неї прямо."
        ),
        "goal": f"Записатися на «{_service}» на {_future(2)} о 14:00.",
    },
}


def caller_prompt(p: dict) -> str:
    return f"""Ти — людина, яка телефонує в заклад «{BUSINESS.name}». Ти звичайний
клієнт, а не асистент. Ти НЕ знаєш, як влаштована система на тому боці.

Твій характер: {p['style']}

Твоя мета: {p['goal']}

Твої дані (називай їх ЛИШЕ коли спитають, і саме такими):
- ім'я: {FACTS['name']}
- телефон: {FACTS['phone']}
- адреса: {FACTS['address']}

Правила:
- Говори українською, короткими репліками, як у телефонній розмові.
- Одна репліка — одна думка. Не переказуй свою мету цілком.
- НЕ вигадуй фактів про себе понад ті, що вище. Якщо питають те, чого немає в
  твоїх даних, — скажи, що не знаєш або що це неважливо.
- Коли мети досягнуто (тебе записали) або стало ясно, що не вийде, попрощайся
  і додай окремим словом: КІНЕЦЬ
- Ніяких пояснень «як модель». Тільки репліка клієнта."""


# ------------------------------- прогін -------------------------------

def _cleanup() -> None:
    conn = sqlite3.connect(crm.DB_PATH)
    for t in ("bookings", "messages", "events"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def run_call(client: OpenAI, tools: list, persona: dict) -> dict:
    """Провести дзвінок. Повертає стенограму й лог інструментів."""
    agent_msgs = [{"role": "system", "content": build_system_prompt()}]
    caller_msgs = [{"role": "system", "content": caller_prompt(persona)}]
    transcript: list[tuple[str, str]] = []
    tool_log: list[tuple] = []

    # Першим говорить агент — так само, як у справжньому дзвінку.
    agent_msgs.append({"role": "developer", "content":
                       f"Привітайся першою, дуже коротко, як {BUSINESS.persona}."})

    for _ in range(MAX_TURNS):
        # --- хід агента (може містити виклики інструментів) ---
        for _ in range(6):
            resp = client.chat.completions.create(
                model=MODEL, messages=agent_msgs, tools=tools, tool_choice="auto")
            msg = resp.choices[0].message
            if not msg.tool_calls:
                break
            agent_msgs.append({"role": "assistant", "content": msg.content or "",
                               "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = agent_tools.call_tool(tc.function.name, args)
                tool_log.append((tc.function.name, args, result))
                agent_msgs.append({"role": "tool", "tool_call_id": tc.id,
                                   "content": json.dumps(result, ensure_ascii=False)})
        agent_text = (msg.content or "").strip()
        agent_msgs.append({"role": "assistant", "content": agent_text})
        transcript.append(("agent", agent_text))
        caller_msgs.append({"role": "user", "content": agent_text})

        # --- хід клієнта ---
        cres = client.chat.completions.create(model=CALLER_MODEL, messages=caller_msgs)
        caller_text = (cres.choices[0].message.content or "").strip()
        caller_msgs.append({"role": "assistant", "content": caller_text})
        done = "КІНЕЦЬ" in caller_text
        caller_text = caller_text.replace("КІНЕЦЬ", "").strip()
        if caller_text:
            transcript.append(("caller", caller_text))
            agent_msgs.append({"role": "user", "content": caller_text})
        if done:
            break

    return {"transcript": transcript, "tools": tool_log}


# ----------------------------- перевірки -----------------------------
#
# Кожна повертає список порушень (порожній список = все добре).

def _caller_said(transcript) -> str:
    return " ".join(t for who, t in transcript if who == "caller").lower()


def check_no_fabricated_data(run) -> list[str]:
    """Агент не має передавати в інструменти те, чого клієнт не казав."""
    said = _caller_said(run["transcript"])
    said_digits = _digits(said)
    bad = []
    for name, args, res in run["tools"]:
        # Відхилена спроба — це спрацював запобіжник, а не брак у базі.
        if name not in ("create_booking", "take_message") or not res.get("success"):
            continue
        phone = _digits(args.get("phone"))
        if phone and phone not in said_digits and phone[1:] not in said_digits:
            bad.append(f"{name}: телефон {phone} клієнт не називав")
        for field in [f.key for f in BOOK.intake] + ["customer_name"]:
            val = (args.get(field) or "").strip()
            if not val:
                continue
            # Достатньо, щоб прозвучало хоч одне змістовне слово зі значення.
            words = [w for w in re.findall(r"[\w']{4,}", val.lower())]
            if words and not any(w[:5] in said for w in words):
                bad.append(f"{name}: «{field}» = {val!r} — такого в розмові не було")
    return bad


def _slot_key(args: dict) -> tuple:
    return (args.get(BOOK.resource_param), args.get("date"), args.get("time"),
            args.get("start_date"), args.get("end_date"))


def check_booked_over_refusal(run) -> list[str]:
    """Спроба записати на слот, який інструмент щойно назвав недоступним."""
    refused = set()
    bad = []
    for name, args, res in run["tools"]:
        key = _slot_key(args)
        if name == "check_availability":
            (refused.discard if res.get("available") else refused.add)(key)
        elif name == "create_booking" and key in refused:
            bad.append(f"спроба записати на {key}, хоча перевірка сказала «зайнято»")
    return bad


def warn_unverified_slot(run) -> list[str]:
    """Запис на слот, який жодного разу не перевіряли.

    Не помилка: crm перевіряє наявність ще раз перед вставкою, тож
    переброні не станеться. Але агент при цьому називає час і ціну, яких не
    підтверджував — і якщо слот виявиться зайнятим, доведеться переігрувати
    вже домовлений запис просто на очах у клієнта.
    """
    ok = set()
    warn = []
    for name, args, res in run["tools"]:
        key = _slot_key(args)
        if name == "check_availability" and res.get("available"):
            ok.add(key)
        elif name == "create_booking" and res.get("success") and key not in ok:
            warn.append(f"час {key[1] or key[3]} {key[2] or ''} названий без перевірки")
    return warn


def check_no_message_after_booking(run) -> list[str]:
    booked = False
    bad = []
    for name, _, res in run["tools"]:
        if name == "create_booking" and res.get("success"):
            booked = True
        elif name == "take_message" and booked and res.get("success"):
            bad.append("take_message після успішного запису — зайвий передзвін")
    return bad


def check_no_loop(run) -> list[str]:
    """Агент не має ставити те саме питання тричі."""
    seen: dict[str, int] = {}
    for who, text in run["transcript"]:
        if who != "agent":
            continue
        for q in re.findall(r"[^.!?]*\?", text):
            k = re.sub(r"\W+", " ", q.lower()).strip()[:40]
            if len(k) < 10:
                continue
            seen[k] = seen.get(k, 0) + 1
    return [f"питання повторюється {n} рази: «{k}…»" for k, n in seen.items() if n >= 3]


def check_no_success_claim_on_error(run) -> list[str]:
    """Якщо запис не вдався, агент не має казати, що записав."""
    ok = any(n == "create_booking" and r.get("success") for n, _, r in run["tools"])
    tried = any(n == "create_booking" for n, _, _ in run["tools"])
    if ok or not tried:
        return []
    words = ("записала", "записав", "записано", "ваш запис", "заброньовано", "чекаємо на вас")
    last = " ".join(t for who, t in run["transcript"][-4:] if who == "agent").lower()
    return ["агент каже, що записав, хоча запис не створено"] if any(w in last for w in words) else []


CHECKS = [
    check_no_fabricated_data,
    check_booked_over_refusal,
    check_no_message_after_booking,
    check_no_loop,
    check_no_success_claim_on_error,
]


def main() -> int:
    if "--list" in _ARGV:
        for k, v in PERSONAS.items():
            print(f"  {k:16} {v['title']}")
        return 0

    names = [n for n in _ARGV if not n.startswith("-")] or list(PERSONAS)
    unknown = [n for n in names if n not in PERSONAS]
    if unknown:
        print(f"Невідомі персони: {unknown}. Доступні: {list(PERSONAS)}")
        return 2

    crm.init_db()
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tools = OpenAILLMAdapter().from_standard_tools(agent_tools.tools_schema())

    total_bad = 0
    for name in names:
        p = PERSONAS[name]
        print(f"\n{'=' * 66}\n{BUSINESS.key} · {name} — {p['title']}\n{'=' * 66}")
        _cleanup()
        run = run_call(client, tools, p)

        if SHOW:
            for who, text in run["transcript"]:
                print(f"  {'🤖' if who == 'agent' else '🧑'} {text}")
            for n, a, r in run["tools"]:
                print(f"    🔧 {n}({a}) -> {r}")

        problems = [v for check in CHECKS for v in check(run)]
        warnings = warn_unverified_slot(run)
        total_bad += len(problems)
        for w in warnings:
            print(f"  ⚠️  {w}")
        if problems:
            for v in problems:
                print(f"  ❌ {v}")
            if not SHOW:  # без діалогу порушення не зрозуміти
                for who, text in run["transcript"]:
                    print(f"     {'🤖' if who == 'agent' else '🧑'} {text}")
        elif not warnings:
            print(f"  ✅ без порушень ({len(run['tools'])} викликів інструментів)")

    _cleanup()
    print(f"\n{'=' * 66}\nПідсумок: {total_bad} порушень у {len(names)} дзвінках")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
