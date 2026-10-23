"""Structured JSON logging with secret redaction and correlation IDs."""

import json
import logging
import re
from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),  # OpenAI/DeepSeek style keys
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
]
_secrets: set[str] = set()


def register_secrets(values: list[str]) -> None:
    _secrets.update(v for v in values if len(v) >= 8)


def redact(text: str) -> str:
    for s in _secrets:
        text = text.replace(s, "[REDACTED]")
    for p in _PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "correlation_id": correlation_id.get(),
        }
        for key in ("task_id", "provider", "model", "duration_ms", "error_category", "state"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, default=str)


def setup_logging(level: str, secrets: list[str], log_file=None) -> None:
    """log_file: also keep the log on disk (var/agw.log, 5 x 5 MB), so it outlives the terminal."""
    from logging.handlers import RotatingFileHandler

    register_secrets(secrets)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        )
    for h in handlers:
        h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs incl. bot token
