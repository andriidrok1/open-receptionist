"""The guards, and the two ways each of them can be wrong.

Every one of these exists because the agent did the thing in a real call, and
the prompt had already told it not to. That is the pattern the project keeps
paying for: "a prompt is a request, not a constraint." So each guard is checked
in both directions — it has to catch the invention, and it must not fire on the
ordinary call, because a guard that blocks real bookings gets switched off and
then nothing is protected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import guards  # noqa: E402
from hangup import says_goodbye  # noqa: E402
from locale_pack import LOCALES_DIR, load_locale  # noqa: E402

ALL_LANGUAGES = sorted(p.stem for p in LOCALES_DIR.glob("*.yaml"))
EN_DIGITS = dict(load_locale("en").raw("speech.digit_words"))


@pytest.fixture(autouse=True)
def _fresh_call():
    """One ledger per call; the harnesses run many in a row."""
    guards.reset()
    yield
    guards.reset()


# ------------------------------ the phone number -----------------------------


def test_a_number_the_caller_read_out_is_accepted():
    guards.hear("my number is 415-555-1234", EN_DIGITS)
    assert guards.was_spoken("4155551234") is True


def test_a_number_the_caller_said_in_words_is_accepted():
    """Speech recognition writes "four one five" as readily as "415", and which
    one you get is not something the booking should depend on."""
    guards.hear("four one five, five five five, one two three four", EN_DIGITS)
    assert guards.was_spoken("4155551234") is True


def test_a_number_split_across_turns_is_accepted():
    """People break a phone number up, and the endpointer breaks it up further —
    it is the single most common way a number arrives in pieces."""
    guards.hear("it's four one five", EN_DIGITS)
    guards.hear("five five five", EN_DIGITS)
    guards.hear("one two three four", EN_DIGITS)
    assert guards.was_spoken("4155551234") is True


def test_the_country_code_the_store_adds_does_not_break_the_match():
    guards.hear("415 555 9876", EN_DIGITS)
    assert guards.was_spoken("+14155559876") is True


def test_an_invented_number_is_refused():
    """The bug this exists for. Given a number the store rejected, the agent
    tried three MORE of its own — 5551232387, 5552387314, 5554671859 — until
    one might pass. It was not asking again; it was guessing."""
    guards.hear("my number is 415-555-1234", EN_DIGITS)
    assert guards.was_spoken("5554671859") is False


def test_one_wrong_digit_is_still_refused():
    """The near miss matters more than the wild guess: it is the one that gets
    written into a booking and fails silently three days later."""
    guards.hear("my number is 415-555-1234", EN_DIGITS)
    assert guards.was_spoken("4155551235") is False


def test_an_empty_ledger_never_blocks():
    """Nobody was listening — the eval harness, a text-only run, the first turn
    of a call. A guard that fires when it has no information stops real
    bookings, which is how guards get removed."""
    assert guards.was_spoken("4155551234") is True


# ---------------------------- the reference code -----------------------------


def test_a_code_a_tool_issued_may_be_spoken():
    guards.remember_ref("JOB-4315")
    assert guards.fabricated_refs("your reference is JOB-4315") == []


def test_a_code_no_tool_issued_is_caught():
    """The worst thing the agent has been caught doing: "your appointment is
    set, your reference is APT-5678", with create_booking never called — and
    APT- belongs to the dental practice, on a plumbing call."""
    guards.remember_ref("JOB-4315")
    assert guards.fabricated_refs("your reference is APT-5678") == ["APT-5678"]


def test_a_plausible_code_with_the_right_prefix_is_still_caught():
    """Checking the prefix would have passed this. The codes actually handed
    out are the only thing that can tell a real one from a convincing one."""
    guards.remember_ref("JOB-4315")
    assert guards.fabricated_refs("your reference is JOB-1234") == ["JOB-1234"]


def test_ordinary_speech_is_not_mistaken_for_a_code():
    assert guards.fabricated_refs("we'll be there between 2-4 today, that's $99") == []


def test_the_ledger_does_not_leak_between_calls():
    """The harnesses run call after call in one process. A code remembered from
    the previous caller would make the next call's invention look legitimate."""
    guards.remember_ref("JOB-4315")
    guards.reset()
    assert guards.fabricated_refs("your reference is JOB-4315") == ["JOB-4315"]


# --------------------- correcting the reply before it is said ----------------


def test_a_wrong_code_is_swapped_for_the_real_one():
    """Substitution beats blocking here. Everything else in that sentence — the
    time, the address, the fee — was right; discarding the whole reply over one
    wrong token would trade a small lie for a confusing silence."""
    guards.remember_ref("JOB-2831")
    fixed, changed, unfixable = guards.correct_refs(
        "Booked for eight. Your reference is APT-5678. See you then."
    )
    assert fixed == "Booked for eight. Your reference is JOB-2831. See you then."
    assert changed == ["APT-5678"]
    assert unfixable is False


