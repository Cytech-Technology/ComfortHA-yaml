"""Five read-only tools exposed to the model.

Everything here reads Home Assistant over GET. There is no write path, and
all model-visible text is bounded and redacted.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Callable

from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from config import MAX_TOOL_CALLS
from ha_client import HAError
from redact import redact

Notify = Callable[[dict], None]

_ha: ContextVar[object | None] = ContextVar("cytech_ha", default=None)
_notify: ContextVar[Notify | None] = ContextVar("cytech_notify", default=None)
_calls: ContextVar[list[int] | None] = ContextVar("cytech_tool_calls", default=None)

_ENTITY_RE = re.compile(r"^[a-z_]{2,40}\.[a-z0-9_]{1,80}$")
_DOMAIN_RE = re.compile(r"^[a-z_]{1,40}$")
_LOG_LINE_RE = re.compile(r"(?i)\b(error|warning|traceback|critical|exception)\b")

MAX_LIST_ROWS = 200
MAX_ENTITY_BYTES = 8_000
MAX_HISTORY_POINTS = 100
MAX_LOG_BYTES = 12_000
MAX_SYSTEM_BYTES = 6_000


def begin_turn(client, notify: Notify | None) -> None:
    """Bind the HA client and event sink for the current worker thread."""
    _ha.set(client)
    _notify.set(notify)
    _calls.set([0])


def _client():
    client = _ha.get()
    if client is None:
        raise ToolError("Home Assistant access is not configured for this add-on.")
    return client


def _event(payload: dict) -> None:
    notify = _notify.get()
    if notify is None:
        return
    try:
        notify(payload)
    except Exception:  # a UI hiccup must not fail the tool
        pass


def _begin(name: str, summary: str) -> None:
    counter = _calls.get()
    if counter is not None:
        counter[0] += 1
        if counter[0] > MAX_TOOL_CALLS:
            raise ToolError(
                "Tool call limit reached for this question. Answer using the "
                "information already gathered and say what you could not check."
            )
    _event({"type": "tool", "status": "start", "name": name, "summary": summary})


def _end(name: str) -> None:
    _event({"type": "tool", "status": "end", "name": name})


def _validate_entity_id(entity_id: str) -> str:
    value = (entity_id or "").strip()
    if not _ENTITY_RE.match(value):
        raise ToolError(
            "That is not a valid Home Assistant entity id. Use the form "
            "'domain.object_id', for example sensor.comfort_system_status."
        )
    return value


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[output truncated]"


def _friendly(state: dict) -> str:
    attributes = state.get("attributes") or {}
    name = attributes.get("friendly_name") or state.get("entity_id", "")
    return str(name)


@beta_tool(name="list_entities")
def list_entities(domain: str = "", name_contains: str = "") -> str:
    """List Home Assistant entities, optionally filtered by domain or name.

    Use this first to discover the exact entity ids on this installation.
    Results are capped at 200 rows.

    Args:
        domain: Optional entity domain such as "sensor", "binary_sensor",
            "alarm_control_panel", "switch", or "button". Empty lists all.
        name_contains: Optional case-insensitive text matched against the
            entity id and friendly name. Empty lists all.
    """
    _begin("list_entities", f"domain={domain or 'any'} name={name_contains or 'any'}")
    try:
        domain = (domain or "").strip().lower()
        if domain and not _DOMAIN_RE.match(domain):
            raise ToolError("The domain filter is not valid.")
        needle = (name_contains or "").strip().lower()[:60]

        try:
            states = _client().states()
        except HAError as exc:
            raise ToolError(str(exc)) from exc

        if not isinstance(states, list):
            raise ToolError("Home Assistant returned an unexpected entity list.")

        matches = []
        for state in states:
            if not isinstance(state, dict):
                continue
            entity_id = str(state.get("entity_id") or "")
            if domain and not entity_id.startswith(domain + "."):
                continue
            if needle and needle not in entity_id.lower() and needle not in _friendly(state).lower():
                continue
            matches.append(
                f"{entity_id} | {state.get('state', 'unknown')} | {_friendly(state)}"
            )

        matches.sort()
        total = len(matches)
        shown = matches[:MAX_LIST_ROWS]
        header = f"{total} matching entities"
        if total > len(shown):
            header += f" (showing the first {len(shown)})"
        if not shown:
            return redact(header + ". Try a different domain or a shorter name filter.")
        return redact(header + ":\n" + "\n".join(shown))
    finally:
        _end("list_entities")


@beta_tool(name="get_entity")
def get_entity(entity_id: str) -> str:
    """Read one entity's current state and attributes.

    Use this for exact values. For Comfort trouble reports always also read
    sensor.comfort_system_status and sensor.comfort_am_status, because
    alarm_control_panel.comfort_alarm shows "disarmed" for Trouble and Alert.

    Args:
        entity_id: Exact entity id, for example
            sensor.comfort_system_status.
    """
    entity_id = _validate_entity_id(entity_id)
    _begin("get_entity", entity_id)
    try:
        try:
            state = _client().state(entity_id)
        except HAError as exc:
            raise ToolError(str(exc)) from exc
        if not isinstance(state, dict):
            raise ToolError("Home Assistant returned an unexpected entity record.")

        attributes = state.get("attributes")
        if isinstance(attributes, dict):
            attributes = {
                key: (value[:500] + "..." if isinstance(value, str) and len(value) > 500 else value)
                for key, value in attributes.items()
            }

        payload = {
            "entity_id": state.get("entity_id", entity_id),
            "state": state.get("state"),
            "friendly_name": _friendly(state),
            "last_changed": state.get("last_changed"),
            "last_updated": state.get("last_updated"),
            "attributes": attributes,
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        return redact(_clip(text, MAX_ENTITY_BYTES))
    finally:
        _end("get_entity")


@beta_tool(name="get_history")
def get_history(entity_id: str, hours: int = 24) -> str:
    """Read recent state changes for one entity.

    Use this to answer "when did it change?" or "has it been like this long?".
    At most 100 recent changes are returned; hours is clamped to 1-168.

    Args:
        entity_id: Exact entity id, for example sensor.cytech_sd_card_status.
        hours: How far back to look, from 1 to 168 hours. Defaults to 24.
    """
    entity_id = _validate_entity_id(entity_id)
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        raise ToolError("hours must be a whole number between 1 and 168.")
    hours = max(1, min(168, hours))
    _begin("get_history", f"{entity_id}, {hours}h")
    try:
        try:
            data = _client().history(entity_id, hours)
        except HAError as exc:
            raise ToolError(str(exc)) from exc

        points: list[dict] = []
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, list):
                    points.extend(item for item in entry if isinstance(item, dict))
                elif isinstance(entry, dict):
                    points.append(entry)

        if not points:
            return redact(
                f"No state changes recorded for {entity_id} in the last {hours} hours."
            )

        points = points[-MAX_HISTORY_POINTS:]
        lines = []
        for point in points:
            timestamp = (
                point.get("last_changed")
                or point.get("last_updated")
                or point.get("lu")
                or "?"
            )
            lines.append(f"{timestamp}  {point.get('state', '?')}")
        body = "\n".join(lines)
        header = (
            f"{entity_id}: {len(points)} recent state change(s) in the last "
            f"{hours} hours (oldest first, latest last):"
        )
        return redact(_clip(header + "\n" + body, MAX_ENTITY_BYTES))
    finally:
        _end("get_history")


@beta_tool(name="get_log_errors")
def get_log_errors(lines: int = 100, pattern: str = "") -> str:
    """Read recent ERROR and WARNING lines from the Home Assistant log.

    Use this when the user reports something failing, restarting, or not
    working. Sensitive values are redacted. Only matching log lines are
    returned, never the whole file.

    Args:
        lines: Maximum matching lines to return, from 20 to 500. Defaults to 100.
        pattern: Optional case-insensitive text to filter for, such as an
            integration or entity name. Empty returns all warnings/errors.
    """
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 100
    lines = max(20, min(500, lines))
    pattern = (pattern or "").strip()[:60]
    _begin("get_log_errors", f"{lines} lines, pattern={pattern or 'any'}")
    try:
        try:
            text = _client().error_log()
        except HAError as exc:
            raise ToolError(str(exc)) from exc

        matches = []
        needle = pattern.lower()
        for line in text.splitlines():
            if needle and needle not in line.lower():
                continue
            if _LOG_LINE_RE.search(line):
                matches.append(line.rstrip())

        if not matches:
            suffix = f" matching {pattern!r}" if pattern else ""
            return redact(
                f"No ERROR or WARNING lines found in the Home Assistant log{suffix}."
            )

        selected = matches[-lines:]
        header = (
            f"{len(selected)} matching log line(s), oldest first. "
            "Stack traces may span several lines."
        )
        return redact(_clip(header + "\n" + "\n".join(selected), MAX_LOG_BYTES))
    finally:
        _end("get_log_errors")


_COMFORT_PREFIXES = (
    "alarm_control_panel.comfort_",
    "sensor.comfort_",
    "binary_sensor.comfort_",
    "sensor.cytech_",
    "binary_sensor.cytech_",
    "button.comfort_",
    "switch.cytech_comfort_mqtt_",
    "number.cytech_comfort_mqtt_",
)


@beta_tool(name="get_system_info")
def get_system_info() -> str:
    """Read a compact overview of this Home Assistant and Comfort system.

    Returns the Home Assistant version, entity count, and the Comfort and
    Cytech system-health entities. Use this at the start of a troubleshooting
    question to see what is installed and what the alarm system reports.
    """
    _begin("get_system_info", "overview")
    try:
        client = _client()
        try:
            config = client.config()
            states = client.states()
        except HAError as exc:
            raise ToolError(str(exc)) from exc

        config = config if isinstance(config, dict) else {}
        states = states if isinstance(states, list) else []
        unit_system = config.get("unit_system") or {}

        lines = [
            f"Home Assistant version: {config.get('version', 'unknown')}",
            f"Timezone: {config.get('time_zone', 'unknown')}",
            f"Temperature unit: {unit_system.get('temperature', 'unknown')}",
            f"Entities: {len(states)}",
            "",
            "Comfort and Cytech entities:",
        ]

        relevant = []
        for state in states:
            if not isinstance(state, dict):
                continue
            entity_id = str(state.get("entity_id") or "")
            if entity_id.startswith(_COMFORT_PREFIXES) or "cytech_comfort_mqtt" in entity_id:
                relevant.append(
                    f"{entity_id} = {state.get('state', 'unknown')} "
                    f"({_friendly(state)})"
                )
        relevant.sort()

        if relevant:
            lines.extend(relevant[:80])
            if len(relevant) > 80:
                lines.append(f"... and {len(relevant) - 80} more Comfort entities.")
        else:
            lines.append(
                "No Comfort entities found. The Comfort add-on may not be installed, "
                "or its entity ids use a different naming scheme."
            )

        return redact(_clip("\n".join(lines), MAX_SYSTEM_BYTES))
    finally:
        _end("get_system_info")


TOOLS = [list_entities, get_entity, get_history, get_log_errors, get_system_info]
