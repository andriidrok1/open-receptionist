"""demo.py — вітрина: сторінка, з якої можна подзвонити боту будь-якої галузі.

Це інструмент продажу, а не наш стенд: друг кидає посилання власнику бізнесу,
той відкриває з телефона, обирає свою галузь і говорить з ботом. Тому сторінка
українською, без реєстрації і з підказками, що казати.

Запуск — через `run_demo.py`, який піднімає ще й ботів. Окремо:
    python demo.py            # тільки вітрина, боти мають бути підняті самі

Чому вітрина окремо від ботів: один процес бота обслуговує один заклад
(`BUSINESS` фіксується на імпорті). Для п'яти вкладок піднімаємо п'ять процесів
на сусідніх портах, а ця сторінка лише знає, куди стукати. Переробляти вибір
закладу на пер-сесію заради демо — міняти працююче ядро без потреби.
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

#: Порядок вкладок = порядок цього списку. Перша — та, що відкриється одразу.
DEMO_BUSINESSES = ["clinic", "emergency", "cleaning", "hotel", "barbershop"]

BASE_PORT = int(os.getenv("DEMO_BASE_PORT", "7860"))
DEMO_PORT = int(os.getenv("DEMO_PORT", "8080"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "180"))


def bot_port(key: str) -> int:
    return BASE_PORT + DEMO_BUSINESSES.index(key)


def demo_db(key: str) -> Path:
    """Окрема база на кожну галузь — щоб демо не змішувалось із робочими даними."""
    return HERE / f"demo_{key}.db"


def _resource_rows(biz) -> list[dict]:
    out = []
    for r in biz.booking.resources:
        out.append(
            {
                "label": r.label,
                "price": r.price,
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


#: Читаємо конфіги один раз на старті — вони не змінюються під час показу.
CARDS = [_card(k) for k in DEMO_BUSINESSES if (HERE / "businesses" / f"{k}.yaml").exists()]

app = FastAPI(title="voice-receptionist · demo")
# /static — зібраний бандл офіційного SDK pipecat (див. client/).
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "demo.html").read_text(encoding="utf-8")


@app.get("/api/businesses")
def api_businesses() -> JSONResponse:
    return JSONResponse(CARDS)


@app.get("/api/now")
def api_now() -> JSONResponse:
    """Час за годинником сервера у тому ж форматі, що created_at у базі.

    Сторінка позначає ним початок дзвінка. Брати час із браузера не можна:
    годинник телефона легко відстає на хвилини, і тоді «що з'явилось у системі
    за цю розмову» промахується — власник бачить «нічого не записано» одразу
    після того, як бот вголос назвав номер броні.
    """
    return JSONResponse({"utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})


@app.get("/api/activity/{key}")
def api_activity(key: str, since: str = "") -> JSONResponse:
    """Що з'явилось у базі закладу після початку дзвінка.

    Читаємо базу напряму, а не через crm: crm прив'язаний до одного закладу на
    процес, а вітрині треба заглядати в усі п'ять. Запит один і той самий для
    будь-якої галузі — схема ж спільна.
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
        bookings, messages, events = [], [], []  # база ще порожня, таблиць немає
    finally:
        conn.close()

    labels = {r.key: r.label for r in load_business(key).booking.resources}
    for b in bookings:
        b["resource_label"] = labels.get(b["resource"], b["resource"])
    return JSONResponse({"bookings": bookings, "messages": messages, "events": events})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("DEMO_HOST", "0.0.0.0"), port=DEMO_PORT)
