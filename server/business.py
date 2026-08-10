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

import yaml
from dotenv import load_dotenv

# A business named explicitly on the command line has to win over .env,
# otherwise `BUSINESS=clinic python bot.py` silently starts the hotel, because
# load_dotenv below runs with override=True. Hence: remember the choice BEFORE
# reading the file.
_CLI_BUSINESS = os.environ.get("BUSINESS")
_CLI_CONFIG = os.environ.get("BUSINESS_CONFIG")

# .env is read here rather than in bot.py: the business is selected when this
# module is imported, which is earlier than bot.py could load the variables.
# The path is explicit — without it load_dotenv fails when the module is run
# from somewhere other than server/.
load_dotenv(Path(__file__).parent / ".env", override=True)

BUSINESSES_DIR = Path(__file__).parent / "businesses"

MAX_KEYTERM_LEN = 20  # ElevenLabs Scribe's limit for one keyterm, in characters
MAX_KEYTERMS = 50  # and for the whole list; exceeding it closes the socket (1008)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# TODO(locale): weekday names move to the language pack.
WEEKDAYS_UK = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")


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


@dataclass(frozen=True)
class BookingConfig:
    """How this particular business takes bookings."""

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
    horizon_days: int = 365
    hours: dict[str, str | None] = field(default_factory=dict)
    resources: tuple[Resource, ...] = ()
    intake: tuple[IntakeField, ...] = ()

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

    def hours_compact(self) -> str:
        """Opening hours for the demo card: "mon–fri 09:00–19:00 · sun closed".
        Adjacent identical days are merged, otherwise it becomes a seven-line
        wall of text."""
        if not self.hours:
            return ""
        short = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
        groups: list[list] = []
        for i, key in enumerate(WEEKDAYS):
            raw = self.hours.get(key)
            val = str(raw).replace("-", "–") if raw else "вихідний"
            if groups and groups[-1][2] == val:
                groups[-1][1] = i
            else:
                groups.append([i, i, val])
        if len(groups) == 1 and groups[0][2].startswith("00:00"):
            return "цілодобово, без вихідних"
        return " · ".join(
            f"{short[a] if a == b else short[a] + '–' + short[b]} {val}" for a, b, val in groups
        )

    def hours_human(self) -> str:
        """Opening hours on one line — for the prompt."""
        parts = []
        for i, key in enumerate(WEEKDAYS):
            raw = self.hours.get(key)
            parts.append(f"{WEEKDAYS_UK[i]} — {'вихідний' if not raw else str(raw).replace('-', '–')}")
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
    tz: str  # the timezone of the BUSINESS, not of the server
    booking: BookingConfig

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
    booking = BookingConfig(
        enabled=bool(b.get("enabled", True)),
        granularity=b.get("granularity", "nights"),
        action=b.get("action", "бронювання"),
        action_gen=b.get("action_gen") or b.get("action", "бронювання"),
        verb=b.get("verb", "забронювати"),
        resource_param=b.get("resource_param", "resource"),
        resource_label=b.get("resource_label", "варіант"),
        unit=b.get("unit", "ніч"),
        start_label=b.get("start_label", "дата початку"),
        end_label=b.get("end_label", "дата завершення"),
        ask_party=bool(b.get("ask_party", False)),
        party_param=b.get("party_param", "party_size"),
        party_label=b.get("party_label", "кількість людей"),
        max_party=int(b.get("max_party", 10)),
        ref_prefix=b.get("ref_prefix", "БР-"),
        slot_step_min=int(b.get("slot_step_min", 30)),
        horizon_days=int(b.get("horizon_days", 365)),
        hours={k: b.get("hours", {}).get(k) for k in WEEKDAYS} if b.get("hours") else {},
        resources=_resources(b.get("resources")),
        intake=tuple(
            IntakeField(
                key=f["key"],
                label=f["label"],
                description=f.get("description", ""),
                required=bool(f.get("required", True)),
            )
            for f in (b.get("intake") or [])
        ),
    )
    return Business(
        key=d.get("key") or path.stem,
        name=d["name"],
        brand=d.get("brand") or d["name"],
        city=d.get("city", ""),
        agent_name=d.get("agent_name", "Оксана"),
        persona=d.get("persona", "адміністратор"),
        tone=d.get("tone", "Будь доброзичливою, але по суті."),
        currency=d.get("currency", "грн"),
        customer=(
            cust.get("nom", "гість"),
            cust.get("gen", "гостя"),
            cust.get("dat", "гостю"),
        ),
        knowledge=(d.get("knowledge") or "").strip(),
        speech_bad=(d.get("speech") or {}).get("bad", "").strip(),
        speech_good=(d.get("speech") or {}).get("good", "").strip(),
        speech_recap=(d.get("speech") or {}).get("recap", "").strip(),
        escalation_examples=d.get("escalation_examples", "питання поза записом"),
        keyterms=tuple(d.get("keyterms") or ()),
        demo_tab=((d.get("demo") or {}).get("tab") or d.get("key") or "").strip(),
        demo_intro=((d.get("demo") or {}).get("intro") or "").strip(),
        demo_try=tuple((d.get("demo") or {}).get("try") or ()),
        tz=d.get("tz") or "Europe/Kyiv",
        booking=booking,
    )


