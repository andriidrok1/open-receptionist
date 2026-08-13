"""business.py — the description of the business this call is being served for.

The agent's core (voice, pipeline, pauses, telephony, handling phone numbers)
has nothing to do with any industry — it is the same for a hotel, a dental
practice and a barbershop. Everything industry-specific lives in a YAML file
under `businesses/`: the persona, what the agent knows about the place, the list
of resources with prices, the opening hours. A new business is a new YAML; the
code is not touched.

The active business is chosen by environment variable:
    BUSINESS=clinic            → businesses/clinic.yaml
    BUSINESS_CONFIG=/path.yaml → any file (a specific client's config, kept
                                 outside the repository)

Two time models cover almost every receptionist:
    nights — a booking over a range of days (hotel, apartments, rentals).
    slots  — an appointment at a specific time of a fixed length (clinic,
             barbershop, garage, tutor).
Both reduce to the same thing: a resource is occupied over [start, end).

Note on language: the functions that build the system prompt return text the
model reads, so it stays in the business's language until the locale layer
lands — those strings are the future contents of locales/uk.yaml. Everything
only humans read is in English.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import phonenumbers
import yaml
from dotenv import load_dotenv

from locale_pack import DEFAULT_LANGUAGE, load_locale

# Kept for the readers below; the environment now wins on its own (see the note
# on load_dotenv), so these are a record of what was asked for rather than a
# rescue from .env.
_CLI_BUSINESS = os.environ.get("BUSINESS")
_CLI_CONFIG = os.environ.get("BUSINESS_CONFIG")
CLI_CRM_DB = os.environ.get("CRM_DB")

# .env is read here rather than in bot.py: the business is selected when this
# module is imported, which is earlier than bot.py could load the variables.
# The path is explicit — without it load_dotenv fails when the module is run
# from somewhere other than server/.
#
# override=False, i.e. .env supplies DEFAULTS and anything already exported
# wins. It used to be the other way round, and that quietly cost real work three
# times: `BUSINESS=clinic python bot.py` started the hotel; a CRM_DB set by the
# simulator was ignored, pointing a database-wiping run at the demo data; and
# `ELEVENLABS_TTS_MODEL=... python run_demo.py` ran the old model while
# reporting nothing. The first two were patched one variable at a time, by
# reading them before this line — which fixes those two and leaves the trap open
# for every variable added afterwards.
#
# The cost of the swap is that a stale exported variable is no longer corrected
# by editing .env. That is the ordinary convention, and it is the one the
# project's own instructions assume every time they say `BUSINESS=clinic`.
load_dotenv(Path(__file__).parent / ".env", override=False)

BUSINESSES_DIR = Path(__file__).parent / "businesses"

MAX_KEYTERM_LEN = 20  # ElevenLabs Scribe's limit for one keyterm, in characters
MAX_KEYTERMS = 50  # and for the whole list; exceeding it closes the socket (1008)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")



@dataclass(frozen=True)
class Resource:
    """The thing being booked: a room type, a service, a chair, a service bay.

    capacity is how many of them can be occupied AT THE SAME TIME (rooms in a
    hotel, chairs in a barbershop, dentists covering this service). That is what
    holds overbooking back.
    """

    key: str
    label: str
    aliases: tuple[str, ...] = ()
    price: int = 0
    capacity: int = 1
    duration_min: int = 0  # slots only
    note: str = ""


@dataclass(frozen=True)
class IntakeField:
    """What else to ask about, besides the time and the name.

    A visit-your-place service (cleaning, an emergency plumber, someone coming
    to measure up) is meaningless without an address: the appointment exists but
    nobody knows where to go. The set of fields differs per industry, so it
    lives in the YAML and from there reaches both the prompt and the tool schema.
    """

    key: str
    label: str
    description: str = ""
    required: bool = True
    #: An extra check to run on the answer, beyond "it is not empty".
    #: Currently only "address", which looks the answer up in a gazetteer —
    #: see address.py for why a spoken street name cannot be trusted as heard.
    validate: str = ""
    #: "string" (the default) or "bool". A bool reaches the model as a typed
    #: true/false tool argument instead of free text — "my kid is fifteen but
    #: very responsible" cannot be compared against a rule, true/false can.
    type: str = "string"
    #: A booking whose answer equals this is REFUSED in code. The prompt says
    #: the same rule in words so the agent declines early and politely; this
    #: makes declining unavoidable. None means no rule.
    refuse_if: object = None
    #: What the refusal error says. Empty falls back to a generic line from
    #: the language pack.
    refusal: str = ""
    #: Plausible invented answers, only ever read by seed_demo.py — a seeded
    #: dashboard card should read like a real job, and "what's wrong" is
    #: vertical-specific text no generic fixture can supply.
    examples: tuple = ()


@dataclass(frozen=True)
class BookingConfig:
    """How this particular business takes bookings."""

    # The language this business speaks. Kept here as well as on Business so
    # that hours can be rendered per business: the demo showcase loads five
    # configs in one process, and they will not all speak the same language.
    language: str = DEFAULT_LANGUAGE
    enabled: bool = True
    granularity: str = "nights"  # nights | slots
    action: str = "бронювання"  # "booking" / "appointment" — used in the prompt
    action_gen: str = "бронювання"  # genitive form: "outside of booking"
    verb: str = "забронювати"
    resource_param: str = "resource"  # argument name in the tool schema
    resource_label: str = "варіант"
    unit: str = "ніч"  # the billing unit for nights
    start_label: str = "дата початку"  # nights: what to call the bounds
    end_label: str = "дата завершення"
    ask_party: bool = False
    party_param: str = "party_size"
    party_label: str = "кількість людей"
    max_party: int = 10
    ref_prefix: str = "БР-"
    slot_step_min: int = 30
    #: Minutes of arrival uncertainty QUOTED to the caller. Zero means exact
    #: times (a dental chair is booked to the minute). Trades quote windows —
    #: "between nine and ten" — because traffic makes the minute a fiction; the
    #: slot underneath stays exact for the capacity math, only the words
    #: change. See crm._spoken.
    arrival_window_min: int = 0
    #: Whether the price must be stated early, unprompted. Drives the runtime
    #: state reminder (hangup.call_state_line): on the first live human call
    #: the agent went the whole three minutes without ever saying the fee,
    #: with the instruction sitting in the knowledge block the entire time.
    announce_price: bool = False
    horizon_days: int = 365
    hours: dict[str, str | None] = field(default_factory=dict)
    resources: tuple[Resource, ...] = ()
    intake: tuple[IntakeField, ...] = ()
    #: Where this business will actually go: city names, postcodes, or both.
    #: Empty means no limit. Checked against the CANONICAL address rather than
    #: against what the caller said, so it only bites once an address has been
    #: verified — a refusal has to rest on a fact, not on a mishearing.
    #:
    #: This is the first of the disqualification rules practitioners say matter
    #: most: the calls that waste a truck are the ones outside the area, outside
    #: the trade, or with nobody old enough to open the door. All of them are
    #: fixed facts, which is exactly what a config can hold and a model should
    #: not be asked to remember.
    service_area: tuple[str, ...] = ()

    @property
    def is_slots(self) -> bool:
        return self.granularity == "slots"

    def resource(self, key: str) -> Resource | None:
        for r in self.resources:
            if r.key == key:
                return r
        return None

    def normalize_resource(self, raw: str | None) -> str | None:
        """A spoken name (in any case or wording) → the canonical key."""
        if not raw:
            return None
        needle = raw.strip().lower()
        for r in self.resources:
            variants = {r.key.lower(), r.label.lower(), *(a.lower() for a in r.aliases)}
            if needle in variants:
                return r.key
        # Loose match: "a standard room", "to have my teeth cleaned"
        for r in self.resources:
            for v in (r.label.lower(), *(a.lower() for a in r.aliases)):
                if v and (v in needle or needle in v):
                    return r.key
        return None

    def hours_for(self, day: date) -> tuple[time, time] | None:
        """Opening hours for a specific day. None means closed."""
        raw = self.hours.get(WEEKDAYS[day.weekday()])
        if not raw:
            return None
        start_s, _, end_s = str(raw).partition("-")
        return time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())

    def is_round_clock(self, day: date) -> bool:
        """Whether the day is 24/7. Needed by the emergency plumber: a call at
        23:30 must not run into "that will not fit before we close" — the middle
        of the night is exactly when such calls happen."""
        h = self.hours_for(day)
        return bool(h) and h[0] == time(0, 0) and h[1] >= time(23, 59)

    @property
    def _pack(self):
        """This business's own language pack (cached in locale_pack)."""
        return load_locale(self.language)

    def _hours_text(self, raw) -> str:
        """One day's hours, spoken the way this language writes a clock.

        The YAML always stores 24-hour ranges because that is unambiguous to
        edit; how they are SHOWN is a property of the language. A US practice
        showing "08:00-17:00" is a small wrongness that adds up.
        """
        start_s, _, end_s = str(raw).partition("-")
        return f"{self._pack.time(start_s.strip())}–{self._pack.time(end_s.strip())}"

    def hours_compact(self) -> str:
        """Opening hours for the demo card: "Mon–Fri 8 AM–5 PM · Sun closed".
        Adjacent identical days are merged, otherwise it becomes a seven-line
        wall of text."""
        if not self.hours:
            return ""
        short = tuple(self._pack.raw("calendar.weekdays_short"))
        groups: list[list] = []
        for i, key in enumerate(WEEKDAYS):
            raw = self.hours.get(key)
            val = self._hours_text(raw) if raw else self._pack.text("calendar.closed")
            if groups and groups[-1][2] == val:
                groups[-1][1] = i
            else:
                groups.append([i, i, val])
        if len(groups) == 1 and str(self.hours.get("mon", "")).startswith("00:00"):
            return self._pack.text("calendar.round_clock")
        return " · ".join(
            f"{short[a] if a == b else short[a] + '–' + short[b]} {val}" for a, b, val in groups
        )

    def hours_human(self) -> str:
        """Opening hours on one line — for the prompt."""
        parts = []
        for i, key in enumerate(WEEKDAYS):
            raw = self.hours.get(key)
            closed = self._pack.text("calendar.closed")
            names = self._pack.raw("calendar.weekdays")
            parts.append(f"{names[i]} — {closed if not raw else self._hours_text(raw)}")
        return "; ".join(parts)


