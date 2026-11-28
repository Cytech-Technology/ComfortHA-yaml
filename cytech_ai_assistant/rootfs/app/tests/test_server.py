import json
import time

import pytest

import server
from config import Settings


def make_settings(tmp_path, **overrides):
    values = dict(
        proxy_url="http://proxy.test",
        installation_token="cyc_testtoken1234567890",
        model="claude-sonnet-5-5",
        ha_base_url="http://ha.test/api",
        ha_token="ha-token",
        port=8099,
        data_dir=tmp_path,
        log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


class FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def app(settings):
    return server.create_app(settings, ha=object())


@pytest.fixture
def client(app):
    return app.test_client()


def test_index_injects_ingress_prefix(client):
    response = client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "/api/hassio_ingress/abc/static/app.js" in html
    assert "/api/hassio_ingress/abc/static/styles.css" in html
    assert "Sonnet 5.5" in html

    cookie = response.headers.get("Set-Cookie", "")
    assert "cytech_sid=" in cookie
    assert "path=/api/hassio_ingress/abc" in cookie.lower()
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_index_without_prefix(client):
    response = client.get("/")
    html = response.get_data(as_text=True)

    assert 'src="/static/app.js"' in html
    assert "path=/" in response.headers.get("Set-Cookie", "").lower()


def test_security_headers(client):
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp


def test_chat_streams_events(client, app):
    def fake_run(session, text, emit):
        emit({"type": "status", "message": "Thinking…"})
        emit(
            {
                "type": "tool",
                "status": "start",
                "name": "get_entity",
                "summary": "sensor.comfort_system_status",
            }
        )
        emit({"type": "tool", "status": "end", "name": "get_entity"})
        emit({"type": "text", "text": "Your system shows **Trouble**."})

    app.extensions["cytech_agent"].run = fake_run

    response = client.post("/api/chat", json={"message": "Is my alarm ok?"})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert "event: text" in body
    assert "Your system shows **Trouble**." in body
    assert "event: tool" in body
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"


def test_chat_rejects_cross_origin(client):
    response = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_chat_rejects_cross_site_fetch(client):
    response = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_chat_accepts_matching_origin(client, app):
    app.extensions["cytech_agent"].run = lambda session, text, emit: None
    response = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200


def test_chat_accepts_panel_header_behind_proxy(client, app):
    app.extensions["cytech_agent"].run = lambda session, text, emit: None
    response = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={
            "Origin": "https://homeassistant.local",
            "X-Cytech-Client": "1",
        },
    )
    assert response.status_code == 200


def test_chat_rejects_empty_message(client):
    response = client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 400
    assert "question" in response.get_json()["error"].lower()


def test_chat_rejects_long_message(client):
    response = client.post("/api/chat", json={"message": "x" * 4001})
    assert response.status_code == 400


def test_chat_requires_installation_token(tmp_path):
    app = server.create_app(make_settings(tmp_path, installation_token=""), ha=object())
    response = app.test_client().post("/api/chat", json={"message": "hi"})

    assert response.status_code == 400
    assert "not set up" in response.get_json()["error"]


def test_chat_requires_home_assistant(tmp_path):
    app = server.create_app(make_settings(tmp_path, ha_token=""), ha=object())
    response = app.test_client().post("/api/chat", json={"message": "hi"})

    assert response.status_code == 400
    assert "Home Assistant" in response.get_json()["error"]


def test_reset_rotates_session(client, app):
    client.get("/")
    store = app.extensions["cytech_sessions"]
    before = len(store._items)

    response = client.post("/api/reset")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert "cytech_sid=" in response.headers.get("Set-Cookie", "")
    assert len(store._items) <= before + 1


def test_usage_relays_installation_token(client, monkeypatch):
    calls = {}

    def fake_get(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["timeout"] = timeout
        return FakeResp({"limits": {"requests": 300}, "used": {"requests": 1}})

    monkeypatch.setattr(server.requests, "get", fake_get)

    response = client.get("/api/usage")

    assert response.status_code == 200
    assert response.get_json()["used"]["requests"] == 1
    assert calls["url"] == "http://proxy.test/api/usage"
    assert calls["headers"]["x-api-key"] == "cyc_testtoken1234567890"


def test_usage_unreachable(client, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise server.requests.RequestException("boom")

    monkeypatch.setattr(server.requests, "get", fake_get)

    response = client.get("/api/usage")

    assert response.status_code == 502
    assert "cannot be reached" in response.get_json()["error"]


def test_usage_not_configured(tmp_path):
    app = server.create_app(make_settings(tmp_path, installation_token=""), ha=object())
    response = app.test_client().get("/api/usage")
    assert response.status_code == 400


class FakeBlock:
    def model_dump(self):
        return {"type": "text", "text": "hi"}


def test_session_store_round_trips_to_disk(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = server.SessionStore(path=path)
    sid, session = store.create()
    session.messages.append({"role": "user", "content": "hello"})
    session.messages.append({"role": "assistant", "content": [FakeBlock()]})
    session.turns = 1
    store.save()

    restored = server.SessionStore(path=path)
    saved = restored.get(sid)

    assert saved is not None
    assert saved.messages[0] == {"role": "user", "content": "hello"}
    assert saved.messages[1]["content"] == [{"type": "text", "text": "hi"}]
    assert saved.turns == 1


def test_session_store_remove_is_persisted(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = server.SessionStore(path=path)
    sid, _session = store.create()
    store.save()
    store.remove(sid)
    store.save()

    assert server.SessionStore(path=path).get(sid) is None


def test_session_store_prunes_expired_conversations(tmp_path):
    path = tmp_path / "sessions.json"
    stale = time.time() - server.COOKIE_MAX_AGE - 60
    path.write_text(
        json.dumps(
            {"gone": {"messages": [], "turns": 0, "last_used": stale}}
        ),
        encoding="utf-8",
    )

    store = server.SessionStore(path=str(path))

    assert store.get("gone") is None


def test_chat_saves_conversation(tmp_path):
    settings = make_settings(tmp_path)
    app = server.create_app(settings, ha=object())

    def fake_run(session, text, emit):
        session.messages.append({"role": "user", "content": text})
        emit({"type": "text", "text": "ok"})

    app.extensions["cytech_agent"].run = fake_run

    response = app.test_client().post("/api/chat", json={"message": "hello"})
    response.get_data()

    saved = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    contents = [
        message["content"]
        for session in saved.values()
        for message in session["messages"]
    ]
    assert "hello" in contents


def test_healthz(client):
    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["configured"] is True
    assert payload["home_assistant"] is True
