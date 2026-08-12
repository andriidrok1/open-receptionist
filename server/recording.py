"""recording.py — keeping what was actually said on a call.

Until now the only trace a call left was which tools were invoked. The owner's
first question about a machine answering their phone is "what did it say to
them?", and there was no answer — not for a complaint, not for improving the
prompt, not for finding out why a booking looks wrong.

This records the transcript — who said what, in text. Cheap, small, and the
thing anyone actually reads.

Audio recording is deliberately NOT here. Two reasons, in order of importance.
Legally it is a different act: several US states require both parties' consent
to record a voice, and the EU has its own rules, so a business that wants it
needs an announcement in its greeting and a decision the code should not make
for them. Practically, pipecat's AudioBufferProcessor needs more care with its
lifecycle than one afternoon allowed — it stayed silent in-pipeline and the
debugging was not paying for itself while the transcript, which answers the
actual question, already worked.
"""

from __future__ import annotations

import json
import uuid

from loguru import logger
from pipecat.frames.frames import (
    AggregatedTextFrame,
    Frame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import guards
from business import L


class CallRecorder:
    """Collects one call's transcript and, optionally, its audio.

    The store is passed in rather than imported so this module stays about
    recording and nothing else.
    """

    def __init__(self, *, business_key: str, started_at: str):
        self.call_id = uuid.uuid4().hex[:12]
        self.business_key = business_key
        self.started_at = started_at
        self.lines: list[dict] = []

    # ------------------------------ transcript ------------------------------

    def add(self, who: str, text: str) -> None:
        """Record one line. `who` is 'caller' or 'agent'."""
        text = (text or "").strip()
        if not text:
            return
        # A running clock rather than a wall time: what matters when reading a
        # transcript back is where in the call something happened.
        self.lines.append({"at": len(self.lines), "who": who, "text": text})

    def add_tool(self, name: str, result: dict) -> None:
        """Record a tool call, so the transcript shows why the agent said what
        it said — "checked availability" between two lines explains a pause."""
        outcome = result.get("ref_code") or result.get("error") or (
            "ok" if result.get("success") or result.get("available") else ""
        )
        self.lines.append({"at": len(self.lines), "who": "tool", "text": f"{name}: {outcome}"})

    def as_text(self) -> str:
        """The conversation as something a human reads."""
        marks = {"caller": "caller", "agent": "agent", "tool": "   ·"}
        return "\n".join(f"{marks.get(x['who'], x['who']):>6}: {x['text']}" for x in self.lines)

    # ------------------------------- finishing -------------------------------

    def summary(self, metrics: dict | None = None) -> dict:
        return {
            "call_id": self.call_id,
            "business": self.business_key,
            "started_at": self.started_at,
            "lines": self.lines,
            "metrics": metrics or {},
        }

    def finish(self, store, metrics: dict | None = None) -> None:
        """Write the call down. Never raises: losing a transcript is bad, and
        breaking the end of a call because of it is worse."""
        try:
            store(self.summary(metrics))
            logger.info(f"[call {self.call_id}] {len(self.lines)} lines recorded")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[call {self.call_id}] could not store the transcript: {e}")


class TranscriptCollector(FrameProcessor):
    """Builds the transcript out of frames as they pass.

    pipecat 1.5 has no TranscriptProcessor, so this listens for the frames
    directly. Two instances are needed, and their positions are not a matter of
    taste — each frame is CONSUMED by the processor it feeds:

    - side="caller" goes before the context aggregator, which swallows
      TranscriptionFrame and never passes it on;
    - side="agent" goes before the TTS, which swallows AggregatedTextFrame.

    A single collector at the tail of the pipeline records nothing at all, which
    is exactly what the first version did.
    """

    def __init__(self, recorder: CallRecorder, side: str, **kwargs):
        super().__init__(**kwargs)
        self._recorder = recorder
        self._side = side
        self._digit_words = dict(L.raw("speech.digit_words", {})) if side == "caller" else {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if self._side == "caller":
            if isinstance(frame, TranscriptionFrame) and getattr(frame, "finalized", True):
                # Final transcripts only: interim ones are guesses, and Scribe
                # emits them even on silence.
                self._recorder.add("caller", frame.text)
                # The same text feeds the guards' ledger of what was actually
                # said. It goes here rather than in bot.py because this is the
                # one place a final caller transcript is guaranteed to pass —
                # the aggregator below consumes the frame.
                guards.hear(frame.text, self._digit_words)
        elif isinstance(frame, AggregatedTextFrame) and not isinstance(frame, TTSTextFrame):
            self._recorder.add("agent", frame.text)
            # The other half of the guards ledger: what the agent has said,
            # for facts like "has the fee been stated yet". This collector
            # sits after ClaimGuard, so it records the repaired reply — the
            # one the caller actually heard.
            guards.note_agent_text(frame.text)

        await self.push_frame(frame, direction)


def format_call(summary: dict) -> str:
    """A stored call as a block of text, for a terminal."""
    head = f"call {summary['call_id']} · {summary['business']} · {summary['started_at']}"
    marks = {"caller": "caller", "agent": "agent", "tool": "   ·"}
    body = "\n".join(f"{marks.get(x['who'], x['who']):>6}: {x['text']}" for x in summary["lines"])
    metrics = summary.get("metrics") or {}
    tail = ""
    if metrics:
        tail = (
            f"\n\n{metrics.get('duration_s')}s · {metrics.get('replies')} replies"
            f" · waited {metrics.get('wait_median_s')}s"
        )
        cost = metrics.get("cost")
        if cost:
            tail += f" · {cost['total']} {cost['currency']}"
    return f"{head}\n{'-' * len(head)}\n{body}{tail}"


if __name__ == "__main__":  # a quick look at the shape, no pipeline needed
    r = CallRecorder(business_key="clinic", started_at="2026-08-11 09:00:00")
    r.add("agent", "Добрий день, стоматологія «Дентал Сіті».")
    r.add("caller", "Хочу записатися на чистку.")
    r.add_tool("check_availability", {"available": True})
    r.add("agent", "Завтра о десятій вільно. Записую?")
    print(format_call(r.summary({"duration_s": 42.0, "replies": 2, "wait_median_s": 2.9})))
    print("\nraw:", json.dumps(r.summary(), ensure_ascii=False)[:120] + "…")
