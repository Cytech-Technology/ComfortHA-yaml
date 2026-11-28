import json

from redact import redact


def test_plain_text_is_unchanged():
    text = "sensor.comfort_system_status = Trouble\nZone 3 is open."
    assert redact(text) == text


def test_empty_text():
    assert redact("") == ""


def test_bearer_and_api_keys():
    assert "sk-ant-api03" not in redact("key sk-ant-api03-abcdefghijkl")
    assert "abcdefgh" not in redact("Authorization: Bearer abcdefgh12345678")


def test_installation_token():
    token = "cyc_" + "a" * 40
    assert token not in redact(f"token is {token} here")


def test_password_assignments():
    out = redact("password=hunter2 and api_key: abc123")
    assert "hunter2" not in out
    assert "abc123" not in out
    assert "[redacted]" in out


def test_json_secret_attributes():
    payload = json.dumps({"password": "hunter2", "friendly_name": "Panel"})
    out = redact(payload)
    assert "hunter2" not in out
    assert "Panel" in out


def test_url_credentials():
    out = redact("see https://installer:secretpw@example.com/path")
    assert "secretpw" not in out
    assert "https://[redacted]@example.com/path" in out


def test_long_hex_and_base64():
    hex_value = "0123456789abcdef" * 3
    assert hex_value not in redact(f"id {hex_value} end")

    b64_value = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqaw"
    assert b64_value not in redact(f"data {b64_value} end")


def test_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefg"
    assert jwt not in redact(f"jwt {jwt}")
