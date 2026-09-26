"""audit: HTTP endpoints. Thin: validate, call the service, return."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
