from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.catalog.service import CatalogSeedError, load_catalog
from tests.catalog.conftest import FIXTURES, Catalog, run_seed

pytestmark = pytest.mark.integration


def write(directory: Path, name: str, content: str) -> None:
    (directory / name).write_text(content, encoding="utf-8")


async def count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        value: int = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
    return value


# --- loading (no database)


def test_fixture_catalog_loads() -> None:
    files = load_catalog(FIXTURES)

    assert sorted(f.standard.code for f in files if f.standard) == ["TEST 900"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("standard: [unclosed", "test.yaml"),
        (
            "dimensions: [{code: d, name: D}]\n"
            "standard: {code: S, title: S, version: 1, effective_date: 2020-01-01}",
            "version",
        ),
        ("disclosures: [{code: '1', title: T}]", "disclosures need a standard"),
        ("dimensions: [{code: custom.site, name: Site}]", "reserved for tenants"),
        (
            "dimensions: [{code: d, name: D, values: [{code: a, label: A}, {code: a, label: B}]}]",
            "unique",
        ),
    ],
)
def test_invalid_files_are_rejected(catalog_dir: Path, content: str, message: str) -> None:
    write(catalog_dir, "test.yaml", content)

    with pytest.raises(CatalogSeedError, match=message):
        load_catalog(catalog_dir)


@pytest.mark.parametrize(
    ("metric", "message"),
    [
        ("{code: m, name: M, data_type: decimal, requirement: required}", "need a unit"),
        (
            "{code: m, name: M, data_type: decimal, unit: parsecs_of_joy, requirement: required}",
            "unknown unit",
        ),
        (
            "{code: m, name: M, data_type: decimal, unit: t, requirement: required,"
            " validation: {minimum: 0.5}}",
            "not floats",
        ),
        ("{code: m, name: M, data_type: text, requirement: sometimes}", "requirement"),
        (
            "{code: m, name: M, data_type: text, requirement: optional,"
            " dimensions: [{code: nope}]}",
            "unknown dimension nope",
        ),
        (
            "{code: test900.1.gross, name: M, data_type: text, requirement: optional}",
            "defined twice",
        ),
    ],
)
def test_invalid_metrics_are_rejected(catalog_dir: Path, metric: str, message: str) -> None:
    write(
        catalog_dir,
        "extra.yaml",
        "standard: {code: X 1, title: X, version: '1', effective_date: 2020-01-01}\n"
        f"disclosures: [{{code: '1', title: T, metrics: [{metric}]}}]\n",
    )

    with pytest.raises(CatalogSeedError, match=message):
        load_catalog(catalog_dir)


# --- applying


@pytest.mark.usefixtures("seed")
async def test_seed_creates_everything_then_is_idempotent(
    db_engine: AsyncEngine, migrated_postgres_url: str
) -> None:
    first = await run_seed(migrated_postgres_url)
    second = await run_seed(migrated_postgres_url)

    assert dict(first.created) == {
        "standards": 1,
        "disclosures": 2,
        "metrics": 3,
        "dimensions": 2,
        "dimension values": 2,
    }
    assert (dict(second.created), dict(second.updated)) == ({}, {})
    assert await count(db_engine, "metric_dimension") == 1


async def test_seeded_rules_are_stored_as_exact_strings(
    catalog: Catalog, db_engine: AsyncEngine
) -> None:
    async with db_engine.connect() as conn:
        rules: dict[str, str] = (
            await conn.execute(
                text("SELECT validation_rules FROM metric_definition WHERE id = :id"),
                {"id": catalog.gross},
            )
        ).scalar_one()

    assert rules == {"minimum": "0", "maximum": "1000000.5"}


@pytest.mark.usefixtures("catalog")
async def test_descriptive_changes_update_in_place(
    catalog_dir: Path, db_engine: AsyncEngine, migrated_postgres_url: str
) -> None:
    path = catalog_dir / "test900.yaml"
    path.write_text(
        path.read_text()
        .replace("Gross direct test emissions", "Gross direct test emissions (renamed)")
        .replace("requirement: optional", "requirement: required\n        retired: true")
    )

    report = await run_seed(migrated_postgres_url, catalog_dir)

    assert dict(report.updated) == {"metrics": 2}
    async with db_engine.connect() as conn:
        retired: bool = (
            await conn.execute(
                text("SELECT retired_at IS NOT NULL FROM metric_definition WHERE code = :c"),
                {"c": "test900.1.method"},
            )
        ).scalar_one()
    assert retired is True


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("unit: t", "unit: kg", "unit"),
        ('maximum: "1000000.5"', 'maximum: "2000000"', "validation"),
        ("required: true}]", "required: false}]", "dimensions"),
        ("data_type: integer", "data_type: decimal", "data_type"),
    ],
)
@pytest.mark.usefixtures("catalog")
async def test_structural_changes_are_refused(
    catalog_dir: Path,
    migrated_postgres_url: str,
    old: str,
    new: str,
    field: str,
) -> None:
    path = catalog_dir / "test900.yaml"
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))

    with pytest.raises(CatalogSeedError, match=f"{field}.*cannot change"):
        await run_seed(migrated_postgres_url, catalog_dir)


@pytest.mark.usefixtures("catalog")
async def test_effective_date_cannot_change(catalog_dir: Path, migrated_postgres_url: str) -> None:
    path = catalog_dir / "test900.yaml"
    path.write_text(path.read_text().replace("2021-01-01", "2022-01-01"))

    with pytest.raises(CatalogSeedError, match="effective_date cannot change"):
        await run_seed(migrated_postgres_url, catalog_dir)


@pytest.mark.usefixtures("catalog")
async def test_failed_seed_changes_nothing(
    catalog_dir: Path, db_engine: AsyncEngine, migrated_postgres_url: str
) -> None:
    path = catalog_dir / "test900.yaml"
    path.write_text(
        path.read_text()
        .replace("Test counts", "Test counts (renamed)")
        .replace("unit: count", "unit: kg")
    )

    with pytest.raises(CatalogSeedError):
        await run_seed(migrated_postgres_url, catalog_dir)

    async with db_engine.connect() as conn:
        title: str = (
            await conn.execute(text("SELECT title FROM disclosure WHERE code = '900-2'"))
        ).scalar_one()
    assert title == "Test counts"
