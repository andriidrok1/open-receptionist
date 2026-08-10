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


REFERENCE = "uk"  # the pack the code was written against


def _placeholders(pack, key) -> set:
    import re

    value = pack.raw(key)
    return set(re.findall(r"\{(\w+)\}", value)) if isinstance(value, str) else set()


@pytest.mark.parametrize("lang", [x for x in ALL_LANGUAGES if x != REFERENCE])
def test_placeholders_match_the_reference_pack(lang):
    """Every pack must use exactly the placeholders the reference one uses.

    A flat "is this name known anywhere in the project" check is not enough: a
    pack could put {price} into an error the code renders with no arguments at
    all, and that only fails on the call where that error fires. Comparing per
    key against the reference catches both directions — a placeholder the code
    will not pass, and one the pack forgot to use.
    """
    ref, pack = load_locale(REFERENCE), load_locale(lang)
    wrong = {
        key: (_placeholders(ref, key), _placeholders(pack, key))
        for key in REQUIRED_KEYS
        if _placeholders(ref, key) != _placeholders(pack, key)
    }
    assert not wrong, f"{lang}: placeholders differ from {REFERENCE} — {wrong}"


def test_reference_pack_uses_only_names_the_code_passes(lang=REFERENCE):
    known = {
        "agent_name", "persona", "name", "speech_bad", "speech_good", "tone",
        "customer_gen", "customer_nom", "customer_nom_cap", "customer_dat",
        "today", "weekday", "time", "slots_suffix", "knowledge", "scope",
        "escalation_examples", "resource_label", "start_label", "end_label",
        "party", "party_label", "unit_cap", "action_upper", "collect",
        "intake_rules", "phone_rules", "recap", "count", "unit", "max", "days",
        "now", "hours", "opens", "closes", "duration", "resource", "known",
        "fields", "price", "currency", "day", "month", "label", "date",
        "start", "end", "units", "money", "action", "topic", "n", "changes",
        "ref_code",
    }
    pack = load_locale(lang)
    unknown = {k: _placeholders(pack, k) - known for k in REQUIRED_KEYS
               if _placeholders(pack, k) - known}
    assert not unknown, f"{lang}: unknown placeholders {unknown}"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_templates_are_parseable(lang):
    """A stray brace in a pack is invisible until the call that renders it."""
    assert load_locale(lang).broken_templates() == []


def test_a_stray_brace_is_caught_on_load():
    pack = Locale("t", {"prompt": {"identity": "hello {"}})
    assert any("identity" in b for b in pack.broken_templates())


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
    assert len(pack.raw("numbers.months")) == 12
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


def _all_text(pack) -> str:
    """Every piece of text in the pack, lists and mappings included.

    Strings only was not enough: months, weekday names, unit forms and
    placeholder names are all lists, and those are exactly the values a caller
    hears most often and the easiest to copy across when writing a new pack from
    an old one. A pack with "Понеділок" in an English weekday list passed.
    """
    def flatten(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [x for v in value.values() for x in flatten(v)]
        if isinstance(value, (list, tuple)):
            return [x for v in value for x in flatten(v)]
        return []

    return "\n".join(x for k in REQUIRED_KEYS for x in flatten(pack.raw(k)))


@pytest.mark.skipif("en" not in ALL_LANGUAGES, reason="no English pack")
def test_english_pack_has_no_leftover_cyrillic():
    """A forgotten Ukrainian line in the English pack is invisible by eye.

    It would not crash anything either — the agent would simply say one
    paragraph in the wrong language, halfway through a call.
    """
    import re

    found = re.findall(r"[Ѐ-ӿ]+", _all_text(load_locale("en")))
    assert not found, f"Cyrillic left in the English pack: {set(found)}"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_pack_carries_the_rules_that_are_not_about_language(lang):
    """Some rules are properties of a voice agent, not of a language.

    A new pack is written from scratch, which makes it easy to drop one of them
    by accident — and each was added after a specific failure on a live call.
    """
    text = _all_text(load_locale(lang)).lower()
    assert "break time=" in text, "the pause tag rule is missing"
    assert "alternatives" in text, "the 'never invent free times' rule is missing"
    assert "error" in text, "the 'an error is not a refusal' rule is missing"
    assert "take_message" in text, "the escalation tool is never mentioned"
    assert "create_booking" in text, "the booking tool is never mentioned"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_turn_taking_lists_are_strings(lang):
    """YAML reads bare on/off/yes/no as booleans.

    An unquoted `on` in the English continuation list became True and crashed
    the classifier instead of merely misbehaving — cheap to hit, confusing to
    diagnose from the traceback.
    """
    words = load_locale(lang).raw("turn_taking.continuations")
    assert words, f"{lang}: no continuation words"
    bad = [w for w in words if not isinstance(w, str)]
    assert not bad, f"{lang}: non-string entries (quote them): {bad}"


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_turn_taking_classifier(lang):
    """The three verdicts must be reachable in every language."""
    from turn_taking import classify_ending

    pack = load_locale(lang)
    words = {w.lower() for w in pack.raw("turn_taking.continuations")}
    end = pack.text("turn_taking.sentence_end")
    first = sorted(words)[0]

    assert classify_ending(f"something {first}", words, end) == "unfinished"
    assert classify_ending("something complete.", words, end) == "finished"
    assert classify_ending("something ambiguous", words, end) == "unsure"
    assert classify_ending("", words, end) == "unsure"