def test_the_newest_booking_is_the_one_being_talked_about():
    guards.remember_ref("JOB-1111")
    guards.remember_ref("JOB-2222")
    fixed, _, _ = guards.correct_refs("your reference is JOB-9999")
    assert "JOB-2222" in fixed


def test_a_confirmation_with_no_booking_at_all_cannot_be_repaired():
    """The dangerous half. With nothing to substitute, inserting a code would
    dress the lie better rather than remove it — the caller is being told a
    plumber is coming when none is. The pipeline replaces the whole reply."""
    fixed, changed, unfixable = guards.correct_refs(
        "Your appointment is set. Reference APT-5678. Thanks!"
    )
    assert unfixable is True
    assert changed == ["APT-5678"]
    assert fixed == "Your appointment is set. Reference APT-5678. Thanks!", (
        "the text is returned untouched; deciding what to say instead is the caller's job"
    )


def test_a_correct_reply_is_left_exactly_as_it_was():
    """A guard that rewrites ordinary replies is worse than no guard."""
    guards.remember_ref("JOB-2831")
    original = "Booked for eight. Your reference is JOB-2831."
    fixed, changed, unfixable = guards.correct_refs(original)
    assert (fixed, changed, unfixable) == (original, [], False)


def test_only_the_code_is_touched():
    guards.remember_ref("JOB-2831")
    fixed, _, _ = guards.correct_refs(
        "Plumber to 1200 Irving at 8 AM, $99, reference APT-5678, see you then"
    )
    assert "1200 Irving" in fixed and "$99" in fixed and "8 AM" in fixed


# ------------------- the processor, not just the function --------------------
#
# Everything above tests correct_refs, which is pure and easy. That is not the
# same as testing the thing in the pipeline: deleting the guard's whole body and
# passing every frame through untouched left all of the tests above green. So
# these drive the processor itself and read what comes out the other side.

RETRACT = "Sorry — hold on. I don't have that booked yet."


def _through_guard(text: str) -> str:
    """Push one reply through ClaimGuard and return what it would have spoken."""
    import asyncio

    from hangup import ClaimGuard
    from pipecat.frames.frames import TextFrame
    from pipecat.processors.frame_processor import FrameDirection

    spoken: list[str] = []
    guard = ClaimGuard(RETRACT)

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, TextFrame):
            spoken.append(frame.text)

    guard.push_frame = capture
    asyncio.run(guard.process_frame(TextFrame(text), FrameDirection.DOWNSTREAM))
    return spoken[0] if spoken else ""


def test_the_processor_speaks_the_real_code():
    guards.remember_ref("JOB-2831")
    assert "JOB-2831" in _through_guard("Booked. Your reference is APT-5678.")


def test_the_processor_never_speaks_an_invented_code():
    guards.remember_ref("JOB-2831")
    assert "APT-5678" not in _through_guard("Booked. Your reference is APT-5678.")


def test_the_processor_retracts_a_confirmation_with_no_booking_behind_it():
    """The caller must not hear "your appointment is set" when it is not."""
    assert _through_guard("Your appointment is set. Reference APT-5678.") == RETRACT


def test_the_processor_leaves_an_honest_reply_alone():
    guards.remember_ref("JOB-2831")
    honest = "I can get someone out at eight. Ninety-nine for the visit. Send him?"
    assert _through_guard(honest) == honest


# -------------------------------- the goodbye --------------------------------


@pytest.fixture
def farewells():
    return tuple(load_locale("en").raw("speech.goodbye_words"))


@pytest.mark.parametrize("reply", [
    "Great. Take care!",
    "Thanks, bye",
    "Is that right? Okay, bye.",
    "No problem at all. Take care!",
])
def test_a_sign_off_hangs_up(reply, farewells):
    assert says_goodbye(reply, farewells) is True


@pytest.mark.parametrize("reply", [
    "Bye for now, but first — what's the address?",
    "Bye for now, but first let me check the address",
    "Take care! Anything else I can help with?",
    "I can get someone out at two today. Send him?",
    # "maybe" contains "bye": a substring match hung up on a sentence about
    # traffic. Whole words only.
    "He should be there by two, an hour later maybe.",
])
def test_a_farewell_mid_conversation_does_not(reply, farewells):
    """The only failure worse than staying on the line is cutting someone off
    mid-question. The first of these was a real false positive in the first
    version, which matched any farewell anywhere in the final sentence."""
    assert says_goodbye(reply, farewells) is False


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_every_language_can_say_goodbye_and_count(lang):
    """Both lists are language data, and a pack missing either silently
    disables a guard for that language rather than failing."""
    pack = load_locale(lang)
    assert pack.raw("speech.goodbye_words"), f"{lang}: no farewells, calls will never hang up"
    digits = pack.raw("speech.digit_words")
    assert set(digits.values()) >= set("0123456789"), (
        f"{lang}: digit_words does not cover every digit, so a number said out loud "
        "cannot be matched against one the agent tries to book with"
    )
