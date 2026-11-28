import json

import pytest

from config import (
    DEFAULT_MODEL,
    DEFAULT_PROXY_URL,
    load_settings,
    session_secret,
)

ENV_KEYS = (
    "PROXY_URL",
    "INSTALLATION_TOKEN",
    "MODEL",
    "HA_BASE_URL",
    "HA_TOKEN",
    "SUPERVISOR_TOKEN",
    "PORT",
    "SESSION_SECRET",
    "OPTIONS_PATH",
    "CYTECH_DATA_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CYTECH_DATA_DIR", str(tmp_path / "data"))


def test_defaults_without_options():
    settings = load_settings()

    assert settings.proxy_url == DEFAULT_PROXY_URL
    assert settings.model == DEFAULT_MODEL
    assert settings.installation_token == ""
    assert settings.ha_token == ""
    assert settings.port == 8099
    assert settings.configured is False
    assert settings.is_ha_configured is False


def test_reads_options_file(monkeypatch, tmp_path):
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps(
            {
                "proxy_url": "https://ai.example/",
                "installation_token": " cyc_install_token ",
                "model": "claude-haiku-4-5",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPTIONS_PATH", str(options))

    settings = load_settings()

    assert settings.proxy_url == "https://ai.example"
    assert settings.installation_token == "cyc_install_token"
    assert settings.model == "claude-haiku-4-5"
    assert settings.configured is True


def test_environment_overrides_options(monkeypatch, tmp_path):
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps(
            {
                "installation_token": "cyc_from_file",
                "model": "claude-haiku-4-5",
                "proxy_url": "https://file.example",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPTIONS_PATH", str(options))
    monkeypatch.setenv("INSTALLATION_TOKEN", "cyc_from_env")
    monkeypatch.setenv("MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("PROXY_URL", "https://env.example")

    settings = load_settings()

    assert settings.installation_token == "cyc_from_env"
    assert settings.model == "claude-sonnet-5-5"
    assert settings.proxy_url == "https://env.example"


def test_invalid_values_fall_back(monkeypatch, tmp_path):
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps({"proxy_url": "ftp://bad", "model": "gpt-4"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPTIONS_PATH", str(options))

    settings = load_settings()

    assert settings.proxy_url == DEFAULT_PROXY_URL
    assert settings.model == DEFAULT_MODEL


def test_supervisor_token_grants_ha_access(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
    assert load_settings().is_ha_configured is True


def test_port_parsing(monkeypatch):
    monkeypatch.setenv("PORT", "9001")
    assert load_settings().port == 9001
    monkeypatch.setenv("PORT", "not-a-port")
    assert load_settings().port == 8099


def test_session_secret_persists(tmp_path):
    first = session_secret(tmp_path)
    second = session_secret(tmp_path)

    assert first == second
    assert len(first) == 64
    assert (tmp_path / "session_secret").read_bytes() == first


def test_session_secret_environment_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SECRET", "fixed-secret")
    assert session_secret(tmp_path) == b"fixed-secret"
