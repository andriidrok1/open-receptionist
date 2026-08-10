"""simulate.py — a call simulator: one model plays the caller, ours answers.

Why this exists next to eval.py. There the scenarios are scripted: known lines,
known outcome. That catches regressions, but not what actually breaks on a real
call — when someone mumbles, changes their mind, names a date that has already
passed, or says nothing about their address until asked twice.

Here the caller is played by a separate model with its own character and its own
set of facts about itself. It does NOT know how our agent works, and behaves
like a person on the phone.

The dialogue is not the point; the checks are. They do not depend on the
scenario, and they catch the class of failures where the agent invented
something:

    - it passed on data the caller never gave (an address from a prompt example);
    - it offered a time no tool ever returned;
    - it booked a slot that never passed a successful check;
    - it filed a callback request on top of a completed booking;
    - it looped, asking the same question a third time.

Every one of those is a real bug caught on a live call. Now they are caught
without a voice, for pennies, in a minute.

Usage:
    python simulate.py                       # every persona for this business
    python simulate.py --business cleaning   # a different business
    python simulate.py mumbler past_date     # selected personas only
    python simulate.py --list                # list the personas
    python simulate.py --show                # print the dialogues in full

Requires OPENAI_API_KEY. One persona costs a few cents.

TODO(locale): the personas speak Ukrainian because the configs do. English
personas arrive with the English configs.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import yaml

_ARGV = sys.argv[1:]
if "--business" in _ARGV:
    i = _ARGV.index("--business")
    os.environ["BUSINESS"] = _ARGV[i + 1]
    del _ARGV[i : i + 2]
SHOW = "--show" in _ARGV
if SHOW:
    _ARGV.remove("--show")

# The simulation clears the database before every persona — and it used to do
# that in the REAL database of the business, demo data and all. The temporary
# database has to be chosen BEFORE importing crm, which fixes the path at import.
os.environ.setdefault(
    "CRM_DB", str(Path(tempfile.gettempdir()) / f"simulate_{os.environ.get('BUSINESS', 'hotel')}.db")
)

import agent_tools  # noqa: E402
import crm  # noqa: E402
from business import BUSINESS, build_system_prompt  # noqa: E402
from openai import OpenAI  # noqa: E402
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CALLER_MODEL = os.getenv("SIMULATE_CALLER_MODEL", "gpt-4o-mini")
MAX_TURNS = 14

# The same settings as bot.py. Without them the test exercises a different
# behaviour than production: parallel tool calls produce the "a callback request
# on top of a booking" bug, which does not exist in production because parallel
# calls are disabled there — so the test kept failing on a bug that was not real.
AGENT_LLM = dict(temperature=0.6, parallel_tool_calls=False)

BOOK = BUSINESS.booking
_digits = lambda s: re.sub(r"\D", "", s or "")  # noqa: E731


# ------------------------------- personas -------------------------------
#
# The callers live in scenarios/<lang>.yaml, next to the language they speak.
# Deliberately not in locales/: a language pack ships to whoever runs this, and
# test fixtures do not belong in it.

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def _load_scenarios(language: str) -> dict:
    path = SCENARIOS_DIR / f"{language}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))
        sys.exit(
            f"no caller scenarios for language {language!r} ({path}). "
            f"Available: {available}. Write them the way the language pack was "
            f"written — from scratch, not translated."
        )
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


SCEN = _load_scenarios(BUSINESS.language)
FACTS = SCEN["facts"]

BOOK = BUSINESS.booking
_service = BOOK.resources[0].label if BOOK.resources else "service"
_service_alt = BOOK.resources[-1].label if len(BOOK.resources) > 1 else _service


def _fill(text: str) -> str:
    """Substitute the business's services and dates relative to today.

    Dates are computed rather than written down: a scenario with a hard-coded
    month rots within a month, which this suite learned the hard way.
    """
    out = text.replace("{service_alt}", _service_alt).replace("{service}", _service)
    for m in set(re.findall(r"\{(plus|minus)_(\d+)\}", out)):
        sign = 1 if m[0] == "plus" else -1
        day = (date.today() + timedelta(days=sign * int(m[1]))).isoformat()
        out = out.replace(f"{{{m[0]}_{m[1]}}}", day)
    return out


PERSONAS = {
    key: {**body, "style": _fill(body["style"]), "goal": _fill(body["goal"])}
    for key, body in SCEN["personas"].items()
}


def caller_prompt(p: dict) -> str:
    return SCEN["caller_prompt"].format(
        business_name=BUSINESS.name, style=p["style"], goal=p["goal"], **FACTS
    )


# ------------------------------- the run -------------------------------

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
    """Run one call. Returns the transcript and the tool log."""
    agent_msgs = [{"role": "system", "content": build_system_prompt()}]
    caller_msgs = [{"role": "system", "content": caller_prompt(persona)}]
    transcript: list[tuple[str, str]] = []
    tool_log: list[tuple] = []

    # The agent speaks first, exactly as on a real call.
    agent_msgs.append({"role": "developer", "content":
                       SCEN["greeting_hint"].format(persona=BUSINESS.persona)})

    for _ in range(MAX_TURNS):
        # --- the agent's turn (may contain tool calls) ---
        for _ in range(6):
            resp = client.chat.completions.create(
                model=MODEL, messages=agent_msgs, tools=tools, tool_choice="auto",
                **AGENT_LLM)
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

        # --- the caller's turn ---
        cres = client.chat.completions.create(model=CALLER_MODEL, messages=caller_msgs)
        caller_text = (cres.choices[0].message.content or "").strip()
        caller_msgs.append({"role": "assistant", "content": caller_text})
        done = SCEN["end_marker"] in caller_text
        caller_text = caller_text.replace(SCEN["end_marker"], "").strip()
        if caller_text:
            transcript.append(("caller", caller_text))
            agent_msgs.append({"role": "user", "content": caller_text})
        if done:
            break

    return {"transcript": transcript, "tools": tool_log}


# ----------------------------- the checks -----------------------------
#
# Each returns a list of violations (an empty list means all is well).

def _caller_said(transcript) -> str:
    return " ".join(t for who, t in transcript if who == "caller").lower()


def check_no_fabricated_data(run) -> list[str]:
    """The agent must not pass tools anything the caller never said."""
    said = _caller_said(run["transcript"])
    said_digits = _digits(said)
    bad = []
    for name, args, res in run["tools"]:
        # A rejected attempt means the guard worked, not that the database is
        # wrong — so only successful calls are inspected.
        if name not in ("create_booking", "take_message") or not res.get("success"):
            continue
        phone = _digits(args.get("phone"))
        # Compare on the national part: the tool stores E.164, the caller says
        # a local number. Slicing one digit off only worked for a one-digit
        # country code (+1) and would have produced a false "never given" for
        # +380.
        national = phone[-9:] if len(phone) > 9 else phone
        if phone and phone not in said_digits and national not in said_digits:
            bad.append(f"{name}: phone {phone} was never given by the caller")
        for field in [f.key for f in BOOK.intake] + ["customer_name"]:
            val = (args.get(field) or "").strip()
            if not val:
                continue
            # One meaningful word from the value having been spoken is enough.
            words = [w for w in re.findall(r"[\w']{4,}", val.lower())]
            if words and not any(w[:5] in said for w in words):
                bad.append(f"{name}: {field} = {val!r} — that never came up in the conversation")
    return bad


def _slot_key(args: dict) -> tuple:
    return (args.get(BOOK.resource_param), args.get("date"), args.get("time"),
            args.get("start_date"), args.get("end_date"))


def check_booked_over_refusal(run) -> list[str]:
    """An attempt to book a slot the tool has just reported as unavailable."""
    refused = set()
    bad = []
    for name, args, res in run["tools"]:
        key = _slot_key(args)
        if name == "check_availability":
            (refused.discard if res.get("available") else refused.add)(key)
        elif name == "create_booking" and key in refused:
            bad.append(
                f"tried to book {key} after the check said it was taken"
            )
    return bad


def warn_unverified_slot(run) -> list[str]:
    """A booking on a slot that was never checked.

    Not an error: crm re-checks availability before the insert, so no double
    booking can happen. But the agent is naming a time and a price it never
    confirmed — and if the slot turns out to be taken, an already agreed booking
    has to be renegotiated in front of the caller.
    """
    ok = set()
    warn = []
    for name, args, res in run["tools"]:
        key = _slot_key(args)
        if name == "check_availability" and res.get("available"):
            ok.add(key)
        elif name == "create_booking" and res.get("success") and key not in ok:
            warn.append(f"time {key[1] or key[3]} {key[2] or ''} was named without being checked")
    return warn


def check_booking_was_checked_at_all(run) -> list[str]:
    """A booking was made without the agent ever asking whether it was free.

    Found on the first English call: asked for "next Tuesday" the model resolved
    the date to a Saturday, announced that ten was taken — nothing had been
    checked — and went straight to booking. The date arithmetic being shaky was
    known; that it also derails tool discipline was not.

    This is deliberately language-independent: it counts tool calls rather than
    reading what was said, so it works the same for every pack.
    """
    booked = any(n == "create_booking" and r.get("success") for n, _, r in run["tools"])
    checked = any(n == "check_availability" for n, _, _ in run["tools"])
    if booked and not checked:
        return ["created a booking without a single check_availability call"]
    return []


def check_no_message_after_booking(run) -> list[str]:
    booked = False
    bad = []
    for name, _, res in run["tools"]:
        if name == "create_booking" and res.get("success"):
            booked = True
        elif name == "take_message" and booked and res.get("success"):
            bad.append("take_message after a successful booking — a pointless callback")
    return bad


def check_no_loop(run) -> list[str]:
    """The agent must not ask the same question three times."""
    seen: dict[str, int] = {}
    for who, text in run["transcript"]:
        if who != "agent":
            continue
        for q in re.findall(r"[^.!?]*\?", text):
            k = re.sub(r"\W+", " ", q.lower()).strip()[:40]
            if len(k) < 10:
                continue
            seen[k] = seen.get(k, 0) + 1
    return [f"the same question asked {n} times: {k!r}" for k, n in seen.items() if n >= 3]


def check_no_success_claim_on_error(run) -> list[str]:
    """If the booking failed, the agent must not claim it succeeded.

    The only check here that has to know the language: it reads what was said
    rather than which tools were called. The words come from the scenarios file.
    """
    ok = any(n == "create_booking" and r.get("success") for n, _, r in run["tools"])
    tried = any(n == "create_booking" for n, _, _ in run["tools"])
    if ok or not tried:
        return []
    # The trigger words are language-specific, so they live with the scenarios.
    # Hard-coding Ukrainian ones here meant this check quietly did nothing at
    # all for an English business — which looks like coverage and is not.
    words = tuple(w.lower() for w in SCEN["claim_words"])
    last = " ".join(t for who, t in run["transcript"][-4:] if who == "agent").lower()
    return ["the agent says it is booked, but no booking exists"] if any(w in last for w in words) else []


CHECKS = [
    check_no_fabricated_data,
    check_booked_over_refusal,
    check_booking_was_checked_at_all,
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
        print(f"Unknown personas: {unknown}. Available: {list(PERSONAS)}")
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
            if not SHOW:  # a violation makes no sense without the dialogue
                for who, text in run["transcript"]:
                    print(f"     {'🤖' if who == 'agent' else '🧑'} {text}")
        elif not warnings:
            print(f"  ✅ clean ({len(run['tools'])} tool calls)")

    _cleanup()
    print(f"\n{'=' * 66}\nSummary: {total_bad} violations across {len(names)} calls")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
