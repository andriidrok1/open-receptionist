"""The system prompt — the one artefact every call depends on and nothing tested.

The prompt is assembled from the business config and the language pack at the
start of each call. Until now nothing checked the result, on the reasoning that
it is "just text". It is not: the calendar block below exists because the agent
was asked for Monday, answered "Monday, August fourteenth" — a Friday — and
booked it. The tools cannot catch that, since only the date reaches them and it
was a real, open slot. The only place it can be caught is here.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

import phonenumbers
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business import CALENDAR_DAYS, build_system_prompt, load_business  # noqa: E402
from locale_pack import load_locale  # noqa: E402

#: One per language, so both packs' weekday names are exercised.
BUSINESSES = ["dental_us", "hotel"]


@pytest.mark.parametrize("key", BUSINESSES)
def test_the_prompt_spells_out_the_next_fortnight(key):
    """Every date the caller is likely to name is in the prompt, already paired
    with its weekday, so the model never has to count days."""
    biz = load_business(key)
    prompt = build_system_prompt(biz)
    today = biz.now().date()

    for offset in range(CALENDAR_DAYS):
        day = today + timedelta(days=offset)
        assert day.isoformat() in prompt, f"{day} is missing from the calendar"


@pytest.mark.parametrize("key", BUSINESSES)
def test_every_date_in_the_calendar_carries_its_real_weekday(key):
    """The pairing itself has to be right.

    A list of dates labelled with the wrong days is worse than no list: the
    model stops guessing and starts trusting it. Checked against the calendar
    module rather than against the pack's own ordering, so an off-by-one shows
    up instead of agreeing with itself.
    """
    biz = load_business(key)
    names = biz.pack.raw("calendar.weekdays")
    prompt = build_system_prompt(biz)

    pairs = re.findall(r"^(\S+) (\d{4}-\d{2}-\d{2})$", prompt, re.MULTILINE)
    assert len(pairs) == CALENDAR_DAYS, f"expected {CALENDAR_DAYS} calendar lines, got {len(pairs)}"

    for name, iso in pairs:
        real = date.fromisoformat(iso)
        assert name == names[real.weekday()], (
            f"{iso} is labelled {name!r}, but it is a {names[real.weekday()]!r}"
        )


@pytest.mark.parametrize("key", BUSINESSES)
def test_the_calendar_starts_today(key):
    """"Tomorrow" is only correct if the first line is today."""
    biz = load_business(key)
    prompt = build_system_prompt(biz)
    first = re.search(r"^\S+ (\d{4}-\d{2}-\d{2})$", prompt, re.MULTILINE)
    assert first, "no calendar block in the prompt"
    assert date.fromisoformat(first.group(1)) == biz.now().date()


@pytest.mark.parametrize("key", BUSINESSES)
def test_the_prompt_states_the_local_phone_length(key):
    """And states the length libphonenumber will actually enforce.

    If these two ever disagree the agent argues with the store in front of the
    caller — it asks for ten digits, the store wants nine, and neither side
    gives way.
    """
    biz = load_business(key)
    prompt = build_system_prompt(biz)
    example = phonenumbers.example_number(biz.country)
    expected = len(phonenumbers.national_significant_number(example))
    assert str(expected) in prompt, f"the prompt never mentions {expected} digits for {biz.country}"


@pytest.mark.parametrize("key", BUSINESSES)
def test_the_prompt_tells_the_model_not_to_count(key):
    """The list only helps if the prompt says to read it.

    Weak on its own — it matches words, not behaviour — but it fails loudly if
    someone trims the instruction while keeping the calendar, which would leave
    the model back where it started with fourteen lines of decoration.
    """
    biz = load_business(key)
    pack = load_locale(biz.pack.lang)
    instruction = pack.raw("prompt.date_hint")
    assert "{calendar}" in instruction, "the pack no longer includes the calendar"

