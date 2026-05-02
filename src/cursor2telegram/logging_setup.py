"""Logging configuration for cursor2telegram."""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(level: str | int | None = None, json: bool | None = None) -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: numeric or named log level. Defaults to ``CURSOR2TELEGRAM_LOG_LEVEL`` or ``INFO``.
        json: emit JSON if true, console-friendly otherwise. Defaults to JSON when not on a TTY.
    """

    if level is None:
        level = os.environ.get("CURSOR2TELEGRAM_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    if json is None:
        json = not sys.stderr.isatty()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "telegram.request"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]
