"""The call trace: moments in, numbers out.

Three layers, each tested on its own terms. `summarize` is pure — hand-written
events in, metrics out — so the arithmetic is checked on numbers chosen by
hand. The observer is fed frames with chosen timestamps, so the turn grouping
(a greeting, a plain turn, a tool turn, an interruption) is checked without a
pipeline. And then a real Pipecat pipeline runs, with a fake cascade that
emits the frames the real one does, and the JSONL on disk is read back —
because the thing that breaks in practice is not the arithmetic, it is a
frame that never reaches the hook.

No network, no keys, no audio hardware. `asyncio.run` rather than
pytest-asyncio, the way the rest of the suite does it.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
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
from pipecat.metrics.metrics import (  # noqa: E402
    LLMTokenUsage,
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import FramePushed  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402

import call_report  # noqa: E402
from observability import (  # noqa: E402
    CallTrace,
    CallTraceObserver,
    format_summary_line,
    load_call,
    percentile,
    summarize,
)

# ------------------------------------------------------------ hand-made events


def ev(event, t, **fields):
    return {"t": t, "ts": "2026-09-29T10:00:00.000+00:00", "call_id": "abc", "event": event,
            **fields}


def a_call() -> list[dict]:
    """Three turns: a greeting nobody waited for, a plain answer, a tool turn."""
    return [
        ev("call_started", 0.0, business="dental_us", transport="phone"),
        # greeting: no caller half
        ev("llm_started", 0.10, turn=1, run=1),
        ev("llm_first_token", 0.50, turn=1, run=1),
        ev("llm_done", 0.90, turn=1, run=1),
        ev("tts_started", 0.95, turn=1),
        ev("tts_first_audio", 1.10, turn=1),
        ev("bot_started", 1.20, turn=1),
        ev("bot_stopped", 3.00, turn=1),
        # plain turn: caller stops at 5.0, hears the reply at 7.0
        ev("user_stopped", 5.00, turn=2),
        ev("transcript", 5.60, turn=2, chars=30),
        ev("turn_closed", 6.10, turn=2),
        ev("llm_started", 6.10, turn=2, run=1),
        ev("llm_first_token", 6.50, turn=2, run=1),
        ev("llm_done", 6.80, turn=2, run=1),
        ev("tts_started", 6.80, turn=2),
        ev("tts_first_audio", 6.95, turn=2),
        ev("bot_started", 7.00, turn=2),
        ev("usage", 7.00, kind="llm", processor="llm", prompt_tokens=1000, completion_tokens=50),
        ev("usage", 7.00, kind="tts", processor="tts", characters=120),
        ev("bot_stopped", 9.00, turn=2),
        # tool turn: two LLM runs around a lookup, 4 s perceived
        ev("user_stopped", 10.00, turn=3),
        ev("transcript", 10.50, turn=3, chars=40),
        ev("turn_closed", 11.10, turn=3),
        ev("llm_started", 11.10, turn=3, run=1),
        ev("tool_started", 11.90, turn=3, name="check_availability", id="c1"),
        ev("tool_done", 11.92, turn=3, name="check_availability", id="c1", dur_s=0.02,
           outcome="rejected", error="slot taken"),
        ev("llm_done", 11.93, turn=3, run=1),
        ev("llm_started", 11.95, turn=3, run=2),
        ev("llm_first_token", 12.40, turn=3, run=2),
        ev("llm_done", 13.50, turn=3, run=2),
        ev("tts_started", 13.50, turn=3),
        ev("tts_first_audio", 13.80, turn=3),
        ev("bot_started", 14.00, turn=3),
        ev("error", 14.50, processor="ElevenLabsTTSService", message="socket closed", fatal=False),
        ev("bot_stopped", 16.00, turn=3),
        ev("call_ended", 16.50, reason="ended"),
    ]


def test_percentile_is_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile(list(range(1, 101)), 95) == 95


def test_per_turn_latency_is_from_the_last_silence_to_the_first_sound():
    s = summarize(a_call())
    plain, tool = s["per_turn"][1], s["per_turn"][2]
    assert plain["latency_s"] == 2.0 and plain["kind"] == "plain"
    assert tool["latency_s"] == 4.0 and tool["kind"] == "tool"
    # the greeting has no one waiting, so it has no latency — but it is a turn
    greeting = s["per_turn"][0]
    assert greeting["latency_s"] is None and greeting["answered"]
    assert s["turns"] == 3 and s["turns_with_tool"] == 1


def test_the_stages_of_a_turn():
    s = summarize(a_call())
    plain = s["per_turn"][1]
    assert plain["turn_timer_s"] == pytest.approx(1.1)
    assert plain["stt_s"] == pytest.approx(0.6)
    assert plain["llm_ttft_s"] == pytest.approx(0.4)
    assert plain["llm_s"] == pytest.approx(0.7)
    assert plain["tts_s"] == pytest.approx(0.15)
    assert plain["speaking_s"] == pytest.approx(2.0)
    tool = s["per_turn"][2]
    assert tool["llm_runs"] == 2
    assert tool["llm_s"] == pytest.approx(0.83 + 1.55)
    assert tool["tools_s"] == pytest.approx(0.02)
    assert tool["tools"] == ["check_availability"]


def test_call_level_distributions_and_buckets():
    s = summarize(a_call())
    assert s["latency"] == {"n": 2, "p50": 2.0, "p95": 4.0, "max": 4.0}
    assert s["latency_plain"]["p50"] == 2.0
    assert s["latency_tool"]["p50"] == 4.0
    assert s["stages"]["stt_s"]["n"] == 2
    assert s["duration_s"] == 16.5


def test_tools_and_errors_are_counted_with_their_outcome():
    s = summarize(a_call())
    assert s["tool_calls"] == 1
    assert s["tools"]["check_availability"] == {
        "calls": 1, "ok": 0, "rejected": 1, "failed": 0, "p50_s": 0.02,
    }
    assert s["errors"] == 1
    assert s["error_messages"] == ["ElevenLabsTTSService: socket closed"]


def test_cost_comes_from_the_existing_pricing_model():
    """The same yaml and the same arithmetic as call_metrics; no price here."""
    from call_metrics import load_pricing

    s = summarize(a_call())
    pricing = load_pricing()
    assert pricing, "pricing.yaml is part of the repo"
    rates = pricing["rates"]
    expected_llm = 1000 / 1e6 * rates["llm_input_per_mtok"] + 50 / 1e6 * rates["llm_output_per_mtok"]
    assert s["cost"]["llm"] == pytest.approx(expected_llm, abs=1e-4)
    assert s["cost"]["tts"] == pytest.approx(120 / 1000 * rates["tts_per_1k_chars"], abs=1e-4)
    assert s["cost"]["stt"] == pytest.approx(16.5 / 60 * rates["stt_per_minute"], abs=1e-4)
    assert s["cost"]["total"] > 0 and s["cost"]["per_minute"] > 0
    assert s["usage"] == {"prompt_tokens": 1000, "completion_tokens": 50, "tts_characters": 120}


def test_the_summary_line_reads_as_one_line():
    line = format_summary_line(summarize(a_call()), Path("logs/calls/abc.jsonl"))
    assert "\n" not in line
    assert "[trace abc] dental_us" in line
    assert "latency p50 2.00s p95 4.00s" in line
    assert "1 tool calls" in line and "1 errors" in line
    assert "$" in line and "logs/calls/abc.jsonl" in line


# ---------------------------------------------------------------- the observer


def pushed(frame: Frame, t: float) -> FramePushed:
    return FramePushed(
        source=None, destination=None, frame=frame,
        direction=FrameDirection.DOWNSTREAM, timestamp=int(t * 1e9),
    )


def feed(observer: CallTraceObserver, script: list[tuple[float, Frame]]) -> None:
    async def go():
        for t, frame in script:
            await observer.on_push_frame(pushed(frame, t))

    asyncio.run(go())


def transcript(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="", timestamp="", finalized=True)


def audio() -> TTSAudioRawFrame:
    return TTSAudioRawFrame(audio=b"\x00" * 320, sample_rate=8000, num_channels=1)


def test_frames_become_turns(tmp_path):
    trace = CallTrace(call_id="t1", business="clinic", logs_dir=tmp_path)
    obs = trace.observer()
    result = FunctionCallResultFrame(
        function_name="create_booking", tool_call_id="x", arguments={}, result={"ref_code": "A1"},
    )
    feed(obs, [
        # greeting
        (0.1, LLMFullResponseStartFrame()),
        (0.5, LLMTextFrame("Hello")),
        (0.6, LLMTextFrame(" there")),        # not a second first token
        (0.9, LLMFullResponseEndFrame()),
        (1.0, TTSStartedFrame()),
        (1.1, audio()),
        (1.2, audio()),                       # only the first is a moment
        (1.3, BotStartedSpeakingFrame()),
        (3.0, BotStoppedSpeakingFrame()),
        # caller: VAD stops, resumes, stops again — one turn, last silence counts
        (4.0, VADUserStoppedSpeakingFrame()),
        (4.4, transcript("I want")),
        (5.0, VADUserStoppedSpeakingFrame()),
        (5.5, transcript("a cleaning")),
        (6.1, UserStoppedSpeakingFrame()),
        (6.1, LLMFullResponseStartFrame()),
        (6.9, FunctionCallInProgressFrame(function_name="create_booking", tool_call_id="x",
                                          arguments={})),
        (6.92, result),
        (6.93, LLMFullResponseEndFrame()),
        (6.95, LLMFullResponseStartFrame()),
        (7.4, LLMTextFrame("Booked")),
        (7.9, LLMFullResponseEndFrame()),
        (7.9, TTSStartedFrame()),
        (8.1, audio()),
        (8.2, BotStartedSpeakingFrame()),
        # the caller cuts in
        (9.0, InterruptionFrame()),
        (9.0, BotStoppedSpeakingFrame()),
        (9.5, VADUserStoppedSpeakingFrame()),
    ])
    turns = {}
    for e in trace.events:
        if "turn" in e:
            turns.setdefault(e["turn"], []).append(e["event"])
    assert turns[1] == ["llm_started", "llm_first_token", "llm_done", "tts_started",
                        "tts_first_audio", "bot_started", "bot_stopped"]
    assert turns[2] == ["user_stopped", "transcript", "user_stopped", "transcript",
                        "turn_closed", "llm_started", "tool_started", "tool_done", "llm_done",
                        "llm_started", "llm_first_token", "llm_done", "tts_started",
                        "tts_first_audio", "bot_started", "interrupted", "bot_stopped"]
    assert turns[3] == ["user_stopped"]

    s = trace.summary()
    assert s["per_turn"][1]["latency_s"] == pytest.approx(8.2 - 5.0)
    assert s["per_turn"][1]["interrupted"] is True
    assert s["per_turn"][1]["llm_runs"] == 2
    tool = next(e for e in trace.events if e["event"] == "tool_done")
    assert tool["outcome"] == "ok" and tool["dur_s"] == pytest.approx(0.02)
    assert s["turns"] == 2  # the third turn was never answered


def test_a_broadcast_frame_is_one_moment(tmp_path):
    """Bot-started travels both ways as two frames with linked ids. Counted
    twice it would open two turns and put the latency on the wrong one."""
    trace = CallTrace(call_id="t2", business="clinic", logs_dir=tmp_path)
    obs = trace.observer()
    down, up = BotStartedSpeakingFrame(), BotStartedSpeakingFrame()
    down.broadcast_sibling_id, up.broadcast_sibling_id = up.id, down.id
    feed(obs, [(1.0, VADUserStoppedSpeakingFrame()), (2.0, down), (2.0, up)])
    assert [e["event"] for e in trace.events] == ["call_started", "user_stopped", "bot_started"]


def test_tool_outcomes_are_told_apart(tmp_path):
    trace = CallTrace(call_id="t3", business="clinic", logs_dir=tmp_path)
    obs = trace.observer()

    def result(**kw):
        return FunctionCallResultFrame(function_name="f", tool_call_id=kw.pop("id"),
                                       arguments={}, **kw)

    feed(obs, [
        (1.0, result(id="a", result={"success": True})),
        (1.1, result(id="b", result={"error": "slot taken"})),
        (1.2, result(id="c", result="boom", error="handler raised")),
    ])
    outcomes = [(e["outcome"], e["error"]) for e in trace.events if e["event"] == "tool_done"]
    assert outcomes == [("ok", None), ("rejected", "slot taken"), ("failed", "handler raised")]


def test_metrics_and_errors_are_recorded(tmp_path):
    trace = CallTrace(call_id="t4", business="clinic", logs_dir=tmp_path)
    obs = trace.observer()
    usage = LLMTokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    feed(obs, [
        (1.0, MetricsFrame(data=[
            TTFBMetricsData(processor="OpenAILLMService#0", value=0.4321),
            LLMUsageMetricsData(processor="OpenAILLMService#0", value=usage),
            TTSUsageMetricsData(processor="ElevenLabsTTSService#0", value=77),
        ])),
        (2.0, ErrorFrame(error="Scribe closed the socket")),
    ])
    kinds = [e["event"] for e in trace.events]
    assert kinds == ["call_started", "ttfb", "usage", "usage", "error"]
    s = trace.summary()
    assert s["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "tts_characters": 77}
    assert s["errors"] == 1 and "Scribe" in s["error_messages"][0]


def test_finish_is_idempotent_and_closes_the_file(tmp_path):
    trace = CallTrace(call_id="t5", business="clinic", logs_dir=tmp_path)
    first = trace.finish("disconnected")
    assert first is not None and trace.finish("ended") is None
    rows = [json.loads(x) for x in (tmp_path / "t5.jsonl").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["call_started", "call_ended"]
    assert rows[-1]["reason"] == "disconnected"
    # nothing is written after the end, and nothing raises
    trace.record("late")
    assert len((tmp_path / "t5.jsonl").read_text().splitlines()) == 2


def test_an_unwritable_log_dir_does_not_break_the_call(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    trace = CallTrace(call_id="t6", business="clinic", logs_dir=blocker / "calls")
    assert trace.path is None
    trace.record("user_stopped", turn=1)
    assert trace.finish() is not None


# ------------------------------------------------------------ a real pipeline


class FakeCascade(FrameProcessor):
    """STT → LLM → tool → TTS → transport, as frames, with no provider behind
    them. On every final transcript it emits what the real cascade emits, with
    small real delays, so the moments have distinct timestamps."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._n = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if not isinstance(frame, TranscriptionFrame):
            return
        self._n += 1
        with_tool = self._n == 2
        await asyncio.sleep(0.01)
        await self.push_frame(UserStoppedSpeakingFrame())
        await self.push_frame(LLMFullResponseStartFrame())
        await asyncio.sleep(0.02)
        if with_tool:
            await self.push_frame(FunctionCallInProgressFrame(
                function_name="check_availability", tool_call_id="tc", arguments={}))
            await asyncio.sleep(0.01)
            await self.push_frame(FunctionCallResultFrame(
                function_name="check_availability", tool_call_id="tc", arguments={},
                result={"available": True}))
            await self.push_frame(LLMFullResponseEndFrame())
            await self.push_frame(LLMFullResponseStartFrame())
            await asyncio.sleep(0.02)
        await self.push_frame(LLMTextFrame("Certainly."))
        await self.push_frame(MetricsFrame(data=[
            LLMUsageMetricsData(processor="llm", value=LLMTokenUsage(
                prompt_tokens=500, completion_tokens=20, total_tokens=520)),
        ]))
        await self.push_frame(LLMFullResponseEndFrame())
        await self.push_frame(TTSStartedFrame())
        await asyncio.sleep(0.01)
        await self.push_frame(audio())
        await self.push_frame(MetricsFrame(data=[TTSUsageMetricsData(processor="tts", value=10)]))
        await self.broadcast_frame(BotStartedSpeakingFrame)
        await asyncio.sleep(0.02)
        await self.broadcast_frame(BotStoppedSpeakingFrame)


