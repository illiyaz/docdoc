import logging
import logging.config
import re

from app.core.settings import get_settings

PII_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    re.compile(r"\b(?:\+1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"),
]


class PIISafeFilter(logging.Filter):
    def _sanitize(self, value: object) -> object:
        if not isinstance(value, str):
            return value

        redacted = value
        for pattern in PII_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._sanitize(record.msg)

        if isinstance(record.args, tuple):
            record.args = tuple(self._sanitize(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._sanitize(value) for key, value in record.args.items()}

        return True


def setup_logging() -> None:
    settings = get_settings()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "pii_safe": {
                    "()": "app.core.logging.PIISafeFilter",
                }
            },
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["pii_safe"],
                }
            },
            "loggers": {
                "": {
                    "handlers": ["console"],
                    "level": settings.log_level.upper(),
                },
                "uvicorn.access": {
                    "handlers": ["console"],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
        }
    )
