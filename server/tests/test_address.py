"""Address checking, and the asymmetry it is built around.

The whole design rests on one distinction: a gazetteer that says "no such
address" and a gazetteer that does not answer must lead to OPPOSITE decisions.
Refuse the first, accept the second. Collapse them and you get one of two bad
systems — one that dispatches trucks to addresses that do not exist, or one that
stops taking emergency work whenever a government API has a bad minute.

Everything here runs offline. The network calls are replaced, because a test
that needs the census geocoder to be up tests the census geocoder.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import address as addr  # noqa: E402
from address import AddressCheck, check_address, in_service_area, provider_for  # noqa: E402

AREA = ("San Francisco", "Daly City")


# ------------------------------ provider choice ------------------------------


def test_us_defaults_to_the_free_provider(monkeypatch):
    """No key, no billing account, no signup — that is the point of the default."""
    monkeypatch.delenv("ADDRESS_PROVIDER", raising=False)
    assert provider_for("US") == "census"


def test_a_country_the_gazetteer_does_not_cover_turns_checking_off(monkeypatch):
    """The census geocoder knows the United States and nothing else.

    Left on elsewhere it would reject every Ukrainian address as nonexistent,
    which is worse than not checking: the agent would refuse every caller while
    sounding certain.
    """
    monkeypatch.delenv("ADDRESS_PROVIDER", raising=False)
    assert provider_for("UA") == "off"


def test_the_environment_overrides_the_country(monkeypatch):
    monkeypatch.setenv("ADDRESS_PROVIDER", "google")
    assert provider_for("US") == "google"


def test_checking_off_yields_no_opinion(monkeypatch):
    """"off" must look like an outage, not like a verdict — otherwise turning
    checking off would start refusing addresses instead of ignoring them."""
    monkeypatch.setenv("ADDRESS_PROVIDER", "off")
    check = check_address("anywhere at all", "US")
    assert check.reachable is False
    assert check.verified is False
    assert check.best == "anywhere at all"


# ------------------------------ the census reply -----------------------------


def _census_payload(matched: str, city: str, zipcode: str) -> dict:
    return {
        "result": {
            "addressMatches": [
                {
                    "matchedAddress": matched,
                    "addressComponents": {"city": city, "zip": zipcode},
                }
            ]
        }
    }


def test_a_misheard_street_is_repaired(monkeypatch):
    """The reason this layer earns its latency.

    Speech recognition gets 44% of street names wrong, and reading the address
    back does not catch it: the agent reads back what it misheard and the
    caller hears what they expect. A gazetteer returns the real spelling.
    """
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")
    monkeypatch.setattr(
        addr,
        "_get_json",
        lambda url, params: _census_payload("1200 IRVING ST, SAN FRANCISCO, CA, 94122", "SAN FRANCISCO", "94122"),
    )
    check = check_address("1200 Urving Street, San Francisco, CA", "US")
    assert check.verified is True
    assert check.best == "1200 IRVING ST, SAN FRANCISCO, CA, 94122"
    assert "Urving" not in check.best


def test_a_street_with_no_city_is_retried_with_the_service_area(monkeypatch):
    """The census matcher needs a city or a zip, and the model routinely passes
    the street alone even when the caller named the city two turns earlier. A
    live call burned five identical create_booking attempts on "1200 Irving
    Street" — a real address — before giving up."""
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")
    asked = []

    def fake(url, params):
        asked.append(params["address"])
        if "San Francisco" in params["address"]:
            return _census_payload("1200 IRVING ST, SAN FRANCISCO, CA, 94122", "SAN FRANCISCO", "94122")
        return {"result": {"addressMatches": []}}

    monkeypatch.setattr(addr, "_get_json", fake)
    check = check_address("1200 Irving Street", "US", hints=("San Francisco", "Daly City"))
    assert check.verified is True
    assert check.best == "1200 IRVING ST, SAN FRANCISCO, CA, 94122"
    assert asked == ["1200 Irving Street", "1200 Irving Street, San Francisco"]


