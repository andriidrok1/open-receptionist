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


# ------------------- changing an existing booking -------------------
#
# Until these existed a booking could only be created, so every mistake was
# permanent — including the agent's own — and "move it to tomorrow" could not be
# served at all.


def _fresh_booking(day_offset: int = 40):
    out = crm.create_booking(
        ROOM, iso(day_offset), "Тарас Мельник", "0632096974", iso(day_offset + 2)
    )
    assert out["success"], out
    return out


def test_find_bookings_by_phone():
    """Callers remember their own number, never a reference code."""
    made = _fresh_booking(41)
    found = crm.find_bookings("063 209 69 74")
    assert found["found"] >= 1
    assert any(b["ref_code"] == made["ref_code"] for b in found["bookings"])
    assert all(b["spoken"] for b in found["bookings"])


def test_find_bookings_on_an_unknown_number_explains_itself():
    out = crm.find_bookings("0501112233")
    assert out.get("found", 0) == 0 and out["error"]


def test_cancelling_frees_the_slot():
    """A cancelled booking must stop occupying its resource."""
    resource = BUSINESS.booking.resources[0]
    start, end = iso(50), iso(51)
    made = [
        crm.create_booking(resource.label, start, f"Тест Місце{i}", "0632096974", end)
        for i in range(resource.capacity)
    ]
    assert all(m["success"] for m in made)
    assert crm.check_availability(resource.label, start, end)["available"] is False

    assert crm.cancel_booking(made[0]["ref_code"])["success"]
    assert crm.check_availability(resource.label, start, end)["available"] is True


def test_cancelling_twice_is_refused_not_silently_repeated():
    made = _fresh_booking(42)
    assert crm.cancel_booking(made["ref_code"])["success"]
    again = crm.cancel_booking(made["ref_code"])
    assert not again["success"] and again["error"]


def test_cancelling_an_unknown_reference_says_so():
    out = crm.cancel_booking("БР-0000")
    assert not out["success"] and "0000" in out["error"]


def test_a_cancelled_booking_is_kept_not_deleted():
    """The business wants to see that someone booked and then called it off."""
    made = _fresh_booking(43)
    crm.cancel_booking(made["ref_code"])
    row = [b for b in crm.list_bookings() if b["ref_code"] == made["ref_code"]]
    assert row and row[0]["status"] == "cancelled"


def test_rescheduling_keeps_the_reference():
    made = _fresh_booking(44)
    moved = crm.reschedule_booking(made["ref_code"], iso(46), iso(47))
    assert moved["success"], moved
    assert moved["ref_code"] == made["ref_code"]
    assert moved["starts_at"] == iso(46)


def test_rescheduling_does_not_collide_with_itself():
    """Moving 10:00 to 10:30 overlaps the booking being moved.

    Without excluding it from the occupancy count the appointment competes with
    itself and the move is refused for no reason a caller could understand.
    """
    resource = BUSINESS.booking.resources[0]
    made = crm.create_booking(
        resource.label, iso(60), "Тарас Мельник", "0632096974", iso(63)
    )
    assert made["success"]
    moved = crm.reschedule_booking(made["ref_code"], iso(61), iso(64))
    assert moved["success"], moved


def test_rescheduling_into_the_past_is_refused():
    made = _fresh_booking(45)
    out = crm.reschedule_booking(made["ref_code"], iso(-3), iso(-1))
    assert not out["success"] and out["error"]


def test_rescheduling_a_cancelled_booking_is_refused():
    made = _fresh_booking(48)
    crm.cancel_booking(made["ref_code"])
    out = crm.reschedule_booking(made["ref_code"], iso(52), iso(53))
    assert not out["success"] and out["error"]


# ---------------------------- call transcripts ----------------------------


def test_a_call_is_stored_and_read_back():
    """The owner's first question is what the agent said to them."""
    import sys as _sys

    _sys.path.insert(0, str(SERVER))
    from recording import CallRecorder

    rec = CallRecorder(business_key=BUSINESS.key, started_at="2026-08-11 09:00:00")
    rec.add("agent", "Добрий день!")
    rec.add("caller", "Хочу записатися.")
    rec.add_tool("check_availability", {"available": True})
    rec.add("agent", "Завтра о десятій вільно.")
    rec.finish(crm.save_call, {"duration_s": 40.0, "replies": 2})

    stored = [c for c in crm.list_calls() if c["call_id"] == rec.call_id]
    assert stored, "the call was not stored"
    call = stored[0]
    assert [x["who"] for x in call["lines"]] == ["agent", "caller", "tool", "agent"]
    assert call["metrics"]["duration_s"] == 40.0


def test_empty_lines_are_not_recorded():
    from recording import CallRecorder

    rec = CallRecorder(business_key=BUSINESS.key, started_at="x")
    rec.add("agent", "  ")
    rec.add("caller", "")
    assert rec.lines == []


def test_storing_a_call_never_raises():
    """Losing a transcript is bad; breaking the end of a call over it is worse."""
    from recording import CallRecorder

    rec = CallRecorder(business_key=BUSINESS.key, started_at="x")
    rec.add("agent", "hello")

    def explode(_summary):
        raise RuntimeError("store is down")

    rec.finish(explode, {})  # must not propagate


# --------------------- the escape hatch takes any spoken number ---------------


