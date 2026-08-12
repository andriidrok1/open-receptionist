"""End-of-turn timing on the utterance shapes live callers actually produce.

The first live human call dictated a phone number in bursts — "415-", "528-",
"4151." — and the STT dutifully punctuated each burst, so the plain rules
closed the turn mid-number: six exchanges to collect one phone number, the
agent answering between the caller's chunks.
"""

import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from locale_pack import load_locale  # noqa: E402
from turn_taking import classify_ending  # noqa: E402

EN = load_locale("en")
CONT = {c.strip().lower() for c in EN.raw("turn_taking.continuations")}
END = EN.text("turn_taking.sentence_end")


@pytest.mark.parametrize("burst", [
    "415-",       # dictation chunk ending in a dash
    "4151.",      # dictation chunk the STT closed with a period
    "+1",         # the country code alone
    "415 528",    # two bare groups
    "Plus one.",  # the same thing in words
    "five five five",  # digit words mid-number
])
def test_a_dictation_burst_is_never_an_ending(burst):
    assert classify_ending(burst, CONT, END) == "unfinished"


@pytest.mark.parametrize("sentence", [
    "Can you come tomorrow at 6:30?",   # a full question ending in a time
    "My address is 1200 Irving Street, San Francisco.",  # digits mid-sentence
])
def test_a_full_sentence_with_numbers_still_closes(sentence):
    """The digit rule is length-gated: a real sentence that happens to contain
    digits must not buy the long wait, or every time quoted back to the agent
    costs an extra second."""
    assert classify_ending(sentence, CONT, END) == "finished"


def test_the_plain_rules_still_work():
    assert classify_ending("I need to book a plumber and", CONT, END) == "unfinished"
    assert classify_ending("That works for me.", CONT, END) == "finished"
    assert classify_ending("no punctuation no hint either way", CONT, END) == "unsure"
