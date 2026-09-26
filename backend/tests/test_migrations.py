import pytest
from alembic import command

from tests.conftest import alembic_config

pytestmark = pytest.mark.integration


def test_downgrade_to_base_and_back(migrated_postgres_url: str) -> None:
    config = alembic_config(migrated_postgres_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
