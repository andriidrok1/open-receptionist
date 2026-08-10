"""The owner's dashboard: FastAPI serves one page plus a JSON stats endpoint.

There is a single page for every industry — the labels arrive from the business
config together with the data, so a hotel sees "Room type / Nights / Guests"
while a barbershop sees "Service / Duration" with no guests column at all.

Running it:
    python dashboard.py                 # http://localhost:8000
    BUSINESS=clinic python dashboard.py # the same page for a dental practice

The numbers are computed from the same records the voice agent creates (crm.py).
"""

import os
from pathlib import Path

import crm
from business import BUSINESS
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title=f"open-receptionist · {BUSINESS.key}")
STATIC = Path(__file__).parent / "static"

BOOK = BUSINESS.booking

BUSINESS_INFO = {
    "brand": os.getenv("BRAND", BUSINESS.brand),
    "city": os.getenv("BRAND_CITY", BUSINESS.city),
    "agent": os.getenv("AGENT_NAME", BUSINESS.agent_name),
    "currency": BUSINESS.currency,
    "is_slots": BOOK.is_slots,
    "labels": {
        "resource": BOOK.resource_label.capitalize(),
        "customer": BUSINESS.customer_nom.capitalize(),
        "action": BOOK.action.capitalize(),
        "unit": None if BOOK.is_slots else BOOK.unit.capitalize(),
        "party": BOOK.party_label.capitalize() if BOOK.ask_party else None,
    },
    # Every label the page shows, from the business's own language pack. The
    # page used to carry them hard-coded, so an English business had an English
    # agent answering the phone and a Ukrainian panel to look at.
    "ui": BUSINESS.pack.raw("dashboard"),
}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    data = crm.stats()
    data["business"] = BUSINESS_INFO
    return JSONResponse(data)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "8000")))
