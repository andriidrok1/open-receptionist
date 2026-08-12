"""Ending a call — the policy, not the plumbing.

The pipeline parts (pushing EndWorkerFrame, receiving BotStoppedSpeakingFrame)
need a running pipeline and are not tested here. What is tested is the part that
decides: how many silences before hanging up, whether the strike counter clears,
and whether two things asking to hang up at once confuse it.

Those are the pieces that cut off a caller mid-sentence, and none of them needs
a microphone to get wrong.

Written with `asyncio.run` rather than pytest-asyncio on purpose: the suite runs
with no plugins, no network and no keys, and one decorator is not worth breaking
that.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hangup import CallEnder, IdleWatch  # noqa: E402


class FakeEnder:
    """A CallEnder without a pipeline under it: records, never pushes frames."""

    def __init__(self):
        self.reasons = []

    def request(self, reason):
        if not self.reasons:
            self.reasons.append(reason)

    @property
    def requested(self):
        return bool(self.reasons)


def watch():
    said = []

    async def speak(line):
        said.append(line)

    return IdleWatch(
        check_line="Are you still there?",
        goodbye_line="I'll let you go.",
        speak=speak,
        ender=FakeEnder(),
    ), said


def test_the_first_silence_only_asks():
    """People put the phone down to look for a diary. Hanging up on that is
    worse than waiting."""
    w, said = watch()
    asyncio.run(w.on_idle())
    assert said == ["Are you still there?"]
    assert not w._ender.requested


def test_the_second_silence_ends_the_call():
    w, said = watch()

    async def twice():
        await w.on_idle()
        await w.on_idle()

    asyncio.run(twice())
    assert said[-1] == "I'll let you go."
    assert w._ender.requested


def test_speaking_again_clears_the_strike():
    """Otherwise a quiet moment early in a call makes an innocent pause later
    the second strike, and the agent hangs up on someone mid-booking."""
    w, said = watch()

    async def quiet_talk_quiet():
        await w.on_idle()
        w.strikes = 0  # what UserStartedSpeakingFrame does in the pipeline
        await w.on_idle()

    asyncio.run(quiet_talk_quiet())
    assert said == ["Are you still there?", "Are you still there?"]
    assert not w._ender.requested


def test_nothing_is_said_over_a_goodbye_already_under_way():
    """The idle timer keeps running while the agent speaks its last sentence.
    Talking into that would cut the goodbye in half."""
    w, said = watch()
    w._ender.request("the agent said the conversation was over")
    asyncio.run(w.on_idle())
    assert said == []
    assert w.strikes == 0


def test_a_second_request_does_not_replace_the_first():
    """The model and the idle timer can both ask to hang up in the same second.
    The reason kept should be the one that actually decided it."""

    async def race():
        ender = CallEnder()
        ender.request("the agent said the conversation was over")
        first = ender._reason
        ender.request("caller went quiet")
        assert ender._reason == first
        if ender._fallback:
            ender._fallback.cancel()

    asyncio.run(race())


# ------------------------------- the state nudge ------------------------------


def _nudge_pass(line_value: str, prior: list | None = None):
    """Run one LLMContextFrame through StateNudge against a stub context."""
    import asyncio

    from hangup import StateNudge
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.frame_processor import FrameDirection

    class Ctx:
        def __init__(self):
            self._messages = list(prior or [])

        def add_message(self, m):
            self._messages.append(m)

    ctx = Ctx()
    nudge = StateNudge(ctx, lambda: line_value)

    async def swallow(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    nudge.push_frame = swallow
    asyncio.run(nudge.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM))
    return ctx, nudge


def test_the_nudge_lands_at_the_tail_of_the_context():
    ctx, _ = _nudge_pass("CALL STATE: check availability first.")
    assert ctx._messages[-1]["role"] == "developer"
    assert "CALL STATE" in ctx._messages[-1]["content"]


def test_a_stale_nudge_is_replaced_not_stacked():
    """One reminder, always current. Left to accumulate, the context fills
    with contradictory old states and the newest is no longer at the tail."""
    import asyncio

    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.frame_processor import FrameDirection

    ctx, nudge = _nudge_pass("CALL STATE: version one.")
    asyncio.run(nudge.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM))
    states = [m for m in ctx._messages if "CALL STATE" in str(m.get("content", ""))]
    assert len(states) == 1


def test_an_empty_line_removes_the_nudge_entirely():
    """Every obligation met — the model should see nothing at all, not an
    empty reminder."""
    import asyncio

    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.frame_processor import FrameDirection

    ctx, nudge = _nudge_pass("CALL STATE: version one.")
    nudge._line_fn = lambda: ""
    asyncio.run(nudge.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM))
    assert all("CALL STATE" not in str(m.get("content", "")) for m in ctx._messages)