@dataclass(frozen=True)
class Business:
    key: str
    name: str  # how the agent says the name of the place out loud
    brand: str  # how the dashboard is labelled
    city: str
    agent_name: str
    persona: str  # "the receptionist at a dental practice"
    tone: str
    currency: str
    customer: tuple[str, str, str]  # guest / patient / customer, in three cases
    knowledge: str
    speech_bad: str
    speech_good: str
    speech_recap: str  # how the summary before confirmation should sound
    escalation_examples: str
    keyterms: tuple[str, ...]
    demo_tab: str  # the short industry name on the demo tab
    demo_intro: str  # what the demo page says about this business
    demo_try: tuple[str, ...]  # the "try saying" suggestions
    language: str  # which language pack the agent speaks (locales/<lang>.yaml)
    voice_id: str  # ElevenLabs voice; empty means the language pack's default
    country: str  # ISO 3166-1 alpha-2, used to parse phone numbers
    tz: str  # the timezone of the BUSINESS, not of the server
    booking: BookingConfig

    @property
    def pack(self):
        """This business's own language pack.

        Public and used everywhere rather than the module-level L: the prompt
        builders take a `biz` argument, and honouring the language of the
        process instead of the language of that business produced a Ukrainian
        prompt wrapped around an English config — which is exactly what
        `python business.py dental_us` used to print.
        """
        return load_locale(self.language)

    def now(self) -> datetime:
        """Now, by the clock of the business.

        Time used to be taken bare (`datetime.now()`), i.e. by the host's clock.
        On a laptop that might be an hour off; in Docker or on Railway it is UTC,
        three hours behind Kyiv. The consequence: between midnight and three in
        the morning the prompt carried YESTERDAY'S date, and booking "for today"
        was rejected as being in the past. For a 24/7 emergency service those
        are peak hours.
        """
        return datetime.now(ZoneInfo(self.tz)).replace(tzinfo=None)

    def today(self) -> date:
        return self.now().date()

    @property
    def customer_nom(self) -> str:
        return self.customer[0]

    @property
    def customer_gen(self) -> str:
        return self.customer[1]

    @property
    def customer_dat(self) -> str:
        return self.customer[2]

    def stt_keyterms(self) -> list[str]:
        """Words the STT must recognise confidently: service names and domain
        vocabulary.

        ElevenLabs Scribe accepts a keyterm of at most 20 characters (characters,
        not bytes) and at most 50 of them. On a violation it CLOSES THE WHOLE
        CONNECTION (1008 invalid_request) rather than ignoring the extra. It
        looks like "speech recognition is completely broken": audio flows, the
        VAD fires, and no transcript ever arrives — and pipecat 1.5 does not even
        show the reason, because it has no handler for the invalid_request
        message type and it sinks into DEBUG.

        So both limits are enforced here instead of being left to the server:
        long labels are split into words, and the list is truncated to
        MAX_KEYTERMS. Truncating here matters because `raw.split()` multiplies
        the number of terms — on a large config the limit of 50 is reachable.
        """
        out: list[str] = []
        for raw in [r.label for r in self.booking.resources] + list(self.keyterms):
            raw = (raw or "").strip()
            if not raw:
                continue
            if len(raw) <= MAX_KEYTERM_LEN:
                out.append(raw)
            else:
                out.extend(w for w in raw.split() if 3 < len(w) <= MAX_KEYTERM_LEN)
        seen, uniq = set(), []
        for t in out:
            if t.lower() not in seen:
                seen.add(t.lower())
                uniq.append(t)
        return uniq[:MAX_KEYTERMS]


