"""Дашборд готелю (Фаза 3): FastAPI віддає сторінку + JSON зі статистикою з hotel.db.

Запуск:
    python dashboard.py          # http://localhost:8000
Дані рахуються з тих самих броней, що створює голосовий агент (pms.py / hotel.db).
"""

import os
from pathlib import Path

import pms
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="hotel-voice-agent · dashboard")
STATIC = Path(__file__).parent / "static"

HOTEL = {
    "name": os.getenv("HOTEL_BRAND", "Riviera Hotel"),
    "city": os.getenv("HOTEL_CITY", "Одеса, Україна"),
    "agent": os.getenv("AGENT_NAME", "Оксана"),
}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    data = pms.stats()
    data["hotel"] = HOTEL
    return JSONResponse(data)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
