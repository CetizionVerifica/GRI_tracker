from decimal import Decimal

import pint
import pytest
from pydantic import ValidationError

from app.modules.catalog.models import DataType
from app.modules.catalog.schemas import ValidationRules, check_metric_shape
from app.modules.catalog.units import unit_registry, validate_unit


@pytest.mark.parametrize(
    "unit",
    ["t", "MWh", "GJ", "m**3", "kg/m**3", "count", "percent", "t CO2e", "kg CO2e", "t CO2e / MWh"],
)
def test_known_units(unit: str) -> None:
    assert validate_unit(f" {unit} ") == unit


@pytest.mark.parametrize("unit", ["", "   ", "furlongs_per_fortnight", "2 kg", "tCO2e"])
def test_unknown_or_malformed_units(unit: str) -> None:
    with pytest.raises(ValueError, match="unit"):
        validate_unit(unit)


def test_bounds_stay_exact_decimals() -> None:
    rules = ValidationRules.model_validate({"minimum": "0.1", "maximum": 5})

    assert rules.minimum == Decimal("0.1")
    assert rules.as_json() == {"minimum": "0.1", "maximum": "5"}


def test_float_bounds_are_rejected() -> None:
    with pytest.raises(ValidationError, match="not floats"):
        ValidationRules.model_validate({"minimum": 0.1})


@pytest.mark.parametrize(
    ("data_type", "rules", "message"),
    [
        (DataType.DECIMAL, {"minimum": "5", "maximum": "1"}, "must not exceed"),
        (DataType.INTEGER, {"minimum": "0.5"}, "whole-number"),
        (DataType.TEXT, {"minimum": "0"}, "do not apply"),
        (DataType.CHOICE, {}, "need choices"),
        (DataType.BOOLEAN, {"max_length": 3}, "do not apply"),
    ],
)
def test_rules_must_fit_the_data_type(
    data_type: DataType, rules: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ValidationRules.model_validate(rules).check_for(data_type)


@pytest.mark.parametrize("choices", [[], ["a", "a"]])
def test_choices_must_be_unique_and_non_empty(choices: list[str]) -> None:
    with pytest.raises(ValidationError):
        ValidationRules.model_validate({"choices": choices})


def test_numeric_metrics_need_a_unit_and_others_must_not_have_one() -> None:
    with pytest.raises(ValueError, match="need a unit"):
        check_metric_shape(DataType.DECIMAL, None, ValidationRules())
    with pytest.raises(ValueError, match="have no unit"):
        check_metric_shape(DataType.TEXT, "kg", ValidationRules())


def test_co2e_converts_between_mass_units() -> None:
    quantity = unit_registry().Quantity(Decimal("1500"), "kg CO2e")

    assert quantity.to("t CO2e").magnitude == Decimal("1.5")


def test_co2e_cannot_be_added_to_plain_mass() -> None:
    registry = unit_registry()

    with pytest.raises(pint.DimensionalityError):
        registry.Quantity(Decimal(1), "t CO2e") + registry.Quantity(Decimal(1), "t")
