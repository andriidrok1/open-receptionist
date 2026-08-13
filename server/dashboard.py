"""The owner's dashboard: an inbox first, a stats page second.

The page answers the question an owner actually opens it with — "what happened
while I wasn't looking, and whom do I owe a call?" — so the first screen is a
feed of bookings, cancellations and callback requests, each with a checkmark
the owner flips. The charts that used to be the whole dashboard live on the
Overview tab.

Running it:
    python dashboard.py                 # http://localhost:8000
    BUSINESS=clinic python dashboard.py # the same page for a dental practice

Access: set DASHBOARD_PASSWORD in .env and the page asks for it once per
browser (a signed cookie keeps the session for 30 days). With no password set
the dashboard answers only to localhost — a real install is reachable from the
internet, and an open inbox full of customers' phone numbers is a leak, not a
convenience.

The numbers are computed from the same records the voice agent creates (crm.py).
"""

import asyncio
import hashlib
import hmac
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import crm
import notify
from business import BUSINESS

app = FastAPI(title=f"open-receptionist · {BUSINESS.key}")


@app.on_event("startup")
async def _start_notifier() -> None:
    # The push loop lives in the dashboard process on purpose: it is the
    # owner-facing component, and the call pipeline must never wait on
    # Telegram. Without a token the feature simply does not exist.
    if notify.TOKEN:
        app.state.notifier = asyncio.create_task(notify.run_forever())
STATIC = Path(__file__).parent / "static"
# The React runtime and the dashboard app itself. Vendored, not a CDN: a
# self-hosted install must not depend on unpkg being reachable — or on the
# customer data page phoning anyone at all.
app.mount("/static", StaticFiles(directory=STATIC), name="static")

BOOK = BUSINESS.booking

# ------------------------------- access -------------------------------

PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
COOKIE = "owner_session"
SESSION_DAYS = 30


def _sign(expires: int) -> str:
    """HMAC over the expiry, keyed off the password itself.

    No session table and nothing random to store: a token is valid exactly
    while the password it was minted under stays the same, so changing the
    password in .env revokes every cookie out there — which is the behaviour
    an installer expects from rotating a credential.
    """
    key = hashlib.sha256(f"open-receptionist dashboard:{PASSWORD}".encode()).digest()
    return hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()


def _make_token() -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    return f"{expires}.{_sign(expires)}"


def _token_ok(token: str) -> bool:
    expires, _, sig = (token or "").partition(".")
    if not expires.isdigit() or not sig:
        return False
    return int(expires) > time.time() and hmac.compare_digest(sig, _sign(int(expires)))


def _is_owner(request: Request) -> bool:
    if PASSWORD:
        return _token_ok(request.cookies.get(COOKIE, ""))
    # No password configured: this is a developer's own machine. Answering
    # non-local requests anyway would turn every forgotten .env into a public
    # list of customers' phone numbers.
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _require_owner(request: Request) -> None:
    if not _is_owner(request):
        raise HTTPException(status_code=401)


def _dash_ui() -> dict:
    return BUSINESS.pack.raw("dashboard")


# ------------------------------- agent health -------------------------------
#
# The sidebar used to show a green "Online" dot that was simply painted on —
# it stayed green with the bot process dead, which is worse than no indicator:
# the owner trusts the dot precisely on the night the agent is down. The check
# is a TCP connect to the bot's port: it proves a process is listening, which
# is exactly what a painted dot only claimed.

BOT_HOST = os.getenv("BOT_HOST", "127.0.0.1")
BOT_PORT = int(os.getenv("BOT_PORT", "7860"))
_HEALTH_TTL = 5.0
_health: dict = {"at": 0.0, "online": False}


async def _agent_online() -> bool:
    """Is anything listening where the bot should be? Cached for a few seconds
    so ten open dashboards do not turn the poll into a knock storm."""
    now = time.monotonic()
    if now - _health["at"] < _HEALTH_TTL:
        return _health["online"]
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(BOT_HOST, BOT_PORT), timeout=1.0
        )
        writer.close()
        online = True
    except OSError:
        online = False
    except TimeoutError:
        online = False
    _health.update(at=now, online=online)
    return online


# ------------------------------- business card -------------------------------

