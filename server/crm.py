"""crm.py — bookings, appointments and callback requests (SQLite).

Industry-independent. This used to be a hotel PMS, until it turned out that a
hotel, a dental practice and a barbershop are the same problem: a RESOURCE is
occupied over the interval [starts_at, ends_at), and it can be occupied at most
`capacity` times at once. The only difference is what measures the interval:

    nights — the customer sets the bounds (check-in/check-out), the billing unit
             is a night;
    slots  — the service sets the bounds (duration from the config), the price
             is flat.

Time format in the database: 'YYYY-MM-DD' for nights, 'YYYY-MM-DDTHH:MM' for
slots. Both compare correctly as strings, which is why one SQL overlap query
serves both models.

The business comes from business.BUSINESS (one process = one business = one
phone number).

Note on language: nothing said out loud lives in this file. Error strings,
month names and unit forms come from the language pack (locale_pack.py), because
the model reads them to the caller. What stays here is the logic.
"""

import json
import os
import re
import sqlite3
import uuid

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, ValidationResult
from datetime import date, datetime, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

from business import BUSINESS, CLI_CRM_DB, L, slot_grid

BIZ = BUSINESS
BOOK = BUSINESS.booking

# Every business gets its own database: hotel.db, clinic.db, barbershop.db.
DB_PATH = Path(CLI_CRM_DB or os.getenv("CRM_DB") or Path(__file__).parent / f"{BIZ.key}.db")

_ISO_MIN = "%Y-%m-%dT%H:%M"

#: How many minutes into the past still count as "now" (see _parse_slot).
GRACE_MIN = 10
#: Upper bound on the length of a single booking, in units (nights).
MAX_UNITS = 60
def _to_business_time(stamp: str) -> str:
    """A `created_at` from SQLite, moved into the business's timezone.

    SQLite stamps rows with datetime('now'), which is UTC — while everything the
    business sees is in its own zone. Left alone, the owner's dashboard showed a
    time offset by their whole UTC offset, and for a Californian practice
    anything logged after 5pm landed on the next calendar day.
    """
    try:
        utc = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo("UTC"))
    except (ValueError, TypeError):
        return stamp
    return utc.astimezone(ZoneInfo(BIZ.tz)).strftime("%Y-%m-%d %H:%M:%S")


def _err(key: str, **kwargs) -> str:
    """An error message from the language pack.

    These are not log lines: the model reads them out loud. Keeping them as
    codes here rather than as prose has three payoffs — they are translated in
    one place, tests can assert on the code instead of on wording that shifts,
    and the whole set can be reviewed at a glance in the YAML, which is how the
    "an error is not the same as no availability" rule gets enforced.
    """
    return L.text(f"errors.{key}", **kwargs)



# ------------------------------- phone -------------------------------


def validate_phone(raw: str | None) -> tuple[str | None, str | None]:
    """Validate a phone number. Returns (E.164, error).

    Why validate at all: transcribing a number from speech, the model
    occasionally adds or drops a digit. Silently accepting that means confirming
    a booking the business can never call back on. Better to return an error so
    the agent asks again.

    Why a library rather than our own rules: the hand-rolled version knew
    exactly one numbering plan and rejected every American number on the first
    try. `phonenumbers` is Google's libphonenumber, and the country comes from
    the business config — so a new country is a config line, not a code change.

    The stored form is E.164 (+380632096974). Unambiguous, and dialable from
    anywhere, which matters the moment a business has customers abroad.
    """
    if not raw:
        return None, _err("phone.missing")
    if not re.search(r"\d", raw):
        return None, _err("phone.no_digits")

    try:
        number = phonenumbers.parse(raw, BIZ.country)
    except NumberParseException:
        return None, _err("phone.invalid")

    # The class of error matters, because the agent reads it out loud and has to
    # know what to ask next. Lumping everything into "no such number" was a
    # regression: a ten-digit mobile with one digit misheard is IS_POSSIBLE but
    # invalid, and telling the caller their number does not exist starts an
    # argument instead of a re-ask.
    reason = phonenumbers.is_possible_number_with_reason(number)
    if reason in (
        ValidationResult.TOO_SHORT,
        ValidationResult.IS_POSSIBLE_LOCAL_ONLY,  # no area or carrier code
        ValidationResult.INVALID_LENGTH,
    ):
        return None, _err("phone.too_short")
    if reason == ValidationResult.TOO_LONG:
        return None, _err("phone.too_long")
    if not phonenumbers.is_valid_number(number):
        digits = phonenumbers.national_significant_number(number)
        return None, _err("phone.wrong_digits", count=len(digits))

    return phonenumbers.format_number(number, PhoneNumberFormat.E164), None


