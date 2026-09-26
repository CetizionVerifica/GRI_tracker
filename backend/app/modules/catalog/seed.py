"""Seed the global GRI catalog from YAML. Operator-only; runs as the schema owner.

    uv run python -m app.modules.catalog.seed            # backend/catalog/*.yaml
    uv run python -m app.modules.catalog.seed --dir PATH

All files are validated before anything is written, and everything is applied in one
transaction: either the whole catalog is seeded or nothing changes.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.db import create_engine, create_session_factory, session_scope
from app.modules.catalog.service import CatalogSeedError, SeedReport, apply_catalog, load_catalog
from app.modules.catalog.units import CATALOG_DIR


async def seed(directory: Path, database_url: str) -> SeedReport:
    files = load_catalog(directory)
    engine = create_engine(database_url)
    try:
        # Don't return inside the loop: that would skip session_scope's commit.
        async for session in session_scope(create_session_factory(engine)):
            report = await apply_catalog(session, files)
    finally:
        await engine.dispose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=CATALOG_DIR, help="folder with *.yaml files")
    args = parser.parse_args()
    settings = get_settings()
    try:
        report = asyncio.run(
            seed(args.dir, settings.migration_database_url or settings.database_url)
        )
    except CatalogSeedError as exc:
        sys.stderr.write(f"Catalog not seeded: {exc}\n")
        return 1
    sys.stdout.write(f"Catalog seeded ({report})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
