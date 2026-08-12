"""demo.py — the showcase: a page you can call any of the bots from.

This is a sales tool, not our test bench: you send the link to a business owner,
they open it on a phone, pick their industry and talk to the bot. Hence no
registration, and hints about what to say.

Normally started through `run_demo.py`, which also brings the bots up. On its own:
    python demo.py            # showcase only; the bots must already be running

Why the showcase is separate from the bots: one bot process serves one business
(`BUSINESS` is fixed at import time). For five tabs we run five processes on
adjacent ports, and this page only needs to know where to knock. Reworking the
business selection to be per-session, just for a demo, would mean changing a
working core for no reason.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from business import load_business
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from locale_pack import LOCALES_DIR, load_locale

HERE = Path(__file__).parent
STATIC = HERE / "static"

#: The page opens in this language whatever the businesses on it are. An
#: American opening a demo and reading Ukrainian labels around an English
#: dental practice concludes the project is not for them, and closes the tab.
DEFAULT_UI_LANGUAGE = os.getenv("DEMO_LANGUAGE", "en")

#: Tab order = the order of this list. The first one opens by default.
DEMO_BUSINESSES = ["dental_us", "clinic", "emergency", "cleaning", "hotel", "barbershop"]

#: Synthesis models the demo can be called on, first one the default.
#:
#: One BOT PROCESS per (business, model) rather than one process told which
#: model to use per call: the model is read when the process starts, and
#: threading a per-session override through the transport, the service settings
#: and the reconnect path is a lot of machinery for a switch that exists to
#: answer one question by ear. A process costs about 230 MB and three seconds of
#: startup, which is the cheaper side of that trade by a wide margin.
#:
#: `ttfb_ms` is measured, not quoted: median of three runs on the shipping voice
#: reading the same booking line (voice_lab.py models). It sits next to the name
#: because the whole point of the switch is hearing what the wait buys.
TTS_MODELS = [
    {"id": "eleven_flash_v2_5", "label": "Flash v2.5", "ttfb_ms": 336},
    {"id": "eleven_multilingual_v2", "label": "Multilingual v2", "ttfb_ms": 1699},
]

BASE_PORT = int(os.getenv("DEMO_BASE_PORT", "7860"))
#: Ports for the second model start a block above the first, so a glance at
#: `ss -tlnp` says which is which.
MODEL_PORT_STRIDE = int(os.getenv("DEMO_MODEL_PORT_STRIDE", "10"))
DEMO_PORT = int(os.getenv("DEMO_PORT", "8080"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "180"))


def bot_port(key: str, model_id: str | None = None) -> int:
    """Where the bot for this business, on this model, is listening."""
    model_index = 0
    if model_id:
        model_index = next(
            (i for i, m in enumerate(TTS_MODELS) if m["id"] == model_id), 0
        )
    return BASE_PORT + model_index * MODEL_PORT_STRIDE + DEMO_BUSINESSES.index(key)


def bot_processes() -> list[tuple[str, str, int]]:
    """(business, model, port) for every process the demo needs running.

    run_demo.py starts exactly these; the page dials exactly these. One list, so
    a model added above cannot be offered on the page without a bot behind it.
    """
    return [
        (key, model["id"], bot_port(key, model["id"]))
        for model in TTS_MODELS
        for key in DEMO_BUSINESSES
        if (HERE / "businesses" / f"{key}.yaml").exists()
    ]


def demo_db(key: str) -> Path:
    """A separate database per industry, so demos never mix with real data."""
    return HERE / f"demo_{key}.db"


def _resource_rows(biz) -> list[dict]:
    """Price rows for the card, labelled in the business's own language.

    The showcase holds several businesses in different languages at once, so
    these come from `biz.pack` rather than from the process-wide one — an
    English practice showing "30 хв" is the first thing a visitor sees.
    """
    out = []
    for r in biz.booking.resources:
        if r.note:
            note = r.note
        elif biz.booking.is_slots and r.duration_min:
            note = biz.pack.text("ui.duration_min", n=r.duration_min)
        else:
            note = biz.pack.text("ui.per_unit", unit=biz.booking.unit)
        out.append({"label": r.label, "price": r.price, "note": note})
    return out


def _card(key: str) -> dict:
    biz = load_business(key)
    return {
        "key": key,
        "tab": biz.demo_tab,
        "brand": biz.brand,
        "name": biz.name,
        "city": biz.city,
        "agent": biz.agent_name,
        "intro": biz.demo_intro,
        "try": list(biz.demo_try),
        "hours": biz.booking.hours_compact(),
        "currency": biz.currency,
        "action": biz.booking.action,
        "resources": _resource_rows(biz),
        "intake": [f.label for f in biz.booking.intake],
        # One port per synthesis model. The page picks by the model chosen next
        # to the call button; `port` is kept as the default so nothing has to
        # know about models to place a call.
        "port": bot_port(key),
        "ports": {m["id"]: bot_port(key, m["id"]) for m in TTS_MODELS},
        "max_call_seconds": MAX_CALL_SECONDS,
        # Which language this agent answers in, named in that language. A
        # property of the business, not of the page: a visitor reading the page
        # in English can still call the Ukrainian clinic, and should be told so
        # before they press the button rather than after.
        "language_name": (biz.pack.raw("demo", None) or {}).get("language_name", biz.pack.lang),
        # What the tools did, in words a visitor understands. From the
        # business's pack, because these lines describe THIS agent's actions and
        # sit right underneath its replies.
        "actions": biz.pack.raw("demo.actions", {}),
    }


#: Configs are read once at startup — they do not change during a demo.
CARDS = [_card(k) for k in DEMO_BUSINESSES if (HERE / "businesses" / f"{k}.yaml").exists()]

app = FastAPI(title="open-receptionist · demo")
# /static holds the bundled official pipecat SDK (see client/).
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "demo.html").read_text(encoding="utf-8")


@app.get("/api/businesses")
def api_businesses() -> JSONResponse:
    return JSONResponse(CARDS)


@app.get("/api/tts-models")
def api_tts_models() -> JSONResponse:
    """What the call button can be switched between, with measured latency."""
    return JSONResponse(TTS_MODELS)


def _ui_languages() -> list[dict]:
    """Every pack that can dress the page, by its own name for itself.

    "Українська" rather than "Ukrainian": someone who needs that option is
    reading in it, and a list of endonyms is scannable to everyone at once.
    Packs without a `demo:` section are skipped rather than shown broken — the
    section is optional, since a language pack exists to run a bot and not to
    translate a showcase.
    """
    out = []
    for path in sorted(LOCALES_DIR.glob("*.yaml")):
        pack = load_locale(path.stem)
        name = (pack.raw("demo", None) or {}).get("language_name")
        if name:
            out.append({"code": path.stem, "name": name})
    return out


@app.get("/api/ui")
def api_ui(lang: str = "") -> JSONResponse:
    """The page's own words, plus which other languages it can be read in.

    Served rather than baked into demo.html so that the strings live in the
    same packs as everything else the project says out loud — one place to
    translate, and the page cannot drift out of step with the agent.
    """
    languages = _ui_languages()
    codes = {x["code"] for x in languages}
    chosen = lang if lang in codes else DEFAULT_UI_LANGUAGE
    if chosen not in codes:  # no pack translates the page: fall back to any
        chosen = languages[0]["code"] if languages else DEFAULT_UI_LANGUAGE
    return JSONResponse(
        {
            "lang": chosen,
            "languages": languages,
            "strings": load_locale(chosen).raw("demo", {}),
        }
    )


@app.get("/api/now")
def api_now() -> JSONResponse:
    """Server time, in the same format the database stamps created_at with.

    The page marks the start of a call with it. Taking the time from the browser
    does not work: a phone's clock is easily minutes out, and then "what
    appeared in the system during this call" misses — the owner is told nothing
    was recorded right after the bot read out a booking reference.
    """
    return JSONResponse({"utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})


@app.get("/api/activity/{key}")
def api_activity(key: str, since: str = "") -> JSONResponse:
    """What appeared in the business's database after the call started.

    We read the database directly rather than through crm: crm is bound to one
    business per process, while the showcase needs to look into all five. The
    query is the same for every industry — the schema is shared.
    """
    path = demo_db(key)
    if key not in DEMO_BUSINESSES or not path.exists():
        return JSONResponse({"bookings": [], "messages": [], "events": []})

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        bookings = [
            dict(r)
            for r in conn.execute(
                "SELECT ref_code, resource, starts_at, ends_at, party_size, customer_name,"
                " phone, total_price, details, created_at FROM bookings"
                " WHERE created_at >= ? ORDER BY created_at DESC LIMIT 5",
                (since,),
            )
        ]
        messages = [
            dict(r)
            for r in conn.execute(
                "SELECT customer_name, phone, topic, created_at FROM messages"
                " WHERE created_at >= ? ORDER BY created_at DESC LIMIT 5",
                (since,),
            )
        ]
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT kind, detail, created_at FROM events WHERE created_at >= ?"
                " ORDER BY created_at LIMIT 20",
                (since,),
            )
        ]
    except sqlite3.OperationalError:
        bookings, messages, events = [], [], []  # empty database, no tables yet
    finally:
        conn.close()

    labels = {r.key: r.label for r in load_business(key).booking.resources}
    for b in bookings:
        b["resource_label"] = labels.get(b["resource"], b["resource"])
    return JSONResponse({"bookings": bookings, "messages": messages, "events": events})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("DEMO_HOST", "0.0.0.0"), port=DEMO_PORT)