#: Words the model plugs into the mandatory name field when it never asked.
_PLACEHOLDER_NAMES = {w.lower() for w in L.raw("placeholder_names")}


def validate_name(raw: str | None) -> tuple[str | None, str | None]:
    """Validate a name. Returns (name, error).

    Why: if the name never came up, the model happily fills the mandatory field
    with a placeholder — "Guest". Formally a booking exists; in practice the
    business has a reservation for nobody in particular. Asking the prompt not
    to invent things is not enough: this is deterministically detectable, so it
    is detected here.
    """
    name = (raw or "").strip()
    if not name:
        return None, _err("name.missing")
    if len(name) < 2 or not re.search(r"[^\W\d_]", name, re.UNICODE):
        return None, _err("name.empty")
    if name.lower().strip(".,!") in _PLACEHOLDER_NAMES:
        return None, _err("name.placeholder", name=name)
    return name, None


def normalize_resource(raw: str) -> str | None:
    """A spoken service or room-type name → the canonical config key."""
    return BOOK.normalize_resource(raw)


# --------------------------------- database ---------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Writes happen inside a transaction (see create_booking), so a concurrent
    # call should wait rather than fail with "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL removes the reader/writer conflict entirely: the showcase polls
    # /api/activity while the bot is writing a booking into the same file.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_RENAMES = {  # the old hotel-only schema → the universal one
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
    """Rename the hotel columns to the universal ones. Idempotent.

    Only needed for databases created before the pivot to a universal
    receptionist — those hold the demo bookings the dashboard is shown with.
    """
    for table, renames in _RENAMES.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        for old, new in renames.items():
            if old in cols and new not in cols:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def init_db() -> None:
    """Create the tables (idempotent) and migrate an older schema if present."""
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
                details       TEXT,
                -- active | cancelled. A booking is never deleted: the business
                -- needs to see that someone called and then called off.
                status        TEXT NOT NULL DEFAULT 'active',
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Callback requests: everything outside booking that the agent honestly
        # hands to a human instead of inventing an answer.
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
        # A log of what the agent did during a call. The demo page turns it into
        # a live feed, and it is the natural basis for a real call log later.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id         TEXT PRIMARY KEY,
                kind       TEXT NOT NULL,
                detail     TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # What was actually said. The event log answers "which tools ran"; this
        # answers "what did it say to them", which is the question an owner
        # actually asks.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id         TEXT PRIMARY KEY,
                business   TEXT NOT NULL,
                started_at TEXT NOT NULL,
                lines      TEXT NOT NULL,
                metrics    TEXT,
                audio_path TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        _migrate(conn)
        # `details` arrived after the schema did — add it to older databases.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bookings)")}
        if "details" not in cols:
            conn.execute("ALTER TABLE bookings ADD COLUMN details TEXT")
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE bookings ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        conn.commit()


def _occupied(
    conn: sqlite3.Connection,
    resource: str,
    starts_at: str,
    ends_at: str,
    exclude_ref: str | None = None,
) -> int:
    """How many units of the resource are already taken over this interval.

    exclude_ref leaves one booking out of the count. Rescheduling needs it:
    moving 10:00 to 10:30 overlaps the booking being moved, so without this the
    appointment would collide with itself and the move would be refused.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM bookings
        WHERE resource = ?
          AND status = 'active'
          AND ref_code IS NOT ?
          AND NOT (ends_at <= ? OR starts_at >= ?)
        """,
        (resource, exclude_ref, starts_at, ends_at),
    ).fetchone()
    return row["cnt"]


# ---------------------------- parsing time ----------------------------


