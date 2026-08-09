"""crm.py — записи, броні й запити на дзвінок (SQLite). Галузе-незалежний шар.

Раніше це був PMS готелю. Виявилось, що готель, клініка й барбершоп — це та сама
задача: РЕСУРС зайнятий на проміжку [starts_at, ends_at), і зайняти його можна
не більше ніж capacity разів одночасно. Різниця тільки в тому, чим міряти
проміжок:

    nights — межі задає гість (заїзд/виїзд), одиниця тарифікації — доба;
    slots  — межі задає послуга (тривалість із конфігу), ціна фіксована.

Формат часу в базі: 'РРРР-ММ-ДД' для nights, 'РРРР-ММ-ДДTГГ:ХХ' для slots.
Обидва порівнюються як рядки — звідси один SQL на перетин для обох моделей.

Заклад береться з business.BUSINESS (один процес = один заклад = один телефон).
"""

import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from business import BUSINESS, slot_grid

BIZ = BUSINESS
BOOK = BUSINESS.booking

# Кожен заклад — своя база: hotel.db, clinic.db, barbershop.db.
DB_PATH = Path(os.getenv("CRM_DB") or Path(__file__).parent / f"{BIZ.key}.db")

_ISO_MIN = "%Y-%m-%dT%H:%M"


# ------------------------------- телефон -------------------------------


def normalize_phone(raw: str | None) -> str | None:
    """Звести телефон до цифр (і провідного +): «067 123 45 67» → «0671234567».
    Якщо цифр немає взагалі (LLM передав слова) — лишаємо як є, щоб проблема
    була видима операторові, а не тихо загубилась."""
    if not raw:
        return raw
    digits = re.sub(r"[^\d+]", "", raw)
    return digits or raw


def validate_phone(raw: str | None) -> tuple[str | None, str | None]:
    """Перевірити український номер. Повертає (нормалізований, помилка).

    Навіщо: модель, розпізнаючи телефон з мовлення, інколи додає або губить
    цифру («нуль шістдесят сім сімсот сімдесят сім сорок п'ять шістдесят сім»
    → 06777745667, одинадцять цифр). Мовчки прийняти такий номер = підтвердити
    запис, за яким закладу не додзвоняться. Краще повернути помилку, щоб агент
    перепитав.

    Приймаємо: 0XXXXXXXXX (10 цифр), 380XXXXXXXXX / +380XXXXXXXXX (12 цифр).
    """
    if not raw:
        return None, "телефон не вказано"

    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None, "у номері немає жодної цифри — продиктуй цифрами, не словами"

    if len(digits) == 12 and digits.startswith("380"):
        digits = "0" + digits[3:]
    elif len(digits) == 11 and digits.startswith("80"):
        digits = "0" + digits[2:]

    if len(digits) != 10 or not digits.startswith("0"):
        return None, (
            f"у номері {len(digits)} цифр замість десяти — "
            "перепитай номер і повтори його вголос групами цифр"
        )
    return digits, None


def normalize_resource(raw: str) -> str | None:
    """Усна назва послуги/типу номера → канонічний ключ конфігу."""
    return BOOK.normalize_resource(raw)


# --------------------------------- база ---------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_RENAMES = {  # стара готельна схема → універсальна
    "bookings": {
        "room_type": "resource",
        "check_in": "starts_at",
        "check_out": "ends_at",
        "guests": "party_size",
        "guest_name": "customer_name",
    },
    "messages": {"guest_name": "customer_name"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Перейменувати готельні колонки в універсальні (ідемпотентно).

    Потрібне тільки для баз, створених до пивоту на універсальний ресепшн —
    у них лежать демо-брони, на яких показують дашборд.
    """
    for table, renames in _RENAMES.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        for old, new in renames.items():
            if old in cols and new not in cols:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def init_db() -> None:
    """Створити таблиці (ідемпотентно) і підтягнути стару схему, якщо є."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id            TEXT PRIMARY KEY,
                ref_code      TEXT UNIQUE NOT NULL,
                resource      TEXT NOT NULL,
                starts_at     TEXT NOT NULL,
                ends_at       TEXT NOT NULL,
                party_size    INTEGER NOT NULL DEFAULT 1,
                customer_name TEXT NOT NULL,
                phone         TEXT NOT NULL,
                total_price   INTEGER NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Запити на зворотний дзвінок: усе, що поза записом і що агент чесно
        # передає людині замість того, щоб вигадувати відповідь.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id            TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                phone         TEXT NOT NULL,
                topic         TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        _migrate(conn)
        conn.commit()


def _occupied(conn: sqlite3.Connection, resource: str, starts_at: str, ends_at: str) -> int:
    """Скільки одиниць ресурсу вже зайнято на цьому проміжку."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM bookings
        WHERE resource = ?
          AND NOT (ends_at <= ? OR starts_at >= ?)
        """,
        (resource, starts_at, ends_at),
    ).fetchone()
    return row["cnt"]


