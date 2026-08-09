"""Заглушка PMS (Property Management System) на SQLite.

Фаза 2: реальная логика броней локально, без интеграции в чужу систему готелю.
Наявність = кількість номерів типу мінус ті, що перетинаються за датами.
Ця ж база стане джерелом даних для дашборда у Фазі 3.

Формат дат скрізь: 'РРРР-ММ-ДД' (ISO). LLM перетворює усну дату в цей формат.
"""

import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "hotel.db"

# Типи номерів: канонічний ключ -> (людська назва укр., базова ціна грн/ніч, всього номерів).
# Порядок = порядок відображення (dict зберігає порядок вставки).
ROOM_TYPES = {
    "standard": ("Стандарт", 1200, 30),
    "superior": ("Покращений", 1800, 25),
    "deluxe": ("Делюкс", 2800, 20),
    "suite": ("Люкс", 4500, 10),
}

# Синоніми з усного мовлення (укр./рос./англ.) -> канонічний ключ
_ALIASES = {
    "стандарт": "standard", "стандартний": "standard", "standard": "standard", "стандартный": "standard",
    "покращений": "superior", "улучшенный": "superior", "superior": "superior", "супіріор": "superior",
    "делюкс": "deluxe", "deluxe": "deluxe", "делюксовий": "deluxe",
    "люкс": "suite", "люксовий": "suite", "suite": "suite", "сьют": "suite", "апартаменти": "suite",
}


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
    бронь, за якою готель не додзвониться. Краще повернути помилку, щоб
    Оксана перепитала.

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
            "перепитай номер у гостя й повтори по цифрах"
        )
    return digits, None


def normalize_room_type(raw: str) -> str | None:
    """Привести назву типу номера (укр./рос./англ.) до канонічного ключа."""
    if not raw:
        return None
    return _ALIASES.get(raw.strip().lower())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Створити таблиці й засіяти типи номерів (ідемпотентно)."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id           TEXT PRIMARY KEY,
                ref_code     TEXT UNIQUE NOT NULL,
                room_type    TEXT NOT NULL,
                check_in     TEXT NOT NULL,
                check_out    TEXT NOT NULL,
                guests       INTEGER NOT NULL,
                guest_name   TEXT NOT NULL,
                phone        TEXT NOT NULL,
                total_price  INTEGER NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Запити на зворотний дзвінок: питання поза бронюванням, які агент
        # передає адміністратору (спа, тварини, банкети тощо).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                guest_name  TEXT NOT NULL,
                phone       TEXT NOT NULL,
                topic       TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def _nights(check_in: str, check_out: str) -> int:
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    return (co - ci).days


def check_availability(check_in: str, check_out: str, room_type: str, guests: int) -> dict:
    """Перевірити наявність номера на дати. Повертає dict з результатом.

    Ключі результату: available (bool), room_type_label, nights, price_per_night,
    total_price, rooms_left, error (якщо щось не так).
    """
    key = normalize_room_type(room_type)
    if key is None:
        return {"available": False, "error": f"невідомий тип номера: {room_type!r}"}

    try:
        nights = _nights(check_in, check_out)
    except ValueError:
        return {"available": False, "error": "неправильний формат дат, очікується РРРР-ММ-ДД"}
    if nights <= 0:
        return {"available": False, "error": "дата виїзду має бути пізніше за дату заїзду"}

    label, price, total_rooms = ROOM_TYPES[key]

    # Скільки номерів цього типу зайнято на пересічні дати.
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM bookings
            WHERE room_type = ?
              AND NOT (check_out <= ? OR check_in >= ?)
            """,
            (key, check_in, check_out),
        ).fetchone()
    booked = row["cnt"]
    rooms_left = total_rooms - booked

    return {
        "available": rooms_left > 0,
        "room_type": key,
        "room_type_label": label,
        "nights": nights,
        "price_per_night": price,
        "total_price": price * nights,
        "rooms_left": max(rooms_left, 0),
    }


def list_bookings() -> list[dict]:
    """Усі броні, найновіші зверху (для дашборда і таблиці)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    out = []
    for r in rows:
        key = r["room_type"]
        label = ROOM_TYPES.get(key, (key, 0, 0))[0]
        try:
            nights = _nights(r["check_in"], r["check_out"])
        except ValueError:
            nights = 0
        out.append(
            {
                "ref_code": r["ref_code"],
                "guest_name": r["guest_name"],
                "phone": r["phone"],
                "room_type": key,
                "room_type_label": label,
                "check_in": r["check_in"],
                "check_out": r["check_out"],
                "nights": nights,
                "guests": r["guests"],
                "total_price": r["total_price"],
                "created_at": r["created_at"],
            }
        )
    return out


def take_message(guest_name: str, phone: str, topic: str) -> dict:
    """Записати запит на зворотний дзвінок (питання поза межами бронювання)."""
    phone, phone_error = validate_phone(phone)
    if phone_error:
        return {"success": False, "error": phone_error}
    if not (topic and topic.strip()):
        return {"success": False, "error": "не вказано суть питання"}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (id, guest_name, phone, topic) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, (guest_name or "").strip() or "Гість", phone.strip(), topic.strip()),
        )
        conn.commit()
    return {"success": True, "note": "запит записано, адміністратор передзвонить"}


