"""business.py — опис закладу, який агент обслуговує в цьому дзвінку.

Ядро агента (голос, пайплайн, паузи, телефонія, робота з телефонами гостей) до
галузі стосунку не має — воно однакове для готелю, стоматології й барбершопа.
Усе галузеве живе в YAML у `businesses/`: персона, знання про заклад, перелік
ресурсів із цінами, графік роботи. Новий заклад = новий YAML, код не чіпаємо.

Активний заклад обирається змінною оточення:
    BUSINESS=clinic            → businesses/clinic.yaml
    BUSINESS_CONFIG=/шлях.yaml → довільний файл (для клієнта поза репозиторієм)

Дві моделі часу покривають майже весь ресепшн:
    nights — бронь на інтервал діб (готель, апартаменти, оренда).
    slots  — запис на конкретний час фіксованої тривалості (клініка, барбершоп,
             СТО, репетитор).
Обидві зводяться до одного: ресурс зайнятий на проміжку [start, end).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Явно вказаний у команді заклад мусить бути сильнішим за .env: інакше
# `BUSINESS=clinic python bot.py` мовчки підіймає готель, бо load_dotenv нижче
# йде з override=True. Тому запам'ятовуємо вибір ДО читання файлу.
_CLI_BUSINESS = os.environ.get("BUSINESS")
_CLI_CONFIG = os.environ.get("BUSINESS_CONFIG")

# .env читаємо тут, а не в bot.py: вибір закладу відбувається на імпорті цього
# модуля — тобто раніше, ніж bot.py встиг би підвантажити змінні.
# Шлях явний: без нього load_dotenv падає, коли модуль запускають не з server/.
load_dotenv(Path(__file__).parent / ".env", override=True)

BUSINESSES_DIR = Path(__file__).parent / "businesses"

#: Ліміт ElevenLabs Scribe на довжину одного keyterm.
MAX_KEYTERM_LEN = 20

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS_UK = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")


@dataclass(frozen=True)
class Resource:
    """Те, що бронюють: тип номера, послуга, майстер, пост на СТО.

    capacity — скільки таких одиниць можна зайняти ОДНОЧАСНО (номерів у готелі,
    крісел у барбершопі, лікарів на цю послугу). Саме вона тримає овербукінг.
    """

    key: str
    label: str
    aliases: tuple[str, ...] = ()
    price: int = 0
    capacity: int = 1
    duration_min: int = 0  # лише для slots
    note: str = ""


@dataclass(frozen=True)
class IntakeField:
    """Що ще треба спитати, крім часу й імені.

    Виїзні послуги (клінінг, аварійка, замір на ремонт) без адреси безглузді:
    запис є, а куди їхати — невідомо. Набір полів у кожної галузі свій, тому
    він живе в YAML і звідти ж потрапляє і в промпт, і в схему інструмента.
    """

    key: str
    label: str
    description: str = ""
    required: bool = True


@dataclass(frozen=True)
class BookingConfig:
    """Як саме цей заклад приймає записи."""

    enabled: bool = True
    granularity: str = "nights"  # nights | slots
    action: str = "бронювання"  # «бронювання» / «запис» — вживається в промпті
    action_gen: str = "бронювання"  # родовий відмінок: «поза межами бронювання/запису»
    verb: str = "забронювати"
    resource_param: str = "resource"  # ім'я аргументу в tool-схемі (room_type/service)
    resource_label: str = "варіант"
    unit: str = "ніч"  # одиниця тарифікації для nights
    start_label: str = "дата початку"  # nights: як називати межі інтервалу
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
        """Усна назва (укр./рос./англ., з відмінками) → канонічний ключ."""
        if not raw:
            return None
        needle = raw.strip().lower()
        for r in self.resources:
            variants = {r.key.lower(), r.label.lower(), *(a.lower() for a in r.aliases)}
            if needle in variants:
                return r.key
        # м'який збіг: «стандартний номер», «на чистку зубів»
        for r in self.resources:
            for v in (r.label.lower(), *(a.lower() for a in r.aliases)):
                if v and (v in needle or needle in v):
                    return r.key
        return None

    def hours_for(self, day: date) -> tuple[time, time] | None:
        """Робочі години на конкретний день. None — вихідний."""
        raw = self.hours.get(WEEKDAYS[day.weekday()])
        if not raw:
            return None
        start_s, _, end_s = str(raw).partition("-")
        return time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())

    def is_round_clock(self, day: date) -> bool:
        """Цілодобовий день. Потрібне аварійці: виклик о 23:30 не має впиратись
        у «не встигає до закриття» — саме вночі такі дзвінки й бувають."""
        h = self.hours_for(day)
        return bool(h) and h[0] == time(0, 0) and h[1] >= time(23, 59)

    def hours_compact(self) -> str:
        """Графік для картки: «пн–пт 09:00–19:00 · сб 10:00–15:00 · нд вихідний».
        Сусідні однакові дні склеюються — інакше виходить простирадло на сім рядків."""
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
        """Графік одним рядком — для промпта («пн–пт 09:00–19:00, сб 10:00–16:00»)."""
        parts = []
        for i, key in enumerate(WEEKDAYS):
            raw = self.hours.get(key)
            parts.append(f"{WEEKDAYS_UK[i]} — {'вихідний' if not raw else str(raw).replace('-', '–')}")
        return "; ".join(parts)


@dataclass(frozen=True)
class Business:
    key: str
    name: str  # як агент називає заклад уголос
    brand: str  # як підписаний дашборд
    city: str
    agent_name: str
    persona: str  # «адміністратор на ресепшені»
    tone: str
    currency: str
    customer: tuple[str, str, str]  # гість / гостя / гостю (наз., род., дав.)
    knowledge: str
    speech_bad: str
    speech_good: str
    speech_recap: str  # як звучить переказ запису перед підтвердженням
    escalation_examples: str
    keyterms: tuple[str, ...]
    demo_tab: str  # коротка назва галузі для вкладки демо
    demo_intro: str  # що написано на демо-сторінці про це заведення
    demo_try: tuple[str, ...]  # підказки «спробуйте сказати»
    booking: BookingConfig

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
        """Слова, які STT має розпізнавати впевнено: назви послуг + доменні.

        ElevenLabs Scribe приймає keyterm не довший за 20 символів і на довшому
        РВЕ ВСЕ З'ЄДНАННЯ (1008 invalid_request), а не ігнорує зайве. Виглядає
        це як «розпізнавання не працює взагалі»: звук іде, VAD спрацьовує, а
        транскриптів немає. Тому довгі назви («Підтримуюче однокімнатна»,
        «Аварійне відкриття дверей») ріжемо на слова — користь для розпізнавання
        лишається, обмеження дотримане.
        """
        out: list[str] = []
        for raw in [r.label for r in self.booking.resources] + list(self.keyterms):
            if len(raw) <= MAX_KEYTERM_LEN:
                out.append(raw)
            else:
                out.extend(w for w in raw.split() if 3 < len(w) <= MAX_KEYTERM_LEN)
        seen, uniq = set(), []
        for t in out:
            if t.lower() not in seen:
                seen.add(t.lower())
                uniq.append(t)
        return uniq


# --------------------------- завантаження YAML ---------------------------


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
    """Прочитати YAML закладу. key=None → BUSINESS_CONFIG / BUSINESS / 'hotel'."""
    path_env = _CLI_CONFIG or os.getenv("BUSINESS_CONFIG")
    if key is None and path_env:
        path = Path(path_env)
    else:
        key = key or _CLI_BUSINESS or os.getenv("BUSINESS", "hotel")
        path = BUSINESSES_DIR / f"{key}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in BUSINESSES_DIR.glob("*.yaml"))
        raise FileNotFoundError(f"немає конфігу закладу {path}; доступні: {available}")

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
        booking=booking,
    )


#: Активний заклад процесу. Один процес обслуговує один заклад — так само, як
#: один телефонний номер належить одному закладу.
BUSINESS = load_business()


# ----------------------------- системний промпт -----------------------------


def _speech_rules(biz: Business) -> str:
    """Як говорити. Це найсильніший важіль природності — сильніший за будь-які
    налаштування TTS: синтезатор озвучує пунктуацію, а не зміст."""
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

Ти НЕ знаєш нічого, чого немає у блоці «ПРО ЗАКЛАД» і у відповідях
інструментів. Не вигадуй факти, ціни й наявність — краще чесно сказати, що
уточниш у колег і передзвониш, і записати {cust} через take_message."""