# ---------------------------- розбір часу ----------------------------


def _parse_nights(start: str, end: str) -> tuple[str, str, int, str | None]:
    """(starts_at, ends_at, кількість діб, помилка)."""
    try:
        ci = date.fromisoformat(start)
        co = date.fromisoformat(end)
    except (ValueError, TypeError):
        return "", "", 0, "неправильний формат дат, очікується РРРР-ММ-ДД"
    if (co - ci).days <= 0:
        return "", "", 0, "дата виїзду має бути пізніше за дату заїзду"
    if ci < date.today():
        return "", "", 0, "ця дата вже минула — уточни, на які дати потрібно"
    if (ci - date.today()).days > BOOK.horizon_days:
        return "", "", 0, f"так далеко наперед не бронюємо (максимум {BOOK.horizon_days} днів)"
    return ci.isoformat(), co.isoformat(), (co - ci).days, None


def _parse_slot(start: str, duration_min: int) -> tuple[str, str, str | None]:
    """(starts_at, ends_at, помилка) для запису на конкретний час."""
    raw = (start or "").strip().replace(" ", "T")
    try:
        begin = datetime.strptime(raw[:16], _ISO_MIN)
    except (ValueError, TypeError):
        return "", "", "неправильний формат часу, очікується РРРР-ММ-ДДTГГ:ХХ"
    finish = begin + timedelta(minutes=duration_min)

    if begin < datetime.now():
        return "", "", "цей час уже минув — запропонуй найближчий вільний"
    if (begin.date() - date.today()).days > BOOK.horizon_days:
        return "", "", f"так далеко наперед не записуємо (максимум {BOOK.horizon_days} днів)"

    hours = BOOK.hours_for(begin.date())
    if not hours:
        return "", "", f"цього дня заклад не працює; графік: {BOOK.hours_human()}"
    open_t, close_t = hours
    if begin.time() < open_t:
        return "", "", f"цього дня відкриваємось о {open_t.strftime('%H:%M')}"
    if finish.time() > close_t or finish.date() != begin.date():
        return "", "", (
            f"візит триває {duration_min} хв і не встигає до закриття "
            f"о {close_t.strftime('%H:%M')} — запропонуй раніший час"
        )
    return begin.strftime(_ISO_MIN), finish.strftime(_ISO_MIN), None


def free_slots(
    resource_key: str, day: date, limit: int = 4, near: datetime | None = None
) -> list[str]:
    """Вільні початки візитів на цей день — «ГГ:ХХ».

    Це те, чим агент рятує розмову, коли названий час зайнятий: назвати два-три
    альтернативи набагато краще, ніж сказати «немає» і чекати нової пропозиції.
    Тому за замовчуванням віддаємо найближчі до бажаного часу, а не перші за
    розкладом: людині, яка просила на дванадцяту, дев'ята ранку не допоможе.
    """
    res = BOOK.resource(resource_key)
    if not res:
        return []
    now = datetime.now()
    hours = BOOK.hours_for(day)
    if not hours:
        return []
    free: list[datetime] = []
    with _connect() as conn:
        for begin in slot_grid(day):
            finish = begin + timedelta(minutes=res.duration_min)
            if begin < now or finish.time() > hours[1]:
                continue
            busy = _occupied(conn, resource_key, begin.strftime(_ISO_MIN), finish.strftime(_ISO_MIN))
            if busy < res.capacity:
                free.append(begin)
    if near:
        free.sort(key=lambda t: abs((t - near).total_seconds()))
    return [t.strftime("%H:%M") for t in sorted(free[:limit])]


# ------------------------------ наявність ------------------------------


