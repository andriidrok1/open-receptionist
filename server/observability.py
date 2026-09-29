"""observability.py — one call, traced: what happened when, and what it cost.

call_metrics.py answers two questions per call — how long the caller waited,
what the minute cost — and stores the totals. This answers the question that
comes right after: *where did the time go?* Per turn, the moment the caller
fell silent, the STT's final transcript, the model's first token and last,
every tool it ran, the synthesiser's first byte of audio, the first sound the
caller heard, and when the bot stopped. From those moments come the numbers
that decide what to fix: p50 / p95 of the perceived wait, split into the parts
we own (the end-of-turn timer), the parts we buy (STT, LLM, TTS) and the parts
we compute (tools).

Every moment is one line of JSON in `logs/calls/<call_id>.jsonl`, written as it
happens, so a call that dies mid-way still leaves its trace. The last line is
the summary; `call_report.py` folds a directory of those files into one table.

How it hooks in: a Pipecat *observer*, not a processor. An observer is told
about every frame the pipeline pushes without sitting in the pipeline, which
matters here for two reasons. Nothing about the pipeline changes — no new
position to get wrong, no frame consumed by mistake. And a processor only sees
the frames that reach its position: the tail sees no LLM or tool frames
(the assistant aggregator consumes them), the head sees no audio. The observer
sees each frame at its first push, which is also the moment closest to when it
actually happened.

Tracing must never break a call. Every file write is wrapped; if the log
directory cannot be created the trace stays in memory and the summary line
still lands in the main log.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from call_metrics import CallTotals, estimate_cost, load_pricing

#: Where the per-call files go. An environment variable rather than a config
#: line: an operator running several businesses on one box wants one folder,
#: and the tests want a temporary one.
LOGS_DIR = Path(os.getenv("CALL_LOGS_DIR") or Path(__file__).parent / "logs" / "calls")

#: The stages of a turn, in the order the caller experiences them. Each maps
#: to a per-turn key produced by `turn_metrics`, and the report prints them in
#: this order.
STAGES = (
    ("turn_timer_s", "end-of-turn timer"),
    ("stt_s", "stt final transcript"),
    ("llm_ttft_s", "llm first token"),
    ("llm_s", "llm total (all runs)"),
    ("tools_s", "tools"),
    ("tts_s", "tts first audio"),
    ("latency_s", "caller stops → bot speaks"),
)


# ------------------------------------------------------------------ the ledger


class CallTrace:
    """The events of one call, in order, and the file they are written to.

    `record` is the only way in. The observer below calls it from the pipeline;
    tests call it directly. Both produce the same rows, so the metrics can be
    tested on hand-written events and trusted on real ones.
    """

    def __init__(
        self,
        *,
        call_id: str,
        business: str,
        transport: str | None = None,
        logs_dir: Path | None = None,
        clock=time.monotonic,
    ):
        self.call_id = call_id
        self.business = business
        self.events: list[dict] = []
        self.finished = False
        self._clock = clock
        self._t0 = clock()
        self._file = None
        self._path: Path | None = None
        logs_dir = Path(logs_dir) if logs_dir is not None else LOGS_DIR
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            self._path = logs_dir / f"{call_id}.jsonl"
            self._file = self._path.open("a", encoding="utf-8")
        except OSError as e:  # the trace stays in memory; the call goes on
            logger.warning(f"[trace {call_id}] cannot write {logs_dir}: {e}")
            self._path = None
        self.record("call_started", business=business, transport=transport)

    @property
    def path(self) -> Path | None:
        return self._path

    def now(self) -> float:
        """Seconds since the trace began, on the trace's own clock."""
        return self._clock() - self._t0

    def record(self, event: str, t: float | None = None, **fields) -> dict:
        """Append one event. `t` is seconds since the call began; by default,
        now. Returns the row so a caller can read what was written."""
        row = {
            "t": round(self.now() if t is None else t, 3),
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "call_id": self.call_id,
            "event": event,
            **fields,
        }
        self.events.append(row)
        if self._file is not None:
            try:
                self._file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                self._file.flush()
            except OSError as e:  # noqa: BLE001 — never let tracing break a call
                logger.warning(f"[trace {self.call_id}] write failed: {e}")
                self._file = None
        return row

    def observer(self) -> CallTraceObserver:
        """The pipeline hook: pass it in `PipelineWorker(observers=[...])`."""
        return CallTraceObserver(self)

    def summary(self) -> dict:
        return summarize(self.events)

    def finish(self, reason: str = "ended") -> dict | None:
        """Close the call: write the summary row, one line to the main log.

        Idempotent. The observer calls it on EndFrame / CancelFrame; bot.py
        calls it again when the transport disconnects, because a closed browser
        tab tears the worker down without an EndFrame ever reaching anyone.
        """
        if self.finished:
            return None
        self.finished = True
        # The call lasts until now, not until its last event: the summary line
        # and the file read back later must agree on the duration.
        summary = summarize(self.events, ended=reason, ended_at=self.now())
        self.record("call_ended", reason=reason, summary=summary)
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        logger.info(format_summary_line(summary, self._path))
        return summary


