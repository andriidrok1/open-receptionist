"""Every business config, checked by the same rules.

The other tests name their businesses: `["dental_us", "hotel"]`. That is fine
for what they check and useless for the failure this file exists to prevent —
adding a vertical and finding out it is broken on a live call, because the tests
exercised the first tab and the new one is the fifth.

It has happened. A service label longer than twenty characters ("Аварійне
відкриття дверей") makes ElevenLabs Scribe close the whole websocket with a
policy violation rather than ignore the extra, which looks exactly like speech
recognition being dead: audio flows, the VAD fires, no transcript ever arrives.
The hotel, the clinic and the barbershop had short labels and worked. Cleaning
and the emergency service did not.

So every check here is parametrised over the directory, not over a list. A new
YAML is covered the moment it is saved.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from business import (  # noqa: E402
    BUSINESSES_DIR,
    MAX_KEYTERM_LEN,
    MAX_KEYTERMS,
    build_system_prompt,
    load_business,
)
from locale_pack import LOCALES_DIR  # noqa: E402

ALL_BUSINESSES = sorted(p.stem for p in BUSINESSES_DIR.glob("*.yaml"))
LANGUAGES = {p.stem for p in LOCALES_DIR.glob("*.yaml")}


def test_there_is_at_least_one_business():
    assert ALL_BUSINESSES, "no business configs in businesses/"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_config_loads(key):
    """The loader is where defaults and cross-checks live; if it refuses, the
    process refuses to start, and that is better found here."""
    assert load_business(key).key


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_prompt_builds(key):
    """Assembling the prompt touches the language pack for a dozen keys. A
    config naming a language whose pack lacks one of them fails here rather
    than on the first call."""
    prompt = build_system_prompt(load_business(key))
    assert len(prompt) > 500, "the prompt came out suspiciously short"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_language_pack_exists(key):
    biz = load_business(key)
    assert biz.language in LANGUAGES, (
        f"{key} speaks {biz.language!r}, but there is no locales/{biz.language}.yaml"
    )


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_keyterms_fit_what_the_stt_accepts(key):
    """Both limits, because breaking either kills the connection outright.

    `stt_keyterms()` already splits long labels and truncates the list — this
    asserts the result, so a change to that logic cannot quietly start emitting
    a term that closes the socket.
    """
    terms = load_business(key).stt_keyterms()
    too_long = [t for t in terms if len(t) > MAX_KEYTERM_LEN]
    assert not too_long, f"{key}: keyterms over {MAX_KEYTERM_LEN} chars: {too_long}"
    assert len(terms) <= MAX_KEYTERMS, f"{key}: {len(terms)} keyterms, limit {MAX_KEYTERMS}"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_resource_keys_are_unique(key):
    keys = [r.key for r in load_business(key).booking.resources]
    assert len(keys) == len(set(keys)), f"{key}: duplicate resource keys in {keys}"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_no_two_resources_answer_to_the_same_word(key):
    """An alias shared by two services silently books the wrong one.

    `normalize_resource` returns the first match, so the caller asks for one
    thing, the agent reads back another, and both are real bookable services —
    nothing downstream can tell. Keys and labels count as aliases here because
    they are matched too.
    """
    seen: dict[str, str] = {}
    clashes = []
    for r in load_business(key).booking.resources:
        for word in {r.key.lower(), r.label.lower(), *(a.lower() for a in r.aliases)}:
            if word in seen and seen[word] != r.key:
                clashes.append(f"{word!r} → both {seen[word]!r} and {r.key!r}")
            seen[word] = r.key
    assert not clashes, f"{key}: " + "; ".join(clashes)


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_resources_can_actually_be_booked(key):
    bk = load_business(key).booking
    if not bk.enabled:
        return
    assert bk.resources, f"{key}: booking is on but there is nothing to book"
    for r in bk.resources:
        assert r.capacity >= 1, f"{key}: {r.key} has capacity {r.capacity}"
        assert r.label.strip(), f"{key}: {r.key} has no label to say out loud"
        if bk.is_slots:
            assert r.duration_min > 0, f"{key}: {r.key} is a slot with no duration"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_opening_hours_parse(key):
    """`hours_for` raises on a malformed range, and it is called per booking
    request rather than at startup — so a typo lands mid-call."""
    from datetime import date, timedelta

    bk = load_business(key).booking
    if not bk.hours:
        return
    day = date(2026, 1, 5)  # a Monday, so a whole week is covered
    for offset in range(7):
        bk.hours_for(day + timedelta(days=offset))


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_slot_step_divides_nothing_into_nothing(key):
    bk = load_business(key).booking
    if bk.is_slots:
        assert bk.slot_step_min > 0, f"{key}: slot_step_min must be positive"
        assert bk.horizon_days > 0, f"{key}: horizon_days must be positive"


#: Argument names the booking tool already uses. An intake field claiming one of
#: them would be shadowed in the tool schema, so the field silently never
#: arrives — and it is required, which means the booking is missing exactly the
#: thing the industry cannot work without.
RESERVED_PARAMS = {"name", "phone", "date", "time", "starts_at", "ends_at", "ref", "notes"}


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_intake_fields_do_not_collide(key):
    bk = load_business(key).booking
    keys = [f.key for f in bk.intake]
    assert len(keys) == len(set(keys)), f"{key}: duplicate intake keys in {keys}"
    reserved = set(keys) & (RESERVED_PARAMS | {bk.resource_param, bk.party_param})
    assert not reserved, f"{key}: intake fields shadow tool arguments: {sorted(reserved)}"
    for f in bk.intake:
        assert f.label.strip(), f"{key}: intake field {f.key} has no label"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_the_agent_has_something_to_say(key):
    """The three speech samples are not decoration: the model copies the `good`
    one directly, and a config that omits them gets the language pack's generic
    fallback, which sounds like no particular business."""
    biz = load_business(key)
    for field in ("speech_bad", "speech_good", "speech_recap"):
        assert getattr(biz, field).strip(), f"{key}: {field} is empty"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_demo_card_is_complete(key):
    """The showcase builds its tabs from these, and a missing one renders as a
    blank card rather than an error."""
    biz = load_business(key)
    assert biz.demo_tab.strip(), f"{key}: no demo tab name"
    assert biz.demo_intro.strip(), f"{key}: no demo intro"
    assert biz.demo_try, f"{key}: no 'try saying' suggestions"


CYRILLIC = re.compile(r"[Ѐ-ӿ]+")


def _every_string(value) -> list[str]:
    """Every string the config contributes, lists and mappings included.

    Top-level strings were not enough: aliases, keyterms and the "try saying"
    suggestions are all lists, and those are exactly the lines copied wholesale
    from an older config.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _every_string(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _every_string(v)]
    return []


