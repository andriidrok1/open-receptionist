"""Дашборд закладу (Фаза 3): FastAPI віддає сторінку + JSON зі статистикою.

Сторінка одна на всі галузі — підписи приходять із конфігу закладу разом із
даними, тож готелю вона показує «Тип номера / Ночей / Гостей», а барбершопу
«Послуга / Тривалість» без колонки гостей.

Запуск:
    python dashboard.py                 # http://localhost:8000, готель
    BUSINESS=clinic python dashboard.py # та сама сторінка для стоматології
Дані рахуються з тих самих записів, що створює голосовий агент (crm.py).
"""

import os
from pathlib import Path

import crm
from business import BUSINESS
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title=f"voice-receptionist · {BUSINESS.key}")
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
        # Без відмінювання міток із YAML: «Завантаження по тип номера» — саме
        # той сорт дрібниці, який видає, що продукт зібрали з шаблону.
        "occupancy": "Завантаження на сьогодні" if BOOK.is_slots else "Завантаження зараз",
    },
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