def _phone_rules(biz: Business) -> str:
    """Правила читання українського номера вголос — однакові для всіх галузей."""
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
    """Додаткові питання галузі — по одному за раз, а не анкетою."""
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
    """Сценарій запису. Кроки однакові, різниця лише в тому, що збираємо:
    інтервал діб (готель) чи дату+час (клініка, барбершоп)."""
    bk = biz.booking
    cust_nom = biz.customer_nom

    if bk.is_slots:
        collect = (
            f"1. Тобі потрібні: {bk.resource_label}, дата і час.\n"
            f"   Якщо {cust_nom} каже розпливчасто («десь зранку», «у вівторок») — назви\n"
            "   конкретний час і спитай, чи підходить, а не проси уточнити.\n"
            "2. Виклич check_availability. Якщо час зайнятий, інструмент поверне вільні\n"
            "   варіанти — назви два-три на вибір, не перелічуй усі.\n"
            "   Тривалість і ціну не вигадуй, бери з відповіді інструмента."
        )
    else:
        party = f", {bk.party_label}" if bk.ask_party else ""
        collect = (
            f"1. Тобі потрібні: {bk.start_label}, {bk.end_label}, {bk.resource_label}{party}.\n"
            f"   Якщо {cust_nom} уже чітко назвав щось із цього — НЕ перепитуй це окремо,\n"
            "   одразу переходь до перевірки. Питай лише те, чого справді бракує.\n"
            "2. Виклич check_availability, щоб дізнатися наявність і повну вартість.\n"
            f"   Назви їх {biz.customer_dat}."
        )
    recap = biz.speech_recap or (
        f"назви {bk.resource_label}, час, ім'я та повну вартість — одним плавним реченням"
    )

    return f"""{bk.action.upper()} — працюй ТІЛЬКИ через інструменти, не вигадуй наявність і ціни:
{collect}
3. Якщо вільно і {cust_nom} згоден — попроси ім'я та номер телефону.{_intake_rules(biz)}
{_phone_rules(biz)}
4. Перед створенням ОБОВ'ЯЗКОВО повтори вголос усі деталі разом:
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
    today = date.today().isoformat()
    weekday = WEEKDAYS_UK[date.today().weekday()]
    bk = biz.booking

    blocks = [
        f"Ти — {biz.agent_name}, {biz.persona} ({biz.name}).\nРозмовляй природною українською мовою.",
        _speech_rules(biz),
        f"Сьогодні {today}, {weekday}. Коли {biz.customer_nom} називає дату чи час усно\n"
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
    """Підказка моделі для першої репліки — вітається агент, не гість."""
    biz = biz or BUSINESS
    return (
        f"Привітайся першою, дуже коротко, як {biz.persona} ({biz.name}), "
        "і запитай, чим можеш допомогти."
    )


def slot_grid(day: date, biz: Business | None = None) -> list[datetime]:
    """Усі можливі початки візитів на цей день за графіком закладу."""
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


if __name__ == "__main__":  # швидка перевірка: python business.py [key]
    import sys

    b = load_business(sys.argv[1]) if len(sys.argv) > 1 else BUSINESS
    print(build_system_prompt(b))