def check_availability(
    resource: str,
    start: str,
    end: str | None = None,
    party_size: int = 1,
) -> dict:
    """Чи вільно, скільки коштує. Єдиний вхід для обох моделей часу.

    nights: start = дата заїзду, end = дата виїзду (РРРР-ММ-ДД).
    slots:  start = 'РРРР-ММ-ДДTГГ:ХХ', end рахується з тривалості послуги.
    """
    key = BOOK.normalize_resource(resource)
    if key is None:
        known = ", ".join(r.label for r in BOOK.resources)
        return {"available": False, "error": f"невідомий варіант {resource!r}; є: {known}"}
    res = BOOK.resource(key)

    if BOOK.is_slots:
        starts_at, ends_at, err = _parse_slot(start, res.duration_min)
        units, price_per_unit = 1, res.price
    else:
        starts_at, ends_at, units, err = _parse_nights(start, end or "")
        price_per_unit = res.price
    if err:
        return {"available": False, "error": err}

    if BOOK.ask_party and party_size > BOOK.max_party:
        return {"available": False, "error": f"максимум {BOOK.max_party} на один запис"}

    with _connect() as conn:
        left = res.capacity - _occupied(conn, key, starts_at, ends_at)

    out = {
        "available": left > 0,
        "resource": key,
        "resource_label": res.label,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "total_price": res.price if BOOK.is_slots else price_per_unit * units,
        "currency": BIZ.currency,
        "left": max(left, 0),
    }
    if BOOK.is_slots:
        wanted = datetime.strptime(starts_at, _ISO_MIN)
        out["duration_min"] = res.duration_min
        if left <= 0:
            # Зайнято — одразу даємо чим замінити: спершу той самий день, потім
            # наступний робочий. Без цього агент тупцює й перепитує наосліп.
            day = wanted.date()
            out["alternatives"] = free_slots(key, day, near=wanted)
            out["alternatives_day"] = day.isoformat()
            for ahead in range(1, 8):
                if out["alternatives"]:
                    break
                nxt = day + timedelta(days=ahead)
                out["alternatives"] = free_slots(key, nxt)
                out["alternatives_day"] = nxt.isoformat()
    else:
        out["units"] = units
        out["unit_label"] = BOOK.unit
        out["price_per_unit"] = price_per_unit
    return out


# ------------------------------- запис -------------------------------


def _generate_ref_code(conn: sqlite3.Connection) -> str:
    """Унікальний номер запису. Шестизначне число (без ведучих нулів), щоб TTS
    читав його природно («Бе-ер сто двадцять три чотириста»), а не по літерах."""
    for _ in range(20):
        code = BOOK.ref_prefix + str(uuid.uuid4().int % 900_000 + 100_000)
        if not conn.execute("SELECT 1 FROM bookings WHERE ref_code = ?", (code,)).fetchone():
            return code
    return BOOK.ref_prefix + str(uuid.uuid4().int % 90_000_000 + 10_000_000)