def test_the_trace_survives_a_real_pipeline(tmp_path):
    """The hook is an observer on the worker: no processor was added to the
    pipeline, and the file on disk is what a call leaves behind."""
    trace = CallTrace(call_id="pipe1", business="emergency_us", transport="phone",
                      logs_dir=tmp_path)
    script = [
        VADUserStoppedSpeakingFrame(), transcript("my sink is leaking"), SleepFrame(0.15),
        VADUserStoppedSpeakingFrame(), transcript("tomorrow at nine"), SleepFrame(0.2),
    ]
    asyncio.run(run_test(
        FakeCascade(), frames_to_send=script, observers=[trace.observer()],
    ))
    assert trace.finished  # EndFrame reached the observer
    rows = [json.loads(x) for x in (tmp_path / "pipe1.jsonl").read_text().splitlines()]
    assert rows[0]["event"] == "call_started" and rows[-1]["event"] == "call_ended"
    kinds = [r["event"] for r in rows]
    for needed in ("user_stopped", "transcript", "turn_closed", "llm_started",
                   "llm_first_token", "llm_done", "tool_started", "tool_done",
                   "tts_started", "tts_first_audio", "bot_started", "bot_stopped", "usage"):
        assert needed in kinds, f"{needed} never reached the observer"
    assert kinds.count("bot_started") == 2, "a broadcast frame was counted twice"

    s = rows[-1]["summary"]
    assert s["turns"] == 2 and s["turns_with_tool"] == 1 and s["tool_calls"] == 1
    assert s["latency"]["n"] == 2
    plain, tool = s["per_turn"]
    assert 0.03 <= plain["latency_s"] < 1.0
    assert tool["latency_s"] > plain["latency_s"]  # two inference passes and a lookup
    assert tool["llm_runs"] == 2
    assert s["usage"]["prompt_tokens"] == 1000 and s["usage"]["tts_characters"] == 20
    assert s["cost"]["total"] > 0

    # and the file reads back to the same numbers
    again = load_call(tmp_path / "pipe1.jsonl")
    assert again["turns"] == 2 and again["ended"] == "ended"
    assert again["latency"]["p50"] == s["latency"]["p50"]