def list_messages() -> list[dict]:
    """Усі запити на зворотний дзвінок, найновіші зверху (для дашборда)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _pct_delta(cur: float, prev: float) -> int | None:
    """Відсоткова зміна період-до-періоду. None, якщо порівнювати нема з чим."""
    if prev <= 0:
        return None
    return round((cur - prev) / prev * 100)


def _availability_status(rooms_left: int, total_rooms: int) -> str:
    """good — є вільні; warning — мало вільних; critical — немає вільних."""
    if rooms_left <= 0:
        return "critical"
    if total_rooms and rooms_left / total_rooms <= 0.25:
        return "warning"
    return "good"


def stats(period_days: int = 30) -> dict:
    """Агрегати для дашборда за поточний період + дельти до попереднього.

    Період рахується за датою створення броні (created_at). KPI — за поточний
    період; дельти — поточний період проти попереднього такого ж вікна.
    Завантаженість номерів — поточний стан інвентаря (активні броні), не період.
    """
    bookings = list_bookings()
    now = datetime.now()
    cur_start = now - timedelta(days=period_days)
    prev_start = now - timedelta(days=2 * period_days)
    today = date.today().isoformat()

    def created(b: dict) -> datetime:
        return datetime.fromisoformat(b["created_at"])

    cur = [b for b in bookings if created(b) >= cur_start]
    prev = [b for b in bookings if prev_start <= created(b) < cur_start]

    def agg(rows: list[dict]) -> dict:
        n = len(rows)
        rev = sum(b["total_price"] for b in rows)
        guests = sum(b["guests"] for b in rows)
        return {
            "total_bookings": n,
            "total_revenue": rev,
            "total_guests": guests,
            "avg_check": round(rev / n) if n else 0,
        }

    cur_kpi = agg(cur)
    prev_kpi = agg(prev)
    deltas = {k: _pct_delta(cur_kpi[k], prev_kpi[k]) for k in cur_kpi}

    by_type = []
    for key, (label, price, total_rooms) in ROOM_TYPES.items():
        in_period = [b for b in cur if b["room_type"] == key]
        # "Активні" — броні, що ще не завершились (виїзд сьогодні або пізніше).
        active = len([b for b in bookings if b["room_type"] == key and b["check_out"] >= today])
        rooms_left = max(total_rooms - active, 0)
        occupancy = round(active / total_rooms * 100) if total_rooms else 0
        by_type.append(
            {
                "room_type": key,
                "label": label,
                "price_per_night": price,
                "total_rooms": total_rooms,
                "booked": active,
                "rooms_left": rooms_left,
                "occupancy_pct": occupancy,
                "status": _availability_status(rooms_left, total_rooms),
                "revenue": sum(b["total_price"] for b in in_period),
            }
        )

    return {
        "period_days": period_days,
        "period_start": cur_start.date().isoformat(),
        "period_end": now.date().isoformat(),
        "kpi": cur_kpi,
        "deltas": deltas,
        "by_type": by_type,
        "bookings": cur,
        "messages": list_messages(),
    }


def _generate_ref_code(conn: sqlite3.Connection) -> str:
    """Унікальний номер броні. Шестизначне число (без ведучих нулів), щоб TTS
    читав його природно («Бе-ер сто двадцять три чотириста»), а не по гекс-літерах."""
    for _ in range(20):
        code = "БР-" + str(uuid.uuid4().int % 900_000 + 100_000)
        if not conn.execute("SELECT 1 FROM bookings WHERE ref_code = ?", (code,)).fetchone():
            return code
    # практично недосяжно за життя демо; запасний варіант — довший номер
    return "БР-" + str(uuid.uuid4().int % 90_000_000 + 10_000_000)


def create_booking(
    check_in: str,
    check_out: str,
    room_type: str,
    guests: int,
    guest_name: str,
    phone: str,
) -> dict:
    """Створити бронювання. Повертає dict з ref_code або error.

    Перед записом ще раз перевіряє наявність (щоб не переброньувати).
    """
    phone, phone_error = validate_phone(phone)
    if phone_error:
        return {"success": False, "error": phone_error}
    avail = check_availability(check_in, check_out, room_type, guests)
    if avail.get("error"):
        return {"success": False, "error": avail["error"]}
    if not avail["available"]:
        return {"success": False, "error": "немає вільних номерів цього типу на ці дати"}

    key = avail["room_type"]
    total_price = avail["total_price"]
    booking_id = uuid.uuid4().hex

    with _connect() as conn:
        ref_code = _generate_ref_code(conn)
        conn.execute(
            """
            INSERT INTO bookings
                (id, ref_code, room_type, check_in, check_out, guests, guest_name, phone, total_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (booking_id, ref_code, key, check_in, check_out, guests, guest_name, phone, total_price),
        )
        conn.commit()

    return {
        "success": True,
        "ref_code": ref_code,
        "room_type_label": avail["room_type_label"],
        "check_in": check_in,
        "check_out": check_out,
        "nights": avail["nights"],
        "guests": guests,
        "guest_name": guest_name,
        "phone": phone,
        "total_price": total_price,
    }