def _parse_nights(start: str, end: str) -> tuple[str, str, int, str | None]:
    """(starts_at, ends_at, number of nights, error)."""
    try:
        ci = date.fromisoformat(start)
        co = date.fromisoformat(end)
    except (ValueError, TypeError):
        return "", "", 0, _err("nights.bad_format")
    if (co - ci).days <= 0:
        return "", "", 0, _err("nights.end_before_start")
    # Without an upper bound, one slip of the tongue in the check-out date turns
    # into a booking of "four hundred nights, four hundred and eighty thousand"
    # — and a room blocked for a year.
    if (co - ci).days > MAX_UNITS:
        return "", "", 0, _err(
            "nights.too_long",
            count=(co - ci).days,
            unit=_plural((co - ci).days, BOOK.unit),
            max=MAX_UNITS,
        )
    if ci < BIZ.today():
        return "", "", 0, _err("nights.in_past", today=BIZ.today().isoformat())
    if (ci - BIZ.today()).days > BOOK.horizon_days:
        return "", "", 0, _err("nights.beyond_horizon", days=BOOK.horizon_days)
    return ci.isoformat(), co.isoformat(), (co - ci).days, None


def _parse_slot(start: str, duration_min: int) -> tuple[str, str, str | None]:
    """(starts_at, ends_at, error) for an appointment at a specific time."""
    raw = (start or "").strip().replace(" ", "T")
    try:
        begin = datetime.strptime(raw[:16], _ISO_MIN)
    except (ValueError, TypeError):
        return "", "", _err("slot.bad_format")
    finish = begin + timedelta(minutes=duration_min)

    # Ten minutes ago is not "in the past", it is "come right now": while the
    # caller is negotiating, the minute they named quietly becomes the past. The
    # emergency plumber config used to lose three or four turns in a row to
    # "that time has already passed" in answer to a request to come immediately
    # — on a call where water is running down a wall.
    if begin < BIZ.now() - timedelta(minutes=GRACE_MIN):
        return "", "", _err("slot.in_past", now=_spoken_now())
    if (begin.date() - BIZ.today()).days > BOOK.horizon_days:
        return "", "", _err("slot.beyond_horizon", days=BOOK.horizon_days)

    hours = BOOK.hours_for(begin.date())
    if not hours:
        return "", "", _err("slot.closed_day", hours=BOOK.hours_human())
    open_t, close_t = hours
    if BOOK.is_round_clock(begin.date()):
        return begin.strftime(_ISO_MIN), finish.strftime(_ISO_MIN), None
    if begin.time() < open_t:
        return "", "", _err("slot.before_opening", opens=L.time(open_t))
    if finish.time() > close_t or finish.date() != begin.date():
        return "", "", _err(
            "slot.after_closing", duration=duration_min, closes=L.time(close_t)
        )
    return begin.strftime(_ISO_MIN), finish.strftime(_ISO_MIN), None


def free_slots(
    resource_key: str, day: date, limit: int = 4, near: datetime | None = None
) -> list[str]:
    """Free appointment start times for this day, as "HH:MM".

    This is what rescues a conversation when the requested time is taken: naming
    two or three alternatives is far better than saying "no" and waiting for the
    caller to guess again. So by default we return the times closest to the one
    they wanted, not the earliest on the schedule — someone who asked for noon
    is not helped by nine in the morning.
    """
    res = BOOK.resource(resource_key)
    if not res:
        return []
    now = BIZ.now()
    hours = BOOK.hours_for(day)
    if not hours:
        return []
    free: list[datetime] = []
    with _connect() as conn:
        for begin in slot_grid(day):
            finish = begin + timedelta(minutes=res.duration_min)
            if begin < now:
                continue
            if not BOOK.is_round_clock(day) and finish.time() > hours[1]:
                continue
            busy = _occupied(conn, resource_key, begin.strftime(_ISO_MIN), finish.strftime(_ISO_MIN))
            if busy < res.capacity:
                free.append(begin)
    if near:
        free.sort(key=lambda t: abs((t - near).total_seconds()))
    # Machine format on purpose: these go straight back into check_availability,
    # whose argument is documented as 24-hour HH:MM. Handing the model "1 PM"
    # and asking it to convert is how "01:00" happens mid-call. The spoken form
    # is added alongside by check_availability.
    return [t.strftime("%H:%M") for t in sorted(free[:limit])]


# ------------------------------ availability ------------------------------

_MONTHS = tuple(L.raw("numbers.months"))
#: Forms of the billing unit, in the order the plural rule expects.
_UNIT_FORMS = {k: tuple(v) for k, v in L.raw("numbers.units").items()}
_PLURAL_RULE = L.text("numbers.plural_rule")


