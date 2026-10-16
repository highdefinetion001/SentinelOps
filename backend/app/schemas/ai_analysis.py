"""Validated storage and API contracts for advisory scan explanations."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AIExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format: Literal["structured", "text"] = "structured"
    summary: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    remediation: list[str] = Field(max_length=8)
    prevention: list[str] = Field(max_length=8)

    @field_validator("summary", "impact")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("explanation text must not be blank")
        return value.strip()

    @field_validator("remediation", "prevention")
    @classmethod
    def require_nonblank_actions(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("recommendations must not be blank")
        if any(len(value) > 500 for value in cleaned):
            raise ValueError("recommendation is too long")
        return cleaned


class AIAnalysisRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    analysis_id: str
    scan_id: str
    server_ids: list[str]
    provider: str
    model: str | None
    status: Literal["pending", "completed", "failed", "disabled"]
    explanation: AIExplanation | None
    created_at: datetime
    updated_at: datetime
    error_summary: str | None
