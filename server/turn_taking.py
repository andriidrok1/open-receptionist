"""turn_taking.py — deciding whether the caller has finished, not just paused.

A fixed silence timer treats "I'd like to book a…" and "I'd like to book a
cleaning." identically: both get the same wait. That is the wrong trade in both
directions — it interrupts the person who paused mid-sentence, and it makes the
person who clearly finished wait for nothing.

Measured on this system: the caller waits about 3.1s in total, of which 1.1s is
that timer. So the ceiling on what better turn-taking can win is roughly a
second — the rest is the model writing its reply. Worth knowing before building
anything clever.

The decision point is the arrival of the final transcript, which lands about
0.6s after the VAD reports silence, while the timer is still running. At that
moment we finally know what was said and can shorten or lengthen what remains.

The errors are not symmetric, and the timings reflect that. Cutting someone off
mid-thought is a conversational failure; waiting an extra half second is a minor
annoyance. So "unfinished" buys much more time than "finished" gives back.

What this deliberately is NOT: a model call to judge completeness. That costs
200-400ms of its own and would have to run on interim transcripts — the ones
disabled here because they were opening turns and interrupting the bot. The
cheap syntactic version below gets most of the benefit for nothing.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import TranscriptionFrame
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)


def classify_ending(text: str, continuations: set[str], sentence_end: str) -> str:
    """"finished" | "unfinished" | "unsure" — how the utterance trails off.

    Two signals, both free:

    - the last word. "and", "to", "I need" mean a sentence is still coming;
      Scribe transcribes them faithfully, and they are the single most reliable
      hint that a pause is not an ending.
    - terminal punctuation. The transcription model adds it where it hears a
      falling intonation, which is exactly the acoustic cue a human uses.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "unsure"

    words = [w for w in stripped.lower().replace("'", "'").split() if w]
    if not words:
        return "unsure"

    raw_last = words[-1]
    last = raw_last.strip(sentence_end + ",;:—-–\"«»()")

    # Digits at the end of a SHORT utterance are dictation, not an ending.
    # A live caller reads a phone number in bursts — "415-", "528-", "4151." —
    # and the STT dutifully punctuates each burst, so the plain rules below
    # would close the turn mid-number (measured: six exchanges to collect one
    # phone number, the agent answering between the caller's chunks). Length-
    # gated so that a full sentence that happens to end in a time ("can you
    # come at 6:30?") is still closed by its question mark.
    if any(c.isdigit() for c in last) and (
        raw_last.rstrip(sentence_end).endswith(("-", "–")) or len(words) <= 4
    ):
        return "unfinished"

    if last in continuations:
        return "unfinished"
    if stripped[-1] in sentence_end:
        return "finished"
    return "unsure"


class SemanticSpeechTimeoutStrategy(SpeechTimeoutUserTurnStopStrategy):
    """End-of-turn timing that reacts to how the sentence ended.

    Drop-in for SpeechTimeoutUserTurnStopStrategy. The base class starts a timer
    when the VAD reports silence; this one re-times it once the transcript
    arrives and there is something to judge.
    """

    def __init__(
        self,
        *,
        continuations: set[str],
        sentence_end: str = ".!?…",
        after_finished: float = 0.2,
        after_unsure: float = 0.5,
        after_unfinished: float = 1.5,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._continuations = {c.strip().lower() for c in continuations}
        self._sentence_end = sentence_end
        self._waits = {
            "finished": after_finished,
            "unsure": after_unsure,
            "unfinished": after_unfinished,
        }
        self._decided = False

    async def reset(self):
        await super().reset()
        self._decided = False

    async def _handle_vad_user_started_speaking(self, frame):
        await super()._handle_vad_user_started_speaking(frame)
        self._decided = False

    async def _handle_transcription(self, frame: TranscriptionFrame):
        await super()._handle_transcription(frame)

        # Only once per turn, only for a final transcript, and only while the
        # base timer is still running — if it has already elapsed the turn is
        # being closed and re-timing it would reopen a decision already made.
        if self._decided or not frame.finalized or self._user_speech_wait_done:
            return
        if self._vad_stopped_time is None:
            return

        verdict = classify_ending(self._text, self._continuations, self._sentence_end)
        self._decided = True
        self._user_speech_timeout = self._waits[verdict]
        logger.debug(
            f"[turn] {verdict}: waiting {self._user_speech_timeout}s more — "
            f"…{self._text[-40:]!r}"
        )
        await self._restart_user_speech_timer()
