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
#: Which tools have been called this call. Ground truth for the state line:
#: "has availability ever been checked" must come from here, not from the
#: model's own belief about what it has done.
_tools_called: set[str] = set()
#: Everything the agent has said so far, lowered. Ground truth for "has the
#: fee been stated" — again a fact about the call, not a memory.
_agent_text: list[str] = []


def reset() -> None:
    """Start a new call."""
    _heard_digits.clear()
    _issued_refs.clear()
    _tools_called.clear()
    _agent_text.clear()
    _failures.clear()


#: Identical failing tool calls, counted. (name, args, error) → how many times.
_failures: dict[tuple, int] = {}


def note_tool(name: str) -> None:
    """A tool the model actually called (successfully or not)."""
    if name:
        _tools_called.add(name)


def note_failure(name: str, args: dict, error: str) -> int:
    """Count an identical failure: same tool, same arguments, same error.

    Found by the fragmented-speech tactic: given an eleven-digit number the
    store kept refusing as too long, the agent asked for it again and called
    create_booking with THE SAME digits six times running. The input was never
    going to change, and neither was the answer. The count lets the error
    message itself change on the third round — from "ask again" to "stop
    asking; offer a callback instead".
    """
    key = (name, repr(sorted((args or {}).items())), error)
    _failures[key] = _failures.get(key, 0) + 1
    return _failures[key]


def tool_was_called(name: str) -> bool:
    return name in _tools_called


def note_agent_text(text: str) -> None:
    """A reply the caller actually heard."""
    if text:
        _agent_text.append(text.lower())


def agent_said(words) -> bool:
    """Whether any of these words has come out of the agent's mouth."""
    said = " ".join(_agent_text)
    return any(str(w).lower() in said for w in words)


#: Spoken forms of the amounts a config is likely to use as a visit fee.
#: The digits always match ("99" appears in "$99"); this covers the agent
#: saying it the way the prompt tells it to — in words.
_SPOKEN_AMOUNTS = {
    49: ("forty-nine", "forty nine"), 59: ("fifty-nine", "fifty nine"),
    69: ("sixty-nine", "sixty nine"), 79: ("seventy-nine", "seventy nine"),
    89: ("eighty-nine", "eighty nine"), 99: ("ninety-nine", "ninety nine"),
    100: ("hundred",), 125: ("one twenty-five", "one twenty five"),
    149: ("one forty-nine", "one forty nine"), 150: ("one fifty",),
    199: ("one ninety-nine", "one ninety nine"),
}


def amount_words(amount: int) -> tuple[str, ...]:
    """How this amount might be said out loud: "99", "ninety-nine"."""
    return (str(amount),) + _SPOKEN_AMOUNTS.get(amount, ())


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


# ------------------------- phone digits in a reply -------------------------
#
# Found on the first live human call: in the final recap the agent read the
# phone number back with a digit missing — "four one five five two eight four
# one five", nine digits of the caller's ten. The prompt forbids repeating the
# number in the recap at all, and that held exactly as well as prompts hold.
# A model copying digits through text drops one often enough to matter, so the
# copy is checked against the ledger of what the caller actually said, and
# snapped back to it before synthesis.

#: Separators inside a spoken digit run: spaces, commas, dashes, pause tags.
_RUN_SEP = r"(?:[,\s–—-]|<break[^>]*/>)+"


def _run_regex(digit_words: dict[str, str]) -> re.Pattern:
    words = sorted(digit_words, key=len, reverse=True)
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(
        rf"\b(?:{alt})\b(?:{_RUN_SEP}(?:{alt})\b)*", re.IGNORECASE
    )


def _decode(run: str, digit_words: dict[str, str]) -> str:
    out = []
    for token in re.findall(r"[^\s,<>/=\"'-]+", run.lower()):
        if token in digit_words:
            out.append(digit_words[token])
    return "".join(out)


def _render(digits: str, digit_words: dict[str, str]) -> str:
    """Digits back into words, grouped the way the prompt reads numbers.

    "oh" for zero when the pack knows it — that is how the prompt tells the
    agent to say it, and the repair should not stick out of the sentence.
    """
    word_of = {}
    for word, digit in digit_words.items():
        word_of.setdefault(digit, word)
    if "0" in word_of and digit_words.get("oh") == "0":
        word_of["0"] = "oh"
    groups = (
        [digits[:3], digits[3:6], digits[6:]]
        if len(digits) == 10
        else [digits[i:i + 3] for i in range(0, len(digits), 3)]
    )
    spoken = [" ".join(word_of.get(d, d) for d in g) for g in groups if g]
    return ' <break time="0.4s" /> '.join(spoken)


def correct_phone(
    text: str, digit_words: dict[str, str], phone_len: int | None
) -> tuple[str, list[str]]:
    """Snap phone-length digit runs in a reply back to what the caller said.

    Only runs of seven digits or more are touched — dates, times and prices
    are said as number words ("ninety-nine"), not digit by digit, so they
    never look like this.

    Three cases:
      * the run matches a full-length stretch of the heard ledger — correct,
        leave it;
      * it is a mangled copy of something the caller said (a digit dropped or
        doubled) — replace it with the best-overlapping full-length stretch;
      * nothing similar was ever heard — there is nothing true to say, so the
        run is removed rather than repaired into a better-dressed lie.

    Returns (text, list of runs that were changed).
    """
    if not text or not digit_words:
        return text, []
    heard = "".join(_heard_digits)
    changed: list[str] = []

    def windows() -> list[str]:
        if not phone_len or len(heard) < phone_len:
            return []
        return [heard[i:i + phone_len] for i in range(len(heard) - phone_len + 1)]

    def best_window(run: str) -> str | None:
        # The most recent window sharing the longest run of digits — people
        # correct themselves, and the number being talked about is the last.
        cands = windows()
        best, best_len = None, 0
        for w in cands:
            k = _overlap(run, w)
            if k >= best_len:  # >= : later windows win ties
                best, best_len = w, k
        return best if best_len >= min(5, len(run)) else None

    def fix(match: re.Match) -> str:
        run = match.group(0)
        digits = _decode(run, digit_words)
        if len(digits) < 7:
            return run
        if digits in heard and (not phone_len or len(digits) == phone_len):
            return run
        good = best_window(digits)
        changed.append(digits)
        return _render(good, digit_words) if good else ""

    return _run_regex(digit_words).sub(fix, text), changed


def _overlap(a: str, b: str) -> int:
    """Length of the longest common substring — cheap, the strings are tiny."""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best