# --------------------------- loading the YAML ---------------------------


def _resources(raw: list[dict] | None) -> tuple[Resource, ...]:
    out = []
    for r in raw or []:
        out.append(
            Resource(
                key=r["key"],
                label=r["label"],
                aliases=tuple(r.get("aliases") or ()),
                price=int(r.get("price") or 0),
                capacity=int(r.get("capacity") or 1),
                duration_min=int(r.get("duration_min") or 0),
                note=r.get("note") or "",
            )
        )
    return tuple(out)


def load_business(key: str | None = None) -> Business:
    """Read a business YAML. key=None → BUSINESS_CONFIG / BUSINESS / 'hotel'."""
    path_env = _CLI_CONFIG or os.getenv("BUSINESS_CONFIG")
    if key is None and path_env:
        path = Path(path_env)
    else:
        key = key or _CLI_BUSINESS or os.getenv("BUSINESS", "hotel")
        path = BUSINESSES_DIR / f"{key}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in BUSINESSES_DIR.glob("*.yaml"))
        raise FileNotFoundError(f"no business config at {path}; available: {available}")

    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    b = d.get("booking") or {}
    cust = d.get("customer") or {}
    # Defaults come from the language, not from this file. A config that omits
    # `unit:` must inherit its own language's word for it, not Ukrainian.
    lang = d.get("language") or DEFAULT_LANGUAGE
    pack = load_locale(lang)
    dflt = pack.raw("defaults.booking")
    biz_dflt = pack.raw("defaults.business")
    booking = BookingConfig(
        language=lang,
        enabled=bool(b.get("enabled", True)),
        granularity=b.get("granularity", "nights"),
        action=b.get("action", dflt["action"]),
        action_gen=b.get("action_gen") or b.get("action", "бронювання"),
        verb=b.get("verb", dflt["verb"]),
        resource_param=b.get("resource_param", "resource"),
        resource_label=b.get("resource_label", dflt["resource_label"]),
        unit=b.get("unit", dflt["unit"]),
        start_label=b.get("start_label", dflt["start_label"]),
        end_label=b.get("end_label", dflt["end_label"]),
        ask_party=bool(b.get("ask_party", False)),
        party_param=b.get("party_param", "party_size"),
        party_label=b.get("party_label", dflt["party_label"]),
        max_party=int(b.get("max_party", 10)),
        ref_prefix=b.get("ref_prefix", dflt["ref_prefix"]),
        slot_step_min=int(b.get("slot_step_min", 30)),
        arrival_window_min=int(b.get("arrival_window_min", 0) or 0),
        announce_price=bool(b.get("announce_price", False)),
        horizon_days=int(b.get("horizon_days", 365)),
        hours={k: b.get("hours", {}).get(k) for k in WEEKDAYS} if b.get("hours") else {},
        resources=_resources(b.get("resources")),
        intake=tuple(
            IntakeField(
                key=f["key"],
                label=f["label"],
                description=f.get("description", ""),
                required=bool(f.get("required", True)),
                validate=(f.get("validate") or "").strip().lower(),
                type=(f.get("type") or "string").strip().lower(),
                refuse_if=f.get("refuse_if", None),
                refusal=(f.get("refusal") or "").strip(),
                examples=tuple(str(x) for x in (f.get("examples") or ())),
            )
            for f in (b.get("intake") or [])
        ),
        service_area=tuple(str(x) for x in (b.get("service_area") or ())),
    )
    # A unit the pack has no forms for degrades silently into "2 ніч" being read
    # out loud, so it is refused here instead.
    if booking.granularity == "nights" and booking.unit not in pack.raw("numbers.units"):
        raise ValueError(
            f"{path.name}: booking.unit {booking.unit!r} has no plural forms in the "
            f"{lang!r} language pack. Available: {sorted(pack.raw('numbers.units'))}"
        )
    return Business(
        key=d.get("key") or path.stem,
        name=d["name"],
        brand=d.get("brand") or d["name"],
        city=d.get("city", ""),
        agent_name=d.get("agent_name", biz_dflt["agent_name"]),
        persona=d.get("persona", biz_dflt["persona"]),
        tone=d.get("tone", biz_dflt["tone"]),
        currency=d.get("currency", biz_dflt["currency"]),
        customer=(
            cust.get("nom", biz_dflt["customer"]["nom"]),
            cust.get("gen", biz_dflt["customer"]["gen"]),
            cust.get("dat", biz_dflt["customer"]["dat"]),
        ),
        knowledge=(d.get("knowledge") or "").strip(),
        speech_bad=(d.get("speech") or {}).get("bad", "").strip(),
        speech_good=(d.get("speech") or {}).get("good", "").strip(),
        speech_recap=(d.get("speech") or {}).get("recap", "").strip(),
        escalation_examples=d.get("escalation_examples", biz_dflt["escalation_examples"]),
        keyterms=tuple(d.get("keyterms") or ()),
        demo_tab=((d.get("demo") or {}).get("tab") or d.get("key") or "").strip(),
        demo_intro=((d.get("demo") or {}).get("intro") or "").strip(),
        demo_try=tuple((d.get("demo") or {}).get("try") or ()),
        language=lang,
        voice_id=(d.get("voice_id") or "").strip(),
        # Falls back to the language pack's default: a Ukrainian-speaking
        # business is almost certainly in Ukraine, and a config that forgets the
        # field should still parse phone numbers correctly.
        country=d.get("country") or pack.text("settings.default_country"),
        tz=d.get("tz") or pack.text("settings.default_tz"),
        booking=booking,
    )