def _plural(n: int, unit: str) -> str:
    """The right form of the unit for this count.

    The RULE is a property of the language, not of the text, so it comes from
    the pack too: Slavic languages need three forms (1 / 2-4 / 5+), English gets
    by with two. A spoken string is read out loud, so "3 night" will not do.
    """
    forms = _UNIT_FORMS.get(unit)
    if not forms:
        return unit
    if _PLURAL_RULE == "english":
        return forms[0] if n == 1 else forms[-1]
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _spoken_now() -> str:
    """The current moment, said the way this language says it.

    The prompt instructs the agent to read an error out loud verbatim, so an ISO
    date and a 24-hour clock inside one would be read to an American caller as
    "two thousand twenty six dash zero eight dash ten, fourteen thirty".
    """
    now = BIZ.now()
    date_part = L.text("spoken.date", day=now.day, month=_MONTHS[now.month - 1])
    return L.text("spoken.datetime", date=date_part, time=L.time(now))


def _spoken(label: str, starts_at: str, ends_at: str, units: int, price: int) -> str:
    """A ready-made summary of the booking — exactly what the agent should say.

    Why: the model is not obliged to do arithmetic. When the tool returned a
    scattering of fields, it regularly voiced its own version: it said
    "check-out on the twentieth" while the twenty-first went into the database,
    and "two nights, two thousand four hundred" for a one-night booking at
    twelve hundred. The caller hears one thing, the business sees another, and
    they argue about it at the front desk. Now every number is assembled into
    one string from the database and the model only has to say it in words.
    """
    def dm(iso: str) -> str:
        d = date.fromisoformat(iso[:10])
        return L.text("spoken.date", day=d.day, month=_MONTHS[d.month - 1])

    money = L.text("spoken.money", price=price, currency=BIZ.currency)
    if BOOK.is_slots:
        return L.text(
            "spoken.slot", label=label, date=dm(starts_at), time=L.time(starts_at[11:16]),
            money=money,
        )
    return L.text(
        "spoken.nights",
        label=label,
        start=dm(starts_at),
        end=dm(ends_at),
        units=units,
        unit=_plural(units, BOOK.unit),
        money=money,
    )


def _alternatives_for(resource_key: str, start: str) -> dict:
    """Free times near what was asked for, in both machine and spoken form.

    Only for slots: the nights model has no grid of times to offer, and
    inventing one would be worse than saying nothing.
    """
    if not BOOK.is_slots:
        return {}
    raw = (start or "").strip().replace(" ", "T")
    try:
        wanted = datetime.strptime(raw[:16], _ISO_MIN)
    except (ValueError, TypeError):
        return {}  # the time could not be read at all — nothing to be near

    day = wanted.date()
    if day < BIZ.today():
        day = BIZ.today()
    times = free_slots(resource_key, day, near=wanted)
    for ahead in range(1, 8):
        if times:
            break
        nxt = day + timedelta(days=ahead)
        times = free_slots(resource_key, nxt)
        day = nxt if times else day
    if not times:
        return {}
    return {
        "alternatives": times,
        "alternatives_day": day.isoformat(),
        "alternatives_spoken": [L.time(t) for t in times],
    }


def check_availability(
    resource: str,
    start: str,
    end: str | None = None,
    party_size: int = 1,
    exclude_ref: str | None = None,
) -> dict:
    """Is it free, and what does it cost. One entry point for both time models.

    nights: start = check-in date, end = check-out date (YYYY-MM-DD).
    slots:  start = 'YYYY-MM-DDTHH:MM', end is derived from the service length.
    """
    # Error responses must NOT carry "available": the model saw
    # `available: false` next to the explanation and told the caller "there is
    # nothing free", when in fact there was plenty. Measured on the emergency
    # plumber config: someone with a burst pipe was told "we have no plumbers",
    # while two crews were free.
    key = BOOK.normalize_resource(resource)
    if key is None:
        known = ", ".join(r.label for r in BOOK.resources)
        return {"error": _err("resource_unknown", resource=repr(resource), known=known)}
    res = BOOK.resource(key)

    if BOOK.is_slots:
        starts_at, ends_at, err = _parse_slot(start, res.duration_min)
        units, price_per_unit = 1, res.price
    else:
        starts_at, ends_at, units, err = _parse_nights(start, end or "")
        price_per_unit = res.price
    if err:
        # An error that says "offer an earlier time" while returning nothing to
        # offer leaves the agent with a choice between silence and invention,
        # and it invents: measured at four calls out of five, once claiming
        # there was no availability without having checked anything. So every
        # schedule error carries real free times with it.
        return {"error": err, **_alternatives_for(key, start)}

    if BOOK.ask_party and party_size > BOOK.max_party:
        return {"error": _err("party_too_large", max=BOOK.max_party)}

    with _connect() as conn:
        left = res.capacity - _occupied(conn, key, starts_at, ends_at, exclude_ref)

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
    if left > 0:
        out["spoken"] = _spoken(res.label, starts_at, ends_at, units, out["total_price"])
    if BOOK.is_slots:
        wanted = datetime.strptime(starts_at, _ISO_MIN)
        out["duration_min"] = res.duration_min
        if left <= 0:
            # Taken — offer a replacement immediately, the same way an error
            # does. Without this the agent stalls and asks the caller to guess.
            out.update(_alternatives_for(key, start))
    else:
        out["units"] = units
        out["unit_label"] = BOOK.unit
        out["price_per_unit"] = price_per_unit
    return out


