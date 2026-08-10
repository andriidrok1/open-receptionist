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
import os

import aiohttp

import agent_tools
import crm
from business import BUSINESS, build_system_prompt, greeting_hint
from dotenv import load_dotenv
from loguru import logger
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
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import (
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService, CommitStrategy
from pipecat.services.elevenlabs.tts import (
    ElevenLabsHttpTTSService,
    ElevenLabsTTSService,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from speech_text import SpeechTextFilter, WholeReplyAggregator

load_dotenv(override=True)


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
    logger.info(f"Starting voice receptionist — {BUSINESS.key}: {BUSINESS.name}")
    crm.init_db()

    api_key = os.getenv("OPENAI_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

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
                language=Language.UK,
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
                language=Language.UK,
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
    # over the whole text rather than per sentence. The cost is that the bot
    # stays silent until the model has finished writing. Turn it off with
    # TTS_WHOLE_REPLY=0. Declared here because both the ElevenLabs branch and
    # the pipeline assembly below need it.
    whole_reply = os.getenv("TTS_WHOLE_REPLY", "1") == "1"

    if elevenlabs_key:
        voice_settings = dict(
            voice=os.getenv("ELEVENLABS_VOICE_ID", "bsourKGZEagmttzrIzmu"),
            model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
            language=Language.UK,
            speed=float(os.getenv("ELEVENLABS_SPEED", "0.95")),
            stability=float(os.getenv("ELEVENLABS_STABILITY", "0.4")),
            style=float(os.getenv("ELEVENLABS_STYLE", "1.0")),
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
                text_filters=[SpeechTextFilter()],
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
                text_filters=[] if whole_reply else [SpeechTextFilter()],
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
    # agent_tools assembles the tool set and its handlers from the business
    # config: an information-only receptionist gets take_message alone, a hotel
    # also gets bookings.
    agent_tools.register(llm, logger)
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
        turn_strategies = UserTurnStrategies(
            start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy(use_interim=False)],
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.1)],
        )
    else:
        # OpenAI's server VAD only sends UserStarted/StoppedSpeakingFrame (not
        # VAD*), so an adapter is needed to map those onto the VAD handlers.
        turn_strategies = UserTurnStrategies(
            start=[TranscriptionUserTurnStartStrategy()],
            stop=[ServerVADSpeechTimeoutStopStrategy(
                user_speech_timeout=0.7, server_vad_stop_secs=0.8)],
        )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=turn_strategies),
    )

    # Pipeline: audio in → [VAD] → STT → aggregator → LLM → TTS → audio out.
    # The VAD processor (for the ElevenLabs STT) must sit BEFORE the STT: its
    # VADUser* frames both commit the STT and feed the end-of-turn strategy
    # further downstream.
    pipeline = Pipeline(
        [
            transport.input(),
            *([vad_processor] if vad_processor is not None else []),
            MuteSTTWhileBotSpeaks(),
            stt,
            user_aggregator,
            llm,
            *([WholeReplyAggregator()] if whole_reply else []),
            tts,
            transport.output(),
            assistant_aggregator,
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

    worker = PipelineWorker(pipeline, params=pipeline_params, observers=[])

    async def greet_first() -> None:
        """The agent speaks first: a short greeting on behalf of the business."""
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
            # TODO(locale): this line belongs in the language pack.
            await worker.queue_frames([
                TTSSpeakFrame("Вибачте, демо-дзвінок обмежений у часі. Гарного дня!"),
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
