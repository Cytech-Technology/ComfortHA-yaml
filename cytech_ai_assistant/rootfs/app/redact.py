"""Remove credentials from text before it reaches the model or the UI."""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"
        ),
        "[redacted-jwt]",
    ),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "[redacted-key]"),
    (re.compile(r"\bcyc_[A-Za-z0-9_-]{16,}"), "[redacted-token]"),
    (
        # JSON-style "name": "value" pairs, as found in entity attributes.
        re.compile(
            r'(?i)"(password|passwd|passphrase|api[_-]?key|access[_-]?token|'
            r'refresh[_-]?token|client[_-]?secret|secret|token)"\s*:\s*'
            r'"(?:[^"\\]|\\.)*"'
        ),
        r'"\1": "[redacted]"',
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|passphrase|api[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|client[_-]?secret|secret|token)\b"
            r"\s*[:=]\s*[^\s,;\"']+"
        ),
        r"\1: [redacted]",
    ),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[redacted]@"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[redacted-hex]"),
    (re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}"), "[redacted-base64]"),
)


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