#: The process's active business. One process serves one business, in the same
#: way that one phone number belongs to one business.
BUSINESS = load_business()

#: The active language pack, named by the business config. Loaded after the
#: business for that reason, and used from function bodies only.
L = load_locale(BUSINESS.language)
WEEKDAY_NAMES = tuple(L.raw("calendar.weekdays"))


# ----------------------------- the system prompt -----------------------------
#
# Everything below returns text that goes to the model. It is written in the
# business's language on purpose, and it is the future content of the language
# pack (locales/<lang>.yaml). The docstrings are in English because they are for
# whoever maintains this file.


def _speech_rules(biz: Business) -> str:
    """How to talk. This is the strongest lever on sounding natural — stronger
    than any TTS setting, because the synthesiser voices punctuation, not
    meaning."""
    return biz.pack.text(
        "prompt.speech_rules",
        speech_bad=biz.speech_bad,
        speech_good=biz.speech_good,
        tone=biz.tone,
        customer_gen=biz.customer_gen,
    )


def _local_phone_digits(country: str) -> int | None:
    """How many digits a local number has here, per libphonenumber.

    From the same library that later rejects the number, so the prompt and the
    store cannot disagree — and taken from a real example number for the
    country rather than written into a table that would go stale as numbering
    plans change.
    """
    try:
        example = phonenumbers.example_number(country)
        return len(phonenumbers.national_significant_number(example)) if example else None
    except Exception:  # noqa: BLE001 — an unknown country is not worth a crash
        return None


