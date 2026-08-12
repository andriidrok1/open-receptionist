"""hangup.py — ending the call, from both sides of the silence.

Until now a call only ended when the caller closed the tab. On a browser demo
that is merely untidy; on a phone line it is a bill. Two ways out are needed,
and they fail in opposite directions:

**The conversation is finished.** The agent says "see you then" and then just
sits there, breathing, while the caller works out whether to hang up first. The
model is the only thing that knows the business is concluded, so it gets a tool.

**The caller went away.** Someone puts the phone down on the counter, or the
line drops without a hangup signal. Nothing in the pipeline notices: the bot
waits for a turn that will never come, and the meter runs. That one is a timer,
not a decision.

Neither may cut the agent off mid-sentence, which is the whole difficulty. A
tool result arrives while the goodbye is still being synthesised, so ending the
call at that moment truncates it — the caller hears "see you th—" and silence,
which reads as a crash rather than a goodbye. So a request to end is recorded
and acted on when the bot next stops speaking.
"""

from __future__ import annotations

import asyncio
import re

import guards

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    Frame,
    LLMContextFrame,
    TextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

#: If the bot never reports that it stopped speaking — synthesis failed, the
#: transport died — end anyway rather than stay on the line forever. Generous
#: enough to cover a long goodbye plus a slow network.
FALLBACK_SECONDS = 20.0


#: A goodbye counts only in the LAST sentence of a reply. "Bye for now, but
#: first — what's the address?" is not a sign-off, and hanging up on it would be
#: the one failure worse than staying on the line.
_SENTENCE_END = re.compile(r"[.!?…]+")


#: How close to the end a farewell has to be. "Take care" lands in the last two
#: words; "Bye for now, but first let me check the address" does not.
_TAIL_WORDS = 6


def says_goodbye(text: str, words) -> bool:
    """Whether this reply ends with the agent signing off.

    Two conditions, and both were added after a false positive rather than out
    of caution. Merely containing a farewell is not enough: "Bye for now, but
    first — what's the address?" contains one, and hanging up on it is the only
    failure worse than staying on the line.

      * A reply that ends in a question is never a sign-off, whatever it says
        earlier.
      * The farewell has to be among the last few words, not merely somewhere in
        the final sentence.
    """
    text = (text or "").strip()
    if not text or text.endswith("?"):
        return False
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    if not parts:
        return False
    tail = " ".join(parts[-1].lower().split()[-_TAIL_WORDS:])
    # Whole words only. A substring match hung up on "maybe", which contains
    # "bye" — and "an hour, maybe" is a thing a dispatcher says about traffic.
    return any(
        re.search(rf"\b{re.escape(w.lower())}\b", tail) for w in words
    )