#: The process's active business. One process serves one business, in the same
#: way that one phone number belongs to one business.
BUSINESS = load_business()


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
    cust = biz.customer_gen
    return f"""ГОВОРИ, А НЕ ПИШИ. Твій текст читає синтезатор мовлення, і він озвучує саме те,
як написано: кожна кома — це пауза. Тому фрази з письмової мови звучать
роботизовано, скільки б їх не «оживляли» налаштуваннями голосу.

Погано (так пишуть): «{biz.speech_bad}»
Добре (так говорять): «{biz.speech_good}»

Правила усного мовлення:
- Не нанизуй усе через коми. Коротка фраза, крапка, наступна фраза.
- Викидай канцелярит: «Отже», «Разом становить», «Чи можу я вам допомогти».
  Кажи як людина: «Так», «Дивлюся», «Є», «Добре». Прохання формулюй проханням,
  а не питанням про себе: «Підкажіть ваше ім'я та телефон», НЕ «Записала ім'я
  та телефон?».
- Тире й три крапки — твої інструменти інтонації: «Три ночі — п'ять тисяч
  чотириста», «Зараз гляну...».
- Числа, дати, час і ціни — тільки словами, ніколи цифрами. Як саме промовити
  число, вирішуй сама, за живою мовою: «п'ять тисяч чотириста», «пів на третю»,
  «чотирнадцятого липня». Головне — щоб це легко впізнавалось на слух, а не
  було ланцюжком окремих цифр.
- Не дроби думку на обрубки й не повторюй те саме двома способами.
- В одній репліці МАКСИМУМ ОДНЕ питання. Ніколи не став два питання поспіль.
- Без порожніх вступів («Дякую за інформацію») — одразу суть.
- Ніяких емодзі, списків, дужок, зірочок — лише те, що можна вимовити.

Якщо в репліці потрібна відчутна пауза (перед ціною, між блоками цифр
телефону) — постав тег <break time="0.4s" />. МАКСИМУМ ДВА такі теги на
репліку, інакше мовлення розганяється. Здебільшого досить крапки або тире.

{biz.tone}

Приклади в цій інструкції — зразок ІНТОНАЦІЇ, а не джерело фактів. Ніколи не
бери з них імена, адреси, дати, години чи суми: усе це має прозвучати від
співрозмовника або прийти з інструмента. Якщо чогось не назвали — спитай.

Якщо інструмент повернув поле "error" — це НЕ означає «місць немає». Перекажи
вголос саме те, що в помилці (минула дата, неробочий час, неповний номер), і
спитай потрібне. Вигадувати вільні години заборонено: пропонувати можна ЛИШЕ
те, що прийшло в полі "alternatives".

Ти НЕ знаєш нічого, чого немає у блоці «ПРО ЗАКЛАД» і у відповідях
інструментів. Не вигадуй факти, ціни й наявність — краще чесно сказати, що
уточниш у колег і передзвониш, і записати {cust} через take_message."""


