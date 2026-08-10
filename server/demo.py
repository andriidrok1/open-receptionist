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

HERE = Path(__file__).parent
STATIC = HERE / "static"

#: Tab order = the order of this list. The first one opens by default.
DEMO_BUSINESSES = ["clinic", "emergency", "cleaning", "hotel", "barbershop"]

BASE_PORT = int(os.getenv("DEMO_BASE_PORT", "7860"))
DEMO_PORT = int(os.getenv("DEMO_PORT", "8080"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "180"))


def bot_port(key: str) -> int:
    return BASE_PORT + DEMO_BUSINESSES.index(key)


def demo_db(key: str) -> Path:
    """A separate database per industry, so demos never mix with real data."""
    return HERE / f"demo_{key}.db"


def _resource_rows(biz) -> list[dict]:
    out = []
    for r in biz.booking.resources:
        out.append(
            {
                "label": r.label,
                "price": r.price,
                # TODO(locale): shown on the demo card, needs the language pack.
                "note": r.note or (
                    f"{r.duration_min} хв"
                    if biz.booking.is_slots and r.duration_min
                    else f"за {biz.booking.unit}"
                ),
            }
        )
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
        "port": bot_port(key),
        "max_call_seconds": MAX_CALL_SECONDS,
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
