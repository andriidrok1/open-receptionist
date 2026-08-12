"""The text layer between the model and the synthesiser.

Nothing tested this module before, which is uncomfortable given what passes
through it: every word the caller hears. It also has previous form — an earlier
version rewrote the string for synthesis while the conversation history kept the
original, and the two cursors walking out of step chewed letters out of the
middle of words.

So the load-bearing test here is not about pauses at all. It is
`test_nothing_but_break_tags_is_ever_added_or_removed`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locale_pack import LOCALES_DIR, load_locale  # noqa: E402
from speech_text import (  # noqa: E402
    MAX_BREAKS,
    MAX_BREAK_SEC,
    SpeechTextFilter,
    sanitize_breaks,
)

BREAK = re.compile(r'<break time="[\d.]+s" />')
PACKS = sorted(p.stem for p in LOCALES_DIR.glob("*.yaml"))


def filter_for(lang: str) -> SpeechTextFilter:
    """The filter exactly as bot.py assembles it, month names and all."""
    pack = load_locale(lang)
    words = (
        list(pack.raw("speech.number_words", ()))
        + list(pack.raw("numbers.months", ()))
        + list(pack.raw("calendar.weekdays", ()))
    )
    return SpeechTextFilter(words, pack.raw("speech.glue_words", ()))


# --------------------------- the one that matters ---------------------------


@pytest.mark.parametrize("lang", PACKS)
@pytest.mark.parametrize(
    "line",
    [
        "So that's a cleaning on Friday, August fourteenth at twelve PM, a hundred and eighty dollars.",
        "Your reference is A P T, four seven one nine. Thanks!",
        "Отже, номер Покращений, три ночі. Разом п'ять тисяч чотириста гривень.",
        "Sure, what service do you need?",
        "",
        "   ",
        "One moment.",
        'Already paused<break time="0.4s" /> here, twelve PM, eighty dollars. Right?',
    ],
)
def test_nothing_but_break_tags_is_ever_added_or_removed(lang, line):
    """Strip the pauses back out and the text must be exactly what went in.

    A filter changes the string sent to synthesis while the original is what the
    conversation remembers. Any drift between them is corruption that shows up
    as missing letters in the transcript, and it is invisible until someone
    reads one.
    """
    out = filter_for(lang).prepare(line)
    assert BREAK.sub("", out) == BREAK.sub("", line)


# ------------------------------ where pauses go ------------------------------


def test_a_price_gets_a_beat_before_the_question():
    """The caller needs the amount to land before being asked to agree to it."""
    out = filter_for("en").prepare(
        "That's twelve PM, a hundred and eighty dollars. Does that sound right?"
    )
    # There is more than one pause in this line, so anchor on the right one:
    # everything between the amount and the question.
    between = out[out.index("dollars"):out.index("Does")]
    assert BREAK.search(between), f"no pause between the price and the question: {out!r}"


def test_the_date_and_the_price_get_a_pause_each():
    """They are separate facts, and a caller needs a beat after each.

    Bridged by "at" and "a", the whole recap reads as one run of number words
    and would collect a single pause at the very end — the one place neither
    fact needs it.
    """
    out = filter_for("en").prepare(
        "So that's Friday, August fourteenth at twelve PM, a hundred and eighty dollars. Right?"
    )
    assert len(BREAK.findall(out)) == 2


def test_prose_that_merely_contains_a_number_is_left_alone():
    """"One moment" is not information. A run of one word never gets a pause."""
    f = filter_for("en")
    for line in ("One moment, let me check.", "Sure, what service do you need?"):
        assert f.prepare(line) == line


def test_a_glue_word_cannot_start_a_run():
    """Otherwise "at the desk" reads as the beginning of a time."""
    line = "Come to the desk on the left and ask for me."
    assert filter_for("en").prepare(line) == line


def test_no_pause_at_the_very_end_of_a_reply():
    """The silence after the reply is already the pause."""
    out = filter_for("en").prepare("Your total is a hundred and eighty dollars.")
    assert "<break" not in out


def test_a_pause_the_model_already_wrote_is_not_doubled():
    out = filter_for("en").prepare(
        'Your number is four one five,<break time="0.4s" /> five five five. Right?'
    )
    assert "<break" in out
    assert "/><break" not in out.replace(" ", "")


@pytest.mark.parametrize("lang", PACKS)
def test_every_pack_can_drive_the_pauses(lang):
    """A pack without the word lists silently disables the feature, which is a
    reasonable default and a bad surprise. Both shipped packs must have them."""
    pack = load_locale(lang)
    assert pack.raw("speech.number_words", []), f"{lang} has no speech.number_words"
    assert pack.raw("speech.glue_words", []), f"{lang} has no speech.glue_words"


def test_without_word_lists_the_filter_only_sanitises():
    plain = SpeechTextFilter()
    line = "That's twelve PM, a hundred and eighty dollars. Right?"
    assert plain.prepare(line) == line


# -------------------------------- the budget --------------------------------


def test_the_total_number_of_pauses_is_capped():
    """ElevenLabs: too many break tags in one generation make the speech race
    and add artifacts. Ours count against the same budget as the model's.

    The line below offers six places a pause could go. Asserting only
    `<= MAX_BREAKS` would move with the constant and pass however high it went,
    so the real check is that we take FEWER than are on offer.
    """
    offered = 6
    out = filter_for("en").prepare(
        "Your number is four one five, five five five, oh one three two, "
        "extension twenty one, at twelve PM, eighty dollars. Right?"
    )
    placed = len(BREAK.findall(out))
    assert placed < offered, "every candidate got a pause — the budget is not being applied"
    assert placed == MAX_BREAKS


def test_a_pause_the_model_asked_for_is_clamped_not_dropped():
    out = sanitize_breaks('Three nights.<break time="2s" /> Confirm?')
    assert f'<break time="{MAX_BREAK_SEC}s" />' in out


def test_markup_that_is_not_a_pause_is_stripped():
    """With SSML parsing on, an unrecognised tag is at best ignored and at worst
    read out loud."""
    out = sanitize_breaks("Hello <b>there</b> and <emphasis>welcome</emphasis>.")
    assert "<" not in out
