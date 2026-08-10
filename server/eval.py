"""Text-level evaluation: scripted dialogues run against the real model.

It uses THE SAME system prompt, the same tools and the same storage as bot.py.
The only difference from a live call is that there is no STT or TTS — input and
output are text. So what is checked here is the LOGIC (dates, prices,
confirmations, tools), not the voice. Anything acoustic (interruptions, noise,
pronunciation) can only be checked by hand through a microphone.

The scenarios differ per business: a hotel books a range of days, a clinic books
a time and offers free slots. The shared core (prompt, tools, phone handling) is
exercised by both sets — which is exactly how a regression from an
"industry-specific" change gets caught.

Usage:
    python eval.py                       # every scenario for the active business
    python eval.py T3 T6                 # selected ones only
    python eval.py --business clinic     # the dental scenarios
    python eval.py --business all        # every business in turn

Requires OPENAI_API_KEY in server/.env (each scenario costs a few cents).
Test records are written under a recognisable test name and cleaned up before
and after the run.

Dates in the scripted lines are computed from today and spelled out in words,
otherwise the evaluation rots along with a hard-coded month.

TODO(locale): the scripted caller lines are Ukrainian because the configs are.
English scenarios arrive with the English configs.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# --business has to be handled BEFORE importing business.py: the business is
# selected at import time.
_ARGV = sys.argv[1:]
if "--business" in _ARGV:
    i = _ARGV.index("--business")
    _BUSINESS_ARG = _ARGV[i + 1]
    del _ARGV[i : i + 2]
else:
    _BUSINESS_ARG = None
if _BUSINESS_ARG and _BUSINESS_ARG != "all":
    os.environ["BUSINESS"] = _BUSINESS_ARG

# A throwaway database, chosen before crm is imported (it fixes the path at
# import time). Without it the scenarios wrote their stubs into the business's
# real database, and a run interrupted mid-way left them on the dashboard.
os.environ.setdefault(
    "CRM_DB",
    str(Path(tempfile.gettempdir()) / f"eval_{os.environ.get('BUSINESS', 'hotel')}.db"),
)

import agent_tools  # noqa: E402
import crm  # noqa: E402
from business import BUSINESS, build_system_prompt  # noqa: E402
from openai import OpenAI  # noqa: E402
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# The same settings as bot.py. Without them the test exercises a different
# behaviour than production: parallel tool calls produce the "a callback request
# on top of a booking" bug, which does not exist in production because parallel
# calls are disabled there — so the test kept failing on a bug that was not real.
AGENT_LLM = dict(temperature=0.6, parallel_tool_calls=False)
MAX_STEPS = 30  # a guard against the dialogue looping forever

_digits = lambda s: re.sub(r"\D", "", s or "")  # noqa: E731


# --------------------- dates and times in words ---------------------
#
# The spoken forms and the scripted lines live in scenarios/<lang>.yaml, next to
# the simulator's personas. Keeping them in Python meant eval could only ever
# speak one language, so the English business had a free-running simulator and
# zero deterministic checks.

import yaml  # noqa: E402

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
_path = SCENARIOS_DIR / f"{BUSINESS.language}.yaml"
if not _path.exists():
    sys.exit(f"no eval scenarios for language {BUSINESS.language!r} ({_path})")
EV = yaml.safe_load(_path.read_text(encoding="utf-8"))["eval"]

TEST_MARKER = EV["test_marker"]
BUSY_STUB = EV["busy_stub"]


def say_day(d: date) -> str:
    """A date the way a caller says it out loud."""
    return EV["say_day"].format(ordinal=EV["ordinals"][d.day], month=EV["months"][d.month])


def say_time(hour: int) -> str:
    """An hour the way a caller says it out loud.

    For a 12-hour language this deliberately produces "at two" rather than
    14:00 — the point of the scenario is to find out whether the model writes
    14:00 or 02:00 into the tool.
    """
    suffix = ""
    if "{suffix}" in EV["say_time"]:
        suffix = "" if hour == 12 else (" in the morning" if hour < 12 else " in the afternoon")
    return EV["say_time"].format(hour=EV["hours"][str(hour)], suffix=suffix)


def next_weekday(weekday: int, min_days: int = 1) -> date:
    """The next occurrence of a weekday (0 = Monday)."""
    d = BUSINESS.today() + timedelta(days=min_days)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def _s_name_turn() -> str:
    return EV["name_turn"]


# ------------------------------ the run ------------------------------


def _cleanup() -> None:
    conn = sqlite3.connect(crm.DB_PATH)
    for table in ("bookings", "messages"):
        conn.execute(f"DELETE FROM {table} WHERE customer_name LIKE ?", (f"%{TEST_MARKER}%",))
    conn.commit()
    conn.close()


def _test_bookings() -> list[dict]:
    return [b for b in crm.list_bookings() if TEST_MARKER in b["customer_name"]]


def _test_messages() -> list[dict]:
    return [m for m in crm.list_messages() if TEST_MARKER in m["customer_name"]]


def _booked(log) -> bool:
    return any(n == "create_booking" and r.get("success") for n, _, r in log)


def _messaged(log) -> bool:
    return any(n == "take_message" and r.get("success") for n, _, r in log)


def run_dialog(client: OpenAI, tools: list, turns: list[str], auto_confirm: bool,
               done=None) -> list[tuple]:
    """Run a scenario: scripted caller lines plus an automatic "yes".

    The automatic confirmation is needed because the number of questions the
    agent asks (repeating the phone number, the full summary before booking) can
    differ from the number of scripted lines.
    Returns the tool call log: [(name, args, result), ...].
    """
    messages = [{"role": "system", "content": build_system_prompt()}]
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
                text, extra = EV["confirm"], extra - 1
            else:
                break
            print(f"  🧑 КЛІЄНТ: {text}")
            messages.append({"role": "user", "content": text})

        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools, tool_choice="auto",
            **AGENT_LLM)
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = agent_tools.call_tool(tc.function.name, args)
                tool_log.append((tc.function.name, args, result))
                print(f"    🔧 {tc.function.name}({args}) -> {result}")
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            print(f"  🤖 {BUSINESS.agent_name.upper()}: {msg.content}")
            messages.append({"role": "assistant", "content": msg.content})

    return tool_log


# ------------------------ scenarios: nights ------------------------

H_IN = BUSINESS.today() + timedelta(days=35)
H_OUT = H_IN + timedelta(days=3)
H_PHONE = EV["phone_digits"]
# The store keeps E.164 now, so the expectation goes through the same
# normalisation the agent's input does — otherwise the test asserts on a format
# rather than on the number.
H_PHONE_STORED = crm.validate_phone(H_PHONE)[0]


def _h_turns(room: str, phone_words: str = "сімсот сімдесят сім, сорок п'ять, шістдесят сім"):
    return (
        f"Доброго дня! Хочу забронювати {room} номер з {say_day(H_IN)} "
        f"по {say_day(H_OUT)}, на двох."
    ), f"Мене звати Тарас Тестовий, телефон нуль шістдесят сім, {phone_words}."


def checks_t3(log) -> list[tuple[bool, str]]:
    bs = _test_bookings()
    out = [(len(bs) == 1, f"рівно 1 бронь у базі (є {len(bs)})")]
    if not bs:
        return out
    b = bs[0]
    out += [
        (b["resource"] == "superior", f"тип Покращений (є {b['resource_label']})"),
        (b["starts_at"] == H_IN.isoformat() and b["ends_at"] == H_OUT.isoformat(),
         f"дати {H_IN} → {H_OUT} (є {b['starts_at']} → {b['ends_at']})"),
        (b["party_size"] == 2, f"2 гостей (є {b['party_size']})"),
        (b["total_price"] == 5400, f"сума 5400 грн (є {b['total_price']})"),
        (b["phone"] == H_PHONE_STORED, f"телефон {H_PHONE} (є {b['phone']})"),
    ]
    return out


def checks_t4(log) -> list[tuple[bool, str]]:
    bs = _test_bookings()
    premature = any(
        n == "create_booking" and crm.normalize_resource(a.get("room_type", "")) == "superior"
        for n, a, _ in log)
    out = [
        (len(bs) == 1, f"рівно 1 бронь у базі (є {len(bs)})"),
        (not premature, "Покращений НЕ створювався передчасно"),
    ]
    if bs:
        b = bs[0]
        out += [
            (b["resource"] == "deluxe", f"тип Делюкс (є {b['resource_label']})"),
            (b["total_price"] == 8400, f"сума 8400 грн (є {b['total_price']})"),
        ]
    return out


def checks_t6(log) -> list[tuple[bool, str]]:
    created = [x for x in log if x[0] == "create_booking" and x[2].get("success")]
    errors = [x for x in log if x[0] == "check_availability" and x[2].get("error")]
    caught_without_tool = not any(x[0] == "check_availability" for x in log)
    return [
        (not created, f"бронь НЕ створено (створено {len(created)})"),
        (caught_without_tool or bool(errors), "помилку дат зловлено (LLM сам або сховище)"),
    ]


def checks_t8(log) -> list[tuple[bool, str]]:
    msgs = _test_messages()
    out = [
        (len(msgs) == 1, f"рівно 1 запит на дзвінок у базі (є {len(msgs)})"),
        (not _test_bookings(), "бронь НЕ створювалась"),
    ]
    if msgs:
        m = msgs[0]
        out += [
            (m["phone"] == crm.validate_phone("0671234567")[0],
             f"телефон 0671234567 (є {m['phone']})"),
            (any(w in m["topic"].lower() for w in ("собак", "спа", "тварин")),
             f"суть питання збережено (є: {m['topic']!r})"),
        ]
    return out


HOTEL_SCENARIOS = {
    "T3": {
        "title": f"Happy path: Покращений {H_IN}–{H_OUT}, 2 гостей, повний flow",
        "turns": list(_h_turns("покращений")),
        "auto_confirm": True, "done": _booked, "checks": checks_t3,
    },
    "T4": {
        "title": "Зміна номера до підтвердження: бронь тільки Делюкс, без передчасної",
        "turns": [*_h_turns("покращений"), "Ні, зачекайте, давайте краще делюкс на ті самі дати."],
        "auto_confirm": True, "done": _booked, "checks": checks_t4,
    },
    "T6": {
        "title": "Погані дати: виїзд раніше заїзду — бронь не створюється",
        "turns": [
            f"Хочу забронювати стандартний номер на двох. Заїзд {say_day(H_OUT)}, "
            f"а виїзд {say_day(H_IN)}.",
        ],
        "auto_confirm": False, "done": None, "checks": checks_t6,
    },
    "T8": {
        "title": "Off-topic: питання про собаку/спа → take_message, без вигадок",
        "turns": [
            "Доброго дня! Скажіть, чи можна у вас зупинитися з собакою? І чи є спа-центр?",
            "Тарас Тестовий, телефон нуль шістдесят сім, сто двадцять три, "
            "сорок п'ять, шістдесят сім.",
        ],
        "auto_confirm": True, "done": _messaged, "checks": checks_t8,
    },
}


# ------------------ scenarios: slots ------------------

# The next Tuesday is a working day everywhere. But the emergency service has a
# seven-day booking horizon, and on a Monday "next Tuesday" fell outside it: the
# scenario failed on "we do not book that far ahead" rather than on what it is
# meant to check.
S_DAY = next_weekday(1, min_days=2)
if (S_DAY - BUSINESS.today()).days > BUSINESS.booking.horizon_days:
    S_DAY = BUSINESS.today() + timedelta(days=1)
S_HOUR = 10
S_BUSY_HOUR = 12
S_PHONE = EV["phone_digits"]
S_PHONE_STORED = crm.validate_phone(S_PHONE)[0]
#: The service C1 books — set in _slot_scenarios for each industry.
BOOK_FIRST = None




def checks_c1(log) -> list[tuple[bool, str]]:
    bs = _test_bookings()
    out = [(len(bs) == 1, f"рівно 1 запис у базі (є {len(bs)})")]
    if not bs:
        return out
    b = bs[0]
    want = f"{S_DAY.isoformat()}T{S_HOUR:02d}:00"
    res = BOOK_FIRST.key
    out += [
        (b["resource"] == res, f"послуга {BOOK_FIRST.label} (є {b['resource_label']})"),
        (b["starts_at"] == want, f"початок {want} (є {b['starts_at']})"),
        (b["total_price"] == BOOK_FIRST.price,
         f"ціна {BOOK_FIRST.price} (є {b['total_price']})"),
        (b["phone"] == S_PHONE_STORED, f"телефон {S_PHONE} (є {b['phone']})"),
        (not _test_messages(), "зайвого запиту на дзвінок після запису немає"),
        *[
            (bool(b["details"].get(f.key)),
             f"записано «{f.label}» (є {b['details'].get(f.key)!r})")
            for f in BUSINESS.booking.intake if f.required
        ],
    ]
    return out


def checks_c2(log) -> list[tuple[bool, str]]:
    """The slot is taken: the agent must offer an alternative, not invent one."""
    busy = f"{S_DAY.isoformat()}T{S_BUSY_HOUR:02d}:00"
    offered = any(x[0] == "check_availability" and x[2].get("alternatives") for x in log)
    booked_busy = [b for b in _test_bookings()
                   if b["starts_at"] == busy and b["customer_name"] != BUSY_STUB]
    return [
        (offered, "інструмент повернув вільні альтернативи"),
        (not booked_busy, f"запис на зайняту годину НЕ створено (створено {len(booked_busy)})"),
    ]


def checks_c3(log) -> list[tuple[bool, str]]:
    created = [x for x in log if x[0] == "create_booking" and x[2].get("success")]
    return [(not created, f"запис у неробочий день НЕ створено (створено {len(created)})")]


def checks_c4(log) -> list[tuple[bool, str]]:
    msgs = _test_messages()
    out = [
        (len(msgs) == 1, f"рівно 1 запит на дзвінок у базі (є {len(msgs)})"),
        (not _test_bookings(), "запис НЕ створювався"),
    ]
    if msgs:
        out.append((len(msgs[0]["topic"]) > 5, f"суть питання збережено ({msgs[0]['topic']!r})"))
    return out


def _closed_case(service_phrase: str) -> tuple[str, str]:
    """A request for a time when the business is closed. Which one depends on the
    schedule: for a clinic that is Sunday, for a barbershop open every day it is
    late in the evening."""
    for ahead in range(1, 8):
        d = BUSINESS.today() + timedelta(days=ahead)
        if BUSINESS.booking.hours_for(d) is None:
            return (
                EV["turns"]["c3"].format(
                    service=service_phrase, day=say_day(d), time=say_time(11)
                ),
                EV["titles"]["c3_closed"],
            )
    if not BUSINESS.booking.is_round_clock(BUSINESS.today()):
        late = next_weekday(1, min_days=2)
        return (
            EV["turns"]["c3"].format(
                service=service_phrase, day=say_day(late), time=say_time(22)
            ),
            EV["titles"]["c3_late"],
        )
    # A 24/7 business is never closed, so aim at a different boundary: horizon.
    far = BUSINESS.today() + timedelta(days=BUSINESS.booking.horizon_days + 25)
    return (
        EV["turns"]["c3"].format(
            service=service_phrase, day=say_day(far), time=say_time(11)
        ),
        EV["titles"]["c3_far"],
    )


def _slot_scenarios(service_phrase: str, expect_key: str, busy_service: str,
                    busy_key: str, offtopic: str, extra: str = "") -> dict:
    """The same skeleton for any business that books appointments by time.

    `extra` is a line carrying whatever the industry intake requires (an address
    and so on): without it create_booking legitimately refuses and the scenario
    would be testing the wrong thing.
    """
    global BOOK_FIRST
    BOOK_FIRST = BUSINESS.booking.resource(expect_key)
    closed_turn, closed_title = _closed_case(service_phrase)
    return {
        "C1": {
            "title": EV["titles"]["c1"].format(
                service=service_phrase, day=say_day(S_DAY), time=say_time(S_HOUR)
            ),
            "turns": [
                EV["turns"]["c1"].format(
                    service=service_phrase, day=say_day(S_DAY), time=say_time(S_HOUR)
                ),
                _s_name_turn() + (f" {extra}" if extra else ""),
            ],
            "auto_confirm": True, "done": _booked, "checks": checks_c1,
        },
        "C2": {
            "title": EV["titles"]["c2"],
            "turns": [
                EV["turns"]["c2"].format(
                    busy_service=busy_service, day=say_day(S_DAY), time=say_time(S_BUSY_HOUR)
                ),
            ],
            "auto_confirm": False, "done": None, "checks": checks_c2,
            "prepare": busy_key,
        },
        "C3": {
            "title": closed_title,
            "turns": [closed_turn],
            "auto_confirm": False, "done": None, "checks": checks_c3,
        },
        "C4": {
            "title": EV["titles"]["c4"].format(offtopic=offtopic),
            "turns": [offtopic, _s_name_turn()],
            "auto_confirm": True, "done": _messaged, "checks": checks_c4,
        },
    }


# The scenario set is built for the ACTIVE business: _slot_scenarios reaches into
# BUSINESS (intake, hours, resources), so all five cannot be assembled at once.
_SLOT_VERTICALS = {
    "dental_us": dict(
        service_phrase="cleaning", expect_key="cleaning",
        busy_service="a filling", busy_key="filling",
        offtopic="Does my insurance cover a root canal?",
    ),
    "clinic": dict(
        service_phrase="професійну чистку", expect_key="cleaning", busy_service="видалити зуб", busy_key="extraction",
        offtopic="Скажіть, а ви приймаєте страховку і чи можна прийти з гострим болем зараз?",
    ),
    "barbershop": dict(
        service_phrase="стрижку", expect_key="haircut", busy_service="комплекс", busy_key="combo",
        offtopic="А можна записатися саме до Дениса, і чи є у вас подарункові сертифікати?",
    ),
    "cleaning": dict(
        service_phrase="прибирання двокімнатної", expect_key="regular_2",
        busy_service="прибрати трикімнатну", busy_key="regular_3",
        offtopic="А ви прибираєте офіси і скільки коштує помити вікна?",
        extra="Адреса — Хрещатик двадцять два, квартира п'ять.",
    ),
    "emergency": dict(
        service_phrase="сантехніка", expect_key="plumber",
        busy_service="відкрити двері, я загубив ключі", busy_key="locksmith",
        offtopic="У квартирі пахне газом, що робити?",
        extra="Адреса — Хрещатик двадцять два, квартира п'ять, тече труба під раковиною.",
    ),
}
SCENARIOS_BY_BUSINESS = {"hotel": HOTEL_SCENARIOS}
if BUSINESS.key in _SLOT_VERTICALS:
    SCENARIOS_BY_BUSINESS[BUSINESS.key] = _slot_scenarios(**_SLOT_VERTICALS[BUSINESS.key])


def _prepare_busy(resource_key: str) -> None:
    """Fill the hour C2's caller will ask for, as many times as there is capacity.

    The resource is given by key rather than by phrase: a conversational form
    might not be recognised by the alias list, and then the scenario would
    quietly be testing something else.
    """
    res = BUSINESS.booking.resource(resource_key)
    assert res, f"немає ресурсу {resource_key} у {BUSINESS.key}"
    for _ in range(res.capacity):
        # The industry's mandatory fields are supplied here too: without an
        # address a cleaning booking is legitimately refused, the stub would not
        # be created, and C2 would quietly be checking a free hour.
        out = crm.create_booking(
            resource=res.label,
            start=f"{S_DAY.isoformat()}T{S_BUSY_HOUR:02d}:00",
            customer_name=BUSY_STUB,
            # From the scenarios file: a Ukrainian number is invalid for a US
            # business, and the stub would fail before the dialogue started.
            phone=EV["phone_digits"],
            details={f.key: "для тесту" for f in BUSINESS.booking.intake},
        )
        assert out.get("success"), f"не вдалось забити годину: {out}"


def run_business(key: str, names: list[str]) -> dict[str, list[tuple[bool, str]]]:
    scenarios = SCENARIOS_BY_BUSINESS.get(key)
    if not scenarios:
        print(f"Для закладу {key} сценаріїв немає")
        return {}
    picked = [n for n in (names or list(scenarios)) if n in scenarios]
    if names and not picked:
        print(f"Для {key} немає сценаріїв {names}; доступні: {list(scenarios)}")
        return {}

    crm.init_db()
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tools = OpenAILLMAdapter().from_standard_tools(agent_tools.tools_schema())

    results = {}
    for name in picked:
        sc = scenarios[name]
        print(f"\n{'=' * 60}\n{key} · {name} — {sc['title']}\n{'=' * 60}")
        _cleanup()
        if sc.get("prepare"):
            _prepare_busy(sc["prepare"])
        log = run_dialog(client, tools, sc["turns"], sc["auto_confirm"], sc["done"])
        results[name] = sc["checks"](log)
    _cleanup()
    return results


def main() -> int:
    names = [n.upper() for n in _ARGV]
    if _BUSINESS_ARG == "all":
        # one process per business: the config is fixed at import time
        import subprocess

        failed = 0
        for key in ["hotel", *_SLOT_VERTICALS]:
            print(f"\n\n{'#' * 60}\n# {key}\n{'#' * 60}")
            failed += subprocess.call(
                [sys.executable, __file__, "--business", key, *names]) != 0
        return 1 if failed else 0

    results = run_business(BUSINESS.key, names)
    if not results:
        return 2

    print(f"\n{'=' * 60}\nПІДСУМОК · {BUSINESS.key}\n{'=' * 60}")
    scenarios = SCENARIOS_BY_BUSINESS[BUSINESS.key]
    failed = 0
    for name, checks in results.items():
        ok = all(c[0] for c in checks)
        failed += not ok
        print(f"{'✅' if ok else '❌'} {name} — {scenarios[name]['title']}")
        for passed, desc in checks:
            print(f"    {'✓' if passed else '✗ FAIL:'} {desc}")

    print(f"\n{len(results) - failed}/{len(results)} сценаріїв пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
