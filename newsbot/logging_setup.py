"""Logging with secret redaction. The engine repo is public, so logs must contain
only counts, story IDs and status. Never log prompts, drafts, config or secrets."""
from __future__ import annotations

import logging
import os
import re

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)
_MIN_SECRET_LEN = 6


def _secret_values() -> list[str]:
    return [
        v for k, v in os.environ.items()
        if _SECRET_NAME.search(k) and v and len(v) >= _MIN_SECRET_LEN
    ]


def redact(text: str, secrets: list[str] | None = None) -> str:
    for s in (secrets if secrets is not None else _secret_values()):
        text = text.replace(s, "***")
    # generic bearer/api-key looking tokens
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", text)
    text = re.sub(r"\b(gsk_|sk-|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}", r"\1***", text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("newsbot")
    logger.setLevel(level.upper())
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
