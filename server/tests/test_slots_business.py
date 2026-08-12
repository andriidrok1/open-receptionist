"""The appointment path, exercised against a business that is not the default.

Four of five Ukrainian businesses and the only English one book by appointment
rather than by the night — and none of it had a deterministic test, because the
business is fixed when the modules are imported and the rest of the suite runs
as a hotel. A regression here would have been invisible.

Each case therefore runs in a subprocess with its own BUSINESS and its own
database, and reports back as JSON. Slower than an in-process test and still
under a second in total.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
SLOTS_BUSINESSES = ["dental_us", "clinic", "emergency"]

PROBE = r"""
import json, sys
from datetime import timedelta
import crm
from business import BUSINESS

crm.init_db()
book = BUSINESS.booking
res = book.resources[0]

def open_day(offset=1):
    d = BUSINESS.today() + timedelta(days=offset)
    for _ in range(9):
        if book.hours_for(d):
            return d
        d += timedelta(days=1)
    return None

day = open_day()
hours = book.hours_for(day)
at = lambda h, m=0: f"{day.isoformat()}T{h:02d}:{m:02d}"

out = {
    "key": BUSINESS.key,
    "language": BUSINESS.language,
    "day": day.isoformat(),
    "weekdays": list(BUSINESS.pack.raw("calendar.weekdays")),
    "round_clock": book.is_round_clock(day),
    "ok": crm.check_availability(res.label, at(hours[0].hour, hours[0].minute)),
    "before_opening": crm.check_availability(res.label, at(max(hours[0].hour - 2, 0))),
    "past": crm.check_availability(res.label, f"{(BUSINESS.today() - timedelta(days=2)).isoformat()}T10:00"),
    "far": crm.check_availability(res.label, f"{(BUSINESS.today() + timedelta(days=book.horizon_days + 5)).isoformat()}T10:00"),
    "unknown_resource": crm.check_availability("no such service", at(hours[0].hour)),
    "message": crm.take_message("Test Person", crm.validate_phone(
        __import__("phonenumbers").format_number(
            __import__("phonenumbers").example_number_for_type(
                BUSINESS.country, __import__("phonenumbers").PhoneNumberType.MOBILE),
            __import__("phonenumbers").PhoneNumberFormat.E164))[0], "a question"),
}

# Fill the first slot to capacity, then ask again: alternatives must appear.
# The industry's required intake has to be supplied here too — without an
# address a cleaning or emergency booking is legitimately refused, the slot
# stays free, and the test would quietly be checking an empty hour.
start = at(hours[0].hour, hours[0].minute)
phone = crm.list_messages()[0]["phone"]
details = {f.key: "test value" for f in book.intake}
made = [
    crm.create_booking(res.label, start, f"Test Person{i}", phone, details=details)
    for i in range(res.capacity)
]
out["filled"] = sum(bool(m.get("success")) for m in made)
out["fill_errors"] = [m.get("error") for m in made if not m.get("success")]
out["when_full"] = crm.check_availability(res.label, start)
print(json.dumps(out, ensure_ascii=False, default=str))
"""


@pytest.fixture(scope="module")
def probes():
    results = {}
    for key in SLOTS_BUSINESSES:
        env = {
            "PATH": "/usr/bin:/bin",
            "BUSINESS": key,
            "CRM_DB": str(Path(tempfile.mkdtemp(prefix=f"slots-{key}-")) / "t.db"),
            "HOME": str(Path.home()),
        }
        proc = subprocess.run(
            [sys.executable, "-c", PROBE], cwd=SERVER, env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, f"{key}: {proc.stderr[-2000:]}"
        results[key] = json.loads(proc.stdout.strip().splitlines()[-1])
    return results


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_an_open_hour_is_bookable(key, probes):
    assert probes[key]["ok"].get("available") is True, probes[key]["ok"]
    assert probes[key]["ok"].get("spoken")


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_errors_never_carry_an_availability_flag(key, probes):
    """The bug this guards against happened on the emergency config.

    The previous version of this test only ever ran the nights path, so the very
    configuration named in its own docstring was never exercised.
    """
    p = probes[key]
    for case in ("past", "far", "unknown_resource"):
        result = p[case]
        assert "error" in result, f"{key}/{case} was expected to fail: {result}"
        assert "available" not in result, (
            f"{key}/{case} carries an availability flag next to an error — "
            f"that is what made the agent answer 'we are full'"
        )


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_before_opening_is_refused_unless_open_around_the_clock(key, probes):
    p = probes[key]
    if p["round_clock"]:
        return
    assert "error" in p["before_opening"], p["before_opening"]


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_the_probe_actually_filled_the_hour(key, probes):
    """Guard for the test itself.

    The first version supplied no intake fields, so every booking was refused,
    the hour stayed free — and the alternatives test below was checking an empty
    slot while looking like it passed.
    """
    p = probes[key]
    assert p["filled"] > 0, f"{key}: nothing was booked — {p['fill_errors']}"


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_a_full_hour_offers_alternatives(key, probes):
    """Saying "no" without an alternative is how a call ends in nothing."""
    full = probes[key]["when_full"]
    assert full.get("available") is False, full
    assert full.get("alternatives"), f"{key}: nothing offered instead"
    assert full.get("alternatives_spoken"), f"{key}: nothing to say out loud"
    for machine in full["alternatives"]:
        assert len(machine) == 5 and machine[2] == ":", (
            f"{key}: alternatives go back into the tool, which documents 24-hour "
            f"HH:MM — got {machine!r}"
        )


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_take_message_works(key, probes):
    """Every escalation goes through this, and nothing tested it."""
    assert probes[key]["message"].get("success") is True, probes[key]["message"]


@pytest.mark.parametrize("key", SLOTS_BUSINESSES)
def test_the_spoken_slot_names_the_real_weekday(key, probes):
    """The weekday the agent says out loud comes from the store, not its memory.

    Listing the fortnight in the prompt made this error rarer and did not end it:
    asked for a Thursday, the agent picked a Tuesday and called it Thursday, with
    the correct calendar in front of it. Nothing downstream catches that — the
    date is real and the slot is free — so the weekday has to arrive as a fact
    alongside the price, in the same sentence the agent is told to read out.
    """
    from datetime import date

    p = probes[key]
    spoken = p["ok"].get("spoken") or ""
    names = p["weekdays"]
    real = names[date.fromisoformat(p["day"]).weekday()]
    assert real in spoken, f"{key}: {spoken!r} does not name {real!r} ({p['day']})"
    others = [n for n in names if n != real and n in spoken]
    assert not others, f"{key}: {spoken!r} also names {others}"
