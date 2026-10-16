"""Stable, validated scan and deterministic drift-detection response contracts."""

from datetime import datetime
from typing import Any, Literal

from backend.app.config.policy import Severity
from backend.app.schemas.fleet import CollectionFailure, FleetModel


class EvidenceValue(FleetModel):
    state: Literal["value", "missing", "null"]
    value: Any = None
    redacted: bool = False


class FindingEvidence(FleetModel):
    field_path: str
    difference_type: Literal["changed_value", "missing_field", "unexpected_field"]
    expected: EvidenceValue
    actual: EvidenceValue


class FindingAssessment(FleetModel):
    severity: Severity
    policy_violation: bool
    policy_ids: list[str]


class PolicyViolation(FleetModel):
    policy_id: str
    field_path: str
    severity: Severity
    observed: EvidenceValue


class DetectionFinding(FleetModel):
    evidence: FindingEvidence
    assessment: FindingAssessment


class PeerSimilarity(FleetModel):
    peer_server_id: str
    matching_fields: int
    compared_fields: int
    score: float


class PeerExclusion(FleetModel):
    server_id: str
    reason: Literal["collection_failed", "missing_validated_state", "different_service_group"]
    detail: str


class PeerComparison(FleetModel):
    server_id: str
    service_group: str | None
    status: Literal["compared", "insufficient_peers", "unavailable"]
    peer_count: int
    is_outlier: bool
    severity: Severity | None = None
    consensus_size: int
    similarities: list[PeerSimilarity]
    excluded_peers: list[PeerExclusion]
    reason: str | None = None


class DetectionServerResult(FleetModel):
    server_id: str
    service_group: str | None
    collection_status: Literal["success", "failed"]
    outcome: Literal["clean", "drifted", "unable_to_assess"]
    baseline_drift_detected: bool | None
    baseline_fingerprint: str
    observed_fingerprint: str | None
    findings: list[DetectionFinding]
    policy_violations: list[PolicyViolation]
    peer_comparison: PeerComparison
    collection_error: CollectionFailure | None = None
    assessment_error: str | None = None


class DetectionScan(FleetModel):
    scan_id: str
    scanned_at: datetime
    status: Literal["complete", "partial", "failed"]
    detector_version: str
    baseline_id: str
    baseline_version: str
    baseline_fingerprint: str
    policy_version: str
    total: int
    successful: int
    failed: int
    drifted: int
    unable_to_assess: int
    servers: list[DetectionServerResult]
