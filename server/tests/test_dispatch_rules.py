"""The two dispatch rules that moved from prompt text into code.

Arrival windows: trades quote "between nine and ten", never "at nine" — the
slot underneath stays exact for the capacity math, only the words change.

Intake refusal: "someone 18 or older must be at the property" lived in the
prompt and held right up until a caller insisted. Now the field is typed, the
rule is in the config, and a booking that violates it cannot be written.

Both need the emergency business loaded, and the business is fixed at import
time — so, like test_slots_business.py, everything runs in a subprocess with
its own BUSINESS and its own database and reports back as JSON.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent

PROBE = r"""
import json
from datetime import timedelta
import crm
from business import BUSINESS
from agent_tools import CREATE_BOOKING_SCHEMA

crm.init_db()
book = BUSINESS.booking
res = book.resources[0]

day = (BUSINESS.today() + timedelta(days=1)).isoformat()
start = f"{day}T09:00"

def booking(adult):
    details = {"address": "1200 Irving Street, San Francisco",
               "problem": "leak under the sink"}
    if adult is not None:
        details["adult_present"] = adult
    return crm.create_booking(
        resource=res.label, start=start, end=None,
        customer_name="Test Person", phone="4155551234", party_size=1,
        details=details,
    )

out = {
    "schema_type": CREATE_BOOKING_SCHEMA.properties["adult_present"]["type"],
    "window_min": book.arrival_window_min,
    "avail_spoken": crm.check_availability(res.label, start, None, 1).get("spoken", ""),
    "refused_bool": booking(False),
    "refused_word": booking("no"),
    "missing": booking(None),
    "booked": booking(True),
}
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def probe() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "PATH": "/usr/bin:/bin",
            "BUSINESS": "emergency_us",
            "CRM_DB": str(Path(tmp) / "probe.db"),
            "ADDRESS_PROVIDER": "off",  # never touch the network from a test
        }
        proc = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=SERVER, env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        return json.loads(proc.stdout)


# ------------------------------ arrival windows ------------------------------


def test_the_spoken_time_is_a_window_not_a_minute(probe):
    """"Between 9 AM and 10 AM" — both ends computed in code, because the
    model would otherwise be doing the arithmetic, and it would get it wrong
    exactly often enough to matter."""
    spoken = probe["avail_spoken"]
    assert "between" in spoken.lower(), spoken
    assert "9 AM" in spoken and "10 AM" in spoken, spoken


def test_the_booking_confirmation_speaks_the_same_window(probe):
    spoken = probe["booked"].get("spoken", "")
    assert "between" in spoken.lower(), spoken


def test_the_slot_underneath_stays_exact(probe):
    """The window is words, not schedule: capacity is still counted over the
    exact [start, start+duration) the caller never hears."""
    assert probe["booked"]["starts_at"].endswith("T09:00")


def test_a_business_without_a_window_keeps_exact_times():
    """Zero is the default, and a dental chair is booked to the minute."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "PATH": "/usr/bin:/bin",
            "BUSINESS": "dental_us",
            "CRM_DB": str(Path(tmp) / "probe.db"),
            "ADDRESS_PROVIDER": "off",
        }
        inner = (
            "import json, crm\n"
            "from datetime import timedelta\n"
            "from business import BUSINESS\n"
            "crm.init_db()\n"
            "book = BUSINESS.booking\n"
            "d = BUSINESS.today() + timedelta(days=1)\n"
            "for _ in range(9):\n"
            "    if book.hours_for(d): break\n"
            "    d += timedelta(days=1)\n"
            "h = book.hours_for(d)\n"
            "start = f'{d.isoformat()}T{h[0].hour:02d}:00'\n"
            "r = crm.check_availability(book.resources[0].label, start, None, 1)\n"
            "print(json.dumps({'window': book.arrival_window_min, 'spoken': r.get('spoken','')}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", inner],
            cwd=SERVER, env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        got = json.loads(proc.stdout)
        assert got["window"] == 0
        assert "arriving between" not in got["spoken"], got["spoken"]


# ------------------------------ intake refusal -------------------------------


def test_the_field_reaches_the_model_typed(probe):
    """true/false can be compared against a rule; "my kid is fifteen but very
    responsible" cannot."""
    assert probe["schema_type"] == "boolean"


def test_no_adult_refuses_the_booking(probe):
    r = probe["refused_bool"]
    assert not r.get("success"), r
    assert "eighteen" in r.get("error", ""), r


def test_the_rule_survives_the_answer_arriving_as_a_word(probe):
    """The value has been through a model, and models sometimes send "no"
    where the schema says boolean."""
    r = probe["refused_word"]
    assert not r.get("success"), r
    assert "eighteen" in r.get("error", ""), r


def test_false_is_an_answer_not_a_gap(probe):
    """The trap this feature nearly shipped with: a truthiness check dropped
    False from the details, and the refusal read as "you never asked" —
    sending the agent to ask the question again instead of declining."""
    missing = probe["missing"].get("error", "")
    refused = probe["refused_bool"].get("error", "")
    assert "adult on site" in missing, missing
    assert refused != missing


def test_an_adult_present_books_normally(probe):
    assert probe["booked"].get("success") is True
