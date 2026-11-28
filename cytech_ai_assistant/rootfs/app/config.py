"""Add-on settings: /data/options.json with environment overrides for development."""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

VALID_MODELS = ("claude-sonnet-5-5", "claude-haiku-4-5")
DEFAULT_MODEL = VALID_MODELS[0]
DEFAULT_PROXY_URL = "https://ai.cytech.biz"

MAX_MESSAGE_CHARS = 4000
MAX_TURNS = 20
MAX_CLOCK_SECONDS = 90
MAX_TOOL_CALLS = 12
MAX_SESSIONS = 50


def _data_dir() -> Path:
    return Path(os.environ.get("CYTECH_DATA_DIR", "/data"))


def _read_options() -> dict:
    path = os.environ.get("OPTIONS_PATH") or str(_data_dir() / "options.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _normalise_model(value: str | None) -> str:
    value = (value or "").strip()
    if value in VALID_MODELS:
        return value
    if value:
        log.warning("Unknown model %r; falling back to %s", value, DEFAULT_MODEL)
    return DEFAULT_MODEL


def _normalise_url(value: str | None, default: str) -> str:
    value = (value or "").strip() or default
    if not (value.startswith("http://") or value.startswith("https://")):
        log.warning("Invalid proxy_url %r; using %s", value, default)
        return default
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    proxy_url: str
    installation_token: str
    model: str
    ha_base_url: str
    ha_token: str
    port: int
    data_dir: Path
    log_level: str

    @property
    def configured(self) -> bool:
        return bool(self.installation_token)

    @property
    def is_ha_configured(self) -> bool:
        return bool(self.ha_token)


def load_settings() -> Settings:
    options = _read_options()
    data_dir = _data_dir()

    proxy_url = _normalise_url(
        _env_str("PROXY_URL") or options.get("proxy_url"), DEFAULT_PROXY_URL
    )
    token = (
        _env_str("INSTALLATION_TOKEN")
        or str(options.get("installation_token") or "")
    ).strip()
    model = _normalise_model(_env_str("MODEL") or options.get("model"))

    ha_base_url = (
        _env_str("HA_BASE_URL") or "http://supervisor/core/api"
    ).rstrip("/")
    ha_token = _env_str("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")

    try:
        port = int(_env_str("PORT") or 8099)
    except ValueError:
        port = 8099

    return Settings(
        proxy_url=proxy_url,
        installation_token=token,
        model=model,
        ha_base_url=ha_base_url,
        ha_token=ha_token,
        port=port,
        data_dir=data_dir,
        log_level=(_env_str("LOG_LEVEL") or "INFO").upper(),
    )


def session_secret(data_dir: Path) -> bytes:
    """Persist a signing secret under /data so restarts keep cookies valid."""
    from_env = _env_str("SESSION_SECRET")
    if from_env:
        return from_env.encode("utf-8")

    path = data_dir / "session_secret"
    try:
        if path.exists():
            value = path.read_bytes().strip()
            if value:
                return value
    except OSError:
        pass

    value = secrets.token_hex(32).encode("ascii")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        os.chmod(path, 0o600)
    except OSError:
        log.warning(
            "Could not persist the session secret under %s; sessions will not "
            "survive a restart.",
            data_dir,
        )
    return value
