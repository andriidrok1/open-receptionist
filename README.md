# open-receptionist

A self-hosted voice receptionist for small businesses. Someone calls, talks to it
normally, and it answers questions about the business, books the appointment, and
honestly hands off anything it doesn't know.

One YAML file describes one business. The core knows nothing about any industry:

```bash
BUSINESS=clinic     python bot.py   # dental practice   — appointments at a set time
BUSINESS=hotel      python bot.py   # hotel             — bookings by the night
BUSINESS=cleaning   python bot.py   # cleaning service  — appointment + address
BUSINESS=emergency  python bot.py   # emergency plumber — 24/7, no closing time
BUSINESS=barbershop python bot.py   # barbershop        — appointments at a set time
```

A new business is a new file in `server/businesses/`. No code is touched.

Built on [Pipecat](https://github.com/pipecat-ai/pipecat). You bring your own
speech-to-text, LLM and text-to-speech keys and pay those providers directly.
Nothing sits in between charging you per minute.

---

## Status — please read before you clone

This is honest, not modest. Here is exactly where it stands today.

**Works, and has been used in real conversations**

- Full voice conversation in the browser (WebRTC), in Ukrainian
- Five business configs: hotel, dental, cleaning, emergency plumber, barbershop
- Booking with availability, capacity, working hours, and required intake fields
  (a cleaning visit will not be created without an address)
- SQLite store, owner dashboard, and a demo page you can hand to a business owner
- Two test harnesses: scripted dialogues and an LLM playing the caller

**In progress, and not there yet**

- **English.** Today the agent speaks Ukrainian. The design for the locale layer
  is done; the language is not just a setting, it is baked into the prompt, the
  tool error strings the model reads, phone formats, month names and 12h/24h time.
- **Real phone numbers.** Twilio works: a real US number was connected through
  the standard webhook + Media Streams flow, and a wire-accurate simulator
  (`server/phone_sim.py`) exercises the full 8 kHz telephony path — see
  [docs/telephony.md](docs/telephony.md). Still to prove on a live line: the
  REST hang-up and a full spoken call over carrier audio (the simulated call
  passed end to end). Telnyx / Plivo share the code path and are untested.
- **Cost per minute.** Not measured yet. When it is, the number and the method to
  reproduce it go here.

**Not planned**

Multi-tenancy, billing, hosted service, CRM/PMS integrations, an owner mobile app.

If you need something you can put in front of paying customers this week, this is
not it yet. If you want to see how a production voice pipeline actually behaves —
and to own the stack instead of renting it — read on.

---

## Who this is for

**People building AI receptionists for local businesses.** Configure a client in
one file, run one process per client, keep the margin instead of paying a platform
a cut of every minute forever.

**A business that wants to own the thing answering its phone.** No vendor deciding
what happens to your call recordings or what next year's pricing is.

**Engineers.** Everything below the fold is about the parts that are not in any
tutorial — see [The hard parts](#the-hard-parts-already-solved).

---

## How it works

```
mic → Silero VAD → ElevenLabs Scribe STT → OpenAI LLM → ElevenLabs TTS → speaker
                                              ↓
                                    tools: check availability,
                                    create booking, take message
                                              ↓
                                        SQLite + dashboard
```

| file | what it owns |
|---|---|
| `server/bot.py` | pipeline, voice, transport (browser / phone). Nothing industry-specific |
| `server/business.py` | reads the business YAML, assembles the system prompt |
| `server/businesses/*.yaml` | **the entire business lives here**: persona, knowledge, services, prices, hours |
| `server/agent_tools.py` | the agent's tools, built from the config — one code path for the bot and the tests |
| `server/crm.py` | bookings, messages, availability, validation |
| `server/dashboard.py` | the owner's panel |
| `server/demo.py`, `run_demo.py` | the showcase page you show a business owner |
| `server/eval.py`, `server/simulate.py` | the test harnesses |

### Two time models cover almost every receptionist

- **`nights`** — booking a range of days. The customer sets the bounds (check-in,
  check-out), price is per night × number of nights. Hotels, apartments, rentals.
- **`slots`** — an appointment at a specific time. The service sets the bounds
  (duration comes from the config), price is flat. Clinics, barbershops, garages,
  tutors.

Both reduce to the same thing: a resource is occupied over `[start, end)` and can
be occupied at most `capacity` times at once. One SQL query for overlap, one code
path — not two parallel branches of business logic.

A business with no bookings at all (`booking.enabled: false`) still gets answers
and callback requests. That is a working receptionist too.

---

## Quickstart

Requires Python 3.11+, an OpenAI key, and an ElevenLabs key for the voice.

```bash
git clone <this repo> && cd open-receptionist
python -m venv .venv && source .venv/bin/activate
pip install -r server/requirements.txt

cd server
cp .env.example .env      # add OPENAI_API_KEY and ELEVENLABS_API_KEY
python bot.py
```

Open <http://localhost:7860/client>, hit **Connect**, allow the microphone, talk.

To see all five businesses side by side with a demo page:

```bash
cd server && python run_demo.py     # → http://localhost:8080
```

That page is the one you show a business owner: their prices and hours, a call
button, a live transcript, a feed of what the agent actually did, and — the part
that sells it — the booking that just appeared in the database.

---

## Describing a business

This is the whole point of the project, so here is a real config, trimmed. It is
in Ukrainian, which is also the proof that this is not an English-only system.

```yaml
name: стоматологія «Дентал Сіті»       # how the agent says the business name out loud
agent_name: Оксана                      # the agent's name
persona: адміністратор клініки          # "receptionist at a dental practice"

speech:                                 # the same thought written vs. spoken
  bad:  Отже, консультація лікаря-стоматолога у вівторок…
  good: Так, у вівторок о десятій вільно. Консультація — триста гривень. Записую?

knowledge: |                            # everything the agent is allowed to know
  Послуги й ціни: Консультація — 300 грн (30 хв)…

booking:
  granularity: slots
  hours: {mon: "09:00-19:00", sat: "10:00-15:00", sun: null}
  resources:
    - key: consult
      label: Консультація
      aliases: [консультацію, огляд, подивитися зуб]   # how people actually say it
      price: 300
      duration_min: 30
      capacity: 2                       # how many dentists cover this service at once
```

`aliases` are not decoration: a caller says "come look at my tooth", not
"consultation". `speech.bad` / `speech.good` are not decoration either — the model
copies the example it is given, so the example has to be in this business's own
voice, not a generic one.

---

## The hard parts, already solved

None of this is in any tutorial. Every item cost a live debugging session.

**The bot heard itself.** With manual commit, everything the microphone picks up
between commits is attributed to the caller — and a commit only happens when the
caller stops talking. Echo from the speakers (quiet enough that local VAD ignores
it, loud enough that Scribe transcribes it perfectly) ended up in the context as
the caller's words. Measured: the bot's own line came back as the human's. Fixed
by muting the STT input while the bot speaks. **On a phone line, where there is no
browser echo cancellation, this is not an edge case — it is guaranteed.**

**Interim transcripts interrupted the bot mid-sentence.** The default turn-start
strategy treats any partial transcript as the user starting to talk, which
broadcasts an interruption. With manual commit no final transcript follows, so the
turn hangs until a 5-second watchdog closes it empty and the LLM never runs. It
sounds exactly like a dropped connection.

**A slow first byte from TTS dropped the entire reply.** If the synthesiser takes
longer than the default 3 seconds to return audio, the whole utterance is
discarded silently — and the service stays paused until the next interruption. Not
a stutter: silence for the rest of the turn.

**One ElevenLabs keyterm longer than 20 characters closes the whole socket.** Not
ignored — closed, with code 1008. It looks like "speech recognition is completely
broken": audio flows, VAD fires, no transcripts ever arrive. Service names are
pulled from the config, so two of five businesses were dead and three were fine.

**Sentence-by-sentence synthesis sounds like three announcements.** Pipecat
synthesises each sentence as it arrives, which is the lowest latency but builds
intonation inside one sentence — the price lands flat and the closing question
doesn't sound like a question. `WholeReplyAggregator` holds the reply and
synthesises it in one piece. It costs about 0.7 s of latency.

**Anything deterministic belongs in code, not in the prompt.** Phone format, real
names instead of placeholders, times in the past, closing hours, overbooking — the
model will violate every one of these politely and confidently. The tools now
return errors the model can read out loud, and a `spoken` string it just recites,
because when it computed the numbers itself it said one date and stored another.

---

## Testing

```bash
python eval.py --business all   # scripted dialogues against the real model
python simulate.py --show       # an LLM plays the caller: mumbles, changes its mind,
                                # gives a wrong number, asks for a date in the past
```

`simulate.py` checks things that do not depend on the scenario: did the agent
invent data the caller never gave, promise a slot the tools never confirmed, claim
it booked something when it didn't, or ask the same question three times.

Both need an OpenAI key. A test layer that runs without any network is the next
thing to build — see the roadmap.

---

## Roadmap

1. **Phone spike** — one real call from a real handset, to find out what breaks
2. **Locale layer** — English as a first-class second language
3. **Phone, properly** — a number you can hand to a business, with the setup documented
4. **Repository hygiene** — comments translated, CI, contribution guide
5. **Numbers** — measured cost per minute, and how speech recognition holds up on a
   narrowband phone line

---

## Support

This is built in the open and issues and pull requests are welcome.

I do not take on setup, integration or consulting work, paid or unpaid. If the
README is not enough to get you running, that is a bug in the README — please open
an issue and it gets fixed for everyone.

---

## License

MIT — see [LICENSE](LICENSE). `server/bot.py` began life as a Pipecat example and
keeps its original BSD-2-Clause notice from Daily.

Ukrainian documentation, which is currently more detailed:
[docs/README.uk.md](docs/README.uk.md).
