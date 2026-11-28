import json

import pytest

import ha_client
from ha_client import HAClient, HAError


def make_client(base_url="http://ha.test/api"):
    return HAClient(base_url, "ha-token")


# ---- websocket URL ----------------------------------------------------


def test_websocket_url_from_http_supervisor():
    client = make_client("http://supervisor/core/api")
    assert client._websocket_url() == "ws://supervisor/core/websocket"


def test_websocket_url_from_https_base():
    client = make_client("https://ha.example.com/api")
    assert client._websocket_url() == "wss://ha.example.com/websocket"


def test_websocket_url_without_api_suffix():
    client = make_client("http://ha.test:8123")
    assert client._websocket_url() == "ws://ha.test:8123/websocket"


# ---- system log formatting -------------------------------------------


def test_format_system_log_sorts_and_formats():
    entries = [
        {
            "level": "ERROR",
            "timestamp": 1700000100.0,
            "source": ["custom_components/comfort/coordinator.py", 42],
            "message": ["Zone %s value %d", "2", 5],
            "exception": None,
        },
        {
            "level": "WARNING",
            "timestamp": 1699999000.0,
            "source": ["homeassistant/core.py", 7],
            "message": ["plain text"],
            "exception": "Traceback (most recent call last):",
        },
    ]

    text = HAClient._format_system_log(entries)
    lines = text.splitlines()

    # Oldest first.
    assert lines[0].startswith("2023-11-14T")
    assert "WARNING" in lines[0]
    assert "plain text" in lines[0]
    assert "Traceback" in lines[1]

    error_line = lines[2]
    assert "ERROR" in error_line
    assert "[custom_components/comfort/coordinator.py/42]" in error_line
    assert "Zone 2 value 5" in error_line


def test_format_system_log_survives_bad_message():
    entries = [
        {
            "level": "WARNING",
            "timestamp": 1700000000.0,
            "source": None,
            "message": ["%d %d", 1],
        },
        {"level": "WARNING", "timestamp": 1700000010.0, "message": None},
        {"level": "WARNING", "timestamp": 1700000020.0, "message": ["100% done"]},
    ]

    text = HAClient._format_system_log(entries)

    assert "%d %d 1" in text
    assert "100% done" in text


def test_format_system_log_empty():
    assert HAClient._format_system_log([]) == ""


# ---- error_log fallback ----------------------------------------------


def test_error_log_uses_rest_when_available():
    client = make_client()
    client._get = lambda *args, **kwargs: "rest log"

    assert client.error_log() == "rest log"


def test_error_log_falls_back_on_404():
    client = make_client()

    def raise_404(*args, **kwargs):
        raise HAError("Home Assistant has no record for that item.", 404)

    client._get = raise_404
    client._system_log_entries = lambda: "system log"

    assert client.error_log() == "system log"


def test_error_log_reraises_other_errors():
    client = make_client()

    def raise_403(*args, **kwargs):
        raise HAError("rejected", 403)

    client._get = raise_403

    with pytest.raises(HAError) as info:
        client.error_log()
    assert info.value.status == 403


# ---- websocket log reading -------------------------------------------


class FakeWebsocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def recv(self):
        return json.dumps(self.incoming.pop(0))

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        self.closed = True


def test_system_log_entries_reads_over_websocket(monkeypatch):
    entries = [
        {
            "level": "ERROR",
            "timestamp": 1700000000.0,
            "source": ["a.py", 1],
            "message": ["boom"],
        }
    ]
    fake = FakeWebsocket(
        [
            {"type": "auth_required", "ha_version": "2026.6.4"},
            {"type": "auth_ok", "ha_version": "2026.6.4"},
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": entries,
            },
        ]
    )
    monkeypatch.setattr(
        ha_client.websocket, "create_connection", lambda *a, **k: fake
    )

    text = make_client()._system_log_entries()

    assert fake.sent[0] == {"type": "auth", "access_token": "ha-token"}
    assert fake.sent[1] == {"id": 1, "type": "system_log/list"}
    assert "ERROR" in text and "boom" in text
    assert fake.closed is True


def test_system_log_entries_rejects_bad_auth(monkeypatch):
    fake = FakeWebsocket(
        [
            {"type": "auth_required"},
            {"type": "auth_invalid", "message": "bad token"},
        ]
    )
    monkeypatch.setattr(
        ha_client.websocket, "create_connection", lambda *a, **k: fake
    )

    with pytest.raises(HAError, match="rejected"):
        make_client()._system_log_entries()
    assert fake.closed is True


def test_system_log_entries_reports_api_error(monkeypatch):
    fake = FakeWebsocket(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": False, "error": {}},
        ]
    )
    monkeypatch.setattr(
        ha_client.websocket, "create_connection", lambda *a, **k: fake
    )

    with pytest.raises(HAError):
        make_client()._system_log_entries()


def test_system_log_entries_wraps_connection_failure(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(ha_client.websocket, "create_connection", refuse)

    with pytest.raises(HAError, match="Cannot reach Home Assistant"):
        make_client()._system_log_entries()