def create_booking(
    resource: str,
    start: str,
    customer_name: str,
    phone: str,
    end: str | None = None,
    party_size: int = 1,
) -> dict:
    """Створити запис. Перед вставкою ще раз перевіряє наявність."""
    phone, phone_error = validate_phone(phone)
    if phone_error:
        return {"success": False, "error": phone_error}
    if not (customer_name and customer_name.strip()):
        return {"success": False, "error": "не вказано ім'я"}

    avail = check_availability(resource, start, end, party_size)
    if avail.get("error"):
        return {"success": False, "error": avail["error"]}
    if not avail["available"]:
        out = {"success": False, "error": "на цей час вільного місця немає"}
        if avail.get("alternatives"):
            out["alternatives"] = avail["alternatives"]
        return out

    with _connect() as conn:
        ref_code = _generate_ref_code(conn)
        conn.execute(
            """
            INSERT INTO bookings
                (id, ref_code, resource, starts_at, ends_at, party_size,
                 customer_name, phone, total_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                ref_code,
                avail["resource"],
                avail["starts_at"],
                avail["ends_at"],
                party_size,
                customer_name.strip(),
                phone,
                avail["total_price"],
            ),
        )
        conn.commit()

    return {
        "success": True,
        "ref_code": ref_code,
        "resource_label": avail["resource_label"],
        "starts_at": avail["starts_at"],
        "ends_at": avail["ends_at"],
        "party_size": party_size,
        "customer_name": customer_name.strip(),
        "phone": phone,
        "total_price": avail["total_price"],
        "currency": BIZ.currency,
    }


def take_message(customer_name: str, phone: str, topic: str) -> dict:
    """Записати запит на зворотний дзвінок (питання поза межами запису)."""
    phone, phone_error = validate_phone(phone)
    if phone_error:
        return {"success": False, "error": phone_error}
    if not (topic and topic.strip()):
        return {"success": False, "error": "не вказано суть питання"}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (id, customer_name, phone, topic) VALUES (?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                (customer_name or "").strip() or BIZ.customer_nom.capitalize(),
                phone.strip(),
                topic.strip(),
            ),
        )
        conn.commit()
    return {"success": True, "note": "запит записано, з людиною зв'яжуться"}


# ------------------------------ читання ------------------------------


def _units_of(row: sqlite3.Row) -> int:
    if BOOK.is_slots:
        return 1
    try:
        return (date.fromisoformat(row["ends_at"]) - date.fromisoformat(row["starts_at"])).days
    except ValueError:
        return 0


def list_bookings() -> list[dict]:
    """Усі записи, найновіші зверху (для дашборда)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    out = []
    for r in rows:
        res = BOOK.resource(r["resource"])
        out.append(
            {
                "ref_code": r["ref_code"],
                "customer_name": r["customer_name"],
                "phone": r["phone"],
                "resource": r["resource"],
                "resource_label": res.label if res else r["resource"],
                "starts_at": r["starts_at"],
                "ends_at": r["ends_at"],
                "units": _units_of(r),
                "party_size": r["party_size"],
                "total_price": r["total_price"],
                "created_at": r["created_at"],
            }
        )
    return out


def list_messages() -> list[dict]:
    """Усі запити на зворотний дзвінок, найновіші зверху."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------ дашборд ------------------------------


def _pct_delta(cur: float, prev: float) -> int | None:
    """Відсоткова зміна період-до-періоду. None, якщо порівнювати нема з чим."""
    if prev <= 0:
        return None
    return round((cur - prev) / prev * 100)


def _load_status(left: int, total: int) -> str:
    """good — є вільні; warning — мало вільних; critical — немає вільних;
    closed — сьогодні заклад не працює (нуль місткості ≠ аншлаг)."""
    if total <= 0:
        return "closed"
    if left <= 0:
        return "critical"
    if total and left / total <= 0.25:
        return "warning"
    return "good"


def _capacity_today(res) -> int:
    """Скільки записів на цей ресурс фізично влазить у сьогоднішній день."""
    if not BOOK.is_slots:
        return res.capacity
    hours = BOOK.hours_for(date.today())
    if not hours or not res.duration_min:
        return 0
    open_min = (
        datetime.combine(date.today(), hours[1]) - datetime.combine(date.today(), hours[0])
    ).seconds // 60
    return res.capacity * max(open_min // res.duration_min, 1)


def stats(period_days: int = 30) -> dict:
    """Агрегати для дашборда за поточний період + дельти до попереднього.

    KPI — за датою СТВОРЕННЯ запису (робота агента). Завантаженість — поточний
    стан: для nights це активні броні проти кількості номерів, для slots —
    сьогоднішні записи проти місткості дня.
    """
    bookings = list_bookings()
    now = datetime.now()
    cur_start = now - timedelta(days=period_days)
    prev_start = now - timedelta(days=2 * period_days)
    horizon = now.strftime(_ISO_MIN) if BOOK.is_slots else date.today().isoformat()
    today_prefix = date.today().isoformat()

    def created(b: dict) -> datetime:
        return datetime.fromisoformat(b["created_at"])

    cur = [b for b in bookings if created(b) >= cur_start]
    prev = [b for b in bookings if prev_start <= created(b) < cur_start]

    def agg(rows: list[dict]) -> dict:
        n = len(rows)
        rev = sum(b["total_price"] for b in rows)
        return {
            "total_bookings": n,
            "total_revenue": rev,
            "total_people": sum(b["party_size"] for b in rows),
            "avg_check": round(rev / n) if n else 0,
        }

    cur_kpi = agg(cur)
    prev_kpi = agg(prev)
    deltas = {k: _pct_delta(cur_kpi[k], prev_kpi[k]) for k in cur_kpi}

    by_resource = []
    for res in BOOK.resources:
        in_period = [b for b in cur if b["resource"] == res.key]
        if BOOK.is_slots:
            booked = len([b for b in bookings if b["resource"] == res.key
                          and b["starts_at"].startswith(today_prefix)])
        else:
            booked = len([b for b in bookings if b["resource"] == res.key
                          and b["ends_at"] >= horizon])
        total = _capacity_today(res)
        left = max(total - booked, 0)
        by_resource.append(
            {
                "resource": res.key,
                "label": res.label,
                "price": res.price,
                "capacity": total,
                "booked": booked,
                "left": left,
                "occupancy_pct": round(booked / total * 100) if total else 0,
                "status": _load_status(left, total),
                "revenue": sum(b["total_price"] for b in in_period),
            }
        )

    return {
        "period_days": period_days,
        "period_start": cur_start.date().isoformat(),
        "period_end": now.date().isoformat(),
        "kpi": cur_kpi,
        "deltas": deltas,
        "by_resource": by_resource,
        "bookings": cur,
        "messages": list_messages(),
    }
