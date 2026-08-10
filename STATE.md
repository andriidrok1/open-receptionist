# Project state

Read this before doing anything. It says what exists, what is actually verified,
what is knowingly broken, and what to work on next.

**Keep it current.** When something here stops being true, fix the line in the
same commit that made it untrue. A state file that lies is worse than none.

Last updated: 2026-08-10 · branch `universal-receptionist`

---

## What this is

A self-hosted voice receptionist for small businesses, heading for open-source
publication as a kit for people starting an AI agency. One YAML file per
business, one per language, no per-minute platform fee between the operator and
their providers.

Strategy and milestones live in the Obsidian vault:
`10-Products/Voice Receptionist/` — the umbrella document is
"Open Source (план и скоуп)". Architecture criticism is in
"Архитектура (разбор)".

---

## How to run it

```bash
cd server
../.venv/bin/python -m pytest tests/ -q      # 100 tests, ~1s, no network, no keys
BUSINESS=clinic ../.venv/bin/python bot.py   # one bot          → :7860/client
../.venv/bin/python run_demo.py              # six bots + showcase → :8080
BUSINESS=dental_us ../.venv/bin/python dashboard.py   # owner panel → :8000

# These cost money (OpenAI, a few cents per scenario):
../.venv/bin/python eval.py --business dental_us   # scripted dialogues
../.venv/bin/python simulate.py --business clinic  # an LLM plays the caller
```

`BUSINESS` picks the config, `CRM_DB` the database. Both are captured **before**
`.env` is read — see Traps.

---

## What works, and how it was verified

| Area | State | Verified by |
|---|---|---|
| Voice pipeline (browser) | works | live calls ending in a booking |
| Six businesses, two languages | works | showcase renders all six correctly |
| Language layer | works | a third language was assembled and run with zero Python changes |
| Booking, capacity, hours, horizon | works | 100 deterministic tests |
| Concurrency guard | works | 10 threads racing for 3 places; mutation-tested |
| Phone numbers per country | works | UA and US, ~30 formats |
| Owner dashboard | works, both languages | browser check, no Cyrillic on the English one |
| eval / simulate | work in both languages | dental_us 4/4, Ukrainian unchanged |
| Telephony | **UNVERIFIED** | code wired, never tested on a real number |

---

## Known gaps, in the order they hurt

1. **A booking has no lifecycle — only insert.** No cancel, no reschedule, no
   idempotency. "Move it to tomorrow" is probably the second most common call
   and is impossible. A repeated tool call creates a second booking.
   *In progress:* the `status` column and its migration are done, the operations
   are not (see Uncommitted below).
2. **Telephony never tested on a real number.** Blocks publication: the README
   promises a phone. Needs a Twilio account — a human step, deliberately
   deferred.
3. **Cost per minute unmeasured.** The headline claim of the whole pitch. Can be
   measured now without telephony: a browser call spends the same STT/LLM/TTS,
   and the phone leg is one extra line item later.
4. **Conversations are not stored.** Only which tools were called. The owner's
   first question is "what did it say to them?" and there is no answer.
5. **No CI, no clean-machine install check.** The tests need neither network nor
   keys, so CI is an hour's work. The README's install instructions have never
   been followed on a machine that did not already have everything.
6. **English voice is a placeholder.** Picked from the premade list, not by ear.
   The Ukrainian one went through `voice_lab.py` and a blind comparison.
7. **Secrets and cost are global.** One `.env` for every business, so an agency
   cannot separate clients' keys or bills.
8. **`call_tool` is synchronous** inside an async pipeline. Microseconds with
   SQLite; the first HTTP integration will stall the audio. One hour to fix.

Deliberately **not** doing now: `Business` as an object instead of a module
global (weeks, no user-visible change), dialogue state machine (a different
product). Both are argued in the architecture document.

---

## Traps — every one of these cost real time

**Language and speech are two layers.** The text layer was made multilingual
while `bot.py` still handed `Language.UK` to recognition and synthesis. An
English business could not take a call at all. Anything reaching a speech
service must come from the pack.

**State is computed at import.** `BUSINESS`, `L`, `DB_PATH`, `BOOK`, tool
schemas — all fixed when the module is imported. Consequences: testing another
business needs a subprocess; `.env` outranked explicitly-set variables twice
(`BUSINESS`, then `CRM_DB`) because `load_dotenv(override=True)` runs first.
Both are captured before dotenv now. Do not add a third.

**md5 of a prompt is useless** — it carries the current time and changes every
minute. Compare prompts by generating both versions at the same moment and
diffing line by line.

**Verify a translation or a refactor by comparing ASTs** with docstrings
stripped (`scratchpad/astcmp.py` pattern): comments never reach the tree, so
what remains is executable code. It caught a real slip.

**Errors are lines the agent reads out loud**, not log entries. They must say
what to ask next, and they must never carry `available: false` — that made the
agent tell someone with a burst pipe there were no plumbers while two crews were
free.

**Anything deterministic belongs in code, not the prompt.** Phone format, real
names, past times, closing hours, capacity, duration. Every rule moved from the
prompt into `crm` stopped being violated.

**A test whose docstring names a bug it cannot catch is worse than none.** Three
were found here: the overbooking test ran sequentially, the spoken-summary test
matched substrings inside a sentence full of numbers, and the Cyrillic check
skipped every list. Mutate the code and watch the test fail before believing it.

**Caller facts in tests must appear in no prompt.** Otherwise "the agent heard
it" cannot be told from "the agent copied it from an example". Enforced by
`tests/test_scenarios.py`.

**Model runs vary.** A full English eval gave 3/4 once and 4/4 on the re-run,
same code. One green run is not proof.

---

## Layout

```
server/
  bot.py            pipeline, voice, transport. Knows no industry
  business.py       business YAML → config + system prompt
  locale_pack.py    the language contract (69 required keys, checked on load)
  crm.py            bookings, validation, availability (SQLite)
  agent_tools.py    tool schemas from the config; one execution path for all
  speech_text.py    pause markup, whole-reply aggregation
  demo.py/run_demo  showcase: six bots on 7860+, page on 8080
  dashboard.py      owner panel
  eval.py           scripted dialogues (needs a key)
  simulate.py       an LLM plays the caller (needs a key)
  voice_lab.py      voice selection lab
  locales/          uk.yaml, en.yaml — the language
  businesses/       six configs — the business
  scenarios/        uk.yaml, en.yaml — test callers and eval data
  tests/            100 deterministic tests
docs/README.uk.md   the older, more detailed Ukrainian documentation
```

The split that matters: **language** (locales) / **business** (businesses) /
**code**. Adding a language is one file in `locales/` plus one in `scenarios/`,
and no Python.

---

## Uncommitted right now

`server/crm.py` — the `status` column, its migration for older databases, and
`_occupied` ignoring cancelled bookings. The cancel and reschedule operations
themselves are not written yet. Either finish them or revert; do not leave a
column nothing uses.