# ------------------------------------------------------------------ the report


def write_call(folder: Path, call_id: str, events: list[dict]) -> Path:
    p = folder / f"{call_id}.jsonl"
    with p.open("w") as f:
        for e in events:
            e = {**e, "call_id": call_id}
            f.write(json.dumps(e) + "\n")
    return p


def test_the_report_aggregates_a_folder(tmp_path):
    full = a_call()
    write_call(tmp_path, "one", full)
    # a call the process died on: no call_ended row, and a torn last line
    write_call(tmp_path, "two", full[:12])
    with (tmp_path / "two.jsonl").open("a") as f:
        f.write('{"t": 6.2, "event": "llm_fi')
    calls = call_report.load_dir(tmp_path)
    assert [c["ended"] for c in calls] == ["ended", "cut"]
    agg = call_report.aggregate(calls)
    assert agg["calls"] == 2 and agg["completed"] == 1 and agg["cut"] == 1
    assert agg["turns"] == 4  # 3 answered + 1 answered in the cut call
    assert agg["latency"]["n"] == 2 and agg["latency"]["p50"] == 2.0  # the greeting has none
    assert agg["tools"]["check_availability"]["rejected"] == 1
    assert agg["errors"] == 1
    assert agg["cost"]["priced_calls"] == 2 and agg["cost"]["total"] > 0
    assert abs(sum(agg["cost"]["share"].values()) - 1.0) < 0.01

    text = call_report.format_report(agg, calls)
    for needed in ("calls            2 (1 completed, 1 cut)", "all turns", "with a tool call",
                   "end-of-turn timer", "check_availability", "socket closed", "of which",
                   "  cut"):
        assert needed in text, text


def test_the_report_cli_prints_text_and_json(tmp_path):
    write_call(tmp_path, "one", a_call())
    out = io.StringIO()
    with redirect_stdout(out):
        assert call_report.main([str(tmp_path), "--calls"]) == 0
    assert "latency (caller stops → bot speaks)" in out.getvalue()
    assert "one           dental_us" in out.getvalue()

    out = io.StringIO()
    with redirect_stdout(out):
        assert call_report.main([str(tmp_path), "--json", "--business", "dental_us"]) == 0
    data = json.loads(out.getvalue())
    assert data["calls"] == 1 and data["latency"]["p95"] == 4.0

    out = io.StringIO()
    with redirect_stdout(out):
        assert call_report.main([str(tmp_path), "--business", "hotel"]) == 0
    assert out.getvalue().strip() == "no calls found"
    assert call_report.main([str(tmp_path / "missing")]) == 2


def test_bot_wires_the_trace_into_the_worker():
    """Read, not run: bot.py needs keys and a transport. The observer must be
    handed to the worker and finished on disconnect, or nothing is traced."""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
    assert "observers=[trace.observer()]" in src
    assert 'trace.finish("disconnected")' in src
