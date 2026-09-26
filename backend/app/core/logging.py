"""Structured logging (structlog) and the request ID middleware.

All log records, including those from the standard library (uvicorn, sqlalchemy), go through one
structlog pipeline and are written to stdout, as JSON unless `log_json` is disabled.
"""

import logging
import re
import sys
import time
import uuid
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
# Incoming IDs are echoed into headers and logs, so accept only a short, safe alphabet.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

access_logger = structlog.stdlib.get_logger("app.access")


class _StdoutHandler(logging.Handler):
    """Writes to whatever `sys.stdout` is at emit time, so later redirection is respected."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = _StdoutHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn installs its own handlers; route its records through ours instead.
    # Its access log is replaced by the middleware below, which carries the request ID.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def _request_id_from(scope: Scope) -> str:
    wanted = REQUEST_ID_HEADER.lower().encode("latin-1")
    for key, value in scope["headers"]:
        if key == wanted:
            candidate: str = value.decode("latin-1")
            if _SAFE_REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return str(uuid.uuid4())


class RequestIdMiddleware:
    """Assigns a request ID, binds it to the log context and echoes it in the response.

    Pure ASGI (not BaseHTTPMiddleware) so context variables propagate to handlers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            fields: dict[str, Any] = {
                "method": scope["method"],
                "path": scope["path"],
                "status": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
            access_logger.info("request", **fields)
            structlog.contextvars.clear_contextvars()
