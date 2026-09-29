#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#
# This file started from a Pipecat example template and has since been
# substantially rewritten. The notice above is kept as BSD-2-Clause requires;
# the project as a whole is MIT (see LICENSE).
#

"""bot.py — the voice receptionist pipeline.

This file is transport and voice only: it knows nothing about hotels or dental
practices. Who is on the other end of the line is defined by a YAML file in
`businesses/` (see business.py), and what the agent can do comes from
agent_tools.py. Running it for a different business is `BUSINESS=clinic`.

Cascade pipeline:
    mic → [Silero VAD] → STT → OpenAI LLM → ElevenLabs TTS → speaker

The STT provider is switched with STT_PROVIDER:
    elevenlabs (default) — Scribe v2 Realtime; speech is segmented by the local
        Silero VAD, because Scribe does not detect speech itself.
    openai — gpt-4o-transcribe with server-side VAD (fallback).

Running it:
    python bot.py                 # the default business
    BUSINESS=clinic python bot.py # a dental practice
then open http://localhost:7860/client and hit Connect (allow the microphone).
"""

import asyncio
import json
import os

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMRunFrame,
    STTMuteFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import CommitStrategy, ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import (
    ElevenLabsHttpTTSService,
    ElevenLabsTTSService,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import (
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import agent_tools
import crm
import guards
import tts_settings
from business import BUSINESS, L, _local_phone_digits, build_system_prompt, greeting_hint
from call_metrics import CallMetrics, format_report
from hangup import CallEnder, ClaimGuard, GoodbyeWatch, IdleWatch, StateNudge, call_state_line
from observability import CallTrace
from recording import CallRecorder, TranscriptCollector
from speech_text import SpeechTextFilter, WholeReplyAggregator
from turn_taking import SemanticSpeechTimeoutStrategy

# Defaults only: an exported variable wins, so a one-off experiment
# (ELEVENLABS_TTS_MODEL=... python run_demo.py) actually takes effect.
# See the long note in business.py.
load_dotenv(override=False)


class ServerVADSpeechTimeoutStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """A SpeechTimeout strategy that understands SERVER-side VAD events.

    The base strategy only listens for VADUser* frames, which come from a local
    VAD. OpenAI's Realtime STT with server-side VAD sends UserStartedSpeaking /
    UserStoppedSpeaking instead — the base strategy never saw those and closed
    the turn on the transcript timer while the caller was still finishing their
    sentence ("Yes. Yes." was cut into two turns and the agent answered twice).
    Here the server events are mapped onto the VAD handlers, so the timer holds
    for as long as the caller is actually speaking.
    """

    def __init__(self, *, server_vad_stop_secs: float, **kwargs):
        super().__init__(**kwargs)
        self._server_vad_stop_secs = server_vad_stop_secs

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, UserStartedSpeakingFrame):
            await self._handle_vad_user_started_speaking(None)
            return ProcessFrameResult.CONTINUE
        if isinstance(frame, UserStoppedSpeakingFrame):
            await self._handle_vad_user_stopped_speaking(
                VADUserStoppedSpeakingFrame(stop_secs=self._server_vad_stop_secs)
            )
            return ProcessFrameResult.CONTINUE
        return await super().process_frame(frame)


class MuteSTTWhileBotSpeaks(FrameProcessor):
    """Stop feeding audio to the STT while the bot is speaking.

    With MANUAL commit, Scribe does not segment speech itself: everything that
    reaches the microphone between two commits is attributed to the caller in
    one chunk — and a commit only happens once the caller has stopped talking.
    So echo from the speakers (quiet, around -55 dBFS: the local Silero VAD
    cannot hear it, but Scribe transcribes it perfectly) settles in the buffer
    and arrives in the context AS THE CALLER'S OWN WORDS. Measured: the bot said
    a sentence about a booking, and that same sentence came back as the human's
    line, displacing what the human actually said.

    Same root cause, second symptom: Scribe emits interim transcripts for that
    echo too, and those open a user turn and interrupt the bot mid-sentence.

    So we mute the STT input while the bot speaks. Barge-in still works: the VAD
    sits UPSTREAM of this processor and will open the turn, and an interruption
    immediately produces BotStoppedSpeakingFrame, which unmutes. At most the
    first fraction of a second of the interruption is lost.

    Place before the STT: Bot*SpeakingFrame travels up to here from
    transport.output(), and we push STTMuteFrame down into the STT service.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            await self.push_frame(STTMuteFrame(mute=True), FrameDirection.DOWNSTREAM)
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)):
            # InterruptionFrame is insurance: if a path ever appears where the
            # bot goes quiet without BotStoppedSpeakingFrame, the mute must not
            # stay on forever — that would mean a deaf bot for the rest of the
            # call.
            await self.push_frame(STTMuteFrame(mute=False), FrameDirection.DOWNSTREAM)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Run the voice bot for this session."""
    aiohttp_session = aiohttp.ClientSession()
    logger.info(
        f"Starting voice receptionist — {BUSINESS.key}: {BUSINESS.name} "
        f"[{BUSINESS.language}/{BUSINESS.country}, {BUSINESS.tz}]"
    )
    crm.init_db()
    # One ledger per call. Without this the digits and reference codes of the
    # PREVIOUS caller survive into the next call served by the same process —
    # so a booking could be written against a number the current caller never
    # said, because the last one did.
    guards.reset()

    api_key = os.getenv("OPENAI_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

    # The language the microphone and the voice work in. It has to come from the
    # same place as the prompt: getting the text layer multilingual while the
    # speech layer stayed hard-coded meant an English business could not take a
    # call at all — the model would answer in English while recognition and the
    # voice were still set to Ukrainian.
    speech_language = Language(L.text("settings.speech_language"))

    # --- Speech-to-Text: provider switch via STT_PROVIDER ---
    # "elevenlabs" (default): Scribe v2 Realtime. It does NOT detect speech on
    #   its own — segmentation is done by the local Silero VAD (the canonical
    #   Pipecat arrangement), which produces the VADUser* frames used both to
    #   commit the STT and to drive the end-of-turn strategy.
    # "openai": gpt-4o-transcribe with server-side VAD (fallback).
    stt_provider = os.getenv("STT_PROVIDER", "elevenlabs" if elevenlabs_key else "openai")
    vad_processor = None

    if stt_provider == "elevenlabs":
        stt = ElevenLabsRealtimeSTTService(
            api_key=elevenlabs_key,
            commit_strategy=CommitStrategy.MANUAL,  # committed by the local Silero VAD
            settings=ElevenLabsRealtimeSTTService.Settings(
                model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime"),
                language=speech_language,
                # Keyterms improve accuracy on domain words (service names and
                # the like). They come from the business config, because every
                # industry has its own.
                keyterms=BUSINESS.stt_keyterms(),
            ),
            # Scribe supports filter_background_audio (you can see it in
            # session_started), but pipecat 1.5 builds the URL from a whitelist
            # and never passes that parameter. So phantom words on silence are
            # suppressed on the client instead: we render only final
            # transcripts, never interim guesses.
        )

        # Losing the connection to Scribe mid-conversation looks like "the bot
        # suddenly went deaf": audio flows, the VAD still shows "listening", and
        # no transcript ever arrives again. We can only fix what we can see, so
        # at minimum this shouts into the log instead of failing silently the
        # way pipecat does by default.
        @stt.event_handler("on_disconnected")
        async def on_stt_disconnected(service):
            logger.error("STT: connection to Scribe was closed")
            # Pipecat only recreates the receive task if it is None, and after
            # the loop exits it stays finished but not None. The socket would
            # then reconnect and audio would flow, but nothing would be reading
            # the replies — a bot that is deaf for the rest of the call.
            task = getattr(service, "_receive_task", None)
            if task is not None and task.done():
                service._receive_task = None

        @stt.event_handler("on_connection_error")
        async def on_stt_error(service, error):
            logger.error(f"STT: connection error from Scribe — {error}")

        # The local VAD does two jobs: it segments speech for the STT, and it
        # marks the boundaries of a turn. Those are two DIFFERENT roles that
        # used to be conflated into one number:
        #   stop_secs          — when a speech segment is considered finished
        #                        (this is what commits the STT);
        #   user_speech_timeout (below) — how much longer to wait in case the
        #                        person simply paused to think mid-sentence.
        # It used to be 0.8 + 0.7: commits came late, and pipecat complained
        # that the allowance for STT latency had collapsed to zero
        # (stop_secs >= the p99 of 0.41s). Now it is 0.4 + 1.1 — the same ~1.5s
        # total before the bot answers, but commits happen twice as often and
        # "let them finish" is held by the parameter meant for it.
        #
        # audio_idle_timeout defaults to 1s, which meant a one-second gap in a
        # mobile connection forcibly ended speech mid-sentence and the agent
        # answered half a question. 3s still catches a muted microphone but not
        # network jitter.
        vad_processor = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.4, min_volume=0.6)
            ),
            audio_idle_timeout=3.0,
        )
    else:
        # Server-side VAD has to be requested EXPLICITLY: turn_detection=None on
        # a transcription session means VAD IS OFF (pipecat's docstring says
        # otherwise and is wrong), and a final transcript never arrives.
        # gpt-realtime-whisper does not support server VAD at all, hence
        # gpt-4o-transcribe. near_field = microphone close by, e.g. a laptop.
        stt = OpenAIRealtimeSTTService(
            api_key=api_key,
            turn_detection={
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 800,
            },
            settings=OpenAIRealtimeSTTService.Settings(
                model=os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe"),
                language=speech_language,
                noise_reduction=os.getenv("OPENAI_STT_NOISE", "near_field"),
            ),
        )

    # --- Text-to-Speech: ElevenLabs flash v2.5, chosen for low latency ---
    # The voice is picked by ear from the library: native to the language, and
    # tagged conversational (the donor is talking, not reading a book aloud —
    # that is where the lively intonation comes from). stability 0.4 is below
    # the 0.5 default: intonation moves more but stays predictable. Without
    # ELEVENLABS_API_KEY the pipeline falls back to OpenAI.
    #
    # whole_reply synthesises the entire reply in one go, so intonation is built
    # over the whole text rather than per sentence.
    #
    # It was assumed to cost around 0.7s of latency. Measured over three calls
    # each way, it does not: 3.13s median wait with it against 3.43s without,
    # which is inside the noise. The waiting the caller feels is dominated by
    # the end-of-turn timer and by how long the model's reply is, and waiting
    # for the last token fits inside that. The intonation is effectively free.
    # Turn it off with TTS_WHOLE_REPLY=0 if you want to see for yourself.
    whole_reply = os.getenv("TTS_WHOLE_REPLY", "1") == "1"

    # Where a pause helps the caller: the words that make up spoken dates,
    # times and prices, from the business's own language pack. Passed into the
    # text layer rather than read there, so speech_text.py stays a module about
    # markup and knows nothing about locales.
    # Month and weekday names join the list here rather than being written out
    # again under `speech:` — they already exist in the pack, and a name spelled
    # two ways in two places is a name that will disagree with itself.
    key_words = dict(
        number_words=(
            list(L.raw("speech.number_words", ()))
            + list(L.raw("numbers.months", ()))
            + list(L.raw("calendar.weekdays", ()))
        ),
        glue_words=L.raw("speech.glue_words", ()),
    )

    if elevenlabs_key:
        voice_settings = dict(
            # Order matters: the business's own choice, then a global override
            # for experiments, then the language default. The other way round —
            # env first — meant one line in .env pinned a Ukrainian voice onto
            # every business, including the English one, which is precisely the
            # bug this whole layer exists to prevent.
            voice=(
                BUSINESS.voice_id
                or os.getenv("ELEVENLABS_VOICE_ID")
                or L.text("settings.default_voice_id")
            ),
            model=tts_settings.MODEL,
            language=speech_language,
            # From tts_settings, not from env directly: voice_lab.py auditions
            # voices with these same numbers, and when the two lists of defaults
            # drifted apart, voices were compared at style 0.0 and then shipped
            # at 1.0.
            speed=tts_settings.SPEED,
            stability=tts_settings.STABILITY,
            style=tts_settings.STYLE,
        )
        # ws is the streaming multi-context transport: every sentence of a reply
        # shares one context_id, so intonation carries across them. http sends a
        # separate request per chunk and breaks intonation; it is kept as a
        # fallback. (The cut-off speech that once made http the default turned
        # out to be a bug in a hand-rolled client, not in the TTS.)
        if os.getenv("ELEVENLABS_TTS_TRANSPORT", "ws") == "http":
            tts = ElevenLabsHttpTTSService(
                api_key=elevenlabs_key,
                aiohttp_session=aiohttp_session,
                settings=ElevenLabsHttpTTSService.Settings(**voice_settings),
                text_filters=[SpeechTextFilter(**key_words)],
            )
        else:
            tts = ElevenLabsTTSService(
                api_key=elevenlabs_key,
                settings=ElevenLabsTTSService.Settings(**voice_settings),
                # Without this, break tags reach the model as ordinary text.
                enable_ssml_parsing=True,
                # Pipecat waits 3 seconds for the first byte of audio by
                # default, and if ElevenLabs is slower it silently discards THE
                # WHOLE reply and stays paused until the next interruption. So
                # the bot does not merely swallow a sentence — it goes mute for
                # the rest of the turn. Ten seconds leaves room for a queue and
                # for the network.
                stop_frame_timeout_s=float(os.getenv("TTS_FIRST_BYTE_TIMEOUT", "10")),
                # With whole_reply the pause sanitiser runs inside
                # WholeReplyAggregator, and there must be NO filter here: a TTS
                # text filter changes the string only for synthesis, and the
                # divergence from the original chewed letters out of words in
                # the conversation history.
                text_filters=[] if whole_reply else [SpeechTextFilter(**key_words)],
            )
    else:
        tts = OpenAITTSService(
            api_key=api_key,
            settings=OpenAITTSService.Settings(
                voice=os.getenv("OPENAI_VOICE", "coral"),
                model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            ),
        )

    # --- LLM: OpenAI, a fast model for low latency ---
    # parallel_tool_calls=False: without it the model sometimes issues several
    # tool calls in one turn, each of which is a separate inference pass, and
    # the reply comes out in fragments (the number one named cause of
    # fragmentation in Pipecat's own guide).
    # temperature 0.6: fewer decorative sentences, a steadier and shorter style.
    llm = OpenAILLMService(
        api_key=api_key,
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            system_instruction=build_system_prompt(),
            temperature=0.6,
            extra={"parallel_tool_calls": False},
        ),
    )
    # What was said, kept for the owner — the question they ask first about a
    # machine answering their phone. Text only: audio recording is a different
    # act legally (several US states need both parties' consent) and turned out
    # to need more care with the buffer's lifecycle than it is worth today.
    #
    # Built here, before the tools, and not further down next to the pipeline:
    # register() hands it to the tool handlers so a lookup shows up in the
    # transcript, and a name used before it is bound is an UnboundLocalError
    # that kills the call at connect — which is exactly what it did.
    recorder = CallRecorder(
        business_key=BUSINESS.key,
        started_at=BUSINESS.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    # Everything the tools write during this call — bookings, messages, events —
    # carries the call's id, so the dashboard can open the exact conversation a
    # record came out of. A ContextVar under the hood: two concurrent calls in
    # one process each stamp their own records.
    crm.set_current_call(recorder.call_id)

    # agent_tools assembles the tool set and its handlers from the business
    # config: an information-only receptionist gets take_message alone, a hotel
    # also gets bookings.
    # Ending the call is a pipeline action, so the object that performs it is
    # built before the tools that ask for it.
    call_ender = CallEnder()
    agent_tools.register(llm, logger, recorder, ender=call_ender)
    context = LLMContext(tools=agent_tools.tools_schema())

    # Turn strategies are set EXPLICITLY. The default TurnAnalyzer is a
    # smart-turn model which, in this combination, never saw the end of speech
    # and closed every turn on a ~2s timeout.
    if vad_processor is not None:
        # ElevenLabs + local Silero VAD: the VAD emits VADUser* frames itself,
        # so the base strategy works directly (start on VAD, stop on a timer).
        #
        # use_interim=False is mandatory: an interim transcript opened the
        # caller's turn and interrupted the bot mid-sentence, and no final
        # transcript followed it (with MANUAL commit one only arrives after a
        # VAD stop). The turn then hung until the 5s watchdog and closed empty
        # — so the bot cut itself off and went silent for five seconds. The
        # strategy itself is kept as a safety net for other STT providers.
        # How long to wait after silence before answering. A fixed timer gives
        # "I'd like to book a…" and "I'd like to book a cleaning." the same
        # wait, which is wrong in both directions. The semantic strategy re-times
        # once the transcript lands — the words themselves say whether that was
        # a pause or an ending. TURN_SEMANTIC=0 falls back to the plain timer.
        if os.getenv("TURN_SEMANTIC", "1") == "1":
            stop_strategy = SemanticSpeechTimeoutStrategy(
                user_speech_timeout=1.1,  # the ceiling, before the transcript arrives
                continuations=set(L.raw("turn_taking.continuations")),
                sentence_end=L.text("turn_taking.sentence_end"),
            )
        else:
            stop_strategy = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.1)
        turn_strategies = UserTurnStrategies(
            start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy(use_interim=False)],
            stop=[stop_strategy],
        )
    else:
        # OpenAI's server VAD only sends UserStarted/StoppedSpeakingFrame (not
        # VAD*), so an adapter is needed to map those onto the VAD handlers.
        turn_strategies = UserTurnStrategies(
            start=[TranscriptionUserTurnStartStrategy()],
            stop=[ServerVADSpeechTimeoutStopStrategy(
                user_speech_timeout=0.7, server_vad_stop_secs=0.8)],
        )
    # How long a silence is allowed to run before the agent asks whether anyone
    # is still there. Long enough that looking up a diary or a phone number does
    # not trigger it — the pause that annoys people is the one that arrives
    # while they are still thinking.
    idle_seconds = float(os.getenv("USER_IDLE_TIMEOUT", "12"))
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=turn_strategies,
            user_idle_timeout=idle_seconds,
        ),
    )

    async def say(line: str) -> None:
        """Speak a fixed line, bypassing the model.

        Straight to synthesis rather than through the LLM: these two lines must
        never be improvised, and a silence is the worst moment to wait another
        two seconds for a model to write one.
        """
        # Recorded by hand, because the transcript is built from the frames the
        # model produces and this line bypasses the model. Without it a call
        # ends with the agent apparently saying goodbye to nobody.
        recorder.add("agent", line)
        await user_aggregator.push_frame(TTSSpeakFrame(line), FrameDirection.DOWNSTREAM)

    idle_watch = IdleWatch(
        check_line=L.text("tools.idle_check"),
        goodbye_line=L.text("tools.idle_goodbye"),
        speak=say,
        ender=call_ender,
    )

    @user_aggregator.event_handler("on_user_turn_idle")
    async def _on_idle(_aggregator):
        await idle_watch.on_idle()

    def store_metrics(summary: dict) -> None:
        """One row per call: the numbers and the conversation together."""
        logger.info("\n" + format_report(summary))
        crm.log_event("call_metrics", json.dumps(summary, ensure_ascii=False, default=str))
        recorder.finish(crm.save_call, summary)

    call_metrics = CallMetrics(on_finish=store_metrics)

    async def claim_recovery() -> None:
        """Give the model the turn it needs to actually file the booking.

        Runs when ClaimGuard has just retracted a fabricated confirmation. The
        caller may already have said goodbye and will never speak again, so
        waiting for their next turn means the booking is silently lost. A
        developer note names the failure, and an immediate run lets the model
        call create_booking (or honestly ask for what is missing) right after
        the retraction line is spoken. `worker` is assigned below, before any
        call reaches the pipeline.
        """
        context.add_message(
            {"role": "developer", "content": L.text("prompt.claim_recovery")}
        )
        await worker.queue_frames([LLMRunFrame()])

    # Pipeline: audio in → [VAD] → STT → aggregator → LLM → TTS → audio out.
    # The VAD processor (for the ElevenLabs STT) must sit BEFORE the STT: its
    # VADUser* frames both commit the STT and feed the end-of-turn strategy
    # further downstream.
    pipeline = Pipeline(
        [
            transport.input(),
            *([vad_processor] if vad_processor is not None else []),
            MuteSTTWhileBotSpeaks(),
            # Near the input, where UserStartedSpeakingFrame passes: the strike
            # counter has to clear the moment the caller speaks again.
            idle_watch,
            stt,
            # Before the aggregator: it consumes TranscriptionFrame and does not
            # pass it on, so anything downstream never sees what the caller said.
            TranscriptCollector(recorder, side="caller"),
            user_aggregator,
            # Between the aggregator and the LLM: keeps one fresh CALL STATE
            # line at the tail of the context — "no availability checked yet",
            # "the fee has not been said" — computed from the guards ledger,
            # never from the model's beliefs. Born of the first live human
            # call: three minutes, fourteen replies, zero tool calls, no fee.
            StateNudge(
                context,
                lambda: call_state_line(BUSINESS.booking, L, BUSINESS.currency),
            ),
            llm,
            *([WholeReplyAggregator(**key_words)] if whole_reply else []),
            # Before the TTS, for the same reason on the other side: the TTS
            # consumes AggregatedTextFrame. This sits after WholeReplyAggregator
            # so it records one reply at a time rather than fragments.
            # Before the transcript collector on purpose: it can REWRITE the
            # reply, and the recording has to be of what the caller heard. A
            # transcript that disagrees with the audio is how a fabricated
            # confirmation survives a review.
            ClaimGuard(
                L.text("tools.claim_retracted"),
                # For snapping a mangled phone read-back to what was heard:
                # the first live recap dropped a digit from the number.
                digit_words=dict(L.raw("speech.digit_words", {})),
                phone_len=_local_phone_digits(BUSINESS.country),
                # The retraction promises "let me put it in properly now" — this
                # is what keeps that promise. Seen on the first phone-pipeline
                # call: the caller rushed "yes, thanks, bye" in one breath, the
                # model invented JOB-1234 instead of calling create_booking, the
                # guard retracted it — and with the caller done talking, no
                # turn would ever come to actually file the job.
                on_retract=claim_recovery,
            ),
            TranscriptCollector(recorder, side="agent"),
            # Reads the assembled reply and hangs up when it is a sign-off. Must
            # sit after the aggregator (so it sees a whole reply, not the first
            # fragment of one) and before the TTS (which consumes the frame).
            GoodbyeWatch(call_ender, L.raw("speech.goodbye_words", ())),
            tts,
            transport.output(),
            assistant_aggregator,
            # After transport.output(), because that is where
            # BotStoppedSpeakingFrame comes from — the signal that the goodbye
            # has actually been spoken and the line can close.
            call_ender,
            # Last on purpose. Every service pushes its metrics downstream, so
            # anywhere earlier collects the frames of whatever happens to be
            # above it — the first attempt sat before the STT and reported zeros
            # for all three services. The end of the line sees the VAD's
            # end-of-speech, the bot's start-of-speech and every metrics frame.
            call_metrics,
        ]
    )

    # Telephony (Twilio and friends) sends 8 kHz mono. If the pipeline is not
    # told about it, every frame is resampled back and forth and the voice takes
    # on a nasal quality.
    is_phone = isinstance(runner_args, WebSocketRunnerArguments)
    pipeline_params = PipelineParams(enable_metrics=True, enable_usage_metrics=True)
    if is_phone:
        pipeline_params.audio_in_sample_rate = 8000
        pipeline_params.audio_out_sample_rate = 8000
        logger.info("Phone mode: 8 kHz")

    # The per-call trace: every turn's moments (caller stops, transcript, first
    # token, tools, first audio) to logs/calls/<call_id>.jsonl, and one summary
    # line when the call ends. An observer rather than a processor, so the
    # pipeline above is untouched and every frame is seen where it is born.
    # Same call id as the transcript, so the two can be read side by side.
    trace = CallTrace(
        call_id=recorder.call_id,
        business=BUSINESS.key,
        transport="phone" if is_phone else "browser",
    )
    worker = PipelineWorker(pipeline, params=pipeline_params, observers=[trace.observer()])

    async def greet_first() -> None:
        """The agent speaks first: a short greeting on behalf of the business."""
        # Unless the owner has taken this shift themselves (dashboard →
        # settings). Checked per call, not per process: the owner flips the
        # schedule from a phone and expects the very next call to obey.
        if not crm.agent_on_duty():
            crm.log_event("off_duty", None)
            await say(L.text("tools.off_duty", brand=BUSINESS.brand))
            call_ender.request("off duty")
            return
        context.add_message({"role": "developer", "content": greeting_hint()})
        await worker.queue_frames([LLMRunFrame()])

    # In the browser we greet once the RTVI client is ready. In telephony there
    # is no RTVI client at all, and the only signal is the transport connecting
    # — which is the call itself.
    if not is_phone:

        @worker.rtvi.event_handler("on_client_ready")
        async def on_client_ready(rtvi):
            await greet_first()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        if is_phone:
            await greet_first()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        # Report before cancelling: when the browser tab simply goes away, the
        # worker is torn down without an EndFrame ever reaching the pipeline,
        # and the call's numbers were silently lost. Which is most calls —
        # people close the tab, they do not press a button.
        call_metrics.report_now()
        trace.finish("disconnected")
        await worker.cancel()

    # The demo page is public and every minute of conversation costs money. The
    # limit lives here rather than only in the interface, because a browser tab
    # can simply be left open.
    max_call = int(os.getenv("MAX_CALL_SECONDS", "0"))
    hang_up_task = None
    if max_call > 0:

        async def hang_up_later() -> None:
            await asyncio.sleep(max_call)
            logger.info(f"Call limit of {max_call}s reached — ending the call")
            # EndFrame rather than cancel(): cancel kills the audio task without
            # draining the buffer, so the bot is cut off mid-word and it sounds
            # like a dropped connection. EndFrame lets it finish saying goodbye.
            await worker.queue_frames([
                TTSSpeakFrame(L.text("notes.call_time_limit")),
                EndFrame(),
            ])

        # Keep the reference: asyncio is allowed to garbage-collect a task
        # nobody holds, and then the limit would simply never fire.
        hang_up_task = asyncio.create_task(hang_up_later())

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        if hang_up_task:
            hang_up_task.cancel()
        await aiohttp_session.close()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    # Phone transports: Pipecat supplies the serialiser (and the audio format)
    # itself, having identified the provider from the first websocket message.
    phone_params = lambda: FastAPIWebsocketParams(  # noqa: E731
        audio_in_enabled=True,
        audio_out_enabled=True,
        add_wav_header=False,
    )
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        "twilio": phone_params,
        "telnyx": phone_params,
        "plivo": phone_params,
    }

    transport = await create_transport(runner_args, transport_params)

    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
