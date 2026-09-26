"""Create a platform admin from the command line. Operator-only.

    uv run python -m app.modules.tenancy.bootstrap --email ops@example.com --name "Ops Admin"

Prompts for the password so it never lands in shell history.
"""

import argparse
import asyncio
import getpass
import sys

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.db import create_engine, create_session_factory, session_scope
from app.core.errors import AppError
from app.modules.tenancy import service
from app.modules.tenancy.schemas import UserCreate


async def _run(data: UserCreate) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        async for session in session_scope(create_session_factory(engine)):
            user = await service.bootstrap_platform_admin(session, data)
            sys.stdout.write(f"Created platform admin {user.email} ({user.id})\n")
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, help="display name")
    parser.add_argument("--phone", help="E.164, e.g. +919812345678")
    args = parser.parse_args()

    password = getpass.getpass("Password (min 12 characters): ")
    if password != getpass.getpass("Repeat password: "):
        sys.stderr.write("Passwords do not match.\n")
        return 1
    try:
        data = UserCreate(
            email=args.email, display_name=args.name, phone=args.phone, password=password
        )
        asyncio.run(_run(data))
    except (ValidationError, AppError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
