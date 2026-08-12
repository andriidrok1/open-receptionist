"""Making the agent's worst habits unreachable rather than discouraged.

Three failures found by the casebook, all of the same shape — the agent stated
something as fact that no tool had produced:

  * It said "your appointment is set, your reference is APT-5678" without ever
    calling create_booking. The code was invented, and the prefix belonged to a
    different business entirely. The caller hangs up believing a plumber is
    coming.
  * Given a phone number the store refused, it tried three MORE numbers of its
    own invention — 5551232387, 5552387314, 5554671859 — until one might pass.
    It was not asking the caller again; it was guessing.
  * Both sides said goodbye and the line stayed open, because ending the call is
    a tool the model has and does not reliably reach for.

The prompt already forbids the first two in as many words. That is the lesson,
stated by someone who ran an agent for eight months and 571 calls: "a prompt is
a request, not a constraint. It holds until a caller phrases something you did
not anticipate, and you find out from a transcript three weeks later."

So none of this is asked for here. A number the caller never said is refused by
the tool. A reference code no tool issued never reaches the speaker. A goodbye
hangs up whether or not the model thought of it.
"""

from __future__ import annotations

import re

#: One process serves one call, the same way one phone number belongs to one
#: business — so the ledger is module-level rather than threaded through every
#: signature. `reset()` exists for the harnesses, which run many calls in a row.
_heard_digits: list[str] = []
#: Ordered, not a set: correcting an invented code means substituting the one
#: actually issued, and when a call has several the newest is the one being
#: talked about.
_issued_refs: list[str] = []


def reset() -> None:
    """Start a new call."""
    _heard_digits.clear()
    _issued_refs.clear()


def hear(text: str, digit_words: dict[str, str] | None = None) -> None:
    """Record what the caller said, reduced to the digits in it.

    Spoken and written forms have to collapse to the same thing: "four one five,
    five five five, one two three four" and "415-555-1234" are the same number,
    and a caller reading one out loud pauses in the middle of it, so the digits
    also have to survive being spread across several utterances. Keeping only
    the digits and joining them does all three.
    """
    if not text:
        return
    lowered = text.lower()
    for word, digit in (digit_words or {}).items():
        lowered = re.sub(rf"\b{re.escape(word)}\b", digit, lowered)
    _heard_digits.append(re.sub(r"\D", "", lowered))


def remember_ref(code: str) -> None:
    """A reference code that a tool really issued, and may therefore be said."""
    code = str(code or "").strip().upper()
    if code and code not in _issued_refs:
        _issued_refs.append(code)


def was_spoken(phone: str | None) -> bool:
    """Whether these digits ever came out of the caller's mouth.

    Compared against the whole call joined together rather than against single
    utterances, because people break a phone number up and the endpointer breaks
    it up further.

    An empty ledger means nobody has been listening — the harnesses that do not
    feed it, and the very start of a call. That answers True: this guard exists
    to catch invention, and a guard that fires when it has no information would
    block ordinary bookings.
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits or not _heard_digits:
        return True
    heard = "".join(_heard_digits)
    if digits in heard:
        return True
    # A number given with a country code, or one the store canonicalised: match
    # on the national part, which is what a person actually reads out.
    return len(digits) > 10 and digits[-10:] in heard


#: What a reference code looks like out loud: two to four letters, a dash, three
#: to six digits. Matches every prefix any config uses, and is not something a
#: person says by accident.
REF_RE = re.compile(r"\b([A-Z]{2,4}-\d{3,6})\b")


def fabricated_refs(text: str) -> list[str]:
    """Reference codes in this reply that no tool ever issued.

    The one that started this returned APT-5678 on an emergency plumbing call,
    where every real code begins JOB-. Checking the prefix alone would have
    missed a plausible JOB-1234, so codes are checked against the ones actually
    handed out.
    """
    return [m.group(1) for m in REF_RE.finditer(text or "") if m.group(1).upper() not in _issued_refs]


def correct_refs(text: str) -> tuple[str, list[str], bool]:
    """Put the real reference code into the reply before it is spoken.

    Two situations, and telling them apart is the whole job:

      A booking exists and the agent misremembered its code. Substituting the
      real one is strictly better than blocking: the caller hears the truth, and
      the sentence around it — "your plumber's booked for eight, reference is…" —
      was correct all along. Blocking would throw away a good reply over one
      wrong token.

      No booking exists at all. Here there is nothing to substitute, and putting
      a code in would make the lie MORE convincing rather than less. The reply
      itself is the fabrication; the caller is being told a plumber is coming
      when none is. That case is reported as unfixable, and the pipeline says
      something honest instead.

    Returns (text, codes replaced or dropped, unfixable).
    """
    bogus = fabricated_refs(text)
    if not bogus:
        return text, [], False
    if not _issued_refs:
        return text, bogus, True
    real = _issued_refs[-1]
    out = text
    for code in bogus:
        out = re.sub(rf"\b{re.escape(code)}\b", real, out)
    return out, bogus, False


def is_goodbye(text: str, words: tuple[str, ...]) -> bool:
    """Whether this reply is the agent signing off."""
    low = (text or "").lower()
    return any(w in low for w in words)
