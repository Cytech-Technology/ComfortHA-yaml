"""GET-only Home Assistant Core REST client.

There is deliberately no method for POST/PUT/DELETE anywhere in this add-on:
the assistant observes, it never changes the system.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import websocket

log = logging.getLogger(__name__)


class HAError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HAClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        max_json_bytes: int = 5_000_000,
        max_text_bytes: int = 2_000_000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_json_bytes = max_json_bytes
        self.max_text_bytes = max_text_bytes
        self._token = token
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, params: dict | None = None, raw: bool = False):
        url = f"{self.base_url}/{path.lstrip('/')}"
        limit = self.max_text_bytes if raw else self.max_json_bytes
        try:
            response = self._session.get(
                url, params=params, timeout=self.timeout, stream=True
            )
        except requests.RequestException as exc:
            log.warning("Home Assistant request failed: %s", type(exc).__name__)
            raise HAError(
                "Cannot reach Home Assistant right now. Check that Home "
                "Assistant is running and try again."
            ) from exc

        with response:
            status = response.status_code
            if status in (401, 403):
                raise HAError(
                    "Home Assistant rejected the add-on's access. Restart the "
                    "add-on, or reinstall it so it receives a fresh token.",
                    status,
                )
            if status == 404:
                raise HAError("Home Assistant has no record for that item.", 404)
            if status >= 400:
                raise HAError(
                    f"Home Assistant returned an error (HTTP {status}).", status
                )

            chunks = bytearray()
            truncated = False
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                chunks.extend(chunk)
                if len(chunks) >= limit:
                    truncated = True
                    break

        if raw:
            text = chunks.decode("utf-8", errors="replace")
            if truncated:
                text += "\n[output truncated]"
            return text

        try:
            return json.loads(chunks.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise HAError("Home Assistant returned an unexpected response.") from exc

    # ---- read-only endpoints used by the tools -------------------------

    def api_status(self) -> dict:
        return self._get("")

    def config(self) -> dict:
        return self._get("config")

    def states(self) -> list:
        return self._get("states")

    def state(self, entity_id: str) -> dict:
        return self._get(f"states/{quote(entity_id, safe='._')}")

    def history(self, entity_id: str, hours: int) -> list:
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        start_text = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get(
            f"history/period/{quote(start_text, safe='')}",
            params={
                "filter_entity_id": entity_id,
                "minimal_response": 1,
                "no_attributes": 1,
            },
        )

    def error_log(self) -> str:
        """Return recent warnings and errors recorded by Home Assistant.

        Home Assistant OS (Supervisor) stopped writing home-assistant.log by
        default, so /api/error_log returns 404 there. Fall back to the
        system_log integration's in-memory entries over the websocket API;
        those are the same recent WARNING/ERROR records the UI shows.
        """
        try:
            return self._get("error_log", raw=True)
        except HAError as exc:
            if exc.status != 404:
                raise
        return self._system_log_entries()

    def _websocket_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base.rstrip("/") + "/websocket"

    def _system_log_entries(self) -> str:
        try:
            ws = websocket.create_connection(
                self._websocket_url(), timeout=self.timeout
            )
        except Exception as exc:
            log.warning("Home Assistant websocket failed: %s", type(exc).__name__)
            raise HAError(
                "Cannot reach Home Assistant right now. Check that Home "
                "Assistant is running and try again."
            ) from exc

        try:
            first = json.loads(ws.recv())
            if isinstance(first, dict) and first.get("type") == "auth_required":
                ws.send(
                    json.dumps(
                        {"type": "auth", "access_token": self._token}
                    )
                )
                auth = json.loads(ws.recv())
                if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
                    raise HAError(
                        "Home Assistant rejected the add-on's access. Restart "
                        "the add-on, or reinstall it so it receives a fresh "
                        "token.",
                        401,
                    )
            ws.send(json.dumps({"id": 1, "type": "system_log/list"}))
            while True:
                message = json.loads(ws.recv())
                if isinstance(message, dict) and message.get("id") == 1:
                    break
            if not message.get("success"):
                raise HAError(
                    "Home Assistant returned an error while reading its log."
                )
            entries = message.get("result")
            if not isinstance(entries, list):
                raise HAError("Home Assistant returned an unexpected log.")
            return self._format_system_log(entries)
        except HAError:
            raise
        except Exception as exc:
            log.warning("Home Assistant log read failed: %s", type(exc).__name__)
            raise HAError(
                "Cannot reach Home Assistant right now. Check that Home "
                "Assistant is running and try again."
            ) from exc
        finally:
            try:
                ws.close()
            except Exception:  # a failed close must not hide the result
                pass

    @staticmethod
    def _format_system_log(entries: list) -> str:
        def order(entry: dict) -> float:
            stamp = entry.get("timestamp")
            return stamp if isinstance(stamp, (int, float)) else 0.0

        lines = []
        for entry in sorted(
            (item for item in entries if isinstance(item, dict)), key=order
        ):
            parts = entry.get("message")
            if isinstance(parts, (list, tuple)) and parts:
                text = str(parts[0])
                args = parts[1:]
                if args:
                    try:
                        text = text % tuple(args)
                    except (TypeError, ValueError):
                        text = " ".join(str(part) for part in parts)
            else:
                text = str(parts or "")

            source = entry.get("source")
            if isinstance(source, (list, tuple)):
                source_text = "/".join(str(part) for part in source)
            else:
                source_text = str(source or "")

            stamp = entry.get("timestamp")
            if isinstance(stamp, (int, float)):
                timestamp = datetime.fromtimestamp(
                    stamp, tz=timezone.utc
                ).isoformat(timespec="seconds")
            else:
                timestamp = str(stamp or "")

            line = (
                f"{timestamp} {entry.get('level') or ''} "
                f"[{source_text}] {text}"
            ).strip()
            exception = entry.get("exception")
            if exception:
                if isinstance(exception, (list, tuple)):
                    exception = "".join(str(part) for part in exception)
                line += "\n" + str(exception)
            lines.append(line)
        return "\n".join(lines)