def _phone_rules(biz: Business) -> str:
    """How to read a phone number out loud — the same for every industry.

    The digit count is in there because of a real call: the caller gave nine
    digits, the agent read the whole booking back to them, they said yes, and
    only then did the store reject the number — so the recap had to be done
    twice and the agent looked like it had not been listening. The store is
    still the authority; this just moves the catch to before the summary rather
    than after it.
    """
    digits = _local_phone_digits(biz.country)
    return biz.pack.text(
        "prompt.phone_rules",
        customer_nom=biz.customer_nom,
        phone_digits=(
            biz.pack.text("prompt.phone_digits", n=digits, country=biz.country) if digits else ""
        ),
    )


def _intake_rules(biz: Business) -> str:
    """The industry's extra questions — one at a time, not as a questionnaire."""
    fields = biz.booking.intake
    if not fields:
        return ""
    lines = "\n".join(
        "   - {}{}{}".format(
            f.label,
            biz.pack.text(
                "prompt.intake.item_required" if f.required else "prompt.intake.item_optional"
            ),
            f" — {f.description}" if f.description else "",
        )
        for f in fields
    )
    return biz.pack.text("prompt.intake.header") + lines + biz.pack.text("prompt.intake.footer")


def _booking_flow(biz: Business) -> str:
    """The booking script. The steps are the same; the only difference is what
    gets collected: a range of days (hotel) or a date and time (clinic,
    barbershop)."""
    bk = biz.booking

    if bk.is_slots:
        collect = biz.pack.text(
            "prompt.booking.collect_slots",
            resource_label=bk.resource_label,
            customer_nom=biz.customer_nom,
        )
    else:
        collect = biz.pack.text(
            "prompt.booking.collect_nights",
            start_label=bk.start_label,
            end_label=bk.end_label,
            resource_label=bk.resource_label,
            party=biz.pack.text("prompt.booking.party_suffix", party_label=bk.party_label)
            if bk.ask_party
            else "",
            customer_nom=biz.customer_nom,
            customer_dat=biz.customer_dat,
            unit_cap=bk.unit.capitalize(),
        )

    recap = biz.speech_recap or biz.pack.text(
        "prompt.booking.recap_fallback", resource_label=bk.resource_label
    )

    return biz.pack.text(
        "prompt.booking.body",
        changes=biz.pack.text("prompt.booking.changes"),
        action_upper=bk.action.upper(),
        collect=collect,
        customer_nom=biz.customer_nom,
        customer_nom_cap=biz.customer_nom.capitalize(),
        intake_rules=_intake_rules(biz),
        phone_rules=_phone_rules(biz),
        recap=recap,
    )


