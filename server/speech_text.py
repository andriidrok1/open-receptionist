"""speech_text.py — preparing the LLM's text for speech synthesis.

ElevenLabs does not understand meaning; it voices what is written. This layer
cleans up pause markup before synthesis.

**break tags** put a pause exactly where punctuation cannot — between blocks of
digits in a phone number, before a price. But ElevenLabs warns explicitly that
too many of them make speech accelerate and produce artifacts, hence the
sanitiser here: at most `MAX_BREAKS` per reply, none longer than
`MAX_BREAK_SEC`. The prompt asks the model for at most two as well, but that is
not something to rely on.

---
WHAT USED TO BE HERE AND WHY IT IS GONE (judged by ear, 2026-08-09).

This module used to place lexical stress marks: the `ukrainian-word-stress`
dictionary (2.9M word forms) put a combining acute U+0301 into every word, plus
a domain layer for the hotel's own vocabulary. The reasoning: the model guesses
stress from spelling and gets it wrong on numerals and dates — exactly where our
prices and phone numbers live.

The hypothesis did NOT hold. In a blind comparison the raw text sounded better
than both blanket and targeted markup.

The reason: the stress problem was a property of the ENGLISH voice that was
reading Ukrainian at the time. A native voice, fine-tuned for the model in use,
places stress correctly on its own, and unusual characters only confuse it.

Lesson worth keeping: **stress marks treat the wrong disease. Get a native voice
for the language first.** If the voice or the model ever changes, the hypothesis
is worth re-testing: `pip install --no-deps ukrainian-word-stress marisa-trie`,
then `Stressifier(stress_symbol=StressSymbol.CombiningAcuteAccent)`.
"""

import re

