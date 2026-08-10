"""call_metrics.py — what a call actually costs, in seconds and in money.

Two questions this answers, and neither had an answer before.

**How long the caller waits.** Pipecat reports time-to-first-byte per service,
which is useful for finding the slow one but is not what a person experiences.
What they experience is the gap between the moment they stop talking and the
moment they hear a voice — and that gap is the sum of the end-of-turn threshold,
the model writing the whole reply, and the synthesiser starting. Nobody had
measured it, so nobody knew which of the three to attack.

**What a minute costs.** The whole pitch against the per-minute platforms rests
on this number, and it was never anything but an assumption.

Usage is always recorded. Money is only computed when `pricing.yaml` says what
things cost — prices change, and a hard-coded rate that quietly goes stale is
worse than no rate at all.

Place it next to the other input-side processors: it needs to see the VAD's
end-of-speech going downstream and the bot's start-of-speech coming back up.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    CancelFrame,
    EndFrame,
    FunctionCallInProgressFrame,
    Frame,
    MetricsFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

PRICING_PATH = Path(__file__).parent / "pricing.yaml"


@dataclass
class CallTotals:
    """Everything measured over one call."""

    started_at: float = field(default_factory=time.monotonic)
    replies: int = 0
    #: Seconds between the caller falling silent and the first sound of the
    #: reply, split by whether the agent had to call a tool. Mixing them is
    #: meaningless: a tool turn is two inference passes plus the lookup, so it
    #: is several seconds slower by construction. Averaging the two produced a
    #: number that swung between 3s and 9s depending on what the caller happened
    #: to ask, and hid every real change underneath that swing.
    wait_times: list[float] = field(default_factory=list)
    wait_times_with_tool: list[float] = field(default_factory=list)
    #: Time-to-first-byte per service, as pipecat reports it.
    ttfb: dict[str, list[float]] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tts_characters: int = 0

    @property
    def duration_s(self) -> float:
        return time.monotonic() - self.started_at

    def summary(self) -> dict:
        def median(values):
            v = sorted(values)
            return round(v[len(v) // 2], 2) if v else None

        waits = sorted(self.wait_times)
        return {
            "duration_s": round(self.duration_s, 1),
            "replies": self.replies,
            "wait_median_s": median(self.wait_times),
            "wait_worst_s": round(waits[-1], 2) if waits else None,
            "wait_median_tool_s": median(self.wait_times_with_tool),
            "replies_plain": len(self.wait_times),
            "replies_with_tool": len(self.wait_times_with_tool),
            "ttfb_median_s": {
                name: round(sorted(v)[len(v) // 2], 2) for name, v in self.ttfb.items() if v
            },
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tts_characters": self.tts_characters,
        }


def load_pricing() -> dict | None:
    """Provider rates, if someone has written them down.

    Deliberately a file rather than constants: rates change, and this number
    ends up in a README as a claim about money. It carries the date it was
    checked so a stale figure is visible rather than believed.
    """
    if not PRICING_PATH.exists():
        return None
    return yaml.safe_load(PRICING_PATH.read_text(encoding="utf-8")) or None


def estimate_cost(totals: CallTotals, pricing: dict) -> dict:
    """Cost of this call, broken down. All rates are per the units in the file."""
    rates = pricing["rates"]
    minutes = totals.duration_s / 60
    llm_in = totals.prompt_tokens / 1_000_000 * rates["llm_input_per_mtok"]
    llm_out = totals.completion_tokens / 1_000_000 * rates["llm_output_per_mtok"]
    tts = totals.tts_characters / 1000 * rates["tts_per_1k_chars"]
    stt = minutes * rates["stt_per_minute"]
    telephony = minutes * rates.get("telephony_per_minute", 0)
    total = llm_in + llm_out + tts + stt + telephony
    return {
        "priced_on": pricing.get("checked", "unknown"),
        "llm": round(llm_in + llm_out, 4),
        "tts": round(tts, 4),
        "stt": round(stt, 4),
        "telephony": round(telephony, 4),
        "total": round(total, 4),
        "per_minute": round(total / minutes, 4) if minutes else None,
        "currency": pricing.get("currency", "USD"),
    }


class CallMetrics(FrameProcessor):
    """Collects the numbers and reports them when the call ends.

    `on_finish` is handed the summary so the caller can store it — the pipeline
    should not know about databases.
    """

    def __init__(self, on_finish=None, **kwargs):
        super().__init__(**kwargs)
        self._totals = CallTotals()
        self._silent_since: float | None = None
        self._tool_this_turn = False
        self._on_finish = on_finish
        self._reported = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            # The clock the caller feels starts here, not when the LLM is asked.
            self._silent_since = time.monotonic()
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._tool_this_turn = True
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._silent_since is not None:
                waited = time.monotonic() - self._silent_since
                bucket = (
                    self._totals.wait_times_with_tool
                    if self._tool_this_turn
                    else self._totals.wait_times
                )
                bucket.append(waited)
                self._totals.replies += 1
                self._silent_since = None
                self._tool_this_turn = False
        elif isinstance(frame, MetricsFrame):
            self._collect(frame)
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._report()

        await self.push_frame(frame, direction)

    def _collect(self, frame: MetricsFrame) -> None:
        for item in frame.data:
            if isinstance(item, TTFBMetricsData):
                self._totals.ttfb.setdefault(item.processor, []).append(item.value)
            elif isinstance(item, LLMUsageMetricsData):
                self._totals.prompt_tokens += item.value.prompt_tokens
                self._totals.completion_tokens += item.value.completion_tokens
            elif isinstance(item, TTSUsageMetricsData):
                self._totals.tts_characters += item.value

    def report_now(self) -> None:
        """Report from outside the pipeline.

        A browser tab closing tears the worker down without an EndFrame ever
        reaching here, so the call's numbers were lost — for most calls, since
        people close the tab rather than pressing a button.
        """
        self._report()

    def _report(self) -> None:
        if self._reported:
            return
        self._reported = True
        summary = self._totals.summary()
        pricing = load_pricing()
        if pricing:
            summary["cost"] = estimate_cost(self._totals, pricing)

        wait = summary["wait_median_s"]
        logger.info(
            f"[call] {summary['duration_s']}s · {summary['replies']} replies · "
            f"median wait {wait}s · {summary['prompt_tokens']}+"
            f"{summary['completion_tokens']} tokens · {summary['tts_characters']} chars"
            + (f" · ${summary['cost']['total']}" if pricing else " · no pricing.yaml")
        )
        if self._on_finish:
            try:
                self._on_finish(summary)
            except Exception as e:  # noqa: BLE001 — metrics must never break a call
                logger.error(f"[call] could not store metrics: {e}")


def format_report(summary: dict) -> str:
    """The same numbers as a block of text, for a terminal."""
    lines = [
        f"call length      {summary['duration_s']}s, {summary['replies']} replies",
        f"caller waits     plain {summary['wait_median_s']}s "
        f"(x{summary['replies_plain']}), with a tool call "
        f"{summary['wait_median_tool_s']}s (x{summary['replies_with_tool']})",
    ]
    for name, value in sorted(summary["ttfb_median_s"].items()):
        lines.append(f"  {name:<28} {value}s to first byte")
    lines.append(
        f"usage            {summary['prompt_tokens']} in + "
        f"{summary['completion_tokens']} out tokens, "
        f"{summary['tts_characters']} synthesised characters"
    )
    cost = summary.get("cost")
    if cost:
        lines.append(
            f"cost             {cost['total']} {cost['currency']} "
            f"({cost['per_minute']}/min) — rates checked {cost['priced_on']}"
        )
        lines.append(
            f"  of which       llm {cost['llm']} · tts {cost['tts']} · stt {cost['stt']}"
            + (f" · phone {cost['telephony']}" if cost["telephony"] else "")
        )
    else:
        lines.append("cost             not computed — no pricing.yaml")
    return "\n".join(lines)


if __name__ == "__main__":  # quick check of the arithmetic, no pipeline needed
    t = CallTotals()
    t.started_at = time.monotonic() - 120
    t.wait_times = [2.1, 2.4, 1.9]
    t.wait_times_with_tool = [4.8]
    t.replies = 4
    t.prompt_tokens, t.completion_tokens, t.tts_characters = 12_000, 900, 1_400
    s = t.summary()
    p = load_pricing()
    if p:
        s["cost"] = estimate_cost(t, p)
    print(format_report(s))
    print("\nraw:", json.dumps(s, ensure_ascii=False, default=str))
