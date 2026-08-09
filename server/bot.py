#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""voice-receptionist — Голосовий ІІ-ресепшн українською для будь-якого закладу.

Цей файл — тільки транспорт і голос: ані готелю, ані клініки він не знає. Хто
на тому кінці дроту, задає YAML у `businesses/` (див. business.py), а що агент
уміє робити — agent_tools.py. Запустити для іншого закладу = BUSINESS=clinic.

Cascade pipeline:
    мікрофон → [Silero VAD] → STT (uk) → OpenAI LLM → ElevenLabs TTS → динамік

STT перемикається через STT_PROVIDER:
    elevenlabs (за замовч.) — Scribe v2 Realtime, найкраще розпізнає українську;
        сегментацію мовлення робить локальний Silero VAD.
    openai — gpt-4o-transcribe із серверним VAD (запасний).

Запуск:
    python bot.py                 # готель (за замовчуванням)
    BUSINESS=clinic python bot.py # стоматологія
потім відкрий http://localhost:7860/client і натисни Connect (дозволь мікрофон).
"""

import asyncio
import os

import agent_tools
import crm
from business import BUSINESS, build_system_prompt, greeting_hint
from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMRunFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
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
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from speech_text import UkrainianSpeechFilter

load_dotenv(override=True)


class ServerVADSpeechTimeoutStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """SpeechTimeout-стратегія, яка розуміє події СЕРВЕРНОГО VAD.

    Базова стратегія слухає лише VADUser*-фрейми (локальний VAD). OpenAI
    Realtime STT із серверним VAD шле UserStartedSpeakingFrame /
    UserStoppedSpeakingFrame — базова стратегія їх не бачила і закривала хід
    за таймером транскрипта, поки гість ще договорював («Так. Так.» різалось
    на два ходи → агент відповідав двічі). Тут серверні події мапляться на
    VAD-обробники, тож таймер тримається, доки гість реально говорить.
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


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Run the voice bot for this session."""
    logger.info(f"Starting voice receptionist (uk) — {BUSINESS.key}: {BUSINESS.name}")
    crm.init_db()

    api_key = os.getenv("OPENAI_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

    # --- Speech-to-Text: перемикач провайдера через STT_PROVIDER ---
    # "elevenlabs" (за замовч.): Scribe v2 Realtime — за незалежними бенчмарками
    #   найкраще розпізнає українську. Сам НЕ детектить мовлення — сегментацію
    #   робить локальний Silero VAD (канонічна схема Pipecat), який виробляє
    #   VADUser*-фрейми і для коміту STT, і для стратегії кінця ходу.
    # "openai": gpt-4o-transcribe із серверним VAD (запасний варіант).
    stt_provider = os.getenv("STT_PROVIDER", "elevenlabs" if elevenlabs_key else "openai")
    vad_processor = None

    if stt_provider == "elevenlabs":
        stt = ElevenLabsRealtimeSTTService(
            api_key=elevenlabs_key,
            commit_strategy=CommitStrategy.MANUAL,  # коміт за локальним Silero VAD
            settings=ElevenLabsRealtimeSTTService.Settings(
                model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime"),
                language=Language.UK,
                # keyterms підвищують точність доменних слів (назви послуг тощо)
                # — беруться з конфігу закладу, бо в кожній галузі вони свої.
                keyterms=BUSINESS.stt_keyterms(),
            ),
            # Scribe вміє filter_background_audio (видно в session_started), але
            # pipecat 1.5 збирає URL за білим списком і цей параметр не передає.
            # Тому фантоми на тиші глушаться на стороні клієнта: показуємо лише
            # фінальний транскрипт, проміжні здогадки — ніколи.
        )
        # Локальний VAD: він і сегментує мовлення для STT, і дає межі ходу.
        # stop_secs=0.8 — пауза, після якої вважаємо, що гість договорив.
        vad_processor = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.8, min_volume=0.6)
            )
        )
    else:
        # Серверний VAD треба задавати ЯВНО: turn_detection=None для
        # transcription-сесій = VAD ВИМКНЕНО (docstring Pipecat бреше), фінальний
        # транскрипт не приходить ніколи. gpt-realtime-whisper серверний VAD не
        # підтримує → gpt-4o-transcribe. near_field — мікрофон близько (ноут).
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

    # --- Text-to-Speech: ElevenLabs (Kateryna Pavlenko, flash v2.5 — низька затримка) ---
    # Голос обраний на слух із бібліотеки: нативний український + мітка
    # conversational (донор говорить, а не начитує книгу — звідси жива інтонація).
    # stability 0.4: нижче за дефолт 0.5 — інтонація гуляє живіше, але лишається
    # передбачуваною. style тримаємо на 0 — він додає затримки, а бюджет розмови
    # і так напружений. Без ELEVENLABS_API_KEY — відкат на OpenAI.
    if elevenlabs_key:
        tts = ElevenLabsTTSService(
            api_key=elevenlabs_key,
            settings=ElevenLabsTTSService.Settings(
                voice=os.getenv("ELEVENLABS_VOICE_ID", "bsourKGZEagmttzrIzmu"),  # Kateryna Pavlenko
                model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
                language=Language.UK,
                speed=float(os.getenv("ELEVENLABS_SPEED", "0.95")),
                stability=float(os.getenv("ELEVENLABS_STABILITY", "0.4")),
                style=float(os.getenv("ELEVENLABS_STYLE", "1.0")),
            ),
            # Без цього break-теги поїдуть у модель як звичайний текст.
            enable_ssml_parsing=True,
            # Наголоси + санітайзер пауз. Фільтри застосовуються вже після
            # склейки речень, тобто бачать цілу репліку, а не уривок стріму.
            text_filters=[UkrainianSpeechFilter()],
        )
    else:
        tts = OpenAITTSService(
            api_key=api_key,
            settings=OpenAITTSService.Settings(
                voice=os.getenv("OPENAI_VOICE", "coral"),
                model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            ),
        )

    # --- LLM: OpenAI (швидка модель для низької затримки) ---
    # parallel_tool_calls=False: без цього модель інколи робить кілька tool-call
    # за один хід — кожен = окремий прохід інференсу, і відповідь дробиться на
    # уривки (named причина №1 фрагментації у гайді автора Pipecat).
    # temperature 0.6: менше зайвих речень-прикрас, стабільніший, коротший стиль.
    llm = OpenAILLMService(
        api_key=api_key,
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            system_instruction=build_system_prompt(),
            temperature=0.6,
            extra={"parallel_tool_calls": False},
        ),
    )
    # Набір інструментів і їх хендлери збирає agent_tools із конфігу закладу:
    # довідковому ресепшену дістанеться лише take_message, готелю — ще й броні.
    agent_tools.register(llm, logger)
    context = LLMContext(tools=agent_tools.tools_schema())
    # Стратегії ходу задаємо ЯВНО (дефолтний TurnAnalyzer = smart-turn модель,
    # яка в нашій зв'язці не бачила межі мовлення і закривала хід таймаутом ~2с).
    # SpeechTimeout закриває хід за 0.7с після фінального транскрипта.
    if vad_processor is not None:
        # ElevenLabs + локальний Silero VAD: VAD сам виробляє VADUser*-фрейми,
        # тож базова стратегія працює напряму (start за VAD, stop за таймером).
        turn_strategies = UserTurnStrategies(
            start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.7)],
        )
    else:
        # OpenAI серверний VAD шле лише UserStarted/StoppedSpeakingFrame (не VAD*),
        # тому потрібен адаптер, що мапить їх на VAD-обробники.
        turn_strategies = UserTurnStrategies(
            start=[TranscriptionUserTurnStartStrategy()],
            stop=[ServerVADSpeechTimeoutStopStrategy(
                user_speech_timeout=0.7, server_vad_stop_secs=0.8)],
        )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=turn_strategies),
    )

    # Pipeline: аудіо-вхід → [VAD] → STT → агрегатор → LLM → TTS → аудіо-вихід
    # VAD-процесор (для ElevenLabs STT) має стояти ПЕРЕД STT: його VADUser*-фрейми
    # і комітять STT, і живлять стратегію кінця ходу нижче за течією.
    pipeline = Pipeline(
        [
            transport.input(),
            *([vad_processor] if vad_processor is not None else []),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    # Телефонія (Twilio та інші) шле 8 кГц mono — якщо не сказати про це
    # пайплайну, кожен кадр ресемплиться туди-сюди, а голос гугнявить.
    is_phone = isinstance(runner_args, WebSocketRunnerArguments)
    pipeline_params = PipelineParams(enable_metrics=True, enable_usage_metrics=True)
    if is_phone:
        pipeline_params.audio_in_sample_rate = 8000
        pipeline_params.audio_out_sample_rate = 8000
        logger.info("Телефонний режим: 8 кГц")

    worker = PipelineWorker(pipeline, params=pipeline_params, observers=[])

    async def greet_first() -> None:
        """Перше слово — за агентом: коротке привітання від імені закладу."""
        context.add_message({"role": "developer", "content": greeting_hint()})
        await worker.queue_frames([LLMRunFrame()])

    # У браузері вітаємось, коли RTVI-клієнт готовий. У телефонії RTVI-клієнта
    # немає взагалі — там сигнал один: підключився транспорт (пішов дзвінок).
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

    # Демо-сторінка публічна, а кожна хвилина розмови коштує грошей. Ліміт
    # тримаємо тут, а не тільки в інтерфейсі: вкладку можна просто не закрити.
    max_call = int(os.getenv("MAX_CALL_SECONDS", "0"))
    if max_call > 0:

        async def hang_up_later() -> None:
            await asyncio.sleep(max_call)
            logger.info(f"Ліміт розмови {max_call}с вичерпано — завершую")
            await worker.cancel()

        asyncio.create_task(hang_up_later())

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    # Телефонні транспорти: серіалізатор (і формат аудіо) Pipecat підставляє сам,
    # визначивши провайдера з першого повідомлення у вебсокеті.
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
