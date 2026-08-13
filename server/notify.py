"""Telegram push to the owner: the inbox, delivered instead of awaited.

The dashboard knows what the owner has not seen; this module makes sure their
phone does too. A booking, a caller's cancellation or a callback request lands
as one Telegram message — the owner's own actions (cancelling from the
dashboard) arrive pre-stamped and are never echoed back at them.

Deliberately NOT in the call pipeline: the agent's job is the caller, and a
slow Telegram API must never add a millisecond to the conversation. Instead a
background loop (started by dashboard.py) scans the database for unstamped
rows and stamps them only after Telegram accepts the send — so delivery is
at-least-once, survives restarts, and a missed push is retried rather than
lost. The horizon logic lives in crm.pending_notifications.

Setup is two minutes and self-hosted end to end:
  1. The installer makes a bot with @BotFather and puts TELEGRAM_BOT_TOKEN
     in server/.env.
  2. The owner opens Settings on the dashboard and taps the connect link
     (t.me/<bot>?start=<code>). Anyone with the code can connect — a partner
     or a foreman too — and Unlink disconnects everyone at once.

No webhooks: pairing uses getUpdates long polling, which works from behind
any NAT, because a self-hosted install must not require a public HTTPS
endpoint just to hear about its own bookings.
"""

import asyncio
import os
import secrets

from loguru import logger

import crm
from business import BUSINESS

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

BOOK = BUSINESS.booking
_KIND_ICON = {"booking": "📅", "cancellation": "❌", "message": "✉️"}


# ------------------------------ pairing state ------------------------------
#
# All of it lives in the settings table, so it survives restarts and is
# editable from the dashboard: the owner's chat ids, the pairing code, and
# the bot's username (fetched once, so the dashboard can build the deep link
# without talking to Telegram in a request handler).


def ensure_pair_code() -> str:
    code = crm.get_setting("telegram_pair_code")
    if not code:
        code = secrets.token_hex(4)  # 8 hex chars: easy to relay, hard to guess
        crm.set_setting("telegram_pair_code", code)
    return code


def chats() -> list[int]:
    stored = crm.get_setting("telegram_chats", [])
    return [int(c) for c in stored] if isinstance(stored, list) else []


def add_chat(chat_id: int) -> None:
    linked = chats()
    if chat_id not in linked:
        crm.set_setting("telegram_chats", linked + [chat_id])


def unlink_all() -> None:
    """Disconnect every phone and burn the code — rotating the invitation is
    the point, or an ex-employee's Telegram keeps receiving customers."""
    crm.set_setting("telegram_chats", [])
    crm.set_setting("telegram_pair_code", secrets.token_hex(4))


# ------------------------------ rendering ------------------------------


def _fmt_money(amount) -> str:
    cur = BUSINESS.currency
    text = f"{amount:g}" if isinstance(amount, (int, float)) else str(amount)
    return f"{cur}{text}" if len(cur) == 1 else f"{text} {cur}"


def _fmt_day(date_str: str) -> str:
    """'2026-08-15' → 'Sat 15.08', weekday named by the pack."""
    try:
        import datetime as dt

        d = dt.date.fromisoformat(date_str[:10])
        weekdays = list(BUSINESS.pack.raw("calendar.weekdays_short"))
        return f"{weekdays[d.weekday()]} {d.day:02d}.{d.month:02d}"
    except (ValueError, IndexError):
        return date_str


def _fmt_when(item: dict) -> str:
    starts, ends = str(item.get("starts_at") or ""), str(item.get("ends_at") or "")
    if not BOOK.is_slots:
        return f"{_fmt_day(starts)} → {_fmt_day(ends)}"
    day, start_hm = _fmt_day(starts), starts[11:16]
    if BOOK.arrival_window_min:
        # The same window the caller heard — never a minute the crew will be
        # judged against.
        import datetime as dt

        try:
            t = dt.datetime.strptime(start_hm, "%H:%M")
            until = (t + dt.timedelta(minutes=BOOK.arrival_window_min)).strftime("%H:%M")
            return f"{day} · {start_hm}–{until}"
        except ValueError:
            pass
    end_hm = ends[11:16]
    return f"{day} · {start_hm}–{end_hm}" if end_hm else f"{day} · {start_hm}"