BUSINESS_INFO = {
    "brand": os.getenv("BRAND", BUSINESS.brand),
    "city": os.getenv("BRAND_CITY", BUSINESS.city),
    "agent": os.getenv("AGENT_NAME", BUSINESS.agent_name),
    "currency": BUSINESS.currency,
    "is_slots": BOOK.is_slots,
    # The business's clock, so "today" on a card means today AT THE BUSINESS —
    # an owner reading from another timezone was shown yesterday as today.
    "tz": BUSINESS.tz,
    # The trades promise "between nine and ten", so the inbox shows the same
    # window the caller heard, not a minute the crew will be judged against.
    "arrival_window_min": BOOK.arrival_window_min,
    # Intake answers are stored under config keys ("address", "problem");
    # these are the human labels the cards print next to them.
    "intake_labels": {f.key: f.label for f in BOOK.intake},
    # Monday-first, matching crm._DAY_KEYS — the settings grid's row labels.
    "weekdays_short": BUSINESS.pack.raw("calendar.weekdays_short"),
    # Whether the page runs behind a password — the UI offers "sign out" only
    # when there is a session to sign out of.
    "auth": bool(PASSWORD),
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


# ------------------------------- pages -------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    if _is_owner(request):
        return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"))
    if not PASSWORD:
        # Reached from another machine with no password set: tell the
        # installer what to do instead of showing a login that cannot succeed.
        return HTMLResponse(
            "<h3>open-receptionist</h3>"
            "<p>Set <code>DASHBOARD_PASSWORD</code> in <code>server/.env</code> "
            "and restart to open this dashboard from other machines.</p>",
            status_code=403,
        )
    ui = _dash_ui()
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    for key in ("login_title", "password", "sign_in", "wrong_password"):
        page = page.replace("{" + key + "}", ui.get(key, key))
    page = page.replace("{brand}", BUSINESS_INFO["brand"])
    return HTMLResponse(page)


class LoginBody(BaseModel):
    password: str = ""


@app.post("/api/login")
async def api_login(body: LoginBody) -> JSONResponse:
    if not PASSWORD:
        raise HTTPException(status_code=403)
    if not hmac.compare_digest(body.password, PASSWORD):
        # A beat of delay is the whole anti-bruteforce budget this needs:
        # the password is one shared secret, not a user database.
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE,
        _make_token(),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# ------------------------------- data -------------------------------


@app.get("/api/stats")
def api_stats(request: Request) -> JSONResponse:
    _require_owner(request)
    data = crm.stats()
    data["business"] = BUSINESS_INFO
    return JSONResponse(data)


@app.get("/api/inbox")
async def api_inbox(request: Request) -> JSONResponse:
    _require_owner(request)
    return JSONResponse(
        {
            "items": crm.inbox(),
            "business": BUSINESS_INFO,
            "agent_online": await _agent_online(),
            # Two different truths about the agent: online is "is the process
            # alive", on_duty is "is it supposed to pick up right now".
            "agent_on_duty": crm.agent_on_duty(),
            # Today at the business, for the cards' "today vs date" labels.
            "today": datetime.now(ZoneInfo(BUSINESS.tz)).strftime("%Y-%m-%d"),
        }
    )


@app.get("/api/calls")
def api_calls(request: Request) -> JSONResponse:
    _require_owner(request)
    return JSONResponse({"calls": crm.call_log()})


@app.get("/api/call/{call_id}")
def api_call(request: Request, call_id: str) -> JSONResponse:
    _require_owner(request)
    call = crm.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404)
    return JSONResponse(call)


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _telegram_state() -> dict:
    """What the Settings page needs to draw the notifications card. No
    Telegram round-trips in a request handler — the username was stored by
    the notifier at startup."""
    if not notify.TOKEN:
        return {"configured": False}
    return {
        "configured": True,
        "chats": len(notify.chats()),
        "pair_code": notify.ensure_pair_code(),
        "bot": crm.get_setting("telegram_bot_username", ""),
    }


@app.get("/api/settings")
def api_settings(request: Request) -> JSONResponse:
    _require_owner(request)
    return JSONResponse(
        {
            "schedule": crm.agent_schedule(),
            "on_duty_now": crm.agent_on_duty(),
            "telegram": _telegram_state(),
        }
    )


@app.post("/api/telegram/unlink")
def api_telegram_unlink(request: Request) -> JSONResponse:
    _require_owner(request)
    notify.unlink_all()
    return JSONResponse({"ok": True, "telegram": _telegram_state()})


class ScheduleBody(BaseModel):
    mode: str
    owner_hours: dict = {}


@app.post("/api/settings")
def api_settings_save(request: Request, body: ScheduleBody) -> JSONResponse:
    _require_owner(request)
    # Validated here, at the door: agent_on_duty() runs at the start of every
    # call, and a malformed span must not be able to take the phone down.
    if body.mode not in ("always", "schedule", "off"):
        raise HTTPException(status_code=422)
    hours = {}
    for day, span in (body.owner_hours or {}).items():
        if day not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            raise HTTPException(status_code=422)
        start, end = str(span.get("from", "")), str(span.get("to", ""))
        if not (_TIME_RE.match(start) and _TIME_RE.match(end)):
            raise HTTPException(status_code=422)
        hours[day] = {"enabled": bool(span.get("enabled")), "from": start, "to": end}
    crm.set_setting("agent_schedule", {"mode": body.mode, "owner_hours": hours})
    return JSONResponse(
        {"ok": True, "schedule": crm.agent_schedule(), "on_duty_now": crm.agent_on_duty()}
    )


class CancelBody(BaseModel):
    ref_code: str


@app.post("/api/cancel")
def api_cancel(request: Request, body: CancelBody) -> JSONResponse:
    _require_owner(request)
    out = crm.cancel_booking(body.ref_code)
    if not out.get("success"):
        return JSONResponse({"ok": False, "error": out.get("error")}, status_code=409)
    # The owner is the one cancelling — not news to them, the checkmark goes
    # straight back on (see crm.mark_handled_by_ref).
    crm.mark_handled_by_ref(out["ref_code"])
    return JSONResponse({"ok": True, "ref_code": out["ref_code"]})


class HandledBody(BaseModel):
    kind: str
    id: str
    handled: bool = True


@app.post("/api/handled")
def api_handled(request: Request, body: HandledBody) -> JSONResponse:
    _require_owner(request)
    if not crm.mark_handled(body.kind, body.id, body.handled):
        raise HTTPException(status_code=404)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    crm.init_db()
    # A password means "meant to be reachable" — the owner opens this from a
    # phone. Without one the page only answers locally anyway, so binding
    # wider would just be surface area.
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0" if PASSWORD else "127.0.0.1")
    uvicorn.run(app, host=host, port=int(os.getenv("DASHBOARD_PORT", "8000")))
