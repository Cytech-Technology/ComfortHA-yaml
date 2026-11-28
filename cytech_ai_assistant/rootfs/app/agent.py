"""Tool Runner agent loop: history, limits, and friendly errors."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import anthropic

import prompt
import tools
from config import MAX_CLOCK_SECONDS, MAX_TURNS, Settings
from ha_client import HAClient
from redact import redact

log = logging.getLogger(__name__)

MAX_TOKENS = 4096
MAX_RUNNER_ITERATIONS = 8
MAX_PAUSE_RESTARTS = 2


class UserError(RuntimeError):
    """An error message that is safe to show in the panel."""


@dataclass
class Session:
    messages: list = field(default_factory=list)
    turns: int = 0
    last_used: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Agent:
    def __init__(self, settings: Settings, ha: HAClient) -> None:
        self.settings = settings
        self.ha = ha
        self._client = anthropic.Anthropic(
            api_key=settings.installation_token or "unconfigured",
            base_url=settings.proxy_url,
            max_retries=0,
            timeout=float(MAX_CLOCK_SECONDS),
        )

    # ---- public entry point -------------------------------------------

    def run(self, session: Session, user_text: str, emit) -> None:
        if not self.settings.configured:
            raise UserError(
                "This assistant is not set up yet. Ask your installer for an "
                "installation token and add it in the add-on configuration."
            )

        with session.lock:
            before = list(session.messages)

            if session.turns >= MAX_TURNS:
                session.messages = []
                session.turns = 0
                before = []
                emit(
                    {
                        "type": "status",
                        "message": "This conversation reached its length limit, "
                        "so a new conversation was started.",
                    }
                )

            session.messages.append({"role": "user", "content": user_text})
            session.turns += 1
            tools.begin_turn(self.ha, emit)
            started = time.monotonic()
            attempts = 0

            while True:
                try:
                    last, usage = self._run_turn(session, emit, started)
                    if usage is not None:
                        emit({"type": "usage", **usage})
                    return
                except UserError:
                    session.messages = before
                    raise
                except anthropic.APIError as exc:
                    session.messages = before
                    raise UserError(self._friendly_error(exc)) from exc
                except ValueError:
                    if attempts == 0:
                        attempts = 1
                        session.messages = list(before)
                        session.messages.append(
                            {"role": "user", "content": user_text}
                        )
                        tools.begin_turn(self.ha, emit)
                        emit(
                            {
                                "type": "status",
                                "message": "Retrying that answer…",
                            }
                        )
                        continue
                    session.messages = before
                    log.warning("tool input parsing failed twice")
                    raise UserError(
                        "The assistant could not format a tool request. Please "
                        "rephrase the question and try again."
                    )
                except Exception:
                    session.messages = before
                    log.exception("agent run failed")
                    raise UserError(
                        "Something went wrong while answering. Please try again."
                    )

    # ---- one turn (restarts on pause_turn) ----------------------------

    def _run_turn(self, session: Session, emit, started: float):
        restarts = 0
        usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        while True:
            runner = self._build_runner(session.messages)
            last = None

            for message in runner:
                self._check_clock(started)
                last = message
                session.messages.append(
                    {"role": "assistant", "content": message.content}
                )
                self._emit_text(message, emit)
                self._add_usage(usage_totals, message)

                has_tool_use = any(
                    getattr(block, "type", None) == "tool_use"
                    for block in message.content
                )
                if has_tool_use and message.stop_reason in (
                    "max_tokens",
                    "refusal",
                ):
                    # Never let the runner execute a tool call from a truncated
                    # or refused turn: the input may be incomplete or unsafe.
                    session.messages.pop()
                    runner.close()
                    if message.stop_reason == "max_tokens":
                        raise UserError(
                            "The answer was too long to finish. Please ask a "
                            "narrower question."
                        )
                    raise UserError(
                        "The assistant could not complete that request. Please "
                        "rephrase the question and try again."
                    )

                response = runner.generate_tool_call_response()
                if response is not None:
                    session.messages.append(response)

            if last is None:
                raise UserError(
                    "The assistant did not return an answer. Please try again."
                )
            if last.stop_reason == "pause_turn":
                if restarts >= MAX_PAUSE_RESTARTS:
                    emit(
                        {
                            "type": "status",
                            "message": "The assistant could not finish this "
                            "answer. Please try a narrower question.",
                        }
                    )
                    return last, usage_totals
                restarts += 1
                continue
            return last, usage_totals

    def _build_runner(self, messages: list):
        options = {}
        if self.settings.model == "claude-sonnet-5-5":
            options["thinking"] = {"type": "adaptive"}
            options["output_config"] = {"effort": "medium"}
        return self._client.beta.messages.tool_runner(
            model=self.settings.model,
            max_tokens=MAX_TOKENS,
            system=prompt.system_blocks(),
            tools=tools.TOOLS,
            messages=messages,
            max_iterations=MAX_RUNNER_ITERATIONS,
            # Stateless: send the full history on every request instead of
            # relying on Anthropic's server-held message-thread beta. Every
            # Anthropic-compatible upstream then sees an ordinary Messages
            # API call, at the cost of resending a short history per turn.
            thread=False,
            **options,
        )

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _check_clock(started: float) -> None:
        if time.monotonic() - started > MAX_CLOCK_SECONDS:
            raise UserError(
                "This is taking longer than it should, so it was stopped. "
                "Please try a narrower question."
            )

    @staticmethod
    def _emit_text(message, emit) -> None:
        for block in message.content:
            if getattr(block, "type", None) == "text":
                text = redact(getattr(block, "text", "") or "")
                if text.strip():
                    emit({"type": "text", "text": text})

    @staticmethod
    def _add_usage(totals: dict, message) -> None:
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        totals["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        totals["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        totals["cache_read_tokens"] += int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        totals["cache_creation_tokens"] += int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        if isinstance(exc, anthropic.AuthenticationError):
            return (
                "The AI installation token is not valid. Ask your installer "
                "for a new token and add it in the add-on configuration."
            )
        if isinstance(exc, anthropic.PermissionDeniedError):
            return (
                "AI access for this installation has been revoked. Contact "
                "Cytech support or your installer."
            )
        if isinstance(exc, anthropic.RateLimitError):
            return _quota_message(exc)
        if isinstance(exc, anthropic.APIConnectionError):
            return (
                "The AI service cannot be reached right now. Check that this "
                "Home Assistant has internet access and try again."
            )
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code >= 500:
                return (
                    "The AI service is temporarily unavailable. Please try "
                    "again in a few minutes."
                )
            return (
                "The AI service rejected the request. Please try again; if it "
                "keeps happening, contact Cytech support."
            )
        return "The AI service returned an unexpected error. Please try again."


def _quota_message(exc: anthropic.RateLimitError) -> str:
    text = "The monthly AI allowance for this installation is used up."
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    retry_after = headers.get("retry-after")
    try:
        seconds = int(retry_after)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        # The proxy truncates the countdown to whole seconds, so round up to
        # the next second to land on the period boundary rather than just
        # before it (which would show the previous day).
        reset = datetime.now(timezone.utc) + timedelta(seconds=seconds + 1)
        text += f" It resets on {reset.day} {reset.strftime('%B %Y')} (UTC)."
    else:
        text += " It resets on the first day of next month (UTC)."
    return text + " Contact Cytech support if you need a higher allowance."