# ---------------------------------------------------------------- the observer


class CallTraceObserver(BaseObserver):
    """Turns frames into trace events, and groups them into turns.

    A turn is one exchange: from the caller falling silent to the bot falling
    silent. The greeting, a fixed line spoken on idle, and the recovery run
    after a retracted claim have no caller half; they open a turn of their own
    so their timings are kept, but they carry no perceived latency — there was
    nobody waiting.

    First push only: a frame is pushed again by every processor that passes
    it along, and the first push is the moment closest to the event. Frames
    the pipeline broadcasts (bot started, user stopped) exist twice, once per
    direction, and are folded back to one by their sibling id.

    Time is the trace's own clock, read when the frame is pushed — an observer
    runs inside the push, so that is the same instant. The pipeline's clock
    would do as well, except that it starts with the pipeline, after the
    services have connected, and the row written before that (`call_started`)
    and the one written after (`call_ended`) would then be on a different
    clock from everything in between.
    """

    def __init__(self, trace: CallTrace, **kwargs):
        super().__init__(observe_every_push=False, **kwargs)
        self._trace = trace
        self._turn = 0
        self._open = False  # a turn exists and the bot has not finished it
        self._bot_started = False
        self._bot_stopped = False
        self._speaking_turn: int | None = None  # which turn the bot's voice belongs to
        self._llm_runs = 0
        self._llm_run_open = False
        self._first_token_seen = False
        self._tts_first_audio_seen = False
        self._tools_started: dict[str, float] = {}
        self._seen_siblings: set[str] = set()

    # -- turn bookkeeping

    def _new_turn(self) -> None:
        self._turn += 1
        self._open = True
        self._bot_started = self._bot_stopped = False
        self._llm_runs = 0
        self._llm_run_open = False
        self._first_token_seen = False
        self._tts_first_audio_seen = False
        self._tools_started.clear()

    def _turn_for_caller_event(self) -> int:
        """The caller spoke: a new turn unless the bot has not answered the
        current one yet (they paused and went on, or the STT is still
        catching up)."""
        if not self._open or self._bot_started:
            self._new_turn()
        return self._turn

    def _turn_for_bot_event(self) -> int:
        """The bot is producing something: continue the turn it owes an answer
        to, or open one if the previous exchange is over."""
        if not self._open or self._bot_stopped:
            self._new_turn()
        return self._turn

    def _dedup(self, frame) -> bool:
        """True if this frame is the second half of a broadcast already seen."""
        sibling = getattr(frame, "broadcast_sibling_id", None)
        if not sibling:
            return False
        if sibling in self._seen_siblings:
            self._seen_siblings.discard(sibling)
            return True
        self._seen_siblings.add(frame.id)
        return False

    # -- the hook

    async def on_push_frame(self, data: FramePushed):
        try:
            self._observe(data.frame, self._trace.now())
        except Exception as e:  # noqa: BLE001 — tracing must never break a call
            logger.warning(f"[trace {self._trace.call_id}] observer error: {e}")

    def _observe(self, frame, t: float) -> None:  # noqa: C901 — one branch per frame kind
        rec = self._trace.record

        if isinstance(frame, TTSAudioRawFrame):
            # Hundreds per reply; only the first of a turn is a moment.
            if self._open and not self._tts_first_audio_seen:
                self._tts_first_audio_seen = True
                rec("tts_first_audio", t, turn=self._turn_for_bot_event())
            return

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            rec("user_stopped", t, turn=self._turn_for_caller_event())
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if not self._dedup(frame):
                rec("turn_closed", t, turn=self._turn_for_caller_event())
        elif isinstance(frame, TranscriptionFrame):
            # Final by class: interim guesses are InterimTranscriptionFrame. The
            # `finalized` flag is not consulted — only one STT service sets it.
            if (frame.text or "").strip():
                rec("transcript", t, turn=self._turn_for_caller_event(), chars=len(frame.text))
        elif isinstance(frame, LLMFullResponseStartFrame):
            turn = self._turn_for_bot_event()
            self._llm_runs += 1
            self._llm_run_open = True
            self._first_token_seen = False
            rec("llm_started", t, turn=turn, run=self._llm_runs)
        elif isinstance(frame, LLMTextFrame):
            if self._llm_run_open and not self._first_token_seen:
                self._first_token_seen = True
                rec("llm_first_token", t, turn=self._turn, run=self._llm_runs)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._llm_run_open:
                self._llm_run_open = False
                rec("llm_done", t, turn=self._turn, run=self._llm_runs)
        elif isinstance(frame, FunctionCallInProgressFrame):
            turn = self._turn_for_bot_event()
            self._tools_started[frame.tool_call_id] = t
            rec("tool_started", t, turn=turn, name=frame.function_name, id=frame.tool_call_id)
        elif isinstance(frame, FunctionCallResultFrame):
            started = self._tools_started.pop(frame.tool_call_id, None)
            rec(
                "tool_done",
                t,
                turn=self._turn_for_bot_event(),
                name=frame.function_name,
                id=frame.tool_call_id,
                dur_s=None if started is None else round(t - started, 3),
                outcome=tool_outcome(frame),
                error=frame.error or _domain_error(frame.result),
            )
        elif isinstance(frame, TTSStartedFrame):
            rec("tts_started", t, turn=self._turn_for_bot_event())
        elif isinstance(frame, BotStartedSpeakingFrame):
            if not self._dedup(frame):
                turn = self._turn_for_bot_event()
                self._bot_started = True
                self._speaking_turn = turn
                rec("bot_started", t, turn=turn)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Attributed to the turn whose reply this was, not the current
            # one: a caller's "uh-huh" under the bot's voice has already opened
            # the next turn, and the reply's length must not land there.
            if not self._dedup(frame) and self._speaking_turn is not None:
                if self._speaking_turn == self._turn:
                    self._bot_stopped = True
                rec("bot_stopped", t, turn=self._speaking_turn)
                self._speaking_turn = None
        elif isinstance(frame, InterruptionFrame):
            if self._speaking_turn is not None:
                rec("interrupted", t, turn=self._speaking_turn)
        elif isinstance(frame, MetricsFrame):
            for item in frame.data:
                if isinstance(item, TTFBMetricsData):
                    rec("ttfb", t, processor=item.processor, value=round(item.value, 3))
                elif isinstance(item, LLMUsageMetricsData):
                    rec(
                        "usage",
                        t,
                        kind="llm",
                        processor=item.processor,
                        prompt_tokens=item.value.prompt_tokens,
                        completion_tokens=item.value.completion_tokens,
                    )
                elif isinstance(item, TTSUsageMetricsData):
                    rec("usage", t, kind="tts", processor=item.processor, characters=item.value)
        elif isinstance(frame, ErrorFrame):
            rec(
                "error",
                t,
                processor=_name(frame.processor),
                message=str(frame.error),
                fatal=bool(frame.fatal),
            )
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._trace.finish("cancelled" if isinstance(frame, CancelFrame) else "ended")