def test_a_message_with_an_unverifiable_number_is_still_recorded():
    """The loop breaker's exit. "Can't put the booking through, I'll take a
    message" — and take_message bounced the same unusable number, closing the
    last way out: the caller was promised a callback nothing recorded. A
    callback request is read by a person, and a person copes with a foreign
    number, so it is stored as spoken and flagged."""
    r = crm.take_message("Dave", "07983227651", "leaking pipes under the sink")
    assert r["success"] is True, r
    assert r["note"] == crm.L.text("notes.message_recorded_unverified")
    flag = crm.L.text("notes.phone_unverified", phone="07983227651")
    with crm._connect() as conn:
        row = conn.execute(
            "SELECT phone, topic FROM messages ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row["phone"] == "07983227651"
    assert flag in row["topic"]


def test_a_message_with_a_valid_number_is_stored_clean():
    r = crm.take_message("Dave", "0501234567", "callback about pricing")
    assert r["success"] is True, r
    assert r["note"] == crm.L.text("notes.message_recorded")


def test_a_message_with_no_digits_at_all_is_still_refused():
    """"As spoken" needs something spoken: with no digits there is nothing a
    human could dial, and recording it would promise a callback to nobody."""
    r = crm.take_message("Dave", "CustomerPhone", "leak")
    assert r.get("success") is not True


# ------------------------------ owner's inbox ------------------------------


def test_inbox_items_carry_the_call_they_came_from():
    """bot.py stamps the call at connect; everything the tools write during it
    must point back at that conversation, or "view call" opens nothing."""
    crm.set_current_call("call-inbox-1")
    out = crm.create_booking(ROOM, iso(40), "Тарас Вхідний", "0632096974", iso(41))
    assert out["success"], out
    msg = crm.take_message("Оля Дзвінкова", "0501234567", "передзвоніть щодо рахунку")
    assert msg["success"], msg
    crm.set_current_call(None)

    items = crm.inbox()
    booking = next(x for x in items if x.get("ref_code") == out["ref_code"])
    message = next(x for x in items if x["type"] == "message" and x["customer_name"] == "Оля Дзвінкова")
    assert booking["call_id"] == "call-inbox-1"
    assert message["call_id"] == "call-inbox-1"
    assert booking["handled"] is False and message["handled"] is False


def test_the_owners_checkmark_flips_both_ways():
    """A thumb on a phone WILL tick the wrong card — undo must exist."""
    out = crm.create_booking(ROOM, iso(42), "Тарас Галочка", "0632096974", iso(43))
    item = next(x for x in crm.inbox() if x.get("ref_code") == out["ref_code"])

    assert crm.mark_handled("booking", item["id"]) is True
    assert next(x for x in crm.inbox() if x["id"] == item["id"])["handled"] is True

    assert crm.mark_handled("booking", item["id"], handled=False) is True
    assert next(x for x in crm.inbox() if x["id"] == item["id"])["handled"] is False

    assert crm.mark_handled("booking", "no-such-row") is False
    assert crm.mark_handled("nonsense-kind", item["id"]) is False


def test_a_cancellation_comes_back_as_unhandled_news():
    """The owner ticked the booking off; then the customer called it off.
    The cancellation must reappear in the feed as a new, unhandled item —
    otherwise a crew drives out to a job that no longer exists."""
    out = crm.create_booking(ROOM, iso(44), "Тарас Скасований", "0632096974", iso(45))
    item = next(x for x in crm.inbox() if x.get("ref_code") == out["ref_code"])
    crm.mark_handled("booking", item["id"])

    assert crm.cancel_booking(out["ref_code"])["success"]

    again = next(x for x in crm.inbox() if x["id"] == item["id"])
    assert again["type"] == "cancellation"
    assert again["handled"] is False


def test_a_reschedule_puts_the_booking_back_in_front_of_the_owner():
    """A moved job is new instructions for whoever drives out."""
    out = crm.create_booking(ROOM, iso(46), "Тарас Перенесений", "0632096974", iso(47))
    item = next(x for x in crm.inbox() if x.get("ref_code") == out["ref_code"])
    crm.mark_handled("booking", item["id"])

    moved = crm.reschedule_booking(out["ref_code"], iso(48), iso(49))
    assert moved["success"], moved

    again = next(x for x in crm.inbox() if x["id"] == item["id"])
    assert again["handled"] is False


def test_call_log_disposition_comes_from_the_database_not_the_transcript():
    """A booking row stamped with the call's id IS the outcome; parsing the
    transcript's tool lines would confuse successes with errors."""
    crm.set_current_call("call-log-booked")
    out = crm.create_booking(ROOM, iso(50), "Тарас Журнал", "0632096974", iso(51))
    assert out["success"], out
    crm.set_current_call(None)

    crm.save_call({
        "call_id": "call-log-booked", "business": "hotel",
        "started_at": "2026-08-12 10:00:00",
        "lines": [{"at": 0, "who": "agent", "text": "Добрий день!"}],
        "metrics": {"duration_s": 42.0, "replies": 3},
    })
    crm.save_call({
        "call_id": "call-log-empty", "business": "hotel",
        "started_at": "2026-08-12 10:05:00", "lines": [], "metrics": {},
    })

    log = {c["call_id"]: c for c in crm.call_log()}
    assert log["call-log-booked"]["disposition"] == "booked"
    assert log["call-log-booked"]["ref_code"] == out["ref_code"]
    assert log["call-log-empty"]["disposition"] == "none"

    full = crm.get_call("call-log-booked")
    assert full["lines"][0]["text"] == "Добрий день!"
    assert crm.get_call("no-such-call") is None
