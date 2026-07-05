"""Заглушка PMS (Property Management System) на SQLite.

Фаза 2: реальная логика броней локально, без интеграции в чужу систему готелю.
Наявність = кількість номерів типу мінус ті, що перетинаються за датами.
Ця ж база стане джерелом даних для дашборда у Фазі 3.

Формат дат скрізь: 'РРРР-ММ-ДД' (ISO). LLM перетворює усну дату в цей формат.
"""

import sqlite3
import uuid
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "hotel.db"

# Типи номерів: канонічний ключ -> (людська назва укр., базова ціна грн/ніч, всього номерів)
ROOM_TYPES = {
    "single": ("одномісний", 1200, 5),
    "double": ("двомісний", 1800, 8),
    "family": ("сімейний", 2500, 3),
}

# Синоніми з усного мовлення -> канонічний ключ
_ALIASES = {
    "одномісний": "single", "одноместный": "single", "single": "single", "сингл": "single",
    "двомісний": "double", "двухместный": "double", "double": "double", "дабл": "double",
    "сімейний": "family", "семейный": "family", "family": "family", "сімейка": "family",
}


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
    avail = check_availability(check_in, check_out, room_type, guests)
    if avail.get("error"):
        return {"success": False, "error": avail["error"]}
    if not avail["available"]:
        return {"success": False, "error": "немає вільних номерів цього типу на ці дати"}

    key = avail["room_type"]
    total_price = avail["total_price"]
    ref_code = "БР-" + uuid.uuid4().hex[:6].upper()
    booking_id = uuid.uuid4().hex

    with _connect() as conn:
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
