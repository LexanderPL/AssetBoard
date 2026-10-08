from __future__ import annotations

import json
import logging
from typing import Any

from assetboard.branding import APP_NAME


class JsonFormatter(logging.Formatter):
    """Format log records as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        return json.dumps(payload, default=str)


def configure_logging(level: str, format_name: str) -> logging.Logger:
    logger = logging.getLogger(APP_NAME.lower())
    logger.setLevel(level)
    logger.propagate = False

    formatter = JsonFormatter() if format_name == "json" else logging.Formatter("%(message)s")
    if logger.handlers:
        logger.handlers[0].setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