# ------------------------------- writing -------------------------------


def _generate_ref_code(conn: sqlite3.Connection) -> str:
    """A unique booking reference — FOUR digits.

    It used to be six, and the model mangled them systematically: the tool
    returned 919873 and the agent said "nine hundred ninety-one thousand eight
    hundred seventy-three" (991873), so the caller wrote down a reference that
    does not exist. A four-digit number is read correctly, and collisions are a
    non-issue at the volume of a single business: uniqueness is checked right
    here, and a six-digit fallback remains if the space is ever exhausted.
    """
    for _ in range(30):
        code = BOOK.ref_prefix + str(uuid.uuid4().int % 9_000 + 1_000)
        if not conn.execute("SELECT 1 FROM bookings WHERE ref_code = ?", (code,)).fetchone():
            return code
    return BOOK.ref_prefix + str(uuid.uuid4().int % 900_000 + 100_000)


def create_booking(
    resource: str,
    start: str,
    customer_name: str,
    phone: str,
    end: str | None = None,
    party_size: int = 1,
    details: dict | None = None,
) -> dict:
    """Create a booking. Availability is re-checked before the insert."""
    phone, phone_error = validate_phone(phone)
    if phone_error:
        return {"success": False, "error": phone_error}
    customer_name, name_error = validate_name(customer_name)
    if name_error:
        return {"success": False, "error": name_error}

    # Industry-mandatory fields (the address to visit, and so on) are checked
    # here rather than left to the prompt: a cleaning job with no address is a
    # defect, not a detail.
    details = {k: v for k, v in (details or {}).items() if v and str(v).strip()}
    missing = [f.label for f in BOOK.intake if f.required and not details.get(f.key)]
    if missing:
        return {"success": False, "error": _err("intake_missing", fields=", ".join(missing))}

    avail = check_availability(resource, start, end, party_size)
    if avail.get("error"):
        return {"success": False, "error": avail["error"]}
    if not avail["available"]:
        out = {"success": False, "error": _err("no_space")}
        if avail.get("alternatives"):
            out["alternatives"] = avail["alternatives"]
        return out

    # The availability check above and the insert below are two separate
    # actions, and a concurrent call fits neatly between them. Measured: seven
    # simultaneous bookings against a resource with capacity=3 all succeeded. So
    # the final decision is taken inside a transaction that holds the write lock
    # for the duration of the check (BEGIN IMMEDIATE).
    with _connect() as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            res = BOOK.resource(avail["resource"])
            taken = _occupied(conn, avail["resource"], avail["starts_at"], avail["ends_at"])
            if res.capacity - taken <= 0:
                conn.execute("ROLLBACK")
                return {"success": False, "error": _err("no_space")}
            ref_code = _generate_ref_code(conn)
            conn.execute(
                """
                INSERT INTO bookings
                    (id, ref_code, resource, starts_at, ends_at, party_size,
                     customer_name, phone, total_price, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    ref_code,
                    avail["resource"],
                    avail["starts_at"],
                    avail["ends_at"],
                    party_size,
                    customer_name,
                    phone,
                    avail["total_price"],
                    json.dumps(details, ensure_ascii=False) if details else None,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "success": True,
        "ref_code": ref_code,
        "spoken": avail.get("spoken", ""),
        "resource_label": avail["resource_label"],
        "starts_at": avail["starts_at"],
        "ends_at": avail["ends_at"],
        "party_size": party_size,
        "customer_name": customer_name,
        "phone": phone,
        "total_price": avail["total_price"],
        "currency": BIZ.currency,
        **({"details": details} if details else {}),
    }


# --------------------- changing an existing booking ---------------------
#
# Until now a booking could only be created. That made every mistake permanent —
# including the agent's own, when it created one before the caller confirmed —
# and made "move it to tomorrow", probably the second most common call a
# receptionist gets, impossible to serve.
#
# Nothing is ever deleted: a cancelled booking keeps its row with status
# 'cancelled', because the business wants to see that someone booked and then
# called it off.


def find_bookings(phone: str) -> dict:
    """Active, still-upcoming bookings for a phone number.

    Callers do not remember reference codes; they remember their own number, and
    on a phone line it is the one thing the system already knows about them.

    Worth stating plainly: this is not authentication. Anyone who knows a number
    can ask what is booked against it. On a real line the caller ID makes that
    reasonable; over the browser demo it is simply an open lookup, and a
    business handling sensitive appointments needs more than this.
    """
    phone, error = validate_phone(phone)
    if error:
        return {"error": error}
    now = BIZ.now().strftime(_ISO_MIN) if BOOK.is_slots else BIZ.today().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE phone = ? AND status = 'active'"
            " AND ends_at >= ? ORDER BY starts_at",
            (phone, now),
        ).fetchall()
    found = [
        {
            "ref_code": r["ref_code"],
            "resource_label": (BOOK.resource(r["resource"]) or BOOK.resources[0]).label,
            "starts_at": r["starts_at"],
            "ends_at": r["ends_at"],
            "customer_name": r["customer_name"],
            "total_price": r["total_price"],
            "spoken": _spoken(
                (BOOK.resource(r["resource"]) or BOOK.resources[0]).label,
                r["starts_at"],
                r["ends_at"],
                _units_of(r),
                r["total_price"],
            ),
        }
        for r in rows
    ]
    if not found:
        return {"found": 0, "error": _err("nothing_booked")}
    return {"found": len(found), "bookings": found}


def _active_booking(conn: sqlite3.Connection, ref_code: str):
    return conn.execute(
        "SELECT * FROM bookings WHERE ref_code = ?", ((ref_code or "").strip().upper(),)
    ).fetchone()


def cancel_booking(ref_code: str) -> dict:
    """Cancel a booking by its reference code."""
    with _connect() as conn:
        row = _active_booking(conn, ref_code)
        if row is None:
            return {"success": False, "error": _err("booking_not_found", ref_code=ref_code)}
        if row["status"] != "active":
            return {"success": False, "error": _err("booking_already_cancelled")}
        conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE ref_code = ?", (row["ref_code"],)
        )
        conn.commit()
    label = (BOOK.resource(row["resource"]) or BOOK.resources[0]).label
    return {
        "success": True,
        "ref_code": row["ref_code"],
        "spoken": _spoken(
            label, row["starts_at"], row["ends_at"], _units_of(row), row["total_price"]
        ),
    }


def reschedule_booking(
    ref_code: str, start: str, end: str | None = None, resource: str | None = None
) -> dict:
    """Move a booking to a new time, keeping its reference code.

    The new slot is checked exactly as a fresh booking would be — hours, horizon,
    capacity — with this booking left out of the count, so it does not collide
    with itself.
    """
    with _connect() as conn:
        row = _active_booking(conn, ref_code)
    if row is None:
        return {"success": False, "error": _err("booking_not_found", ref_code=ref_code)}
    if row["status"] != "active":
        return {"success": False, "error": _err("booking_already_cancelled")}

    label = resource or (BOOK.resource(row["resource"]) or BOOK.resources[0]).label
    avail = check_availability(
        label, start, end, row["party_size"], exclude_ref=row["ref_code"]
    )
    if avail.get("error"):
        return {"success": False, "error": avail["error"]}
    if not avail["available"]:
        out = {"success": False, "error": _err("no_space")}
        if avail.get("alternatives"):
            out["alternatives"] = avail["alternatives"]
            out["alternatives_spoken"] = avail.get("alternatives_spoken")
        return out

    with _connect() as conn:
        conn.execute(
            "UPDATE bookings SET resource = ?, starts_at = ?, ends_at = ?, total_price = ?"
            " WHERE ref_code = ?",
            (
                avail["resource"],
                avail["starts_at"],
                avail["ends_at"],
                avail["total_price"],
                row["ref_code"],
            ),
        )
        conn.commit()
    return {
        "success": True,
        "ref_code": row["ref_code"],
        "spoken": avail.get("spoken", ""),
        "starts_at": avail["starts_at"],
        "ends_at": avail["ends_at"],
        "total_price": avail["total_price"],
    }


def save_call(summary: dict) -> None:
    """Store one call's transcript. Swallows errors on purpose — the same rule
    as the event log: recording a call must never be able to break one."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO calls (id, business, started_at, lines, metrics, audio_path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    summary["call_id"],
                    summary.get("business", BIZ.key),
                    summary.get("started_at", ""),
                    json.dumps(summary.get("lines", []), ensure_ascii=False),
                    json.dumps(summary.get("metrics", {}), ensure_ascii=False, default=str),
                    summary.get("audio"),
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        from loguru import logger

        logger.error(f"could not store the call transcript: {e}")


def list_calls(limit: int = 20) -> list[dict]:
    """Recent calls, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {
            "call_id": r["id"],
            "business": r["business"],
            "started_at": r["started_at"],
            "created_at": _to_business_time(r["created_at"]),
            "lines": json.loads(r["lines"]),
            "metrics": json.loads(r["metrics"]) if r["metrics"] else {},
            "audio": r["audio_path"],
        }
        for r in rows
    ]


def log_event(kind: str, detail: str | None = None) -> None:
    """Record something the agent did. Swallows errors on purpose: the log must
    never be able to break a call."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO events (id, kind, detail) VALUES (?, ?, ?)",
                (uuid.uuid4().hex, kind, detail),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def list_events(since: str = "") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, detail, created_at FROM events WHERE created_at >= ?"
            " ORDER BY created_at",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def take_message(customer_name: str, phone: str, topic: str) -> dict:
    """Record a callback request (a question outside the booking flow)."""
    phone, phone_error = validate_phone(phone)
    if phone_error:
        return {"success": False, "error": phone_error}
    if not (topic and topic.strip()):
        return {"success": False, "error": _err("message_no_topic")}
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
    return {"success": True, "note": L.text("notes.message_recorded")}


# ------------------------------ reading ------------------------------


def _units_of(row: sqlite3.Row) -> int:
    if BOOK.is_slots:
        return 1
    try:
        return (date.fromisoformat(row["ends_at"]) - date.fromisoformat(row["starts_at"])).days
    except ValueError:
        return 0


def list_bookings() -> list[dict]:
    """Every booking, newest first (for the dashboard)."""
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
                "status": r["status"],
                "details": json.loads(r["details"]) if r["details"] else {},
                "created_at": _to_business_time(r["created_at"]),
            }
        )
    return out


