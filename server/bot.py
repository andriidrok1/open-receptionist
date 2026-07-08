#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""hotel-voice-agent — Голосовий ІІ-ресепшн для готелю (українською).

Фаза 1: локальна голосова петля (cascade pipeline), увесь стек на OpenAI
    мікрофон → OpenAI Realtime STT (uk) → OpenAI LLM → OpenAI TTS (uk) → динамік

«Детекція» мовлення (коли гість почав/закінчив говорити, і перебивання) робить
серверний VAD усередині OpenAI Realtime STT (turn_detection=None).

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
from pipecat.frames.frames import (
    Frame,
    LLMRunFrame,
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
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.services.openai.tts import OpenAITTSService
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

Відповідай ОДНИМ коротким плавним висловлюванням, як у живій телефонній
розмові. НЕ розбивай думку на кілька окремих коротких речень поспіль —
уривчасті фрази звучать роботизовано. Наприклад, замість «Готово. Номер
броні сто двадцять три. Дякую.» скажи одним подихом: «Чудово, вашу бронь
підтверджено, номер броні сто двадцять три, дякуємо, що обрали нас». В одній
репліці — МАКСИМУМ ОДНЕ питання; ніколи не став два питання поспіль і не
перефразовуй одне й те саме питання двічі. Без пустих вступів («Дякую за
інформацію») — одразу суть. Будь теплою й доброзичливою, але по суті.

НІКОЛИ не оголошуй перевірку окремою реплікою і не чекай згоди гостя на неї.
Якщо в тебе вже є дати, тип номера і кількість гостей — ВИКЛИКАЙ інструмент
у цьому ж ході, без фраз «зараз перевірю, мить» з подальшим мовчанням. Гість
не мусить нічого відповідати, щоб перевірка відбулася.

Сьогодні {today}. Коли гість називає дату усно («чотирнадцятого липня», «на
наступних вихідних»), сам переведи її у формат РРРР-ММ-ДД. Якщо рік не названо —
бери найближчий майбутній.

Номери й ціни за ніч: Стандарт — 1200 грн, Покращений — 1800 грн, Делюкс —
2800 грн, Люкс — 4500 грн. Заїзд з 14:00, виїзд до 12:00. Сніданок включено,
паркінг безкоштовний.

БРОНЮВАННЯ — працюй тільки через інструменти, не вигадуй наявність і ціни сам:
1. Збери по черзі: дати заїзду і виїзду, тип номера, кількість гостей.
   Якщо гість уже чітко назвав щось із цього («з чотирнадцятого по сімнадцяте
   липня, на двох») — НЕ перепитуй це окремо, одразу переходь до перевірки.
   Питай лише те, чого справді бракує.
2. Виклич check_availability, щоб дізнатися наявність і повну вартість. Назви їх гостю.
3. Якщо вільно і гість згоден — попроси ім'я та номер телефону. Телефон
   ОБОВ'ЯЗКОВО повтори вголос цифра за цифрою («нуль, шість, сім…») і спитай,
   чи правильно записала — по телефону цифри часто чуються нечітко. Якщо гість
   виправляє — повтори виправлений номер ще раз.
4. Перед створенням броні ОБОВ'ЯЗКОВО повтори вголос УСІ деталі разом: тип
   номера, дати заїзду і виїзду, кількість гостей, ім'я та повну вартість
   («Отже, покращений номер з чотирнадцятого по сімнадцяте липня, двоє гостей,
   на ім'я Тарас, разом п'ять тисяч чотириста гривень — підтверджуєте?»).
   НЕ скорочуй це до одного питання про ціну — гість має почути всю бронь.
5. ТІЛЬКИ після усного «так» на повний переказ броні виклич create_booking.
   Потім назви гостю номер броні (ref_code) чітко, як звичайне число, і подякуй
   — усе одним плавним реченням.
Якщо номерів немає — вибачся і запропонуй інші дати або інший тип номера.

Якщо питання поза межами бронювання (спа, тварини, банкети, скарги тощо) —
чесно скажи, що уточниш у адміністратора і він передзвонить. Попроси ім'я та
телефон (телефон теж повтори по цифрах) і виклич take_message. НЕ вигадуй
відповідь і не кажи «записала», доки інструмент не підтвердив запис."""


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
        "Створити бронювання в системі готелю. Виклич ТІЛЬКИ після того, як ти вголос "
        "повторила гостю ВСІ деталі броні (тип номера, дати, кількість гостей, ім'я, "
        "телефон, повну вартість) і гість явно підтвердив («так»)."
    ),
    properties={
        "check_in_date": {"type": "string", "description": "Дата заїзду у форматі РРРР-ММ-ДД"},
        "check_out_date": {"type": "string", "description": "Дата виїзду у форматі РРРР-ММ-ДД"},
        "room_type": {"type": "string", "enum": _ROOM_TYPE_ENUM, "description": "Тип номера"},
        "guests": {"type": "integer", "description": "Кількість гостей"},
        "guest_name": {"type": "string", "description": "Ім'я гостя"},
        "phone": {
            "type": "string",
            "description": "Номер телефону ВИКЛЮЧНО цифрами, напр. 0671234567. "
            "Ніколи не передавай словами ('нуль шість сім') — тільки цифри.",
        },
    },
    required=["check_in_date", "check_out_date", "room_type", "guests", "guest_name", "phone"],
)


async def _handle_take_message(params: FunctionCallParams) -> None:
    a = params.arguments
    result = pms.take_message(
        guest_name=a.get("guest_name"),
        phone=a.get("phone"),
        topic=a.get("topic"),
    )
    logger.info(f"[tool] take_message({dict(a)}) -> {result}")
    await params.result_callback(result)


TAKE_MESSAGE_SCHEMA = FunctionSchema(
    name="take_message",
    description=(
        "Записати запит на зворотний дзвінок від адміністратора. Виклич, коли питання "
        "гостя поза межами бронювання і ти пообіцяла, що адміністратор передзвонить. "
        "Спершу дізнайся ім'я та телефон гостя."
    ),
    properties={
        "guest_name": {"type": "string", "description": "Ім'я гостя"},
        "phone": {
            "type": "string",
            "description": "Телефон для зворотного дзвінка ВИКЛЮЧНО цифрами, напр. 0671234567. "
            "Ніколи не передавай словами — тільки цифри.",
        },
        "topic": {"type": "string", "description": "Суть питання гостя, одним-двома реченнями"},
    },
    required=["guest_name", "phone", "topic"],
)


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
    logger.info("Starting hotel receptionist bot (uk)")
    pms.init_db()

    api_key = os.getenv("OPENAI_API_KEY")

    # --- Speech-to-Text: OpenAI Realtime STT, українська ---
    # Серверний VAD треба задавати ЯВНО: turn_detection=None ("server default")
    # для transcription-сесій означає VAD ВИМКНЕНО — фінальний транскрипт не
    # приходить ніколи, агент "не чує" гостя (перевірено проти API 2026-07-06).
    # gpt-realtime-whisper серверний VAD не підтримує взагалі, тому модель
    # gpt-4o-transcribe (вона ж точніша на українських числах/датах).
    # noise_reduction="far_field" — під телефонну/кімнатну акустику.
    stt = OpenAIRealtimeSTTService(
        api_key=api_key,
        turn_detection={
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            # 800мс: коротша пауза різала фразу гостя навпіл («Однак… 17 липня»)
            # і агент відповідав ДВІЧІ — на кожен шматок окремо.
            "silence_duration_ms": 800,
        },
        settings=OpenAIRealtimeSTTService.Settings(
            model=os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe"),
            language=Language.UK,
            # near_field = мікрофон близько (ноутбук/гарнітура — локальні тести).
            # Для телефонії (Фаза 4) перемкнути на far_field через env.
            noise_reduction=os.getenv("OPENAI_STT_NOISE", "near_field"),
        ),
    )

    # --- Text-to-Speech: ElevenLabs (Sarah, flash v2.5 — низька затримка) ---
    # Звучить помітно живіше за OpenAI TTS українською. speed 0.95 = трохи
    # повільніше за замовчування. Без ELEVENLABS_API_KEY — відкат на OpenAI.
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
    if elevenlabs_key:
        tts = ElevenLabsTTSService(
            api_key=elevenlabs_key,
            settings=ElevenLabsTTSService.Settings(
                voice=os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"),  # Sarah
                model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
                language=Language.UK,
                speed=float(os.getenv("ELEVENLABS_SPEED", "0.95")),
            ),
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
    # Прив'язати хендлери до імен інструментів.
    llm.register_function("check_availability", _handle_check_availability)
    llm.register_function("create_booking", _handle_create_booking)
    llm.register_function("take_message", _handle_take_message)

    tools = ToolsSchema(
        standard_tools=[CHECK_AVAILABILITY_SCHEMA, CREATE_BOOKING_SCHEMA, TAKE_MESSAGE_SCHEMA]
    )
    context = LLMContext(tools=tools)
    # Стратегії ходу задаємо ЯВНО. Дефолтний TurnAnalyzer (smart-turn модель)
    # у зв'язці з серверним VAD + gpt-4o-transcribe (без стрім-дельт) не бачив
    # межі мовлення і закривав хід по таймауту ~2с — саме звідси була велика
    # затримка відповіді. SpeechTimeout закриває хід за 0.7с після фінального
    # транскрипта.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[TranscriptionUserTurnStartStrategy()],
                stop=[ServerVADSpeechTimeoutStopStrategy(
                    user_speech_timeout=0.7, server_vad_stop_secs=0.8)],
            ),
        ),
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
