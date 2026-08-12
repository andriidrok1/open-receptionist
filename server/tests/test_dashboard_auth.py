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