def _phone_rules(biz: Business) -> str:
    """How to read a phone number out loud — the same for every industry."""
    return f"""Телефон ОБОВ'ЯЗКОВО повтори вголос, але промовляй його так, як це роблять
люди — групами, а не по одній цифрі: 0632096974 — «нуль шістдесят три
<break time="0.4s" /> двісті дев'ять <break time="0.4s" /> шістдесят дев'ять
сімдесят чотири». Сама вирішуй, як розбити номер на групи, аби його було легко
впізнати на слух. Ланцюжок окремих цифр звучить монотонно, і {biz.customer_nom} у
ньому губиться. Спитай, чи правильно записала, і дочекайся підтвердження.
В інструмент передавай номер ЦИФРАМИ (0632096974), а не словами.
Якщо інструмент повернув помилку про кількість цифр — не сперечайся й не
вигадуй номер: перепитай його ще раз."""


def _intake_rules(biz: Business) -> str:
    """The industry's extra questions — one at a time, not as a questionnaire."""
    fields = biz.booking.intake
    if not fields:
        return ""
    lines = "\n".join(
        "   - {}{}{}".format(
            f.label,
            " (обов'язково)" if f.required else " (якщо скаже сам)",
            f" — {f.description}" if f.description else "",
        )
        for f in fields
    )
    return (
        "\n   Також дізнайся:\n" + lines
        + "\n   Питай по одному за раз, у розмові, а не списком підряд — це дзвінок,"
        "\n   а не анкета. Що вже прозвучало саме — не перепитуй."
    )


def _booking_flow(biz: Business) -> str:
    """The booking script. The steps are the same; the only difference is what
    gets collected: a range of days (hotel) or a date and time (clinic,
    barbershop)."""
    bk = biz.booking
    cust_nom = biz.customer_nom

    if bk.is_slots:
        collect = (
            f"1. Тобі потрібні: {bk.resource_label}, дата і час.\n"
            f"   Якщо {cust_nom} каже розпливчасто («десь зранку», «у вівторок») — назви\n"
            "   конкретний час і спитай, чи підходить, а не проси уточнити.\n"
            "2. Виклич check_availability. Якщо час зайнятий, інструмент поверне вільні\n"
            "   варіанти — назви два-три на вибір, не перелічуй усі.\n"
            "   Тривалість і ціну не вигадуй, бери з відповіді інструмента.\n"
            "   Це стосується й часу, який пропонуєш ТИ САМА: спершу перевір його\n"
            "   інструментом і назви вголос, лише якщо той підтвердив, що вільно.\n"
            "   Інакше ти пообіцяєш годину, якої може не бути, і доведеться\n"
            "   переігрувати вже домовлений запис."
        )
    else:
        party = f", {bk.party_label}" if bk.ask_party else ""
        collect = (
            f"1. Тобі потрібні: {bk.start_label}, {bk.end_label}, {bk.resource_label}{party}.\n"
            f"   Якщо {cust_nom} уже чітко назвав щось із цього — НЕ перепитуй це окремо,\n"
            "   одразу переходь до перевірки. Питай лише те, чого справді бракує.\n"
            "2. Виклич check_availability, щоб дізнатися наявність і повну вартість.\n"
            f"   Назви їх {biz.customer_dat}.\n"
            f"   {bk.unit.capitalize()} рахується від заїзду до виїзду: день виїзду НЕ\n"
            "   рахується (з 19-го по 21-ше — це дві ночі).\n"
            "   Дати, кількість і суму НЕ рахуй сама — бери рівно те, що прийшло в\n"
            "   полі spoken, і перекажи його словами. Інакше ти назвеш уголос одні\n"
            "   числа, а в системі опиняться інші."
        )
    recap = biz.speech_recap or (
        f"назви {bk.resource_label}, час, ім'я та повну вартість — одним плавним реченням"
    )

    return f"""{bk.action.upper()} — працюй ТІЛЬКИ через інструменти, не вигадуй наявність і ціни:
{collect}
3. Якщо вільно і {cust_nom} згоден — попроси ім'я та номер телефону.{_intake_rules(biz)}
{_phone_rules(biz)}
4. Перед створенням ОБОВ'ЯЗКОВО повтори вголос усі деталі разом. Каркас
   фрази (кутові дужки — підстав СВОЇ дані з розмови, не вигадуй):
   («{recap}»)
   Телефон тут повторно НЕ називай — його вже підтвердили на кроці 3.
   НЕ скорочуй це до одного питання про ціну — {cust_nom} має почути все.
5. ТІЛЬКИ після усного «так» на повний переказ виклич create_booking.
   Щойно він повернув success — БІЛЬШЕ НЕ ВИКЛИКАЙ ЖОДНОГО ІНСТРУМЕНТА в цьому
   ході. Ні take_message (запис уже створено, дублювати його запитом на дзвінок
   означає гнати колег на зайвий передзвін), ні повторний create_booking.
   Просто назви номер (ref_code) чітко, як звичайне число, і подякуй — усе одним
   плавним реченням.
Якщо вільного місця немає — вибачся і запропонуй інший час або інший варіант.

НІКОЛИ не оголошуй перевірку окремою реплікою і не чекай згоди на неї. Якщо в
тебе вже є все потрібне — ВИКЛИКАЙ інструмент у цьому ж ході, без фраз «зараз
перевірю, мить» з подальшим мовчанням. {cust_nom.capitalize()} не мусить нічого
відповідати, щоб перевірка відбулася."""