class ClaimGuard(FrameProcessor):
    """Repairs the reply on its way to the synthesiser.

    Sits between the assembled reply and the TTS, which is the last moment
    anything can be done about what the caller will actually hear.

    A reference code the agent got wrong is REPLACED with the one the store
    issued, rather than blocked. The rest of that sentence — the time, the
    address, the fee — was right, and throwing the whole reply away over one
    wrong token would trade a small lie for a confusing silence.

    A reference code when no booking exists cannot be repaired that way. There
    is no true value to substitute, and inserting one would make the lie better
    dressed. The whole reply is replaced with a line that says so plainly.

    Place it BEFORE the agent-side transcript collector, so what gets recorded
    is what was said rather than what the model wanted to say — a transcript
    that disagrees with the audio is how these bugs stay invisible for weeks.
    """

    def __init__(
        self,
        retract_line: str,
        digit_words: dict[str, str] | None = None,
        phone_len: int | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._retract = retract_line
        self._digit_words = dict(digit_words or {})
        self._phone_len = phone_len

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and getattr(frame, "text", ""):
            fixed, changed, unfixable = guards.correct_refs(frame.text)
            if unfixable:
                logger.warning(
                    f"[claim] a booking was confirmed that no tool made: {changed} — "
                    "replacing the reply"
                )
                frame = TextFrame(self._retract)
            elif changed:
                logger.warning(f"[claim] invented reference {changed} → the real one")
                frame = TextFrame(fixed)
            # Digit-by-digit numbers are checked against what the caller said:
            # the first live human call ended with a recap that read the phone
            # back with a digit missing. Runs after the ref repair — the
            # retraction line carries no digits, so the order is safe either
            # way.
            if self._digit_words:
                fixed, runs = guards.correct_phone(
                    frame.text, self._digit_words, self._phone_len
                )
                if runs:
                    logger.warning(f"[claim] mangled phone read-back {runs} → as heard")
                    frame = TextFrame(fixed)
        await self.push_frame(frame, direction)


class GoodbyeWatch(FrameProcessor):
    """Hangs up when the agent signs off, whether or not it used the tool.

    The tool exists and the model reaches for it perhaps two calls in three —
    measured on the casebook: three of eleven calls ended in goodbyes on both
    sides with the line still open. That is the ordinary shape of a rule that
    lives only in a prompt.

    So the decision is taken from the text instead. The agent still has the
    tool, and this changes nothing when it uses it — CallEnder ignores a second
    request. What it removes is the case where the model simply forgot.

    Place it where it sees the agent's assembled reply, i.e. after the reply
    aggregator and before the synthesiser.
    """

    def __init__(self, ender: "CallEnder", words, **kwargs):
        super().__init__(**kwargs)
        self._ender = ender
        self._words = tuple(words or ())

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (
            self._words
            and isinstance(frame, TextFrame)
            and not self._ender.requested
            and says_goodbye(getattr(frame, "text", ""), self._words)
        ):
            self._ender.request("the agent said goodbye")
        await self.push_frame(frame, direction)


class CallEnder(FrameProcessor):
    """Hangs up once the agent has finished its last sentence.

    Place it where it can see BotStoppedSpeakingFrame coming back up from the
    transport output — next to the other tail-end processors.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reason: str | None = None
        self._fallback: asyncio.Task | None = None

    def request(self, reason: str) -> None:
        """Ask for the call to end after the current sentence.

        Safe to call from a tool handler, which is the point: the handler runs
        while the reply is still being spoken.
        """
        if self._reason:
            return
        self._reason = reason
        logger.info(f"[hangup] requested: {reason}")
        self._fallback = asyncio.create_task(self._end_anyway())

    @property
    def requested(self) -> bool:
        return self._reason is not None

    async def _end_anyway(self) -> None:
        try:
            await asyncio.sleep(FALLBACK_SECONDS)
        except asyncio.CancelledError:
            return
        logger.warning("[hangup] the bot never stopped speaking — ending anyway")
        await self._end()

    async def _end(self) -> None:
        if self._fallback:
            self._fallback.cancel()
            self._fallback = None
        await self.push_frame(
            EndWorkerFrame(reason=self._reason or "ended"), FrameDirection.UPSTREAM
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if self._reason and isinstance(frame, BotStoppedSpeakingFrame):
            await self._end()


def call_state_line(book, pack, currency: str) -> str:
    """What the model has NOT done yet this call, as one short reminder.

    Written from ground truth — the guards ledger of tools actually called and
    words actually spoken — never from the model's own account of itself.

    Why this exists: on the first live human call the model went three minutes
    and fourteen replies without calling a single tool. It offered "tonight at
    six" and "tomorrow at nine" out of thin air and never once said the visit
    fee. The system prompt demands both, from four thousand tokens away, past
    a conversation full of fragmented half-turns. The casebook never caught it
    because a model playing the caller speaks in clean full sentences.

    A reminder computed in code and injected next to the LAST message is the
    cheapest structural fix: it sits where attention actually lands, it names
    only what is still missing, and it disappears item by item as the facts
    change. It is still a prompt — but one that cannot rot, because it is
    regenerated from the ledger every turn.
    """
    parts = []
    if book.enabled and book.resources and not guards.tool_was_called("check_availability"):
        parts.append(pack.text("prompt.state.no_availability"))
    fee = next((r.price for r in book.resources if r.price), 0)
    if (
        book.announce_price
        and fee
        and not guards.agent_said(guards.amount_words(fee))
    ):
        parts.append(pack.text("prompt.state.fee_unsaid", currency=currency, fee=fee))
    return " ".join(parts)


class StateNudge(FrameProcessor):
    """Keeps one up-to-date state reminder at the tail of the LLM context.

    Sits between the user aggregator and the LLM, where the assembled context
    passes on its way to inference. Each time, the previous reminder (if any)
    is removed and a fresh one appended — so the model always sees the current
    state exactly once, next to the newest message, and sees nothing at all
    once every obligation is met.
    """

    def __init__(self, context, line_fn, **kwargs):
        super().__init__(**kwargs)
        self._context = context
        self._line_fn = line_fn
        self._last_msg: dict | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            messages = self._context._messages
            if self._last_msg is not None and self._last_msg in messages:
                messages.remove(self._last_msg)
                self._last_msg = None
            line = self._line_fn()
            if line:
                self._last_msg = {"role": "developer", "content": line}
                self._context.add_message(self._last_msg)
        await self.push_frame(frame, direction)


class IdleWatch(FrameProcessor):
    """What to do the first time the caller goes quiet, and the second.

    One strike, not straight to hanging up: people put the phone down to look
    for a diary, and a receptionist who hangs up on that is worse than one who
    waits. But a second silence, after being asked directly, is not thinking
    time — it is an empty line.

    Pipecat's aggregator already detects the silence and emits the event, so
    what is added here is the policy. It is a processor only because of the
    counter: without resetting on the caller speaking again, one quiet moment
    early in a call would make the next one — half a conversation later, and
    perfectly innocent — the second strike, and the agent would hang up on
    someone mid-booking.
    """

    def __init__(self, *, check_line: str, goodbye_line: str, speak, ender: CallEnder, **kwargs):
        super().__init__(**kwargs)
        self._check = check_line
        self._goodbye = goodbye_line
        self._speak = speak
        self._ender = ender
        self.strikes = 0

    async def on_idle(self) -> None:
        if self._ender.requested:
            return  # already on the way out; do not talk over the goodbye
        self.strikes += 1
        if self.strikes == 1:
            logger.info("[idle] caller has gone quiet — checking")
            await self._speak(self._check)
        else:
            logger.info("[idle] still quiet — ending the call")
            self._ender.request("caller went quiet")
            await self._speak(self._goodbye)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self.strikes = 0
        await self.push_frame(frame, direction)