from pipecat.frames.frames import (
    AggregatedTextFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

MAX_BREAKS = 3  # any more and the speech starts to race
MAX_BREAK_SEC = 0.6

#: How long a pause after a piece of key information. Long enough to be heard as
#: a beat, short enough not to read as the agent having finished. Well inside
#: ElevenLabs' 3s ceiling and inside MAX_BREAK_SEC, so the sanitiser leaves it
#: alone.
KEY_PAUSE_SEC = 0.4

#: A run of number words shorter than this gets no pause. "One moment" and "just
#: a second" are not information; "twelve PM" and "eighty dollars" are. Two is
#: the smallest threshold that tells them apart.
MIN_KEY_RUN = 2

_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*|\d+", re.UNICODE)
_CLAUSE_END_RE = re.compile(r"[.,;:!?…]")

# The model writes the tag however it likes: without the self-closing slash, in
# single quotes, with a different attribute. A narrow pattern only matched the
# canonical form and let the rest through to synthesis untouched — past the
# count limit and past the duration clamp.
_BREAK_RE = re.compile(r"""<break\s+time=["'](\d*\.?\d+)s["']\s*/?>""", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]*>")


def sanitize_breaks(text: str) -> str:
    """Keep at most MAX_BREAKS tags, none longer than MAX_BREAK_SEC.

    Anything else that looks like markup is stripped: with SSML parsing enabled,
    ElevenLabs will at best ignore an unrecognised tag and at worst read it out.
    """
    kept = 0

    def repl(m: re.Match) -> str:
        nonlocal kept
        if kept >= MAX_BREAKS:
            return " "
        kept += 1
        secs = min(float(m.group(1)), MAX_BREAK_SEC)
        return f'<break time="{secs}s" />'

    text = _BREAK_RE.sub(repl, text)
    # Guard against any remaining markup. Our own break tags are already in
    # place, so cut only what is not one of them.
    return _ANY_TAG_RE.sub(lambda m: m.group(0) if m.group(0).startswith("<break ") else " ", text)


def add_key_pauses(text: str, number_words: set[str], glue_words: set[str]) -> str:
    """Put a beat after each date, time, price or spelled-out number.

    A caller cannot rewind. "So that's a cleaning on Friday August fourteenth at
    twelve PM a hundred and eighty dollars does that sound right" arrives as one
    unbroken stream, and by the price they have lost the date. Punctuation alone
    does not fix it: a comma is a very short pause, and the model does not always
    write one where the information ends.

    The unit found here is a RUN of number words — "fourteenth at twelve PM" —
    not a single word, because a lone number is usually prose ("one moment").
    Glue words count only inside a run, never at its edges, so "at the desk"
    cannot start one.

    Deliberately deterministic rather than asked of the model. The prompt has
    requested break tags at key moments all along and the model obliges perhaps
    half the time — and "half the time" is indistinguishable from random to
    whoever is listening.

    Nothing is inserted at the very end of the reply (silence follows anyway),
    next to a pause the model already wrote, or once the budget is used up.
    """
    if not number_words:
        return text

    runs, start, last_number, prev_end = [], None, None, 0
    for m in _WORD_RE.finditer(text):
        word = m.group(0).lower().replace("’", "'")
        # Punctuation ends a run even when number words continue after it. Without
        # this, "August fourteenth at twelve PM, a hundred and eighty dollars"
        # is one run bridged by "at" and "a", and the date and the price share a
        # single pause at the end — which is the one place neither of them needs
        # it. A comma counts: in a spoken recap it IS the boundary between one
        # fact and the next.
        if start is not None and _CLAUSE_END_RE.search(text[prev_end:m.start()]):
            runs.append((start, last_number.end()))
            start, last_number = None, None
        prev_end = m.end()

        if word in number_words:
            if start is None:
                start = m.start()
            last_number = m
        elif word in glue_words and start is not None:
            continue  # may sit inside a run; the run still ends on the last number
        else:
            if start is not None and last_number is not None:
                runs.append((start, last_number.end()))
            start, last_number = None, None
    if start is not None and last_number is not None:
        runs.append((start, last_number.end()))

    # Only runs of real substance, and counted in words rather than characters.
    runs = [(a, b) for a, b in runs if len(_WORD_RE.findall(text[a:b])) >= MIN_KEY_RUN]
    if not runs:
        return text

    budget = MAX_BREAKS - len(_BREAK_RE.findall(text))
    if budget <= 0:
        return text

    out, cursor, used = [], 0, 0
    for _, end in runs:
        if used >= budget:
            break
        # Step over the punctuation that closes the phrase, so the pause lands
        # after "…eighty dollars." rather than inside it.
        tail = end
        while tail < len(text) and text[tail] in ".,;:!?—–-":
            tail += 1
        rest = text[tail:]
        if not rest.strip():
            break  # end of the reply; the silence after it is the pause
        if rest.lstrip().startswith("<break"):
            continue  # the model already put one here
        out.append(text[cursor:tail])
        out.append(f'<break time="{KEY_PAUSE_SEC}s" />')
        cursor = tail
        used += 1
    out.append(text[cursor:])
    return "".join(out)


class SpeechTextFilter:
    """Text filter applied before TTS. Implements pipecat's BaseTextFilter.

    The interface is implemented structurally rather than by inheritance, so the
    module stays importable and testable on its own, without the pipeline.

    The word lists are passed IN rather than read from a language pack here, so
    this module still knows nothing about locales and stays testable with three
    words and no YAML. Given none, it only sanitises and inserts nothing.
    """

    def __init__(self, number_words=(), glue_words=()):
        self._numbers = {w.lower() for w in number_words}
        self._glue = {w.lower() for w in glue_words}

    def prepare(self, text: str) -> str:
        """All processing in one call — convenient to test without a pipeline.

        Order matters: pauses are added first and sanitised after, so our own
        insertions are subject to the same budget and clamp as the model's. The
        other way round, the count could be exceeded by exactly the tags we add.
        """
        if not text or not text.strip():
            return text
        return sanitize_breaks(add_key_pauses(text, self._numbers, self._glue))

    # --- pipecat BaseTextFilter interface ---

    async def filter(self, text: str) -> str:
        return self.prepare(text)

    async def update_settings(self, settings):
        pass

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass


class WholeReplyAggregator(FrameProcessor):
    """Collect the LLM's reply and hand it to TTS in one piece.

    By default pipecat synthesises sentence by sentence: the first sentence is
    already being spoken while the model is still writing the second. That is
    the lowest possible latency, but intonation is then built within a single
    sentence — the synthesiser does not know another phrase is coming, so it
    closes each one with a full stop. A three-sentence reply sounds like three
    separate announcements.

    Here we wait for the end of the reply and send the whole text at once:
    intonation carries across the reply, the price does not land in a vacuum,
    and a closing question actually sounds like a question. The cost is latency
    — the bot stays silent until the model has finished writing.

    We emit an `AggregatedTextFrame` rather than a plain `TextFrame` because it
    bypasses the TTS service's own aggregator. Plain text gets synthesised once
    and then flushed again from that internal buffer at the end of the response,
    so every reply was voiced twice.

    The pause sanitiser lives here too. It used to sit in the TTS service's
    `text_filters`, and that corrupted text: a filter changes the string that
    goes to synthesis while the original stays as it was, and the spoken-word
    tracker walks both cursors in step. Tags of different lengths ("2s" ->
    "0.6s") shift one cursor relative to the other, and the conversation history
    ended up with letters chewed out of the middle of words. Sanitising before
    the text forks means synthesis and context receive the exact same string.

    Place between the LLM and TTS.
    """

    def __init__(self, number_words=(), glue_words=(), **kwargs):
        super().__init__(**kwargs)
        self._parts: list[str] = []
        self._collecting = False
        # Same filter the sentence-by-sentence path uses, so both routes through
        # the pipeline produce identical text.
        self._filter = SpeechTextFilter(number_words, glue_words)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._parts, self._collecting = [], True
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = self._filter.prepare("".join(self._parts)).strip()
            self._parts, self._collecting = [], False
            if text:
                await self.push_frame(
                    AggregatedTextFrame(text=text, aggregated_by="llm_response"), direction
                )
            await self.push_frame(frame, direction)
        elif self._collecting and isinstance(frame, TextFrame) and not isinstance(
            frame, (TTSTextFrame,)
        ):
            # Hold the text back; it will go out as a single frame.
            self._parts.append(frame.text)
        elif isinstance(frame, InterruptionFrame):
            # Interrupted — nobody needs the half-written reply.
            self._parts, self._collecting = [], False
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)


if __name__ == "__main__":
    f = SpeechTextFilter()
    samples = [
        'Recording.<break time="0.4s" /> Three nights.<break time="2s" /> Your phone:'
        '<break time="0.3s" /> oh six seven<break time="0.3s" /> one two three.',
        "No markup here, nothing should change.",
    ]
    for s in samples:
        print("\nIN :", s)
        print("OUT:", f.prepare(s))
