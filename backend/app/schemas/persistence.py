"""Validated API contracts for persistent inventory, baselines, and scan history."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from backend.app.schemas.detection import DetectionScan
from backend.app.schemas.fleet import FleetModel
from backend.app.schemas.server_state import EnvironmentMetadata, ServerConfig


class ServerInventoryRecord(FleetModel):
    server_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    display_name: str
    role: str | None
    server_type: str | None = None
    lifecycle_status: Literal[
        "newly_enrolled", "pending_baseline", "active", "identity_conflict", "decommissioned"
    ]
    reachability: Literal["unknown", "reachable", "unreachable", "not_configured"]
    status: Literal[
        "newly_enrolled",
        "provisioning",
        "starting",
        "healthy",
        "registered",
        "failed",
        "unavailable",
        "pending_baseline",
        "active",
        "unreachable",
        "identity_conflict",
        "decommissioned",
        "not_configured",
    ]
    first_seen: datetime
    last_seen: datetime | None
    last_attempt: datetime | None
    current_baseline_id: str | None
    current_baseline_version: int | None
    current_baseline_fingerprint: str | None
    decommissioned_at: datetime | None
    managed: bool = False
    template_id: str | None = None
    template_version: str | None = None
    provisioning_status: str | None = None
    health_status: str | None = None
    error_step: str | None = None
    provisioning_error: str | None = None
    container_id: str | None = None
    container_name: str | None = None
    description: str | None = None


class ServerInventoryList(FleetModel):
    items: list[ServerInventoryRecord]
    limit: int
    offset: int


class ServerDetail(ServerInventoryRecord):
    current_baseline: "BaselineRecord | None" = None


class BaselineSnapshot(FleetModel):
    schema_version: Literal["1.0"]
    service_group: str
    config: ServerConfig
    environment: EnvironmentMetadata


class BaselineRecord(FleetModel):
    baseline_id: str
    server_id: str
    version: int
    status: Literal["pending", "approved", "superseded", "rejected"]
    snapshot: BaselineSnapshot
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version: str
    created_at: datetime
    created_by: str
    created_reason: str | None
    approved_at: datetime | None
    approved_by: str | None
    approval_reason: str | None
    is_current: bool = False


class BaselineList(FleetModel):
    server_id: str
    items: list[BaselineRecord]


class CreateBaselineCandidateRequest(FleetModel):
    requested_by: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class ApproveBaselineRequest(FleetModel):
    confirm: Literal[True]
    approved_by: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class DecommissionRequest(FleetModel):
    confirm: Literal[True]


class ScanSummary(FleetModel):
    scan_id: str
    started_at: datetime
    completed_at: datetime
    status: Literal["complete", "partial", "failed"]
    detector_version: str
    total: int
    successful: int
    failed: int
    drifted: int
    unable_to_assess: int
    pending_baseline: int = Field(
        description=(
            "Count of server assessments without an approved baseline, not candidate records."
        )
    )


class ScanList(FleetModel):
    items: list[ScanSummary]
    limit: int
    offset: int


class ScanDetail(FleetModel):
    started_at: datetime
    completed_at: datetime
    result: DetectionScan
