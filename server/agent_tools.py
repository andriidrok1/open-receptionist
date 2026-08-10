"""agent_tools.py — the agent's tools, assembled from the business config.

One source of truth for two consumers: the live bot (bot.py) and the text-level
evaluation (eval.py). The schemas used to live in bot.py while eval.py executed
them through a separate mapping of its own — and the two drifted apart every
time an argument changed.

Which tools exist depends on the business:
    booking.enabled=false → take_message only (a pure information desk);
    granularity=nights    → check_availability(start date, end date, ...);
    granularity=slots     → check_availability(date, time, service).

Note on language: every `description` here is read by the model, so the text
lives in the language pack (locales/<lang>.yaml), not in this file.
"""

from business import BUSINESS, L
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

import sqlite3

from loguru import logger as _logger

import crm

BIZ = BUSINESS
BOOK = BUSINESS.booking

_OPTIONAL_INTAKE = {f.key for f in BOOK.intake if not f.required}

PHONE_DESC = L.text("tools.phone_arg")


def _resource_prop() -> dict:
    """The options as human-readable labels.

    The model maps spoken language onto those far more accurately than onto
    technical keys; crm.normalize_resource turns the label back into a key.
    """
    return {
        "type": "string",
        "enum": [r.label for r in BOOK.resources],
        "description": BOOK.resource_label.capitalize(),
    }


def _when_props() -> dict:
    """Time arguments — different for the two booking models."""
    if BOOK.is_slots:
        return {
            "date": {"type": "string", "description": L.text("tools.date_arg")},
            "time": {"type": "string", "description": L.text("tools.time_arg")},
        }
    return {
        "start_date": {
            "type": "string",
            "description": L.text("tools.start_arg", label=BOOK.start_label.capitalize()),
        },
        "end_date": {
            "type": "string",
            "description": L.text("tools.end_arg", label=BOOK.end_label.capitalize()),
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
    """Industry-specific fields (address, what happened) as separate arguments.

    Separate, so the model cannot bury them in free text where they cannot be
    recovered afterwards.
    """
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
        L.text("tools.check_availability", customer_dat=BIZ.customer_dat, action=BOOK.action)
        + (L.text("tools.check_availability_slots_suffix") if BOOK.is_slots else "")
    ),
    properties=_booking_props(),
    required=list(_booking_props()),
)

_CREATE_PROPS = {
    **_booking_props(),
    **_intake_props(),
    "customer_name": {
        "type": "string",
        "description": L.text("tools.name_arg", customer_nom=BIZ.customer_nom),
    },
    "phone": {"type": "string", "description": PHONE_DESC},
}

CREATE_BOOKING_SCHEMA = FunctionSchema(
    name="create_booking",
    description=L.text("tools.create_booking", action=BOOK.action, customer_nom=BIZ.customer_nom),
    properties=_CREATE_PROPS,
    required=[k for k in _CREATE_PROPS if k not in _OPTIONAL_INTAKE],
)

TAKE_MESSAGE_SCHEMA = FunctionSchema(
    name="take_message",
    description=L.text("tools.take_message", action=BOOK.action),
    properties={
        "customer_name": {
            "type": "string",
            "description": L.text("tools.name_arg", customer_nom=BIZ.customer_nom),
        },
        "phone": {"type": "string", "description": PHONE_DESC},
        "topic": {"type": "string", "description": L.text("tools.topic_arg")},
    },
    required=["customer_name", "phone", "topic"],
)


def tools_schema() -> ToolsSchema:
    standard = [TAKE_MESSAGE_SCHEMA]
    if BOOK.enabled:
        standard = [CHECK_AVAILABILITY_SCHEMA, CREATE_BOOKING_SCHEMA, TAKE_MESSAGE_SCHEMA]
    return ToolsSchema(standard_tools=standard)


def _when(args: dict) -> tuple[str, str | None]:
    """The model's arguments → (start, end) in the form crm understands."""
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
    """Execute a tool. Synchronous — both the bot and the evaluation come here.

    Storage failures are turned into an ordinary tool error rather than allowed
    to escape. An exception here never reaches result_callback, so the model is
    left holding a tool call with no result and the caller hears silence — the
    event log already swallows its own errors for exactly this reason, and the
    booking path had no such guard.
    """
    try:
        return _call_tool(name, args)
    except sqlite3.Error as e:
        _logger.error(f"[tool] {name} failed against the store: {e}")
        return {"error": L.text("errors.storage_unavailable")}


def _call_tool(name: str, args: dict) -> dict:
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
    return {"error": L.text("errors.tool_unknown", name=name)}


def register(llm, logger=None) -> None:
    """Bind the handlers to a pipecat LLM service (the live bot's path).

    Every call is written to the event log, which the demo page turns into a
    feed of "what the agent just did". This cannot be done over RTVI: a server
    message sent from the tail of the pipeline never reaches the client, and
    inventing a way around that purely for the demo costs more than one row in a
    table which will later become the business's own call log.

    The evaluation harness does not come through here (it calls call_tool
    directly), so tests do not pollute the log.
    """

    def make(name: str):
        async def handler(params):
            args = dict(params.arguments)
            result = call_tool(name, args)
            if logger:
                logger.info(f"[tool] {name}({args}) -> {result}")
            crm.log_event(name, result.get("ref_code") or result.get("error"))
            await params.result_callback(result)

        return handler

    for schema in tools_schema().standard_tools:
        llm.register_function(schema.name, make(schema.name))
