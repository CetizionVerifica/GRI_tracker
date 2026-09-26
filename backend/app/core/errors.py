"""Application error types and their HTTP mapping as RFC 9457 problem details.

Services raise these; routers never build error responses by hand. Every error response is
`application/problem+json` and carries the request ID so it can be matched to the logs.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import REQUEST_ID_HEADER

PROBLEM_JSON = "application/problem+json"
PROBLEM_TYPE_PREFIX = "urn:gri-kpi:problem:"

logger = structlog.stdlib.get_logger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad-request"
    title: str = "Bad request"
    headers: Mapping[str, str] | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    """Also used for resources in another tenant, so their existence is not revealed."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not-found"
    title = "Not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    title = "Conflict"


class UnauthenticatedError(AppError):
    """Missing, invalid or expired credentials."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    title = "Unauthenticated"
    headers = {"WWW-Authenticate": "Bearer"}  # noqa: RUF012  (read-only class constant)


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission-denied"
    title = "Permission denied"


def _request_id(request: Request) -> str | None:
    request_id: str | None = getattr(request.state, "request_id", None)
    return request_id


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str | None = None,
    extensions: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_PREFIX + code,
        "title": title,
        "status": status_code,
        "instance": request.url.path,
    }
    if detail is not None:
        body["detail"] = detail
    request_id = _request_id(request)
    if request_id is not None:
        body["request_id"] = request_id
    if extensions:
        body.update(extensions)

    response_headers = dict(headers or {})
    if request_id is not None:
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        media_type=PROBLEM_JSON,
        headers=response_headers,
    )


def _code_for_status(status_code: int) -> tuple[str, str]:
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "Error"
    return phrase.lower().replace(" ", "-").replace("'", ""), phrase


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered only for AppError
    return problem_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.message,
        headers=exc.headers,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code, title = _code_for_status(exc.status_code)
    # Starlette's default detail is just the status phrase; don't repeat it.
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    return problem_response(
        request,
        status_code=exc.status_code,
        code=code,
        title=title,
        detail=detail,
        headers=exc.headers,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return problem_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation-error",
        title="Validation error",
        detail="The request is invalid.",
        extensions={"errors": errors},
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Runs outside the request ID middleware, so bind the ID explicitly for this log line.
    logger.error("unhandled_exception", request_id=_request_id(request), exc_info=exc)
    return problem_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal-error",
        title="Internal server error",
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