def test_a_hint_already_in_the_text_is_not_appended_again(monkeypatch):
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")
    asked = []

    def fake(url, params):
        asked.append(params["address"])
        return {"result": {"addressMatches": []}}

    monkeypatch.setattr(addr, "_get_json", fake)
    check = check_address("1 Nowhere Lane, Daly City", "US", hints=("San Francisco", "Daly City"))
    assert check.verified is False
    assert "1 Nowhere Lane, Daly City, Daly City" not in asked


def test_an_outage_mid_retry_stops_the_hint_loop(monkeypatch):
    """The retries only follow an answered no-match. When the provider goes
    down, each extra hint would add a full timeout to a live call where the
    caller is listening to silence."""
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")
    calls = {"n": 0}

    def fake(url, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": {"addressMatches": []}}
        return None  # provider went away

    monkeypatch.setattr(addr, "_get_json", fake)
    check = check_address("1200 Irving Street", "US", hints=("San Francisco", "Daly City"))
    assert calls["n"] == 2, "the second hint must not be tried against a dead provider"
    assert check.reachable is True, "the first answer stands: a real no-match"
    assert check.verified is False


def test_no_match_is_a_refusal_not_an_outage(monkeypatch):
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")
    monkeypatch.setattr(addr, "_get_json", lambda url, params: {"result": {"addressMatches": []}})
    check = check_address("1 Nowhere Lane, Atlantis, CA", "US")
    assert check.reachable is True, "the provider answered; that is not an outage"
    assert check.verified is False


def test_an_unreachable_provider_is_not_a_refusal(monkeypatch):
    """A timeout must never look like "this address does not exist".

    An emergency service that stops booking because a geocoder is down has
    turned a minor outage into lost jobs on the one night it cannot afford to.
    """
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")
    monkeypatch.setattr(addr, "_get_json", lambda url, params: None)
    check = check_address("1200 Irving Street, San Francisco", "US")
    assert check.reachable is False
    assert check.verified is False
    assert check.best == "1200 Irving Street, San Francisco", "the spoken address must survive"


def test_an_empty_address_is_not_sent_anywhere(monkeypatch):
    monkeypatch.setenv("ADDRESS_PROVIDER", "census")

    def explode(*_args, **_kwargs):
        raise AssertionError("the geocoder was called with nothing to look up")

    monkeypatch.setattr(addr, "_get_json", explode)
    assert check_address("   ", "US").verified is False


def test_google_without_a_key_degrades_to_an_outage(monkeypatch):
    """A missing key is a misconfiguration by the operator. The caller should
    not lose their booking over it, so it reads as "no opinion"."""
    monkeypatch.setenv("ADDRESS_PROVIDER", "google")
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    check = check_address("1200 Irving St", "US")
    assert check.reachable is False and check.verified is False


# ------------------------------- service area --------------------------------


def _verified(city: str = "SAN FRANCISCO", postcode: str = "94122") -> AddressCheck:
    return AddressCheck(raw="x", canonical="x", city=city, postcode=postcode, verified=True)


def test_the_area_is_matched_on_the_canonical_city():
    assert in_service_area(_verified("SAN FRANCISCO"), AREA) is True
    assert in_service_area(_verified("BURNABY"), AREA) is False


def test_case_and_punctuation_do_not_decide_whether_a_truck_rolls():
    assert in_service_area(_verified("san francisco,"), AREA) is True


def test_a_postcode_counts_as_an_area():
    """Owners think in whichever unit their patch is drawn in — some name
    cities, some list ZIPs."""
    assert in_service_area(_verified("SOMEWHERE", "94014"), ("94014", "94015")) is True


def test_no_declared_area_means_no_limit():
    assert in_service_area(_verified("ANYWHERE"), ()) is True


def test_an_unverified_address_is_never_ruled_out_of_area():
    """Being unable to place an address is not evidence that it is far away.

    Refusing here would convert a geocoder hiccup into "we don't come to your
    neighbourhood", which is both wrong and the kind of thing a caller repeats
    to their neighbours.
    """
    outside = AddressCheck(raw="1229 Oakridge Drive, Burnaby", verified=False, reachable=True)
    assert in_service_area(outside, AREA) is True


# --------------------- wired into the booking, end to end --------------------
#
# crm binds its business at import time, so this runs in a subprocess with
# BUSINESS=emergency_us and its own database. The geocoder is stubbed inside the
# probe: what is under test is what create_booking DOES with each verdict.

PROBE = r"""
import json
from datetime import timedelta
import crm
from address import AddressCheck
from business import BUSINESS

crm.init_db()
book = BUSINESS.booking
res = book.resources[0]
day = BUSINESS.today() + timedelta(days=1)
start = f"{day.isoformat()}T10:00"

def book_with(verdict):
    crm.check_address = lambda raw, country="US", hints=(): verdict
    return crm.create_booking(
        resource=res.label, start=start, customer_name="Michael Greene",
        phone="4155551234",
        details={"address": "1200 Urving Street, San Francisco",
                 "problem": "leak under the sink", "adult_present": "yes"},
    )

out = {}

# 1. Verified and inside the area: the booking is written with the REPAIRED text.
out["verified"] = book_with(AddressCheck(
    raw="1200 Urving Street, San Francisco",
    canonical="1200 IRVING ST, SAN FRANCISCO, CA, 94122",
    city="SAN FRANCISCO", postcode="94122", verified=True, reachable=True))

# 2. The gazetteer answered and found nothing: refuse.
out["not_found"] = book_with(AddressCheck(
    raw="1 Nowhere Lane", verified=False, reachable=True))

# 3. Verified but outside the service area: refuse.
out["out_of_area"] = book_with(AddressCheck(
    raw="1229 Oakridge Drive, Burnaby", canonical="1229 OAKRIDGE DR, BURNABY",
    city="BURNABY", verified=True, reachable=True))

# 4. Gazetteer unreachable: the job goes through, address as spoken.
out["unreachable"] = book_with(AddressCheck(
    raw="1200 Urving Street, San Francisco", reachable=False))

# Straight from the table, not through find_bookings: that call is the view the
# agent reads back to a caller looking up their own booking, and it deliberately
# carries no intake details. What the plumber is sent is the stored row.
with crm._connect() as conn:
    rows = conn.execute("SELECT details FROM bookings ORDER BY rowid").fetchall()
out["stored_addresses"] = [
    (json.loads(r["details"] or "{}") or {}).get("address") for r in rows
]
print("@@" + json.dumps(out))
"""


@pytest.fixture(scope="module")
def booked() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "PATH": "/usr/bin:/bin",
            "BUSINESS": "emergency_us",
            "CRM_DB": str(Path(tmp) / "probe.db"),
            "ADDRESS_PROVIDER": "off",  # the stub decides; never touch the network
        }
        proc = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=SERVER, env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        line = next(x for x in proc.stdout.splitlines() if x.startswith("@@"))
        return json.loads(line[2:])


