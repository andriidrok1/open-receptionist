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
from datetime import date

import pms
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
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
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)

# Назва готелю — міняй тут (потрапляє в системний промпт і привітання).
HOTEL_NAME = os.getenv("HOTEL_NAME", "готель «Дніпро»")

# Системний промпт: персона ресепшена + інструкції з роботи з інструментами.
# Фаза 2: агент реально перевіряє наявність і створює бронь через function calling.
def build_system_prompt() -> str:
    today = date.today().isoformat()
    return f"""Ти — Оксана, адміністратор на ресепшені ({HOTEL_NAME}).
Розмовляй природною українською мовою. Твої відповіді ОЗВУЧУЮТЬСЯ вголос, тому:
не використовуй емодзі, списки, дужки, зірочки чи будь-яке форматування — лише
звичайні речення, які легко вимовити. Ціни називай словами природно
(наприклад «тисяча вісімсот гривень за ніч»).

Тримайся коротко: 1–2 речення за раз, як у живій телефонній розмові. Став одне
питання за раз. Будь теплою й доброзичливою, але по суті.

Сьогодні {today}. Коли гість називає дату усно («чотирнадцятого липня», «на
наступних вихідних»), сам переведи її у формат РРРР-ММ-ДД. Якщо рік не названо —
бери найближчий майбутній.

Номери й ціни за ніч: Стандарт — 1200 грн, Покращений — 1800 грн, Делюкс —
2800 грн, Люкс — 4500 грн. Заїзд з 14:00, виїзд до 12:00. Сніданок включено,
паркінг безкоштовний.

БРОНЮВАННЯ — працюй тільки через інструменти, не вигадуй наявність і ціни сам:
1. Збери по черзі: дати заїзду і виїзду, тип номера, кількість гостей.
2. Виклич check_availability, щоб дізнатися наявність і повну вартість. Назви їх гостю.
3. Якщо вільно і гість згоден — попроси ім'я та номер телефону.
4. ОБОВ'ЯЗКОВО повтори всю бронь вголос одним реченням і попроси підтвердити
   («Отже, двомісний номер з чотирнадцятого по сімнадцяте липня, троє гостей,
   на ім'я Андрій — все вірно?»).
5. ТІЛЬКИ після усного «так» виклич create_booking. Потім назви гостю номер
   броні (ref_code) і подякуй.
Якщо номерів немає — вибачся і запропонуй інші дати або інший тип номера.

Якщо питання складне або поза межами бронювання — чесно скажи, що передаси
адміністратору, і запропонуй записати контакт для зворотного дзвінка."""


# --- Function-calling tools: агент реально ходить у PMS ---

_ROOM_TYPE_ENUM = ["Стандарт", "Покращений", "Делюкс", "Люкс"]


async def _handle_check_availability(params: FunctionCallParams) -> None:
    a = params.arguments
    result = pms.check_availability(
        check_in=a.get("check_in_date"),
        check_out=a.get("check_out_date"),
        room_type=a.get("room_type"),
        guests=int(a.get("guests") or 1),
    )
    logger.info(f"[tool] check_availability({dict(a)}) -> {result}")
    await params.result_callback(result)


async def _handle_create_booking(params: FunctionCallParams) -> None:
    a = params.arguments
    result = pms.create_booking(
        check_in=a.get("check_in_date"),
        check_out=a.get("check_out_date"),
        room_type=a.get("room_type"),
        guests=int(a.get("guests") or 1),
        guest_name=a.get("guest_name"),
        phone=a.get("phone"),
    )
    logger.info(f"[tool] create_booking({dict(a)}) -> {result}")
    await params.result_callback(result)


CHECK_AVAILABILITY_SCHEMA = FunctionSchema(
    name="check_availability",
    description=(
        "Перевірити наявність номера і повну вартість на конкретні дати. "
        "Виклич перед тим, як називати гостю ціну або підтверджувати бронь."
    ),
    properties={
        "check_in_date": {"type": "string", "description": "Дата заїзду у форматі РРРР-ММ-ДД"},
        "check_out_date": {"type": "string", "description": "Дата виїзду у форматі РРРР-ММ-ДД"},
        "room_type": {"type": "string", "enum": _ROOM_TYPE_ENUM, "description": "Тип номера"},
        "guests": {"type": "integer", "description": "Кількість гостей"},
    },
    required=["check_in_date", "check_out_date", "room_type", "guests"],
)

CREATE_BOOKING_SCHEMA = FunctionSchema(
    name="create_booking",
    description=(
        "Створити бронювання в системі готелю. Виклич ТІЛЬКИ після того, як гість "
        "усно підтвердив усі деталі броні."
    ),
    properties={
        "check_in_date": {"type": "string", "description": "Дата заїзду у форматі РРРР-ММ-ДД"},
        "check_out_date": {"type": "string", "description": "Дата виїзду у форматі РРРР-ММ-ДД"},
        "room_type": {"type": "string", "enum": _ROOM_TYPE_ENUM, "description": "Тип номера"},
        "guests": {"type": "integer", "description": "Кількість гостей"},
        "guest_name": {"type": "string", "description": "Ім'я гостя"},
        "phone": {"type": "string", "description": "Номер телефону для звʼязку"},
    },
    required=["check_in_date", "check_out_date", "room_type", "guests", "guest_name", "phone"],
)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Run the voice bot for this session."""
    logger.info("Starting hotel receptionist bot (uk)")
    pms.init_db()

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
            system_instruction=build_system_prompt(),
        ),
    )
    # Прив'язати хендлери до імен інструментів.
    llm.register_function("check_availability", _handle_check_availability)
    llm.register_function("create_booking", _handle_create_booking)

    tools = ToolsSchema(standard_tools=[CHECK_AVAILABILITY_SCHEMA, CREATE_BOOKING_SCHEMA])
    context = LLMContext(tools=tools)
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
