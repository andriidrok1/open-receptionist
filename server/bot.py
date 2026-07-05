#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""hotel-voice-agent — Голосовий ІІ-ресепшн для готелю (українською).

Фаза 1: локальна голосова петля (cascade pipeline)
    мікрофон → Deepgram STT (uk) → Claude → ElevenLabs TTS (uk) → динамік

«Детекція» мовлення (коли гість почав/закінчив говорити, і перебивання) робить
Silero VAD, вшитий у user_aggregator нижче.

Запуск:
    uv run bot.py         # або: python bot.py
потім відкрий http://localhost:7860/client і натисни Connect (дозволь мікрофон).
"""

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)

# Назва готелю — міняй тут (потрапляє в системний промпт і привітання).
HOTEL_NAME = os.getenv("HOTEL_NAME", "готель «Дніпро»")

# Системний промпт: персона ресепшена. Українською, під озвучку.
# Фаза 1 НЕ пише броні в базу — агент лише веде розмову і збирає деталі усно
# з обов'язковим підтвердженням. Реальний запис броні — Фаза 2 (function calling).
SYSTEM_PROMPT = f"""Ти — Оксана, адміністратор на ресепшені ({HOTEL_NAME}).
Розмовляй природною українською мовою. Твої відповіді ОЗВУЧУЮТЬСЯ вголос, тому:
не використовуй емодзі, списки, дужки, зірочки чи будь-яке форматування — лише
звичайні речення, які легко вимовити.

Тримайся коротко: 1–2 речення за раз, як у живій телефонній розмові. Став одне
питання за раз. Будь теплою й доброзичливою, але по суті.

Що ти вмієш: відповідати на питання про готель (час заїзду і виїзду, наявність
номерів, ціни, сніданок, паркінг) і приймати попереднє бронювання. Коли гість
хоче забронювати, збери по черзі: дати заїзду і виїзду, тип номера, кількість
гостей, ім'я та номер телефону. ОБОВ'ЯЗКОВО в кінці повтори бронювання вголос
одним реченням і попроси підтвердити («Отже, ... — все вірно?»), перш ніж
вважати його прийнятим.

Якщо не знаєш відповіді або питання складне — чесно скажи, що передаси
адміністратору, і запропонуй записати контакт для зворотного дзвінка.

Довідкова інформація (заглушка, поки немає інтеграції з системою готелю):
- Заїзд з 14:00, виїзд до 12:00.
- Є одномісні, двомісні та сімейні номери. Сніданок включено. Є безкоштовний паркінг.
- Точні ціни й наявність на конкретні дати ти уточнюєш у адміністратора."""


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Run the voice bot for this session."""
    logger.info("Starting hotel receptionist bot (uk)")

    # --- Speech-to-Text: Deepgram Nova-3, українська ---
    # numerals=True — правильно розпізнає числа/дати (критично для бронювань).
    # smart_format/punctuate — чистіший текст для LLM.
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            model="nova-3",
            language="uk",
            smart_format=True,
            punctuate=True,
            numerals=True,
            interim_results=True,
        ),
    )

    # --- Text-to-Speech: ElevenLabs Flash v2.5, українська ---
    # Flash v2.5 — низька затримка (<75 мс) + українська мова.
    # ELEVENLABS_VOICE_ID візьми з бібліотеки голосів ElevenLabs
    # (краще мультимовний або український голос).
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        settings=ElevenLabsTTSService.Settings(
            voice=os.getenv("ELEVENLABS_VOICE_ID"),
            model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            language=Language.UK,
        ),
    )

    # --- LLM: Claude (швидка модель для низької затримки) ---
    llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        settings=AnthropicLLMService.Settings(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            system_instruction=SYSTEM_PROMPT,
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        # Silero VAD = детекція мовлення: коли гість говорить/замовк, перебивання.
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    # Pipeline: аудіо-вхід → STT → агрегатор → LLM → TTS → аудіо-вихід
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[],
    )

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        # Перше слово — за агентом: коротке привітання українською.
        context.add_message(
            {
                "role": "developer",
                "content": (
                    f"Привітайся першою, дуже коротко, як адміністратор {HOTEL_NAME}, "
                    "і запитай, чим можеш допомогти."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }

    transport = await create_transport(runner_args, transport_params)

    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
