"""The language pack contract.

These tests are the reason "adding a language is one file" is a claim and not a
hope. They run without a network or an API key, so they can gate every commit.
"""

import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from locale_pack import (  # noqa: E402
    LOCALES_DIR,
    REQUIRED_KEYS,
    Locale,
    MissingLocaleKey,
    load_locale,
)

ALL_LANGUAGES = sorted(p.stem for p in LOCALES_DIR.glob("*.yaml"))


def test_there_is_at_least_one_language():
    assert ALL_LANGUAGES, "no language packs in locales/"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_pack_is_complete(lang):
    """Every pack defines every key the code asks for.

    This is what makes a new language safe to add: forget a key and the process
    refuses to start, with the list, instead of producing a prompt that is
    quietly missing a paragraph.
    """
    assert load_locale(lang).missing_keys() == []


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_no_placeholder_is_left_unfilled(lang):
    """Every `{placeholder}` in the pack is one the code actually passes.

    A typo in a placeholder name does not fail on load — it fails on the call
    that renders that template, which might be the booking confirmation.
    """
    import re

    pack = load_locale(lang)
    known = {
        "agent_name", "persona", "name", "speech_bad", "speech_good", "tone",
        "customer_gen", "customer_nom", "customer_nom_cap", "customer_dat",
        "today", "weekday", "time", "slots_suffix", "knowledge", "scope",
        "escalation_examples", "resource_label", "start_label", "end_label",
        "party", "party_label", "unit_cap", "action_upper", "collect",
        "intake_rules", "phone_rules", "recap", "count", "unit", "max", "days",
        "now", "hours", "opens", "closes", "duration", "resource", "known",
        "fields", "name", "price", "currency", "day", "month", "label", "date",
        "start", "end", "units", "money", "action", "topic",
    }
    unknown = {}
    for key in REQUIRED_KEYS:
        value = pack.raw(key)
        if not isinstance(value, str):
            continue
        for found in re.findall(r"\{(\w+)\}", value):
            if found not in known:
                unknown.setdefault(key, set()).add(found)
    assert not unknown, f"{lang}: unknown placeholders {unknown}"


def test_incomplete_pack_is_rejected_loudly():
    """A pack missing a key must fail on load, naming what is missing."""
    pack = Locale("test", {"calendar": {"weekdays": []}})
    missing = pack.missing_keys()
    assert "prompt.speech_rules" in missing
    assert len(missing) > 50


def test_unknown_key_raises_rather_than_returning_empty():
    pack = load_locale(ALL_LANGUAGES[0])
    with pytest.raises(MissingLocaleKey):
        pack.text("prompt.does_not_exist")


def test_template_missing_argument_raises():
    """Rendering with a missing argument must not silently produce a stub."""
    pack = load_locale(ALL_LANGUAGES[0])
    with pytest.raises(MissingLocaleKey):
        pack.text("errors.slot.in_past")  # wants `now`


def test_whitespace_is_not_trimmed():
    """The intake header carries a trailing newline that matters.

    It was trimmed once, which glued the list of extra questions onto the line
    above it inside the prompt. Cheap to assert, expensive to notice by eye.
    """
    pack = load_locale(ALL_LANGUAGES[0])
    header = pack.text("prompt.intake.header")
    assert header.startswith("\n")
    assert header.endswith("\n")


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_time_format_is_supported(lang):
    pack = load_locale(lang)
    assert pack.raw("settings.time_format") in ("12h", "24h")


def test_time_rendering_24h():
    pack = Locale("t", {"settings": {"time_format": "24h"}})
    assert pack.time("10:00") == "10:00"
    assert pack.time("22:30") == "22:30"
    assert pack.time("09:05") == "09:05"


def test_time_rendering_12h():
    pack = Locale("t", {"settings": {"time_format": "12h"}})
    assert pack.time("10:00") == "10 AM"
    assert pack.time("22:30") == "10:30 PM"
    assert pack.time("00:00") == "12 AM"
    assert pack.time("12:00") == "12 PM"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_calendar_shapes(lang):
    pack = load_locale(lang)
    assert len(pack.raw("calendar.weekdays")) == 7
    assert len(pack.raw("calendar.weekdays_short")) == 7
    assert len(pack.raw("numbers.months_genitive")) == 12
    assert pack.raw("numbers.plural_rule") in ("slavic", "english")


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_unit_forms_match_the_plural_rule(lang):
    """A Slavic pack needs three forms per unit, an English one needs two.

    Getting this wrong shows up as "3 ніч" being read to a caller.
    """
    pack = load_locale(lang)
    expected = 3 if pack.raw("numbers.plural_rule") == "slavic" else 2
    for unit, forms in pack.raw("numbers.units").items():
        assert len(forms) == expected, f"{lang}: {unit} has {len(forms)} forms, expected {expected}"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_country_code_looks_like_iso(lang):
    code = load_locale(lang).text("settings.default_country")
    assert len(code) == 2 and code.isupper()
