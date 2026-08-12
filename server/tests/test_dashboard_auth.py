"""The dashboard's lock. Deterministic, no network.

The page holds every customer's name and phone number, so the rules under test
are blunt: with a password set, nothing answers without a valid session cookie;
with no password set, nothing answers anyone but localhost.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

# Read at import time by dashboard.py, so it must be set before the import —
# the same reason conftest.py sets BUSINESS and CRM_DB at the top.
os.environ["DASHBOARD_PASSWORD"] = "s3cret-test"

import crm  # noqa: E402

crm.init_db()

import dashboard  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def fresh_client() -> TestClient:
    """Each test gets its own cookie jar — a session leaking from one test
    into the next would hide exactly the failures this file exists to catch."""
    return TestClient(dashboard.app)


# ------------------------------ locked out ------------------------------


def test_every_data_route_is_locked_without_a_session():
    c = fresh_client()
    for url in ("/api/stats", "/api/inbox", "/api/calls", "/api/call/whatever"):
        assert c.get(url).status_code == 401, url
    assert c.post("/api/handled", json={"kind": "booking", "id": "x"}).status_code == 401


def test_the_page_shows_login_not_data_without_a_session():
    page = fresh_client().get("/")
    assert page.status_code == 200
    assert "/api/login" in page.text
    assert "dashboard.js" not in page.text


def test_wrong_password_is_refused():
    c = fresh_client()
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.get("/api/inbox").status_code == 401


def test_a_tampered_cookie_is_refused():
    c = fresh_client()
    c.cookies.set(dashboard.COOKIE, "9999999999.deadbeef")
    assert c.get("/api/inbox").status_code == 401


# ------------------------------ let in ------------------------------


def test_login_opens_the_data_and_logout_closes_it():
    c = fresh_client()
    assert c.post("/api/login", json={"password": "s3cret-test"}).status_code == 200
    assert c.get("/api/inbox").status_code == 200
    assert "dashboard.js" in c.get("/").text

    c.get("/logout", follow_redirects=False)
    assert c.get("/api/inbox").status_code == 401


def test_handled_answers_404_for_an_unknown_item():
    c = fresh_client()
    c.post("/api/login", json={"password": "s3cret-test"})
    r = c.post("/api/handled", json={"kind": "booking", "id": "no-such-row"})
    assert r.status_code == 404


# ------------------------------ cancel button ------------------------------


def test_owner_cancel_flips_the_booking_and_keeps_it_out_of_the_new_pile():
    """The owner cancelled it themselves — the cancellation must not come back
    at them as unread news (that rule exists for CALLERS' cancellations)."""
    from datetime import timedelta

    from business import BUSINESS

    start = (BUSINESS.today() + timedelta(days=55)).isoformat()
    end = (BUSINESS.today() + timedelta(days=56)).isoformat()
    room = BUSINESS.booking.resources[0].label
    made = crm.create_booking(room, start, "Тарас Дашбордний", "0632096974", end)
    assert made["success"], made

    c = fresh_client()
    c.post("/api/login", json={"password": "s3cret-test"})
    r = c.post("/api/cancel", json={"ref_code": made["ref_code"]})
    assert r.status_code == 200 and r.json()["ok"] is True

    item = next(x for x in crm.inbox() if x.get("ref_code") == made["ref_code"])
    assert item["type"] == "cancellation"
    assert item["handled"] is True

    # Cancelling twice is a conflict, not a success.
    assert c.post("/api/cancel", json={"ref_code": made["ref_code"]}).status_code == 409


def test_cancel_of_an_unknown_ref_is_a_conflict_not_a_crash():
    c = fresh_client()
    c.post("/api/login", json={"password": "s3cret-test"})
    r = c.post("/api/cancel", json={"ref_code": "JOB-0000"})
    assert r.status_code == 409
    assert r.json()["error"]


def test_cancel_is_locked_without_a_session():
    r = fresh_client().post("/api/cancel", json={"ref_code": "JOB-0000"})
    assert r.status_code == 401


# ------------------------------ duty schedule ------------------------------


def test_schedule_saves_validates_and_reads_back():
    c = fresh_client()
    c.post("/api/login", json={"password": "s3cret-test"})
    try:
        r = c.post("/api/settings", json={
            "mode": "schedule",
            "owner_hours": {"mon": {"enabled": True, "from": "08:00", "to": "18:00"}},
        })
        assert r.status_code == 200
        saved = c.get("/api/settings").json()["schedule"]
        assert saved["mode"] == "schedule"
        assert saved["owner_hours"]["mon"]["enabled"] is True

        # Broken input must bounce at the door — agent_on_duty() runs at the
        # start of every call and a bad span must not take the phone down.
        assert c.post("/api/settings", json={"mode": "sometimes"}).status_code == 422
        assert c.post("/api/settings", json={
            "mode": "schedule", "owner_hours": {"mon": {"from": "8am", "to": "18:00"}},
        }).status_code == 422
        assert c.post("/api/settings", json={
            "mode": "schedule", "owner_hours": {"someday": {"from": "08:00", "to": "18:00"}},
        }).status_code == 422
    finally:
        c.post("/api/settings", json={"mode": "always"})


def test_settings_are_locked_without_a_session():
    c = fresh_client()
    assert c.get("/api/settings").status_code == 401
    assert c.post("/api/settings", json={"mode": "off"}).status_code == 401


# ------------------------------ agent health ------------------------------


def test_agent_health_is_measured_not_painted(monkeypatch):
    """The old dashboard drew a green dot unconditionally; this one must
    report what is actually listening on the bot's port."""
    import asyncio
    import socket

    # Nothing listens here: a bound-then-closed port is free by definition.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    monkeypatch.setattr(dashboard, "BOT_PORT", free_port)
    dashboard._health["at"] = 0.0  # the cache must not serve a stale verdict
    assert asyncio.run(dashboard._agent_online()) is False

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    monkeypatch.setattr(dashboard, "BOT_PORT", listener.getsockname()[1])
    dashboard._health["at"] = 0.0
    assert asyncio.run(dashboard._agent_online()) is True
    listener.close()


def test_inbox_payload_carries_health_and_the_business_today():
    c = fresh_client()
    c.post("/api/login", json={"password": "s3cret-test"})
    dashboard._health["at"] = 0.0
    data = c.get("/api/inbox").json()
    assert data["agent_online"] in (True, False)
    assert len(data["today"]) == 10  # YYYY-MM-DD, in the business's zone


# ------------------------------ no password ------------------------------
#
# _is_owner is tested directly here: TestClient stamps every request as coming
# from the host "testclient", and what matters in this mode is precisely the
# distinction between localhost and everyone else.


def _request(host: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host), cookies={})


def test_no_password_means_localhost_only(monkeypatch):
    monkeypatch.setattr(dashboard, "PASSWORD", "")
    assert dashboard._is_owner(_request("127.0.0.1")) is True
    assert dashboard._is_owner(_request("::1")) is True
    assert dashboard._is_owner(_request("203.0.113.7")) is False


def test_no_password_refuses_remote_pages_with_advice_not_a_login():
    """A login form that cannot succeed is a trap; the 403 page says what to
    set instead."""
    import unittest.mock

    with unittest.mock.patch.object(dashboard, "PASSWORD", ""):
        page = fresh_client().get("/")
    assert page.status_code == 403
    assert "DASHBOARD_PASSWORD" in page.text


def test_changing_the_password_revokes_old_sessions(monkeypatch):
    c = fresh_client()
    c.post("/api/login", json={"password": "s3cret-test"})
    assert c.get("/api/inbox").status_code == 200
    monkeypatch.setattr(dashboard, "PASSWORD", "rotated")
    assert c.get("/api/inbox").status_code == 401
