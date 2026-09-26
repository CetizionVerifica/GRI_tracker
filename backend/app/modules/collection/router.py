"""collection: HTTP endpoints. Thin: validate, call the service, return."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/collection", tags=["collection"])
