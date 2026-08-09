"""agent_tools.py — інструменти агента, зібрані з конфігу закладу.

Одне джерело правди для двох споживачів: живого бота (bot.py) і текстового
eval'а (eval.py). Раніше схеми жили в bot.py, а їх виконання дублювалось в
eval.py окремим маппінгом — і при кожній зміні аргументів вони розповзались.

Набір інструментів залежить від закладу:
    booking.enabled=false → лише take_message (чистий довідковий ресепшн);
    granularity=nights    → check_availability(дата заїзду, дата виїзду, ...);
    granularity=slots     → check_availability(дата, час, послуга).
"""

from business import BUSINESS
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

import crm

BIZ = BUSINESS
BOOK = BUSINESS.booking

_OPTIONAL_INTAKE = {f.key for f in BOOK.intake if not f.required}

PHONE_DESC = (
    "Номер телефону ВИКЛЮЧНО цифрами, напр. 0671234567. "
    "Ніколи не передавай словами («нуль шість сім») — тільки цифри."
)


def _resource_prop() -> dict:
    """Перелік варіантів людськими назвами — модель мапить на них усну мову
    точніше, ніж на технічні ключі; ключ підставить crm.normalize_resource."""
    return {
        "type": "string",
        "enum": [r.label for r in BOOK.resources],
        "description": BOOK.resource_label.capitalize(),
    }


def _when_props() -> dict:
    """Аргументи часу — різні для двох моделей запису."""
    if BOOK.is_slots:
        return {
            "date": {"type": "string", "description": "Дата візиту у форматі РРРР-ММ-ДД"},
            "time": {"type": "string", "description": "Час початку у форматі ГГ:ХХ (24 години)"},
        }
    return {
        "start_date": {
            "type": "string",
            "description": f"{BOOK.start_label.capitalize()} у форматі РРРР-ММ-ДД",
        },
        "end_date": {
            "type": "string",
            "description": f"{BOOK.end_label.capitalize()} у форматі РРРР-ММ-ДД",
        },
    }


def _party_props() -> dict:
    if not BOOK.ask_party:
        return {}
    return {
        BOOK.party_param: {"type": "integer", "description": BOOK.party_label.capitalize()}
    }


def _booking_props() -> dict:
    return {**_when_props(), BOOK.resource_param: _resource_prop(), **_party_props()}


def _intake_props() -> dict:
    """Поля галузі (адреса, тип роботи) — окремими аргументами, щоб модель не
    ховала їх у вільному тексті, звідки їх потім не дістати."""
    return {
        f.key: {
            "type": "string",
            "description": f.description or f.label.capitalize(),
        }
        for f in BOOK.intake
    }


CHECK_AVAILABILITY_SCHEMA = FunctionSchema(
    name="check_availability",
    description=(
        f"Перевірити, чи вільно, і дізнатися вартість. Виклич перед тим, як називати "
        f"{BIZ.customer_dat} ціну або підтверджувати {BOOK.action}. "
        + ("Якщо названий час зайнятий — повертає вільні альтернативи." if BOOK.is_slots else "")
    ),
    properties=_booking_props(),
    required=list(_booking_props()),
)

_CREATE_PROPS = {
    **_booking_props(),
    **_intake_props(),
    "customer_name": {"type": "string", "description": f"Ім'я, як представився {BIZ.customer_nom}"},
    "phone": {"type": "string", "description": PHONE_DESC},
}

CREATE_BOOKING_SCHEMA = FunctionSchema(
    name="create_booking",
    description=(
        f"Створити {BOOK.action} в системі закладу. Виклич ТІЛЬКИ після того, як ти вголос "
        f"повторила всі деталі і {BIZ.customer_nom} явно підтвердив («так»)."
    ),
    properties=_CREATE_PROPS,
    required=[k for k in _CREATE_PROPS if k not in _OPTIONAL_INTAKE],
)

TAKE_MESSAGE_SCHEMA = FunctionSchema(
    name="take_message",
    description=(
        "Записати запит на зворотний дзвінок від людини. Виклич, коли питання поза "
        f"межами {BOOK.action} і ти пообіцяла, що передзвонять. "
        f"Спершу дізнайся ім'я та телефон."
    ),
    properties={
        "customer_name": {"type": "string", "description": f"Ім'я, як представився {BIZ.customer_nom}"},
        "phone": {"type": "string", "description": PHONE_DESC},
        "topic": {"type": "string", "description": "Суть питання, одним-двома реченнями"},
    },
    required=["customer_name", "phone", "topic"],
)


def tools_schema() -> ToolsSchema:
    standard = [TAKE_MESSAGE_SCHEMA]
    if BOOK.enabled:
        standard = [CHECK_AVAILABILITY_SCHEMA, CREATE_BOOKING_SCHEMA, TAKE_MESSAGE_SCHEMA]
    return ToolsSchema(standard_tools=standard)


def _when(args: dict) -> tuple[str, str | None]:
    """Аргументи моделі → (start, end) у форматі, який розуміє crm."""
    if BOOK.is_slots:
        return f"{(args.get('date') or '').strip()}T{(args.get('time') or '').strip()}", None
    return args.get("start_date"), args.get("end_date")


def _party(args: dict) -> int:
    if not BOOK.ask_party:
        return 1
    try:
        return int(args.get(BOOK.party_param) or 1)
    except (TypeError, ValueError):
        return 1


def call_tool(name: str, args: dict) -> dict:
    """Виконати інструмент. Синхронно — і бот, і eval ходять сюди."""
    if name == "check_availability":
        start, end = _when(args)
        return crm.check_availability(
            resource=args.get(BOOK.resource_param),
            start=start,
            end=end,
            party_size=_party(args),
        )
    if name == "create_booking":
        start, end = _when(args)
        return crm.create_booking(
            resource=args.get(BOOK.resource_param),
            start=start,
            end=end,
            customer_name=args.get("customer_name"),
            phone=args.get("phone"),
            party_size=_party(args),
            details={f.key: args.get(f.key) for f in BOOK.intake},
        )
    if name == "take_message":
        return crm.take_message(args.get("customer_name"), args.get("phone"), args.get("topic"))
    return {"error": f"невідомий інструмент {name}"}


def register(llm, logger=None) -> None:
    """Прив'язати хендлери до LLM-сервісу pipecat."""

    def make(name: str):
        async def handler(params):
            args = dict(params.arguments)
            result = call_tool(name, args)
            if logger:
                logger.info(f"[tool] {name}({args}) -> {result}")
            await params.result_callback(result)

        return handler

    for schema in tools_schema().standard_tools:
        llm.register_function(schema.name, make(schema.name))
