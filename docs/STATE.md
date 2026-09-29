# Project state

Read this before doing anything. It says what exists, what is actually verified,
what is knowingly broken, and what to work on next.

**Keep it current.** When something here stops being true, fix the line in the
same commit that made it untrue. A state file that lies is worse than none.

Last updated: 2026-09-29 · branch `observability`

---

## What this is

A self-hosted voice receptionist for small businesses, published as a kit for
people selling and installing AI receptionists. One YAML file per business,
one per language, no per-minute platform fee between the operator and their
providers.

---

## How to run it

```bash
cd server
../.venv/bin/python -m pytest tests/ -q      # 446 tests, ~9s, no network, no keys
BUSINESS=clinic ../.venv/bin/python bot.py   # one bot          → :7860/client
../.venv/bin/python run_demo.py              # all bots + showcase → :8080
BUSINESS=dental_us ../.venv/bin/python dashboard.py   # owner panel → :8000
../.venv/bin/python phone_sim.py             # a Twilio-shaped caller (needs a bot
                                             # started with -t twilio)
../.venv/bin/python call_report.py --calls   # latency / tools / errors / cost over
                                             # every traced call in logs/calls/

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
| Seven businesses, two languages | works | showcase renders all correctly |
| Language layer | works | a third language was assembled and run with zero Python changes |
| Booking, capacity, hours, horizon | works | 446 deterministic tests |
| Cancel / reschedule / find by phone | works | tests + a live English call |
| Call transcripts + cost/latency metrics | works | live calls; stored per call |
| Per-call trace: latency by stage, tools, errors, cost → `logs/calls/*.jsonl` + `call_report.py` | works in-pipeline, not yet on a live call | 19 tests, one of which runs a real Pipecat pipeline with a fake cascade and reads the file back; the harness-generated report is in the README. A live call has not been traced yet (no keys on the machine that wrote it) |
| Semantic end-of-turn | works, effect unproven | classifier right on 16 cases; paired A/B inconclusive |
| Alternatives on every schedule error | works | agent offers only what the tool returned |
| Concurrency guard | works | 10 threads racing for 3 places; mutation-tested |
| Phone numbers per country | works | UA and US, ~30 formats |
| Owner dashboard | works, both languages | React inbox + calls + duty schedule; auth tests, playwright screenshots |
| Dashboard auth | works | password sessions, localhost-only fallback, rotation revokes cookies — all tested |
| Duty schedule | works | owner picks their own hours; bot checks per call; safe-degradation tested |
| Showcase page language picker | works | English by default, switches without a reload, choice survives F5 |
| Dates the agent books | fixed | the prompt now lists 14 days with their weekdays; mutation-tested |
| eval / simulate | work in both languages | dental_us 4/4, Ukrainian unchanged |
| Telephony (Twilio) | works up to the carrier | a real US number reached the stack; `phone_sim.py` passes the full 8 kHz path end to end; REST hang-up needs one live call |
| Docker install | works | compose smoke-tested: both containers, shared volume |
| Cost per minute | measured | $0.09/min avg over 12 calls / 20 min; per-call breakdown stored |
| Telegram push to the owner | works up to Telegram's API | delivery/stamp/pairing semantics fully tested with an injected sender; one live round-trip with a real bot token still owed |
| CI | works | ruff + the full suite on every push; first clean-machine run green (1m09s) |

---

## Known gaps, in the order they hurt

1. **One live carrier call still owed.** Everything up to the carrier is
   verified; the REST hang-up and narrowband audio on a real handset are not.
   The number and webhook flow are documented in docs/telephony.md.
2. **One live Telegram round-trip still owed.** The push pipeline is tested
   end to end with an injected sender; the real Telegram API has not been hit
   with a live bot token yet.
3. **Audio recording.** Transcripts are stored now; audio is not. Legally a
   separate act (several US states need both parties' consent), and pipecat's
   AudioBufferProcessor stayed silent in-pipeline — parked rather than solved.
4. **English voice is still Rachel**, the oldest premade ElevenLabs voice, and
   it sounds like it. Ten candidates are synthesised and waiting to be picked
   by ear: `python voice_lab.py --lang en serve --port 8090`.
5. **Secrets and cost are global.** One `.env` for every business, so an agency
   cannot separate clients' keys or bills — mitigated by one-install-per-client
   (docs/install.md), unsolved for many clients on one box.
6. **`call_tool` is synchronous** inside an async pipeline. Microseconds with
   SQLite; the first HTTP integration will stall the audio. One hour to fix.
7. **The agent can promise an hour it never checked.** Measured: 4 calls out of
   5 when the time is outside opening hours. Nothing gets double-booked — the
   store refuses — but a promise withdrawn a minute later sounds bad. Only fixed
   properly by turn state deciding which tools exist.
8. **The caller waits ~3s**, of which ~1.1s is our timer and ~1.8s is the model
   writing. A speculative start (run the model at 0.4s of silence, cancel if
   they resume) is the only way meaningfully below two seconds; pipecat has the
   cancellation machinery but no ready-made version. The trace now splits
   every turn into timer / STT / first token / tools / TTS, so the next
   attempt is measured per stage instead of by stopwatch.
9. **`CallMetrics` sorts every turn into the "plain" bucket on pipecat 1.12.**
   It sits at the tail and keys the tool bucket off `FunctionCallInProgressFrame`,
   which the assistant aggregator no longer forwards (requirements pin
   `>=1.5`, 1.12 installs). The stored `wait_median_tool_s` is therefore
   `None` on a fresh install. The trace does not have this problem (it is an
   observer); either port `CallMetrics` to the same hook or read the buckets
   from the trace.

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
| Cost | $0.09/min averaged over the whole recorded corpus (12 calls, 20 min, mixed verticals); ~80% synthesis. Short Ukrainian cleaning calls ran $0.03–0.05, English emergency/dental $0.09–0.13 |
| Whole-reply synthesis | costs nothing measurable: 3.13s against 3.43s per sentence |

Every call now stores its own numbers, so this table can be rebuilt from real
traffic rather than from probes — and since the trace, per stage:
`python call_report.py` prints p50 / p95 of the caller's wait split into the
end-of-turn timer, the STT's final transcript, the model's first token and
total, tool time and the synthesiser's first byte, over every call in
`logs/calls/`.

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

**The model cannot do calendar arithmetic, and fails silently at it.** Asked for
Monday on a Tuesday, it answered "Monday, August fourteenth" — a Friday — and
booked it. Nothing downstream can catch this: only the date reaches the tools,
and it was a real, open slot. The prompt now lists fourteen days with their
weekdays and forbids counting. Assume the same for anything else arithmetical.

**A processor at the tail of the pipeline sees no LLM, tool or text frames.**
The assistant aggregator consumes `LLMFullResponseStart/End`, `LLMTextFrame`,
`FunctionCallInProgress/Result` and does not push them on (pipecat 1.12), and
the TTS consumes `LLMTextFrame` before that. Anything that needs the moments of
a turn — first token, tool duration — cannot be a processor at one position; it
is either three processors or a `BaseObserver` on the worker, which is told
about every frame at its first push. `observability.py` is the observer.

**bot.py is the least tested file in the project and the one every call uses.**
Building the pipeline needs a live transport, keys and a microphone, so nothing
executes it. A name used thirty lines before it was assigned killed every call
at connect while 117 tests stayed green. `tests/test_static.py` runs a linter
over the modules for exactly that class of bug — it reads code instead of
running it.

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
  observability.py  the per-call trace: every turn's moments → logs/calls/<id>.jsonl
  call_report.py    a folder of traces → one table (p50/p95 by stage, tools, errors, cost)
  recording.py      the transcript of a call
  turn_taking.py    deciding whether the caller finished or just paused
  pricing.yaml      provider rates, with the date they were checked
  demo.py/run_demo  showcase: all bots on 7860+, page on 8080
  dashboard.py      owner panel: auth + API (UI in static/, React vendored)
  phone_sim.py      a Twilio-shaped caller over a real websocket
  guards.py         makes the agent's worst habits unreachable (invented refs, guessed numbers)
  hangup.py         ending the call from both sides of the silence; the claim guard runs here
  eval.py           scripted dialogues (needs a key)
  simulate.py       an LLM plays the caller (needs a key)
  voice_lab.py      voice selection lab
  locales/          uk.yaml, en.yaml — the language
  businesses/       seven configs + _template.yaml — the business
  scenarios/        uk.yaml, en.yaml — test callers and eval data
  logs/calls/       one JSONL per traced call (gitignored)
  tests/            446 deterministic tests
docs/               telephony.md, install.md, new-business.md, README.uk.md,
                    STATE.md (this file), building-on-pipecat.md
```

The split that matters: **language** (locales) / **business** (businesses) /
**code**. Adding a language is one file in `locales/` plus one in `scenarios/`,
and no Python.

---

## Uncommitted right now

Nothing — the tree is clean at every commit. Ten English voices are
synthesised in `voice_lab_out/` (untracked) and waiting for someone to listen
and choose.