def test_a_verified_address_books(booked):
    assert booked["verified"].get("success") is True, booked["verified"]


def test_the_stored_address_is_the_corrected_one(booked):
    """The plumber reads the booking, not the transcript.

    Storing "Urving" because that is what was said would move the error from the
    phone call into the job sheet, where nobody can catch it.

    Checked on the specific row rather than across all of them: two of the four
    cases book, and the other one is SUPPOSED to keep the misheard spelling —
    an earlier version of this test asserted "Urving" appears nowhere, which
    contradicted the outage case sitting three tests below it.
    """
    written = booked["stored_addresses"]
    assert len(written) == 2, f"expected the two accepted cases to be stored, got {written}"
    assert written[0] == "1200 IRVING ST, SAN FRANCISCO, CA, 94122"


def test_an_address_that_does_not_exist_is_refused(booked):
    assert booked["not_found"].get("success") is False
    assert booked["not_found"].get("error")


def test_an_address_outside_the_area_is_refused_by_the_store(booked):
    """Not by the prompt. The knowledge block asks the agent to decline, and it
    did in testing — but a prompt is a request, and this is the constraint."""
    assert booked["out_of_area"].get("success") is False
    assert "BURNABY" in booked["out_of_area"].get("error", "").upper()


def test_an_outage_does_not_stop_the_dispatch(booked):
    assert booked["unreachable"].get("success") is True, booked["unreachable"]


def test_the_spoken_address_survives_an_outage(booked):
    """The second stored row is the one booked while the gazetteer was down."""
    assert booked["stored_addresses"][-1] == "1200 Urving Street, San Francisco", (
        "with no verdict available the address must be stored exactly as the caller said it"
    )