def list_messages() -> list[dict]:
    """Every callback request, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    return [{**dict(r), "created_at": _to_business_time(r["created_at"])} for r in rows]


# ------------------------------ dashboard ------------------------------


def _pct_delta(cur: float, prev: float) -> int | None:
    """Period-over-period change in percent. None when there is nothing to
    compare against."""
    if prev <= 0:
        return None
    return round((cur - prev) / prev * 100)


def _load_status(left: int, total: int) -> str:
    """good — space available; warning — nearly full; critical — full;
    closed — the business is not open today (zero capacity is not a full house).
    """
    if total <= 0:
        return "closed"
    if left <= 0:
        return "critical"
    if total and left / total <= 0.25:
        return "warning"
    return "good"


def _capacity_today(res) -> int:
    """How many bookings for this resource physically fit into today."""
    if not BOOK.is_slots:
        return res.capacity
    hours = BOOK.hours_for(BIZ.today())
    if not hours or not res.duration_min:
        return 0
    open_min = (
        datetime.combine(BIZ.today(), hours[1]) - datetime.combine(BIZ.today(), hours[0])
    ).seconds // 60
    return res.capacity * max(open_min // res.duration_min, 1)


def stats(period_days: int = 30) -> dict:
    """Dashboard aggregates for the current period, plus deltas to the previous.

    The KPIs are keyed on when a booking was CREATED — that is the agent's work.
    Occupancy is the current state instead: for nights it is active bookings
    against the number of rooms, for slots it is today's appointments against
    the capacity of the day.
    """
    bookings = list_bookings()
    now = BIZ.now()
    cur_start = now - timedelta(days=period_days)
    prev_start = now - timedelta(days=2 * period_days)
    horizon = now.strftime(_ISO_MIN) if BOOK.is_slots else BIZ.today().isoformat()
    today_prefix = BIZ.today().isoformat()

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
