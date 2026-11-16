"""Pydantic request models with exact decimal validation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field, GetCoreSchemaHandler, field_validator
from pydantic_core import core_schema


class DecimalExact:
    """JSON decimal parsed as :class:`Decimal` from text, never from float.

    A JSON number would lose its original digits through binary floating
    point, so float inputs are rejected; callers must send decimal strings
    (e.g. ``"1.003355"``). Integers are accepted and converted exactly.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls.validate,
            json_schema_input_schema=core_schema.str_schema(
                pattern=r"^-?\d+(\.\d+)?$"
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: format(value, "f"), when_used="json"
            ),
        )

    @staticmethod
    def validate(value: Any) -> Decimal:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("must be a decimal string, not a boolean or float")
        if not isinstance(value, (str, int, Decimal)):
            raise ValueError("must be a decimal string")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("is not a valid decimal") from None
        if not parsed.is_finite():
            raise ValueError("must be a finite number")
        return parsed


class MethodBase(BaseModel):
    method_id: str = Field(min_length=1, max_length=128)
    allowed_charges: list[int] = Field(min_length=1, max_length=3)
    error: DecimalExact

    @field_validator("allowed_charges")
    @classmethod
    def _check_charges(cls, values: list[int]) -> list[int]:
        for charge in values:
            if charge < 1 or charge > 3:
                raise ValueError("each allowed charge must be between 1 and 3")
        return values

    @field_validator("error")
    @classmethod
    def _check_error(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("error must be non-negative")
        return value


class MethodCreate(MethodBase):
    pass


class MethodUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    allowed_charges: list[int] = Field(min_length=1, max_length=3)
    error: DecimalExact

    @field_validator("allowed_charges")
    @classmethod
    def _check_charges(cls, values: list[int]) -> list[int]:
        for charge in values:
            if charge < 1 or charge > 3:
                raise ValueError("each allowed charge must be between 1 and 3")
        return values

    @field_validator("error")
    @classmethod
    def _check_error(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("error must be non-negative")
        return value


class PeakIn(BaseModel):
    peak_id: str = Field(min_length=1, max_length=128)
    mz: DecimalExact

    @field_validator("mz")
    @classmethod
    def _positive_mz(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("m/z must be positive")
        return value


class RunBase(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    method_id: str = Field(min_length=1, max_length=128)
    peaks: list[PeakIn] = Field(min_length=1, max_length=12)

    @field_validator("peaks")
    @classmethod
    def _unique_peak_ids(cls, values: list[PeakIn]) -> list[PeakIn]:
        ids = [peak.peak_id for peak in values]
        if len(set(ids)) != len(ids):
            raise ValueError("peak_id values must be unique within a run")
        return values


class RunCreate(RunBase):
    pass


class RunUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    peaks: list[PeakIn] = Field(min_length=1, max_length=12)

    @field_validator("peaks")
    @classmethod
    def _unique_peak_ids(cls, values: list[PeakIn]) -> list[PeakIn]:
        ids = [peak.peak_id for peak in values]
        if len(set(ids)) != len(ids):
            raise ValueError("peak_id values must be unique within a run")
        return values


class AnalysisRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    method_id: str = Field(min_length=1, max_length=128)
    expected_method_revision: int = Field(ge=1)
    expected_run_revision: int = Field(ge=1)
