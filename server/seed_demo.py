"""Fill a business database with demo records, so the dashboard looks alive.

Running it:
    python seed_demo.py                    # ~130 bookings over ~65 days
    BUSINESS=clinic python seed_demo.py    # appointments on the slot grid
    python seed_demo.py --keep             # add without clearing first

The data is realistic but invented. For a real client, simply do not run this
script — the dashboard counts the records the voice agent actually created.

TODO(locale): the name lists below are Ukrainian; an English business needs its
own, which is one more thing the language pack should carry.
"""

import argparse
import random

import phonenumbers
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import crm
from business import BUSINESS, slot_grid

random.seed(42)  # reproducibility

FIRST = [
    "Андрій", "Олена", "Ігор", "Марія", "Юлія", "Олександр", "Наталія", "Дмитро",
    "Софія", "Максим", "Ірина", "Богдан", "Тетяна", "Віктор", "Оксана", "Сергій",
    "Катерина", "Павло", "Анна", "Роман", "Вікторія", "Микола", "Дарина", "Тарас",
]
LAST = [
    "Коваленко", "Мельник", "Сидоренко", "Ткаченко", "Бондаренко", "Шевченко",
    "Кравченко", "Бойко", "Ковальчук", "Поліщук", "Гончаренко", "Лисенко",
    "Марченко", "Савченко", "Руденко", "Мороз", "Кузьменко", "Пономаренко",
]

BOOK = BUSINESS.booking
# The first items on the price list are ordered more often than the last ones,
# otherwise the revenue chart comes out flat — and a flat chart in a demo reads
# as a placeholder.
WEIGHTS = [0.40, 0.32, 0.20, 0.08, 0.05, 0.03][: len(BOOK.resources)]


_EXAMPLE_NUMBER = phonenumbers.example_number_for_type(
    BUSINESS.country, phonenumbers.PhoneNumberType.MOBILE
) or phonenumbers.example_number(BUSINESS.country)


def _phone() -> str:
    """A plausible, valid number for the business's own country.

    It used to build a Ukrainian number unconditionally and fall back to the raw
    string when validation failed — which, before numbers were parsed per
    country, could never happen. Once it could, seeding an American practice
    filled its dashboard with numbers its own agent would reject.
    """
    base = phonenumbers.national_significant_number(_EXAMPLE_NUMBER)
    for _ in range(200):
        candidate = base[:-6] + f"{random.randint(0, 999999):06d}"
        value, _error = crm.validate_phone(candidate)
        if value:
            return value
    raise RuntimeError(f"cannot generate a valid {BUSINESS.country} number from {base}")


def _slot_span(created: datetime) -> tuple[str, str, int] | None:
    """A random appointment that is free per the schedule, near the created date."""
    res = random.choices(BOOK.resources, weights=WEIGHTS)[0]
    for _ in range(20):
        day = created.date() + timedelta(days=random.randint(0, 14))
        grid = slot_grid(day)
        if not grid:
            continue
        begin = random.choice(grid)
        finish = begin + timedelta(minutes=res.duration_min)
        hours = BOOK.hours_for(day)
        if finish.time() > hours[1]:
            continue
        return (
            begin.strftime("%Y-%m-%dT%H:%M"),
            finish.strftime("%Y-%m-%dT%H:%M"),
            res.price,
        ), res.key
    return None


def _nights_span(created: datetime) -> tuple[tuple[str, str, int], str]:
    res = random.choices(BOOK.resources, weights=WEIGHTS)[0]
    nights = random.randint(1, 5)
    # ~40% are future bookings (so occupancy is not empty), the rest is history.
    if random.random() < 0.4:
        check_in = date.today() + timedelta(days=random.randint(0, 30))
    else:
        check_in = created.date() + timedelta(days=random.randint(1, 10))
    check_out = check_in + timedelta(days=nights)
    return (check_in.isoformat(), check_out.isoformat(), res.price * nights), res.key


def seed(n: int, keep: bool) -> None:
    crm.init_db()
    with sqlite3.connect(crm.DB_PATH) as conn:
        if not keep:
            conn.execute("DELETE FROM bookings")
        made = 0
        for _ in range(n):
            created = datetime.now() - timedelta(
                days=random.randint(0, 65), hours=random.randint(0, 23)
            )
            span = _slot_span(created) if BOOK.is_slots else _nights_span(created)
            if not span:
                continue
            (starts_at, ends_at, price), key = span
            conn.execute(
                """
                INSERT INTO bookings
                    (id, ref_code, resource, starts_at, ends_at, party_size,
                     customer_name, phone, total_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    BOOK.ref_prefix + uuid.uuid4().hex[:6].upper(),
                    key,
                    starts_at,
                    ends_at,
                    random.randint(1, 4) if BOOK.ask_party else 1,
                    f"{random.choice(FIRST)} {random.choice(LAST)}",
                    _phone(),
                    price,
                    created.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            made += 1
        conn.commit()

    total = crm.stats()
    print(
        f"{BUSINESS.key}: seeded {made} records into {crm.DB_PATH.name}. "
        f"In period: {total['kpi']['total_bookings']}, "
        f"revenue {total['kpi']['total_revenue']} {BUSINESS.currency}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not clear existing records")
    ap.add_argument("-n", type=int, default=130, help="how many records to generate")
    args = ap.parse_args()
    seed(args.n, args.keep)
