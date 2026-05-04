"""
Structured logging configuration.

In Cloud Run, logs written to stdout in JSON are automatically parsed by
Cloud Logging. Locally, we keep it human-readable.
"""
import logging
import sys
import json
import time
from typing import Any

from core.settings import get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Any extras attached via logger.info("msg", extra={"contact_id": ...})
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


_RESERVED_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
    "name", "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


def configure_logging() -> None:
    """Wire up root logging based on Settings. Call once at app startup."""
    settings = get_settings()

    handler = logging.StreamHandler(sys.stdout)
    if settings.is_local:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Quiet down noisy libraries unless explicitly debugging them.
    for noisy in ("uvicorn.access", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
