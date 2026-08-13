"""Telegram push: what gets delivered, what gets stamped, who gets linked.

No network anywhere — the send function is injected. The rules under test:
an item is stamped only after a delivery succeeded, the owner's own actions
are never echoed back at them, and the pairing code is the only sentence
this bot answers a stranger.
"""

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import crm  # noqa: E402
import notify  # noqa: E402
from business import BUSINESS  # noqa: E402

crm.init_db()


def _fresh_booking(name="Тест Пушовий", phone="0671112233"):
    start = (BUSINESS.today() + timedelta(days=40)).isoformat()
    end = (BUSINESS.today() + timedelta(days=41)).isoformat()
    room = BUSINESS.booking.resources[0].label
    made = crm.create_booking(room, start, name, phone, end)
    assert made["success"], made
    return made


def _clear_chats():
    crm.set_setting("telegram_chats", [])


def _drain():
    """Stamp the backlog other test files leave behind. The pending queue is
    oldest-first with a limit — exactly like the real sweep — so a test about
    a fresh item must first do what the real worker would have done by now."""
    while True:
        items = crm.pending_notifications(limit=500)
        if not items:
            return
        for item in items:
            crm.mark_notified(item["kind"], item["id"])


class Recorder:
    def __init__(self, ok=True):
        self.sent: list[tuple[int, str]] = []
        self.ok = ok

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))
        return self.ok


# ------------------------------ pending + stamps ------------------------------


def test_a_new_booking_is_pending_until_marked():
    _drain()
    made = _fresh_booking()
    pending = crm.pending_notifications()
    mine = [p for p in pending if p.get("ref_code") == made["ref_code"]]
    assert mine and mine[0]["kind"] == "booking"

    crm.mark_notified("booking", mine[0]["id"])
    left = [p for p in crm.pending_notifications() if p.get("ref_code") == made["ref_code"]]
    assert left == []


def test_a_callers_cancellation_is_separate_news():
    _drain()
    made = _fresh_booking()
    item = next(p for p in crm.pending_notifications() if p.get("ref_code") == made["ref_code"])
    crm.mark_notified("booking", item["id"])

    assert crm.cancel_booking(made["ref_code"])["success"]
    again = [p for p in crm.pending_notifications() if p.get("ref_code") == made["ref_code"]]
    assert again and again[0]["kind"] == "cancellation"
    crm.mark_notified("cancellation", again[0]["id"])


def test_the_owners_own_cancellation_is_never_pushed():
    """mark_handled_by_ref is the dashboard's cancel path — it pre-stamps."""
    _drain()
    made = _fresh_booking()
    item = next(p for p in crm.pending_notifications() if p.get("ref_code") == made["ref_code"])
    crm.mark_notified("booking", item["id"])

    assert crm.cancel_booking(made["ref_code"])["success"]
    crm.mark_handled_by_ref(made["ref_code"])
    assert all(
        p.get("ref_code") != made["ref_code"] for p in crm.pending_notifications()
    )


def test_messages_are_pending_too():
    _drain()
    out = crm.take_message("Пушова Особа", "0674445566", "передзвоніть щодо рахунку")
    assert out["success"]
    mine = [p for p in crm.pending_notifications() if p["kind"] == "message"
            and p.get("topic") == "передзвоніть щодо рахунку"]
    assert mine
    crm.mark_notified("message", mine[0]["id"])


# ------------------------------ delivery semantics ------------------------------


def test_nothing_is_stamped_when_no_phone_is_connected():
    """The news must wait for the owner, not be marked seen by nobody."""
    _drain()
    _clear_chats()
    made = _fresh_booking()
    send = Recorder()
    assert asyncio.run(notify.deliver_pending(send)) == 0
    assert send.sent == []
    assert any(p.get("ref_code") == made["ref_code"] for p in crm.pending_notifications())
    # cleanup: deliver it so later tests see a clean slate
    item = next(p for p in crm.pending_notifications() if p.get("ref_code") == made["ref_code"])
    crm.mark_notified("booking", item["id"])


def test_delivery_sends_to_every_chat_and_stamps():
    _drain()
    try:
        crm.set_setting("telegram_chats", [111, 222])
        made = _fresh_booking()
        send = Recorder()
        delivered = asyncio.run(notify.deliver_pending(send))
        assert delivered >= 1
        texts = [t for chat, t in send.sent if made["ref_code"] in t]
        assert len(texts) == 2  # both phones got it
        assert all(p.get("ref_code") != made["ref_code"] for p in crm.pending_notifications())
    finally:
        _clear_chats()


def test_a_failed_send_leaves_the_stamp_off():
    _drain()
    try:
        crm.set_setting("telegram_chats", [111])
        made = _fresh_booking()
        assert asyncio.run(notify.deliver_pending(Recorder(ok=False))) == 0
        assert any(p.get("ref_code") == made["ref_code"] for p in crm.pending_notifications())
        # and the retry sweep delivers it
        assert asyncio.run(notify.deliver_pending(Recorder(ok=True))) >= 1
    finally:
        _clear_chats()


# ------------------------------ rendering ------------------------------


def test_the_message_carries_what_the_owner_needs():
    _drain()
    made = _fresh_booking(name="Оксана Рендер", phone="0503334455")
    item = next(p for p in crm.pending_notifications() if p.get("ref_code") == made["ref_code"])
    text = notify.render(item)
    assert "Оксана Рендер" in text
    assert made["ref_code"] in text
    # the title is the pack's, not hard-coded English
    assert BUSINESS.pack.text("notify.booking_title", brand=BUSINESS.brand) in text
    crm.mark_notified("booking", item["id"])


# ------------------------------ pairing ------------------------------


def test_only_the_right_code_links_a_chat():
    try:
        _clear_chats()
        code = notify.ensure_pair_code()
        assert notify.handle_update(
            {"message": {"chat": {"id": 555}, "text": "/start wrong-code"}}
        ) is None
        assert notify.chats() == []

        reply = notify.handle_update(
            {"message": {"chat": {"id": 555}, "text": f"/start {code}"}}
        )
        assert reply is not None and reply[0] == 555
        assert notify.chats() == [555]

        # linking twice does not duplicate
        notify.handle_update({"message": {"chat": {"id": 555}, "text": f"/start {code}"}})
        assert notify.chats() == [555]
    finally:
        _clear_chats()


def test_strangers_get_silence():
    assert notify.handle_update({"message": {"chat": {"id": 777}, "text": "hello"}}) is None
    assert notify.handle_update({"message": {}}) is None
    assert notify.handle_update({}) is None


def test_unlink_burns_the_code_too():
    try:
        code = notify.ensure_pair_code()
        crm.set_setting("telegram_chats", [1, 2])
        notify.unlink_all()
        assert notify.chats() == []
        assert notify.ensure_pair_code() != code
    finally:
        _clear_chats()
