"""Scalar display metadata for bounded interpreters; saved ledgers stay compatible."""
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator

from cadenza_contracts.ledger import Assumption

DisplayText = Annotated[str, Field(max_length=500)]


class IntentAssumption(Assumption):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    field: DisplayText
    value: DisplayText | int | float | bool | None
    basis: DisplayText

    @field_validator("value")
    @classmethod
    def bounded_display_value(cls, value):
        if len(str(value)) > 500:
            raise ValueError("Assumption display value is too long")
        return value