def render(item: dict) -> str:
    """One inbox item as one phone message — compact, and everything the
    owner needs to act is present: who, what, when, and the number to call."""
    pack = BUSINESS.pack
    kind = item["kind"]
    icon = _KIND_ICON.get(kind, "")
    title = pack.text(f"notify.{kind}_title", brand=BUSINESS.brand)
    lines = [f"{icon} {title}".strip()]
    who = " · ".join(x for x in (item.get("customer_name"), item.get("phone")) if x)
    if who:
        lines.append(who)
    if kind == "message":
        if item.get("topic"):
            lines.append(item["topic"])
        return "\n".join(lines)

    what = item.get("resource_label") or ""
    when = _fmt_when(item)
    price = item.get("total_price")
    core = " · ".join(x for x in (what, when) if x)
    if price and kind == "booking":
        core = f"{core} · {_fmt_money(price)}"
    lines.append(f"{core}  ({item['ref_code']})" if item.get("ref_code") else core)

    labels = {f.key: f.label for f in BOOK.intake}
    for key, value in (item.get("details") or {}).items():
        shown = {True: "✓", False: "✗"}.get(value, value)
        lines.append(f"{labels.get(key, key)}: {shown}")
    return "\n".join(lines)


# ------------------------------ the two loops ------------------------------


def handle_update(update: dict) -> tuple[int, str] | None:
    """One getUpdates entry → optionally (chat_id, reply_text).

    Only a correct pairing code gets an answer. Anything else is silence, not
    a hint — this bot talks to strangers about nothing, because the messages
    it sends contain customers' names and phone numbers.
    """
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat or not text:
        return None
    code = text.removeprefix("/start").strip()
    if code and code == crm.get_setting("telegram_pair_code"):
        add_chat(int(chat))
        return int(chat), BUSINESS.pack.text("notify.connected", brand=BUSINESS.brand)
    return None


async def deliver_pending(send) -> int:
    """Push every unstamped item to every linked chat; stamp on success.

    With no chats linked nothing is stamped — the news waits (within the
    horizon) for the owner to connect, instead of being marked seen by a
    phone that does not exist. A send that fails leaves the stamp off too:
    the next sweep retries.
    """
    linked = chats()
    if not linked:
        return 0
    delivered = 0
    for item in crm.pending_notifications():
        text = render(item)
        if any([await send(chat, text) for chat in linked]):
            crm.mark_notified(item["kind"], item["id"])
            delivered += 1
    return delivered


async def run_forever() -> None:
    """Started by dashboard.py when TELEGRAM_BOT_TOKEN is set."""
    import aiohttp

    ensure_pair_code()
    async with aiohttp.ClientSession() as session:

        async def call(method: str, **params):
            try:
                async with session.post(f"{API}/{method}", json=params) as r:
                    data = await r.json()
                    if not data.get("ok"):
                        logger.warning(f"[notify] {method}: {data}")
                        return None
                    return data["result"]
            except (aiohttp.ClientError, TimeoutError) as e:
                logger.warning(f"[notify] {method} failed: {e}")
                return None

        me = await call("getMe")
        if me:
            crm.set_setting("telegram_bot_username", me.get("username", ""))
            logger.info(f"[notify] telegram bot @{me.get('username')} ready")
        else:
            logger.warning("[notify] getMe failed — check TELEGRAM_BOT_TOKEN")

        async def send(chat_id: int, text: str) -> bool:
            return await call("sendMessage", chat_id=chat_id, text=text) is not None

        async def updates_loop():
            offset = None
            while True:
                result = await call("getUpdates", timeout=25, offset=offset)
                if result is None:
                    await asyncio.sleep(5)
                    continue
                for update in result:
                    offset = update["update_id"] + 1
                    reply = handle_update(update)
                    if reply:
                        await send(*reply)

        async def pending_loop():
            while True:
                try:
                    await deliver_pending(send)
                except Exception as e:  # a bad row must not kill the loop
                    logger.warning(f"[notify] delivery sweep failed: {e}")
                await asyncio.sleep(5)

        await asyncio.gather(updates_loop(), pending_loop())