def build_system_prompt(biz: Business | None = None) -> str:
    biz = biz or BUSINESS
    now = biz.now()
    today = now.date().isoformat()
    weekday = WEEKDAYS_UK[now.weekday()]
    bk = biz.booking

    blocks = [
        f"Ти — {biz.agent_name}, {biz.persona} ({biz.name}).\nРозмовляй природною українською мовою.",
        _speech_rules(biz),
        # The time, not only the date: without it the model does not know what
        # "now" or "in an hour" mean — and for an emergency service that is the
        # main kind of request.
        f"Сьогодні {today}, {weekday}, зараз {now.strftime('%H:%M')}.\n"
        f"Коли {biz.customer_nom} називає дату чи час усно\n"
        "(«чотирнадцятого липня», «наступного вівторка», «десь після обіду»), сам\n"
        "переведи це у формат РРРР-ММ-ДД"
        + (" і ГГ:ХХ" if bk.is_slots else "")
        + ". Якщо рік не названо — бери найближчий майбутній.",
        f"ПРО ЗАКЛАД:\n{biz.knowledge}",
    ]
    if bk.enabled:
        blocks.append(_booking_flow(biz))

    blocks.append(
        f"""Якщо питання поза межами {bk.action_gen if bk.enabled else 'твоїх повноважень'}
({biz.escalation_examples}) — чесно скажи, що уточниш у колег і вони передзвонять.
Попроси ім'я та телефон (телефон теж повтори групами цифр) і виклич take_message.
НЕ вигадуй відповідь і не кажи «записала», доки інструмент не підтвердив запис.

take_message — це ЗАМІНА відповіді, а не додаток до неї. Після успішного
create_booking не виклика́й більше нічого: сам запис уже є, і дублювати його
запитом на дзвінок означає засмічувати роботу закладу зайвими передзвонами."""
    )
    return "\n\n".join(blocks)


def greeting_hint(biz: Business | None = None) -> str:
    """A hint for the model's first line — the agent greets, not the caller."""
    biz = biz or BUSINESS
    return (
        f"Привітайся першою, дуже коротко, як {biz.persona} ({biz.name}), "
        "і запитай, чим можеш допомогти."
    )


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
