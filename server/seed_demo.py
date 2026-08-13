"""Fill a business database with demo records, so the dashboard looks alive.

Running it:
    python seed_demo.py                    # ~130 bookings over ~65 days
    BUSINESS=clinic python seed_demo.py    # appointments on the slot grid
    python seed_demo.py --keep             # add without clearing first

Besides bookings this seeds what the inbox needs to look real: intake details
(address, problem — from `examples` in the business YAML), a share of
cancellations, callback messages, and the owner's checkmarks on everything
old enough that a live owner would have dealt with it. Calls are NOT invented:
a transcript is a record of something said, and there is nothing honest to
fabricate one from.

The data is realistic but invented. For a real client, simply do not run this
script — the dashboard counts the records the voice agent actually created.

"""

import argparse
import json
import random

import phonenumbers
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

import crm
from business import BUSINESS, slot_grid

random.seed(42)  # reproducibility

# Names come from the language pack: seeding an English business with Ukrainian
# surnames made its dashboard look half-finished.
FIRST = list(BUSINESS.pack.raw("fixtures.first_names"))
LAST = list(BUSINESS.pack.raw("fixtures.last_names"))

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


def _details() -> str | None:
    """Intake answers for one seeded booking, from the YAML's `examples`.

    A bool field with a refuse_if rule always gets the passing value — a
    booking that exists is one the rules let through. Optional fields are
    answered about half the time, like real callers do.
    """
    out = {}
    for f in BOOK.intake:
        if f.type == "bool":
            if f.refuse_if is not None:
                out[f.key] = str(f.refuse_if).strip().lower() not in ("true", "yes", "1")
            else:
                out[f.key] = random.random() < 0.9
        elif f.examples and (f.required or random.random() < 0.5):
            out[f.key] = random.choice(f.examples)
    return json.dumps(out, ensure_ascii=False) if out else None


def _utc(moment: datetime) -> str:
    """The format and zone SQLite's datetime('now') stamps rows with — mixing
    local time in made every seeded row jump hours on the dashboard."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seed(n: int, keep: bool) -> None:
    crm.init_db()
    now = datetime.now(timezone.utc)
    with sqlite3.connect(crm.DB_PATH) as conn:
        if not keep:
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM messages")
        made = 0
        for i in range(n):
            # The three newest records land within the last few hours, so a
            # freshly seeded inbox opens on something that just happened
            # rather than on last week.
            if i < 3:
                created = now - timedelta(minutes=random.randint(20, 240))
            else:
                created = now - timedelta(
                    days=random.randint(0, 65), hours=random.randint(0, 23)
                )
            span = _slot_span(created) if BOOK.is_slots else _nights_span(created)
            if not span:
                continue
            (starts_at, ends_at, price), key = span

            # ~8% called off; the rest handled once they are a couple of days
            # old — a live owner keeps their inbox near zero, and 130 unread
            # cards would read as "nobody looks at this".
            cancelled = i >= 3 and random.random() < 0.08
            cancelled_at = created + timedelta(hours=random.uniform(2, 30)) if cancelled else None
            age_days = (now - created).days
            handled_at = None
            if age_days >= 2 or (age_days >= 1 and random.random() < 0.5):
                handled_at = (cancelled_at or created) + timedelta(hours=random.uniform(1, 20))

            conn.execute(
                """
                INSERT INTO bookings
                    (id, ref_code, resource, starts_at, ends_at, party_size,
                     customer_name, phone, total_price, details, status,
                     cancelled_at, handled_at, created_at,
                     notified_at, cancel_notified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _details(),
                    "cancelled" if cancelled else "active",
                    _utc(cancelled_at) if cancelled_at else None,
                    _utc(handled_at) if handled_at else None,
                    _utc(created),
                    # Seeded rows are born already "delivered": demo data on a
                    # phone reads as real customers, and it is not.
                    _utc(created),
                    _utc(cancelled_at) if cancelled_at else None,
                ),
            )
            made += 1

        topics = list(BUSINESS.pack.raw("fixtures.callback_topics", ()) or ())
        msgs = 0
        for i, topic in enumerate(random.sample(topics, min(4, len(topics)))):
            created = now - timedelta(hours=random.uniform(1, 6) if i == 0 else random.uniform(6, 96))
            handled = (now - created) > timedelta(days=1)
            conn.execute(
                "INSERT INTO messages (id, customer_name, phone, topic, handled_at,"
                " created_at, notified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    f"{random.choice(FIRST)} {random.choice(LAST)}",
                    _phone(),
                    topic,
                    _utc(created + timedelta(hours=random.uniform(1, 8))) if handled else None,
                    _utc(created),
                    _utc(created),
                ),
            )
            msgs += 1
        conn.commit()

    total = crm.stats()
    fresh = sum(1 for x in crm.inbox() if not x["handled"])
    print(
        f"{BUSINESS.key}: seeded {made} bookings and {msgs} messages into"
        f" {crm.DB_PATH.name}. In period: {total['kpi']['total_bookings']},"
        f" revenue {total['kpi']['total_revenue']} {BUSINESS.currency},"
        f" unhandled in the inbox: {fresh}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not clear existing records")
    ap.add_argument("-n", type=int, default=130, help="how many records to generate")
    args = ap.parse_args()
    seed(args.n, args.keep)
