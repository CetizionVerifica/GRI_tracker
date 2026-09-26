"""catalog: Pydantic schemas for the API and for the YAML seed files."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.modules.catalog.models import (
    NUMERIC_TYPES,
    TENANT_CODE_PREFIX,
    DataType,
    Requirement,
)
from app.modules.catalog.units import validate_unit

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
_CODE_PATTERN = r"^[a-z0-9]+([._-][a-z0-9]+)*$"


def _not_tenant_code(code: str) -> str:
    if code.startswith(TENANT_CODE_PREFIX):
        raise ValueError(f"codes starting with {TENANT_CODE_PREFIX!r} are reserved for tenants")
    return code


# Codes of global (seeded) rows.
Code = Annotated[
    str,
    StringConstraints(pattern=_CODE_PATTERN, max_length=128),
    AfterValidator(_not_tenant_code),
]
TenantCode = Annotated[
    str,
    StringConstraints(pattern=r"^custom\.[a-z0-9]+([._-][a-z0-9]+)*$", max_length=128),
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- validation rules (shared by the API and the seed files)


class ValidationRules(Input):
    """Rules a collected value must satisfy. Which rules apply depends on the data type.

    Decimal bounds must be given as strings or integers, never floats, so they stay exact.
    """

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    max_length: int | None = Field(default=None, gt=0, le=100_000)
    choices: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] | None = None

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("give decimal bounds as strings (e.g. '0.5'), not floats")
        return value

    @field_validator("choices")
    @classmethod
    def _unique_choices(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or len(set(value)) != len(value)):
            raise ValueError("choices must be a non-empty list without duplicates")
        return value

    def check_for(self, data_type: DataType) -> None:
        """Raise ValueError if these rules don't fit the data type."""
        allowed = {
            DataType.DECIMAL: {"minimum", "maximum"},
            DataType.INTEGER: {"minimum", "maximum"},
            DataType.TEXT: {"max_length"},
            DataType.CHOICE: {"choices"},
            DataType.BOOLEAN: set(),
            DataType.DATE: set(),
        }[data_type]
        used = {name for name, value in self if value is not None}
        if extra := used - allowed:
            raise ValueError(f"rules {sorted(extra)} do not apply to {data_type} metrics")
        if data_type == DataType.CHOICE and self.choices is None:
            raise ValueError("choice metrics need choices")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        if data_type == DataType.INTEGER and any(
            bound is not None and bound != bound.to_integral_value()
            for bound in (self.minimum, self.maximum)
        ):
            raise ValueError("integer metrics need whole-number bounds")

    def as_json(self) -> dict[str, Any]:
        """JSON for storage. Decimals become strings, so no precision is lost."""
        return self.model_dump(mode="json", exclude_none=True)


def check_metric_shape(data_type: DataType, unit: str | None, rules: ValidationRules) -> str | None:
    """Validate unit and rules against the data type. Returns the cleaned unit."""
    if data_type in NUMERIC_TYPES:
        if unit is None:
            raise ValueError(f"{data_type} metrics need a unit")
        unit = validate_unit(unit)
    elif unit is not None:
        raise ValueError(f"{data_type} metrics have no unit")
    rules.check_for(data_type)
    return unit


# --- API: standards


class DisclosureOut(Output):
    id: UUID
    code: str
    title: str
    effective_until: date | None


class StandardOut(Output):
    id: UUID
    code: str
    title: str
    version: str
    effective_date: date
    effective_until: date | None


class StandardDetail(StandardOut):
    disclosures: list[DisclosureOut]


# --- API: dimensions


class DimensionValueCreate(Input):
    code: TenantCode
    label: Name
    sort_order: int = 0


class DimensionValueUpdate(Input):
    label: Name | None = None
    sort_order: int | None = None
    retired: bool | None = None


class DimensionValueOut(Output):
    id: UUID
    dimension_id: UUID
    code: str
    label: str
    sort_order: int
    is_custom: bool
    retired_at: datetime | None


