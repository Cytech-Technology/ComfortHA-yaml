"""Cytech AI Assistant ingress panel."""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, jsonify, make_response, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from agent import Agent, Session, UserError
from config import (
    MAX_MESSAGE_CHARS,
    MAX_SESSIONS,
    Settings,
    load_settings,
    session_secret,
)
from ha_client import HAClient

log = logging.getLogger("server")

COOKIE_NAME = "cytech_sid"
COOKIE_MAX_AGE = 7 * 24 * 3600
_DONE = object()


def _json_default(value):
    """Serialize the SDK's content-block objects for local session storage."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    raise TypeError(f"cannot save {type(value).__name__}")


class SessionStore:
    """Conversations kept in memory, mirrored to /data so a restart keeps them.

    The file holds user questions and answers on this Home Assistant device
    only; it is never sent anywhere. Entries older than the browser cookie
    lifetime are pruned, and New chat deletes the current conversation.
    """

    def __init__(
        self, max_sessions: int = MAX_SESSIONS, path: str | None = None
    ) -> None:
        self._max = max_sessions
        self._path = path
        self._items: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("could not read saved conversations; starting fresh")
            return
        if not isinstance(data, dict):
            return
        cutoff = time.time() - COOKIE_MAX_AGE
        for sid, saved in data.items():
            if not isinstance(sid, str) or not isinstance(saved, dict):
                continue
            messages = saved.get("messages")
            last_used = saved.get("last_used")
            if (
                not isinstance(messages, list)
                or not isinstance(last_used, (int, float))
                or last_used < cutoff
            ):
                continue
            self._items[sid] = Session(
                messages=messages,
                turns=int(saved.get("turns") or 0),
                last_used=float(last_used),
            )
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def save(self) -> None:
        if not self._path:
            return
        cutoff = time.time() - COOKIE_MAX_AGE
        with self._lock:
            snapshot = {
                sid: {
                    "messages": list(session.messages),
                    "turns": session.turns,
                    "last_used": session.last_used,
                }
                for sid, session in self._items.items()
                if session.last_used >= cutoff
            }
        temporary = self._path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(
                    snapshot,
                    handle,
                    ensure_ascii=False,
                    default=_json_default,
                )
            os.replace(temporary, self._path)
        except (OSError, TypeError, ValueError):
            log.warning("could not save conversations")

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            session = self._items.get(sid)
            if session is not None:
                session.last_used = time.time()
                self._items.move_to_end(sid)
            return session

    def create(self) -> tuple[str, Session]:
        sid = secrets.token_urlsafe(24)
        session = Session(last_used=time.time())
        with self._lock:
            self._items[sid] = session
            while len(self._items) > self._max:
                self._items.popitem(last=False)
        return sid, session

    def remove(self, sid: str) -> None:
        with self._lock:
            self._items.pop(sid, None)


def create_app(settings: Settings | None = None, ha: HAClient | None = None) -> Flask:
    settings = settings or load_settings()
    if ha is None and settings.ha_token:
        ha = HAClient(settings.ha_base_url, settings.ha_token)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    store = SessionStore(
        path=os.path.join(str(settings.data_dir), "sessions.json")
    )
    serializer = URLSafeTimedSerializer(
        session_secret(settings.data_dir), salt="cytech-ai-session"
    )
    agent = Agent(settings, ha)

    app.extensions["cytech_settings"] = settings
    app.extensions["cytech_agent"] = agent
    app.extensions["cytech_sessions"] = store
    app.extensions["cytech_serializer"] = serializer

    # ---- helpers ------------------------------------------------------

    def base() -> str:
        return (
            request.headers.get("X-Ingress-Path") or request.script_root or ""
        ).rstrip("/")

    def is_https() -> bool:
        proto = request.headers.get("X-Forwarded-Proto", "")
        return proto.lower() == "https" or request.is_secure

    def resolve_session() -> tuple[str, Session, bool]:
        sid = None
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            try:
                sid = serializer.loads(raw, max_age=COOKIE_MAX_AGE)
            except (BadSignature, SignatureExpired):
                sid = None
        session = store.get(sid)
        if session is not None:
            return sid, session, False
        new_sid, new_session = store.create()
        return new_sid, new_session, True

    def set_session_cookie(response, sid: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            serializer.dumps(sid),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=is_https(),
            path=base() or "/",
        )

    def same_origin() -> bool:
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        # The panel's own JS sets this header. A cross-site page cannot: a
        # custom header on fetch forces a CORS preflight that this app never
        # approves, and an HTML form cannot set headers at all. This also
        # survives Supervisor ingress rewriting the Host header.
        if request.headers.get("X-Cytech-Client") == "1":
            return True
        origin = request.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_host = urlsplit(origin).netloc
        except ValueError:
            return False
        allowed = {request.host}
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded:
            allowed.add(forwarded)
        return origin_host in allowed

    # ---- pages --------------------------------------------------------

    @app.get("/")
    def index():
        sid, _session, is_new = resolve_session()
        response = make_response(
            render_template(
                "index.html",
                base=base(),
                model=settings.model,
                configured=settings.configured,
                ha_configured=settings.is_ha_configured,
            )
        )
        if is_new:
            set_session_cookie(response, sid)
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---- chat ---------------------------------------------------------

    @app.post("/api/chat")
    def chat():
        if not same_origin():
            return jsonify({"error": "Request rejected."}), 403
        if not settings.configured:
            return (
                jsonify(
                    {
                        "error": "This assistant is not set up yet. Ask your "
                        "installer for an installation token."
                    }
                ),
                400,
            )
        if not settings.is_ha_configured:
            return (
                jsonify(
                    {
                        "error": "Home Assistant access is not available to "
                        "this add-on. Restart the add-on; if it persists, "
                        "reinstall it."
                    }
                ),
                400,
            )

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid request."}), 400
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return jsonify({"error": "Please type a question."}), 400
        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            return (
                jsonify(
                    {
                        "error": f"Please keep questions under "
                        f"{MAX_MESSAGE_CHARS} characters."
                    }
                ),
                400,
            )

        sid, session, is_new = resolve_session()

        events: queue.Queue = queue.Queue()
        cancelled = threading.Event()

        def emit(event: dict) -> None:
            if not cancelled.is_set():
                events.put(event)

        def worker() -> None:
            try:
                agent.run(session, message, emit)
            except UserError as exc:
                emit({"type": "error", "message": str(exc)})
            except Exception:
                log.exception("chat worker failed")
                emit(
                    {
                        "type": "error",
                        "message": "Something went wrong while answering. "
                        "Please try again.",
                    }
                )
            finally:
                store.save()
                events.put(_DONE)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        emit({"type": "status", "message": "Thinking…"})

        def generate():
            try:
                while True:
                    event = events.get()
                    if event is _DONE:
                        break
                    yield (
                        f"event: {event['type']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
            except GeneratorExit:
                cancelled.set()
                raise
            finally:
                cancelled.set()

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        if is_new:
            set_session_cookie(response, sid)
        return response

    @app.post("/api/reset")
    def reset():
        if not same_origin():
            return jsonify({"error": "Request rejected."}), 403
        old_sid = None
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            try:
                old_sid = serializer.loads(raw, max_age=COOKIE_MAX_AGE)
            except (BadSignature, SignatureExpired):
                old_sid = None
        if old_sid:
            store.remove(old_sid)
        sid, _session = store.create()
        store.save()
        response = jsonify({"ok": True})
        set_session_cookie(response, sid)
        return response

    # ---- usage and health --------------------------------------------

    @app.get("/api/usage")
    def usage():
        if not settings.configured:
            return jsonify({"error": "Not configured."}), 400
        try:
            upstream = requests.get(
                settings.proxy_url + "/api/usage",
                headers={
                    "x-api-key": settings.installation_token,
                    "Accept": "application/json",
                },
                timeout=10,
            )
        except requests.RequestException:
            return (
                jsonify({"error": "The AI service cannot be reached right now."}),
                502,
            )
        if upstream.status_code != 200:
            return jsonify({"error": "Usage is not available right now."}), 502
        try:
            data = upstream.json()
        except ValueError:
            return jsonify({"error": "Usage is not available right now."}), 502
        if not isinstance(data, dict):
            return jsonify({"error": "Usage is not available right now."}), 502
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz():
        return jsonify(
            {
                "status": "ok",
                "configured": settings.configured,
                "home_assistant": settings.is_ha_configured,
            }
        )

    # ---- response hardening ------------------------------------------

    @app.after_request
    def harden(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' "
            "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'self'",
        )
        return response

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": "Request is too large."}), 413

    return app


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_app(settings)
    log.info(
        "Cytech AI Assistant listening on :%s model=%s configured=%s",
        settings.port,
        settings.model,
        settings.configured,
    )
    app.run(host="0.0.0.0", port=settings.port, threaded=True)


if __name__ == "__main__":
    main()
