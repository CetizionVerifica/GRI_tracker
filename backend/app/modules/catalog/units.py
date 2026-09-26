"""Units for metric definitions, checked with pint.

Extra unit definitions are data, not code: if backend/catalog/units.txt exists, it is loaded on
top of pint's defaults (pint definition syntax).
"""

from functools import cache
from pathlib import Path

import pint

# backend/catalog: the seed files and optional unit definitions. Data, not code.
CATALOG_DIR = Path(__file__).resolve().parents[3] / "catalog"
UNIT_DEFINITIONS = CATALOG_DIR / "units.txt"


@cache
def unit_registry() -> pint.UnitRegistry:
    registry = pint.UnitRegistry()
    if UNIT_DEFINITIONS.exists():
        registry.load_definitions(str(UNIT_DEFINITIONS))
    return registry


def validate_unit(unit: str) -> str:
    """Return the unit as written if pint understands it; raise ValueError otherwise."""
    unit = unit.strip()
    if not unit:
        raise ValueError("unit must not be blank")
    try:
        unit_registry().parse_units(unit)
    except (pint.UndefinedUnitError, pint.DefinitionSyntaxError, ValueError, AttributeError) as exc:
        raise ValueError(f"unknown unit {unit!r}") from exc
    return unit
