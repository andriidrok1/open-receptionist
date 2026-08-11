# Project state

Read this before doing anything. It says what exists, what is actually verified,
what is knowingly broken, and what to work on next.

**Keep it current.** When something here stops being true, fix the line in the
same commit that made it untrue. A state file that lies is worse than none.

Last updated: 2026-08-11 · branch `universal-receptionist`

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
../.venv/bin/python -m pytest tests/ -q      # 117 tests, ~1s, no network, no keys
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
| Booking, capacity, hours, horizon | works | 117 deterministic tests |
| Cancel / reschedule / find by phone | works | tests + a live English call |
| Call transcripts + cost/latency metrics | works | live calls; stored per call |
| Semantic end-of-turn | works, effect unproven | classifier right on 16 cases; paired A/B inconclusive |
| Alternatives on every schedule error | works | agent offers only what the tool returned |
| Concurrency guard | works | 10 threads racing for 3 places; mutation-tested |
| Phone numbers per country | works | UA and US, ~30 formats |
| Owner dashboard | works, both languages | browser check, no Cyrillic on the English one |
| eval / simulate | work in both languages | dental_us 4/4, Ukrainian unchanged |
| Telephony | **UNVERIFIED** | code wired, never tested on a real number |

---

## Known gaps, in the order they hurt

1. **Telephony never tested on a real number.** Blocks publication: the README
   promises a phone. Needs a Twilio account — a human step, deliberately
   deferred.
2. **Audio recording.** Transcripts are stored now; audio is not. Legally a
   separate act (several US states need both parties' consent), and pipecat's
   AudioBufferProcessor stayed silent in-pipeline — parked rather than solved.
3. **No CI, no clean-machine install check.** The tests need neither network nor
   keys, so CI is an hour's work. The README's install instructions have never
   been followed on a machine that did not already have everything.
4. **English voice is a placeholder.** Picked from the premade list, not by ear.
   The Ukrainian one went through `voice_lab.py` and a blind comparison.
5. **Secrets and cost are global.** One `.env` for every business, so an agency
   cannot separate clients' keys or bills.
6. **`call_tool` is synchronous** inside an async pipeline. Microseconds with
   SQLite; the first HTTP integration will stall the audio. One hour to fix.
7. **The agent can promise an hour it never checked.** Measured: 4 calls out of
   5 when the time is outside opening hours. Nothing gets double-booked — the
   store refuses — but a promise withdrawn a minute later sounds bad. Only fixed
   properly by turn state deciding which tools exist.
8. **The caller waits ~3s**, of which ~1.1s is our timer and ~1.8s is the model
   writing. A speculative start (run the model at 0.4s of silence, cancel if
   they resume) is the only way meaningfully below two seconds; pipecat has the
   cancellation machinery but no ready-made version.
9. **No idle handling.** If the caller goes quiet the bot waits forever. On a
   phone line that is a dead end, and telephony is next.

Deliberately **not** doing now: `Business` as an object instead of a module
global (weeks, no user-visible change), dialogue state machine (a different
product). Both are argued in the architecture document.

---

## Measured numbers

Browser calls, Ukrainian cleaning config, 2026-08-11. Re-measure rather than
trust these: rates change and so does the machine.

| | |
|---|---|
| Caller waits | ~3.1s median (1.1s our turn timer, ~1.8s the model writing, 0.13s synthesis) |
| Recognition | 0.64s, hidden under the turn timer since it runs in parallel |
| Cost | ~$0.04/min, about 60% of it synthesis |
| Whole-reply synthesis | costs nothing measurable: 3.13s against 3.43s per sentence |

Every call now stores its own numbers, so this table can be rebuilt from real
traffic rather than from probes.

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

**The browser probe's speech file has a fixed date in it** ("11 серпня"), so on
that day of the month it asks for a time in the past and measures something else
entirely. It read as an agent bug for a while. Regenerate the WAV, or read the
transcript before believing what a live probe seems to show.

**A prompt rule loses to the model's own confidence.** Told a time outside
opening hours, the model answered on its own 4 times out of 5 rather than
calling the tool, and strengthening the tool description changed nothing —
still 4 of 5. What did work was giving it something true to say: errors now
carry real free times, and when it does call the tool it offers exactly those.

**Latency measurements need pairing and bucketing.** Two mistakes made in one
sitting: comparing runs an hour apart caught the machine drifting rather than
the change (the same code gave 3.1s and 5.7s), and averaging turns that call a
tool with turns that do not hides everything, because a tool turn is two
inference passes and several seconds slower by construction. Alternate the
configurations back to back, and read the two buckets separately.

**The caller's wait is ~3s and only ~1.1s of it is ours.** The rest is the model
writing its reply. No amount of cleverness in turn-taking can win more than
about a second — worth knowing before optimising it.

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
  call_metrics.py   what a call costs, in seconds and in money
  recording.py      the transcript of a call
  turn_taking.py    deciding whether the caller finished or just paused
  pricing.yaml      provider rates, with the date they were checked
  demo.py/run_demo  showcase: six bots on 7860+, page on 8080
  dashboard.py      owner panel
  eval.py           scripted dialogues (needs a key)
  simulate.py       an LLM plays the caller (needs a key)
  voice_lab.py      voice selection lab
  locales/          uk.yaml, en.yaml — the language
  businesses/       six configs — the business
  scenarios/        uk.yaml, en.yaml — test callers and eval data
  tests/            117 deterministic tests
docs/README.uk.md   the older, more detailed Ukrainian documentation
```

The split that matters: **language** (locales) / **business** (businesses) /
**code**. Adding a language is one file in `locales/` plus one in `scenarios/`,
and no Python.

---

## Uncommitted right now

Nothing.
