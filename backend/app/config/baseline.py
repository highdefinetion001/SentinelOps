"""Loading and validation for separately approved baseline documents."""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator

from backend.app.schemas.server_state import EnvironmentMetadata, ServerConfig, StrictModel


class ApprovalMetadata(StrictModel):
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    change_reference: str = Field(min_length=1)

    @field_validator("approved_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must include a timezone")
        return value

    @field_validator("approved_by", "change_reference")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approval value must not be blank")
        return value


class BaselineV1(StrictModel):
    baseline_id: str = Field(min_length=1)
    service_group: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    schema_version: Literal["1.0"]
    approval: ApprovalMetadata
    expected_config: ServerConfig
    expected_environment: EnvironmentMetadata
    policy_version: str = Field(min_length=1)


def load_baseline(path: str | Path) -> BaselineV1:
    """Read and validate an approved baseline; state reports cannot update it."""
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read baseline at {path}: {exc}") from exc
    try:
        return BaselineV1.model_validate_json(contents)
    except ValidationError as exc:
        raise ValueError(f"Invalid baseline at {path}: {exc}") from exc