#: Configs that speak English, by what they declare rather than by what they are
#: called. Selecting on a `_us` filename suffix looked equivalent and was not:
#: an English config named `plumbing.yaml` skipped the check below silently,
#: which is the same shape of hole as the test itself is meant to close.
ENGLISH_BUSINESSES = [k for k in ALL_BUSINESSES if load_business(k).language == "en"]


@pytest.mark.parametrize("key", ENGLISH_BUSINESSES)
def test_english_configs_have_no_leftover_cyrillic(key):
    """A new English config is written by copying a Ukrainian one.

    A forgotten line does not crash anything — the agent simply says one
    sentence in the wrong language, halfway through a call, and only a listener
    notices.

    Values only, not the raw file: comments never reach the model, the caller or
    the demo page, and this file's job is to catch what does. A first version
    scanned the whole file and failed on a comment citing a research document
    with a Russian title, which is a fact about the project rather than a defect
    in the config.
    """
    raw = yaml.safe_load((BUSINESSES_DIR / f"{key}.yaml").read_text(encoding="utf-8")) or {}
    found = [w for s in _every_string(raw) for w in CYRILLIC.findall(s)]
    assert not found, f"{key}: Cyrillic left in an English config: {sorted(set(found))[:10]}"


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_round_the_clock_configs_say_so_consistently(key):
    """A 24/7 business must be 24/7 on every day of the week.

    Six days round the clock and one that closes at 17:00 is almost always a
    typo, and it shows up as the agent refusing a three-in-the-morning call one
    night in seven — the single call an emergency service exists for.
    """
    from datetime import date, timedelta

    bk = load_business(key).booking
    if not bk.hours:
        return
    monday = date(2026, 1, 5)
    flags = [bk.is_round_clock(monday + timedelta(days=i)) for i in range(7)]
    assert len(set(flags)) == 1 or not any(flags), (
        f"{key}: open round the clock on some days but not others: {flags}"
    )


@pytest.mark.parametrize("key", ALL_BUSINESSES)
def test_declared_key_matches_the_filename(key):
    """`BUSINESS=x` loads x.yaml and then trusts the `key:` inside it. When they
    disagree, the database rows are written under one name and read under the
    other."""
    raw = yaml.safe_load((BUSINESSES_DIR / f"{key}.yaml").read_text(encoding="utf-8")) or {}
    declared = raw.get("key")
    assert declared in (None, key), f"{key}.yaml declares key: {declared!r}"
