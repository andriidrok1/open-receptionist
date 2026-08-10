"""Deterministic checks for the booking store. No network, no API key, no model.

Every case here is a bug that actually happened on a call, or a boundary the
agent walked into. They run in under a second, which is the point: the model
layer is slow and costs money, so anything decidable without it is decided here.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import crm  # noqa: E402
from business import BUSINESS  # noqa: E402

crm.init_db()

TODAY = BUSINESS.today()
ROOM = BUSINESS.booking.resources[0].label


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# ------------------------------- phone -------------------------------


@pytest.mark.parametrize(
    "raw",
    ["0632096974", "+380632096974", "380632096974", "063 209 69 74", "(063) 209-69-74"],
)
def test_ukrainian_numbers_normalise_to_e164(raw):
    value, error = crm.validate_phone(raw)
    assert error is None, error
    assert value == "+380632096974"


@pytest.mark.parametrize("raw", ["", None, "нуль шість сім", "abc"])
def test_phone_rejects_non_numbers(raw):
    value, error = crm.validate_phone(raw)
    assert value is None and error


def test_phone_rejects_wrong_length():
    """The model drops or adds a digit when transcribing speech."""
    assert crm.validate_phone("063209697")[0] is None
    assert crm.validate_phone("06320969741")[0] is None


def test_phone_error_is_a_spoken_instruction_not_a_log_line():
    """The agent reads this out loud, so it has to say what to do next."""
    _, error = crm.validate_phone("063209697")
    assert len(error) > 20


# ------------------------------- name -------------------------------


def test_name_accepts_a_real_name():
    assert crm.validate_name("Тарас Мельник") == ("Тарас Мельник", None)


@pytest.mark.parametrize("raw", ["", " ", "1", "-", "Гість", "гість", "guest", "Клієнт"])
def test_name_rejects_placeholders(raw):
    """The model fills the mandatory field with a stub when it never asked.

    The booking then exists on paper and is useless in practice.
    """
    value, error = crm.validate_name(raw)
    assert value is None and error


# ------------------------------ dates ------------------------------


def test_nights_are_counted_between_check_in_and_check_out():
    out = crm.check_availability(ROOM, iso(1), iso(3))
    assert out["units"] == 2, "19th to 21st is two nights, not three"


def test_check_out_before_check_in_is_refused():
    assert "error" in crm.check_availability(ROOM, iso(3), iso(1))


def test_same_day_is_refused():
    assert "error" in crm.check_availability(ROOM, iso(1), iso(1))


def test_past_dates_are_refused():
    assert "error" in crm.check_availability(ROOM, iso(-2), iso(1))


def test_absurdly_long_stays_are_refused():
    """One slip of the tongue used to book a room for four hundred nights."""
    assert "error" in crm.check_availability(ROOM, iso(1), iso(1 + crm.MAX_UNITS + 1))


def test_beyond_the_horizon_is_refused():
    horizon = BUSINESS.booking.horizon_days
    assert "error" in crm.check_availability(ROOM, iso(horizon + 5), iso(horizon + 7))


def test_errors_never_carry_an_availability_flag():
    """`available: false` next to an error made the agent say "we are full".

    On the emergency config that meant telling someone with a burst pipe there
    were no plumbers while two crews were free.
    """
    for bad in (
        crm.check_availability(ROOM, iso(3), iso(1)),
        crm.check_availability(ROOM, iso(-2), iso(1)),
        crm.check_availability("no such thing", iso(1), iso(2)),
    ):
        assert "error" in bad
        assert "available" not in bad


# ---------------------------- the spoken line ----------------------------


def test_spoken_summary_exists_when_available():
    out = crm.check_availability(ROOM, iso(1), iso(3))
    assert out["spoken"], "the agent has nothing to read out"


def _numbers(text: str) -> list[int]:
    import re

    return [int(x) for x in re.findall(r"\d+", text)]


@pytest.mark.parametrize("nights", [1, 2, 3, 7])
def test_spoken_summary_agrees_with_the_stored_numbers(nights):
    """The agent said "two nights, 2400" for a one-night booking at 1200.

    Asserting `str(units) in spoken` proved nothing: the sentence already holds
    the two dates, the count and the price, so a wrong count of 3 matched the
    "13" of a date. Here every number in the spoken sentence has to be one the
    tool actually returned — which is the property that was violated.
    """
    out = crm.check_availability(ROOM, iso(1), iso(1 + nights))
    spoken = _numbers(out["spoken"])
    expected = {
        out["units"],
        out["total_price"],
        int(out["starts_at"][8:10]),
        int(out["ends_at"][8:10]),
    }
    assert out["units"] in spoken, f"the number of nights is not said: {out['spoken']}"
    assert out["total_price"] in spoken, f"the price is not said: {out['spoken']}"
    stray = [n for n in spoken if n not in expected]
    assert not stray, f"{out['spoken']} contains numbers the tool never returned: {stray}"


@pytest.mark.parametrize("nights,expected", [(1, "ніч"), (2, "ночі"), (5, "ночей"), (21, "ніч")])
def test_slavic_plural_forms(nights, expected):
    out = crm.check_availability(ROOM, iso(1), iso(1 + nights))
    assert f"{nights} {expected}" in out["spoken"]


# ------------------------------ booking ------------------------------


def test_booking_round_trip():
    out = crm.create_booking(ROOM, iso(10), "Тарас Мельник", "0632096974", iso(12))
    assert out["success"], out
    assert out["ref_code"].endswith(tuple("0123456789"))
    assert out["phone"] == "+380632096974"
    assert len(out["ref_code"].split("-")[-1]) == 4, "six digits get misread out loud"


def test_booking_refuses_a_placeholder_name():
    out = crm.create_booking(ROOM, iso(11), "Гість", "0632096974", iso(12))
    assert not out["success"] and out["error"]


def test_capacity_is_not_exceeded_sequentially():
    """The plain arithmetic: more requests than places, one at a time."""
    resource = BUSINESS.booking.resources[-1]
    start, end = iso(20), iso(21)
    made = 0
    for i in range(resource.capacity + 3):
        out = crm.create_booking(resource.label, start, f"Тест Тестенко{i}", "0632096974", end)
        made += bool(out.get("success"))
    assert made == resource.capacity


def test_capacity_is_not_exceeded_concurrently():
    """The actual guard: simultaneous calls racing for the last place.

    The sequential test above passes even with the transaction removed — it
    measures arithmetic, not the race. This one measures the race: seven
    concurrent bookings against three places all used to succeed, and removing
    BEGIN IMMEDIATE makes this fail while everything else stays green.
    """
    import concurrent.futures

    resource = BUSINESS.booking.resources[0]
    start, end = iso(30), iso(31)
    attempts = resource.capacity + 7

    def book(i):
        return crm.create_booking(
            resource.label, start, f"Тест Паралельний{i}", "0632096974", end
        )

    with concurrent.futures.ThreadPoolExecutor(attempts) as pool:
        results = list(pool.map(book, range(attempts)))

    succeeded = sum(bool(r.get("success")) for r in results)
    assert succeeded == resource.capacity, (
        f"{succeeded} of {attempts} concurrent bookings went through, "
        f"capacity is {resource.capacity}"
    )


# ---------------------------- callback requests ----------------------------
#
# Every escalation goes through take_message — the path the agent takes instead
# of inventing an answer — and none of it had a test.


def test_take_message_records_a_request():
    out = crm.take_message("Тарас Мельник", "0632096974", "питання про парковку")
    assert out["success"] and out["note"]
    assert crm.list_messages()[0]["phone"] == "+380632096974"


def test_take_message_validates_the_phone():
    out = crm.take_message("Тарас Мельник", "нуль шість сім", "питання")
    assert not out["success"] and out["error"]


def test_take_message_needs_a_topic():
    out = crm.take_message("Тарас Мельник", "0632096974", "  ")
    assert not out["success"] and out["error"]


def test_take_message_without_a_name_is_labelled_not_invented():
    """It falls back to a generic word on purpose, unlike a booking.

    A callback request with no name is still useful — someone rings back on that
    number. A booking with no name is not, which is why create_booking refuses
    the same stub this one accepts. Worth pinning so the two never converge by
    accident.
    """
    out = crm.take_message("", "0632096974", "питання")
    assert out["success"]
    assert crm.list_messages()[0]["customer_name"]