class DimensionCreate(Input):
    code: TenantCode
    name: Name
    values: list[DimensionValueCreate] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _unique_value_codes(self) -> Self:
        codes = [v.code for v in self.values]
        if len(set(codes)) != len(codes):
            raise ValueError("value codes must be unique")
        return self


class DimensionOut(Output):
    id: UUID
    code: str
    name: str
    is_custom: bool
    values: list[DimensionValueOut]


# --- API: metrics


class MetricDimensionIn(Input):
    dimension_id: UUID
    is_required: bool = False


class MetricCreate(Input):
    disclosure_id: UUID
    code: TenantCode
    name: Name
    description: Description | None = None
    data_type: DataType
    unit: str | None = None
    requirement: Requirement
    validation_rules: ValidationRules = Field(default_factory=ValidationRules)
    dimensions: list[MetricDimensionIn] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        self.unit = check_metric_shape(self.data_type, self.unit, self.validation_rules)
        ids = [d.dimension_id for d in self.dimensions]
        if len(set(ids)) != len(ids):
            raise ValueError("a dimension can be attached only once")
        return self


class MetricUpdate(Input):
    """Descriptive fields only. Type, unit, rules and dimensions never change."""

    name: Name | None = None
    description: Description | None = None
    requirement: Requirement | None = None
    retired: bool | None = None


class MetricDimensionOut(Output):
    dimension_id: UUID
    code: str
    name: str
    is_required: bool


class MetricOut(Output):
    id: UUID
    disclosure_id: UUID
    code: str
    name: str
    description: str | None
    data_type: DataType
    unit: str | None
    requirement: Requirement
    validation_rules: dict[str, Any]
    is_custom: bool
    retired_at: datetime | None
    dimensions: list[MetricDimensionOut]


# --- seed files (backend/catalog/*.yaml)


class SeedDimensionValue(Input):
    code: Code
    label: Name
    retired: bool = False


class SeedDimension(Input):
    code: Code
    name: Name
    values: list[SeedDimensionValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_value_codes(self) -> Self:
        codes = [v.code for v in self.values]
        if len(set(codes)) != len(codes):
            raise ValueError(f"dimension {self.code}: value codes must be unique")
        return self


class SeedMetricDimension(Input):
    code: Code
    required: bool = False


class SeedMetric(Input):
    code: Code
    name: Name
    description: Description | None = None
    data_type: DataType
    unit: str | None = None
    requirement: Requirement
    validation: ValidationRules = Field(default_factory=ValidationRules)
    dimensions: list[SeedMetricDimension] = Field(default_factory=list)
    retired: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Self:
        self.unit = check_metric_shape(self.data_type, self.unit, self.validation)
        codes = [d.code for d in self.dimensions]
        if len(set(codes)) != len(codes):
            raise ValueError(f"metric {self.code}: a dimension can be attached only once")
        return self


class SeedDisclosure(Input):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    title: Name
    effective_until: date | None = None
    metrics: list[SeedMetric] = Field(default_factory=list)


class SeedStandard(Input):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    title: Name
    version: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    effective_date: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.effective_until is not None and self.effective_until < self.effective_date:
            raise ValueError(f"{self.code}: effective_until is before effective_date")
        return self


class CatalogFile(Input):
    """One seed file: global dimensions, and/or one standard version with its disclosures."""

    standard: SeedStandard | None = None
    disclosures: list[SeedDisclosure] = Field(default_factory=list)
    dimensions: list[SeedDimension] = Field(default_factory=list)

    @model_validator(mode="after")
    def _disclosures_need_standard(self) -> Self:
        if self.disclosures and self.standard is None:
            raise ValueError("disclosures need a standard")
        codes = [d.code for d in self.disclosures]
        if len(set(codes)) != len(codes):
            raise ValueError("disclosure codes must be unique within a standard")
        if self.standard is not None:
            for disclosure in self.disclosures:
                until = disclosure.effective_until
                if until is not None and until < self.standard.effective_date:
                    raise ValueError(
                        f"disclosure {disclosure.code}: effective_until is before the standard's"
                        " effective_date"
                    )
        return self