def tool_outcome(frame: FunctionCallResultFrame) -> str:
    """ok / rejected / failed.

    `rejected` is the tool saying no in a sentence the model reads out loud —
    the slot is taken, the number is not one the caller said. That is the
    receptionist working, not breaking, and it is counted apart from `failed`,
    which is the handler raising.
    """
    if frame.error:
        return "failed"
    if _domain_error(frame.result):
        return "rejected"
    return "ok"


def _domain_error(result) -> str | None:
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return None


def _name(processor) -> str | None:
    if processor is None:
        return None
    return getattr(processor, "name", None) or type(processor).__name__


# ------------------------------------------------------------- events → numbers


def percentile(values, p: float) -> float | None:
    """Nearest-rank percentile; p in [0, 100]. None on no data."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = max(0, min(len(v) - 1, math.ceil(p / 100 * len(v)) - 1))
    return round(v[k], 3)


def _dist(values) -> dict:
    v = [x for x in values if x is not None]
    return {
        "n": len(v),
        "p50": percentile(v, 50),
        "p95": percentile(v, 95),
        "max": round(max(v), 3) if v else None,
    }


@dataclass
class Turn:
    """One exchange, rebuilt from its events."""

    index: int
    user_stopped: list[float] = field(default_factory=list)
    turn_closed: float | None = None
    #: Gaps from the last silence to the transcript / to the turn closing. A
    #: caller who pauses and goes on produces two silences and two transcripts;
    #: what the STT took is the gap inside each pair, not the span of the whole
    #: pause, so each is measured against the silence just before it.
    stt_gap: float | None = None
    timer_gap: float | None = None
    transcript_chars: int = 0
    llm_started: list[float] = field(default_factory=list)
    llm_first_token: float | None = None
    llm_done: list[float] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    tts_started: float | None = None
    tts_first_audio: float | None = None
    bot_started: float | None = None
    bot_stopped: float | None = None
    interrupted: bool = False


def _turns(events) -> list[Turn]:
    turns: dict[int, Turn] = {}
    for e in events:
        n = e.get("turn")
        if n is None:
            continue
        turn = turns.setdefault(n, Turn(n))
        kind, t = e["event"], e["t"]
        last_stop = turn.user_stopped[-1] if turn.user_stopped else None
        if kind == "user_stopped":
            turn.user_stopped.append(t)
        elif kind == "turn_closed":
            turn.turn_closed = t
            turn.timer_gap = _sub(t, last_stop)
        elif kind == "transcript":
            turn.stt_gap = _sub(t, last_stop)
            turn.transcript_chars += e.get("chars", 0)
        elif kind == "llm_started":
            turn.llm_started.append(t)
        elif kind == "llm_first_token":
            if turn.llm_first_token is None:
                turn.llm_first_token = t
        elif kind == "llm_done":
            turn.llm_done.append(t)
        elif kind == "tool_done":
            turn.tools.append(e)
        elif kind == "tts_started":
            if turn.tts_started is None:
                turn.tts_started = t
        elif kind == "tts_first_audio":
            if turn.tts_first_audio is None:
                turn.tts_first_audio = t
        elif kind == "bot_started":
            if turn.bot_started is None:
                turn.bot_started = t
        elif kind == "bot_stopped":
            turn.bot_stopped = t
        elif kind == "interrupted":
            turn.interrupted = True
    return [turns[k] for k in sorted(turns)]


def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(a - b, 3)


def turn_metrics(turn: Turn) -> dict:
    """The stages of one turn, in seconds.

    `latency_s` is the number the caller feels: from the last moment they fell
    silent to the first sound of the reply. The others explain it. They do not
    add up to it — the STT runs under the end-of-turn timer, and the TTS
    starts before the model has finished — which is the point of having them
    separately: a stage that overlaps costs nothing, and one that does not is
    the one to attack.
    """
    anchor = turn.user_stopped[-1] if turn.user_stopped else turn.turn_closed
    llm_s = None
    if turn.llm_started and turn.llm_done:
        llm_s = round(sum(_sub(d, s) or 0 for s, d in zip(turn.llm_started, turn.llm_done)), 3)
    tools_s = None
    if turn.tools:
        tools_s = round(sum(x.get("dur_s") or 0 for x in turn.tools), 3)
    return {
        "turn": turn.index,
        "kind": "tool" if turn.tools else "plain",
        "latency_s": _sub(turn.bot_started, anchor),
        "turn_timer_s": turn.timer_gap,
        "stt_s": turn.stt_gap,
        "llm_ttft_s": _sub(turn.llm_first_token, turn.llm_started[0] if turn.llm_started else None),
        "llm_s": llm_s,
        "llm_runs": len(turn.llm_started),
        "tools_s": tools_s,
        "tools": [x.get("name") for x in turn.tools],
        "tts_s": _sub(turn.tts_first_audio, turn.tts_started),
        "speaking_s": _sub(turn.bot_stopped, turn.bot_started),
        "interrupted": turn.interrupted,
        "answered": turn.bot_started is not None,
    }


def summarize(events: list[dict], ended: str | None = None, ended_at: float | None = None) -> dict:
    """Everything the report needs about one call, from its events alone.

    Pure: the same rows in, the same numbers out, whether they come from a
    live observer or a file read back a month later. Cost reuses the pricing
    model in call_metrics.py — the usage is in the events, the rates are in
    pricing.yaml, and nothing here knows a price.
    """
    head = next((e for e in events if e["event"] == "call_started"), {})
    last_t = max(max((e["t"] for e in events), default=0.0), ended_at or 0.0)
    turns = [turn_metrics(t) for t in _turns(events)]
    answered = [t for t in turns if t["answered"]]
    with_latency = [t for t in answered if t["latency_s"] is not None]

    tools = Counter()
    tool_outcomes: dict[str, Counter] = defaultdict(Counter)
    tool_durations: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e["event"] == "tool_done":
            tools[e["name"]] += 1
            tool_outcomes[e["name"]][e.get("outcome", "ok")] += 1
            if e.get("dur_s") is not None:
                tool_durations[e["name"]].append(e["dur_s"])
    errors = [e for e in events if e["event"] == "error"]
    ttfb: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e["event"] == "ttfb":
            ttfb[e["processor"]].append(e["value"])

    totals = CallTotals()
    # CallTotals measures its own duration from a monotonic start; hand it the
    # call's real length so `estimate_cost` prices the minutes we observed.
    totals.started_at = time.monotonic() - last_t
    for e in events:
        if e["event"] == "usage":
            if e.get("kind") == "llm":
                totals.prompt_tokens += e.get("prompt_tokens", 0)
                totals.completion_tokens += e.get("completion_tokens", 0)
            elif e.get("kind") == "tts":
                totals.tts_characters += e.get("characters", 0)

    summary = {
        "call_id": head.get("call_id"),
        "business": head.get("business"),
        "transport": head.get("transport"),
        "started_at": head.get("ts"),
        "ended": ended,
        "duration_s": round(last_t, 1),
        "turns": len(answered),
        "turns_with_tool": sum(1 for t in answered if t["kind"] == "tool"),
        "interruptions": sum(1 for t in turns if t["interrupted"]),
        "latency": _dist(t["latency_s"] for t in with_latency),
        "latency_plain": _dist(t["latency_s"] for t in with_latency if t["kind"] == "plain"),
        "latency_tool": _dist(t["latency_s"] for t in with_latency if t["kind"] == "tool"),
        "stages": {key: _dist(t[key] for t in turns) for key, _ in STAGES if key != "latency_s"},
        # As the services measure themselves, request to first byte. The stage
        # numbers above are seen from outside; these are the providers' own.
        "ttfb": {name: _dist(v) for name, v in sorted(ttfb.items())},
        "tool_calls": sum(tools.values()),
        "tools": {
            name: {
                "calls": n,
                **{k: tool_outcomes[name].get(k, 0) for k in ("ok", "rejected", "failed")},
                "p50_s": percentile(tool_durations[name], 50),
            }
            for name, n in tools.most_common()
        },
        "errors": len(errors),
        "error_messages": [
            f"{e.get('processor') or '?'}: {e.get('message')}" for e in errors[:5]
        ],
        "usage": {
            "prompt_tokens": totals.prompt_tokens,
            "completion_tokens": totals.completion_tokens,
            "tts_characters": totals.tts_characters,
        },
        "per_turn": turns,
    }
    pricing = load_pricing()
    if pricing and last_t > 0:
        summary["cost"] = estimate_cost(totals, pricing)
        # yaml reads `checked: 2026-08-10` as a date; the rows are JSON
        summary["cost"]["priced_on"] = str(summary["cost"]["priced_on"])
    return summary


def format_summary_line(s: dict, path: Path | None = None) -> str:
    """One line for the main log, at the end of a call."""
    lat = s["latency"]
    st = s["stages"]

    def p50(key):
        v = st.get(key, {}).get("p50")
        return "–" if v is None else f"{v:.2f}"

    def sec(v):
        return "–" if v is None else f"{v:.2f}"

    cost = s.get("cost")
    money = (
        f"${cost['total']:.3f} (${cost['per_minute']:.3f}/min)" if cost else "no pricing.yaml"
    )
    return (
        f"[trace {s['call_id']}] {s['business']} · {s['duration_s']}s · {s['turns']} turns"
        f" · latency p50 {sec(lat['p50'])}s p95 {sec(lat['p95'])}s"
        f" (stt {p50('stt_s')} · llm {p50('llm_s')} · tts {p50('tts_s')})"
        f" · {s['tool_calls']} tool calls · {s['errors']} errors · {money}"
        + (f" · {path}" if path else "")
    )


# ---------------------------------------------------------------- reading back


def read_events(path: Path) -> list[dict]:
    """The rows of one call file. A torn last line (the process died while
    writing it) is dropped rather than fatal — the call still counts."""
    rows = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_call(path: Path) -> dict:
    """A stored call's summary, recomputed from its events.

    Recomputed rather than read from the last line, so a call that never got a
    `call_ended` row (the process was killed) is reported as what it was: a cut
    call with the turns it did have.
    """
    events = read_events(path)
    ended = next((e.get("reason") for e in events if e["event"] == "call_ended"), None)
    summary = summarize(events, ended=ended or "cut")
    summary["path"] = str(path)
    return summary