#: How far ahead the prompt spells the calendar out. Two weeks covers "next
#: Monday", "a week from Thursday" and "the end of the month" for anything
#: booked by phone, at a cost of fourteen short lines.
CALENDAR_DAYS = 14


def _calendar_lines(biz: Business, now: datetime) -> str:
    """The next fortnight as weekday-and-date pairs.

    Telling the model the date and expecting it to work out which day "Monday"
    is turns out not to work: asked for Monday on a Tuesday, it answered
    "Monday, August fourteenth" — a Friday — and booked it. Nothing downstream
    could catch that, because the weekday never reaches the tools; only the date
    does, and that date was a real, bookable, open slot.

    Language models are unreliable at date arithmetic and reliable at reading a
    list, so this hands them the list. It needs no new keys in the pack: the
    weekday names are already there, and an ISO date is the same in every
    language.
    """
    names = biz.pack.raw("calendar.weekdays")
    days = []
    for offset in range(CALENDAR_DAYS):
        day = (now + timedelta(days=offset)).date()
        days.append(f"{names[day.weekday()]} {day.isoformat()}")
    return "\n".join(days)


def build_system_prompt(biz: Business | None = None) -> str:
    biz = biz or BUSINESS
    now = biz.now()
    bk = biz.booking

    blocks = [
        biz.pack.text("prompt.identity", agent_name=biz.agent_name, persona=biz.persona, name=biz.name),
        _speech_rules(biz),
        # The time, not only the date: without it the model does not know what
        # "now" or "in an hour" mean — and for an emergency service that is the
        # main kind of request.
        biz.pack.text(
            "prompt.date_hint",
            today=now.date().isoformat(),
            weekday=biz.pack.raw("calendar.weekdays")[now.weekday()],
            # Through the pack, not strftime: the whole point of a 12-hour
            # setting is that the model is not told "it's 14:30 right now" and
            # then asked to say times the way people do.
            time=biz.pack.time(now),
            customer_nom=biz.customer_nom,
            slots_suffix=biz.pack.text("prompt.date_hint_slots_suffix") if bk.is_slots else "",
            calendar=_calendar_lines(biz, now),
        ),
        biz.pack.text("prompt.about", knowledge=biz.knowledge),
    ]
    if bk.enabled:
        blocks.append(_booking_flow(biz))

    blocks.append(
        biz.pack.text(
            "prompt.escalation",
            scope=bk.action_gen if bk.enabled else biz.pack.text("prompt.escalation_scope_fallback"),
            escalation_examples=biz.escalation_examples,
        )
    )
    return "\n\n".join(blocks)


def greeting_hint(biz: Business | None = None) -> str:
    """A hint for the model's first line — the agent greets, not the caller."""
    biz = biz or BUSINESS
    return biz.pack.text("prompt.greeting_hint", persona=biz.persona, name=biz.name)


def slot_grid(day: date, biz: Business | None = None) -> list[datetime]:
    """Every possible appointment start on this day, per the opening hours."""
    biz = biz or BUSINESS
    hours = biz.booking.hours_for(day)
    if not hours:
        return []
    open_t, close_t = hours
    step = timedelta(minutes=biz.booking.slot_step_min)
    cur = datetime.combine(day, open_t)
    end = datetime.combine(day, close_t)
    out = []
    while cur < end:
        out.append(cur)
        cur += step
    return out


if __name__ == "__main__":  # quick check: python business.py [key]
    import sys

    b = load_business(sys.argv[1]) if len(sys.argv) > 1 else BUSINESS
    print(build_system_prompt(b))
