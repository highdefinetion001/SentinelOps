"""Validated collection and demo-control response contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.server_state import ServerStateV1


class FleetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CollectionFailure(FleetModel):
    kind: Literal[
        "timeout",
        "http_error",
        "invalid_json",
        "invalid_state",
        "response_too_large",
        "identity_conflict",
        "unreachable",
        "request_error",
        "not_configured",
    ]
    message: str
    upstream_status: int | None = None


class ServerCollection(FleetModel):
    server_id: str
    status: Literal["success", "failed"]
    fingerprint: str | None = None
    state: ServerStateV1 | None = None
    display_name: str | None = None
    role: str | None = None
    collector_version: str | None = None
    instance_id: str | None = Field(default=None, exclude=True)
    error: CollectionFailure | None = None


class FleetCollection(FleetModel):
    collected_at: datetime
    status: Literal["complete", "partial", "failed"]
    total: int = Field(ge=0)
    successful: int = Field(ge=0)
    failed: int = Field(ge=0)
    servers: list[ServerCollection]


class DriftRequest(FleetModel):
    scenario: Literal["security_drift", "performance_drift"]


class DriftResponse(FleetModel):
    server_id: str
    scenario: Literal["security_drift", "performance_drift"]
    status: Literal["applied"]


class ResetResponse(FleetModel):
    server_id: str
    status: Literal["reset"]
    verified: Literal[True]
    fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
