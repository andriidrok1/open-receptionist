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
import guards

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
            # A bool field is typed as one: "will an adult be there" answered
            # as free text ("my kid is fifteen but responsible") cannot be
            # compared against a rule; true/false can.
            "type": "boolean" if f.type == "bool" else "string",
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


REF_CODE_PROP = {
    "type": "string",
    # The business's own prefix, so the example the model is shown matches the
    # codes it will actually be given.
    "description": L.text("tools.ref_code_arg", ref_prefix=BOOK.ref_prefix),
}

FIND_BOOKING_SCHEMA = FunctionSchema(
    name="find_booking",
    description=L.text("tools.find_booking"),
    properties={"phone": {"type": "string", "description": PHONE_DESC}},
    required=["phone"],
)

CANCEL_BOOKING_SCHEMA = FunctionSchema(
    name="cancel_booking",
    description=L.text("tools.cancel_booking"),
    properties={"ref_code": REF_CODE_PROP},
    required=["ref_code"],
)

RESCHEDULE_BOOKING_SCHEMA = FunctionSchema(
    name="reschedule_booking",
    description=L.text("tools.reschedule_booking"),
    properties={"ref_code": REF_CODE_PROP, **_when_props()},
    required=["ref_code", *_when_props()],
)


#: Ending the call is the model's decision because the model is the only thing
#: that knows the business is concluded. It takes no arguments: anything it
#: could carry — a reason, a summary — is already in the transcript, and an
#: argument is one more thing to get wrong on the last turn of a call.
END_CALL_SCHEMA = FunctionSchema(
    name="end_call",
    description=L.text("tools.end_call"),
    properties={},
    required=[],
)


def tools_schema() -> ToolsSchema:
    standard = [TAKE_MESSAGE_SCHEMA]
    if BOOK.enabled:
        standard = [
            CHECK_AVAILABILITY_SCHEMA,
            CREATE_BOOKING_SCHEMA,
            FIND_BOOKING_SCHEMA,
            CANCEL_BOOKING_SCHEMA,
            RESCHEDULE_BOOKING_SCHEMA,
            TAKE_MESSAGE_SCHEMA,
        ]
    return ToolsSchema(standard_tools=standard + [END_CALL_SCHEMA])


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
    # Ground truth for the per-turn state reminder: which tools have actually
    # been called, as opposed to which the model believes it has called.
    guards.note_tool(name)
    # A phone number the caller never said is refused before it reaches the
    # store. Found by the casebook: given a number the store rejected, the agent
    # invented three more and tried each one, rather than asking again. The
    # prompt forbids exactly this and did not hold, so it is no longer asked
    # for — a booking cannot be written against digits nobody uttered.
    if name in ("create_booking", "take_message") and not guards.was_spoken(args.get("phone")):
        return {"error": L.text("errors.phone.not_spoken")}
    try:
        result = _call_tool(name, args)
    except sqlite3.Error as e:
        _logger.error(f"[tool] {name} failed against the store: {e}")
        return {"error": L.text("errors.storage_unavailable")}
    # Only a code that was really issued may be spoken; see guards.fabricated_refs.
    if isinstance(result, dict) and result.get("ref_code"):
        guards.remember_ref(result["ref_code"])
    # The same call with the same arguments failing the same way, again: after
    # the third round the error stops saying "ask again" and says "stop". The
    # fragmented-speech tactic caught the agent re-submitting an unusable
    # number six times — the input was never going to change, and neither was
    # the answer, and the caller was stuck in the loop with it.
    if isinstance(result, dict) and result.get("error"):
        if guards.note_failure(name, args, str(result["error"])) >= 3:
            result = {**result, "error": L.text("errors.giving_up")}
    return result


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
    if name == "find_booking":
        return crm.find_bookings(args.get("phone"))
    if name == "cancel_booking":
        return crm.cancel_booking(args.get("ref_code"))
    if name == "reschedule_booking":
        start, end = _when(args)
        return crm.reschedule_booking(args.get("ref_code"), start, end)
    if name == "take_message":
        return crm.take_message(args.get("customer_name"), args.get("phone"), args.get("topic"))
    return {"error": L.text("errors.tool_unknown", name=name)}


def register(llm, logger=None, recorder=None, ender=None) -> None:
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
            if name == "end_call":
                # Not a store operation, so it does not go through call_tool.
                # The hangup is only REQUESTED here: the goodbye is still being
                # spoken while this runs, and ending now would cut it off.
                if ender:
                    ender.request("the agent said the conversation was over")
                result = {"success": True}
                if logger:
                    logger.info("[tool] end_call — hanging up after this sentence")
                crm.log_event(name, None)
                await params.result_callback(result)
                return
            result = call_tool(name, args)
            if logger:
                logger.info(f"[tool] {name}({args}) -> {result}")
            crm.log_event(name, result.get("ref_code") or result.get("error"))
            if recorder:
                # Into the transcript too: "checked availability" between two
                # lines is what explains a three-second pause to whoever reads
                # the call back.
                recorder.add_tool(name, result)
            await params.result_callback(result)

        return handler

    for schema in tools_schema().standard_tools:
        llm.register_function(schema.name, make(schema.name))
