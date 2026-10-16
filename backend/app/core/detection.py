"""Deterministic baseline, policy, and peer comparison for validated fleet results."""

import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from backend.app.config.baseline import BaselineV1
from backend.app.config.policy import (
    DetectionPolicy,
    PolicyRuleViolation,
    Severity,
    policy_violations,
)
from backend.app.core.fingerprinting import (
    canonical_json,
    fingerprint_config_environment,
    fingerprint_state,
)
from backend.app.schemas.detection import (
    DetectionFinding,
    DetectionScan,
    DetectionServerResult,
    EvidenceValue,
    FindingAssessment,
    FindingEvidence,
    PeerComparison,
    PeerExclusion,
    PeerSimilarity,
    PolicyViolation,
)
from backend.app.schemas.fleet import FleetCollection, ServerCollection
from backend.app.schemas.server_state import ServerStateV1

DifferenceType = Literal["changed_value", "missing_field", "unexpected_field"]
_MISSING = object()
_SENSITIVE_PATH = re.compile(
    r"(?i)(?:^|[._-])(secret|password|passwd|token|credential|authorization|auth|api[_-]?key|private[_-]?key)(?:$|[._-])"
)
_SECRET_TEXT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SEVERITY_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
DETECTOR_VERSION = "1.0.0"


class DriftDetector:
    def __init__(self, baseline: BaselineV1, policy: DetectionPolicy) -> None:
        if policy.applies_to.service_group != baseline.service_group:
            raise ValueError("Policy service_group must match the approved baseline service_group")
        self.baseline = baseline
        self.policy = policy
        self.baseline_fingerprint = fingerprint_config_environment(
            baseline.expected_config, baseline.expected_environment
        )

    def detect(
        self,
        collection: FleetCollection,
        *,
        scan_id: str | None = None,
        scanned_at: datetime | None = None,
        approved_baselines: Mapping[str, BaselineV1] | None = None,
        blocked_servers: Mapping[str, str] | None = None,
    ) -> DetectionScan:
        valid_states = {
            result.server_id: result.state
            for result in collection.servers
            if result.status == "success" and result.state is not None
        }
        peers = _peer_comparisons(collection.servers, valid_states, self.policy)
        results = [
            self._detect_server(
                result,
                peers[result.server_id],
                baseline=(
                    approved_baselines.get(result.server_id)
                    if approved_baselines is not None
                    else self.baseline
                ),
                blocked_reason=(blocked_servers or {}).get(result.server_id),
            )
            for result in collection.servers
        ]
        results.sort(key=lambda result: result.server_id)
        return DetectionScan(
            scan_id=scan_id or str(uuid.uuid4()),
            scanned_at=scanned_at or datetime.now(UTC),
            status=collection.status,
            detector_version=DETECTOR_VERSION,
            baseline_id=self.baseline.baseline_id if approved_baselines is None else None,
            baseline_version=self.baseline.baseline_version if approved_baselines is None else None,
            baseline_fingerprint=self.baseline_fingerprint if approved_baselines is None else None,
            policy_version=self.policy.policy_version,
            total=collection.total,
            successful=collection.successful,
            failed=collection.failed,
            drifted=sum(result.outcome == "drifted" for result in results),
            unable_to_assess=sum(result.outcome == "unable_to_assess" for result in results),
            pending_baseline=sum(result.outcome == "pending_baseline" for result in results),
            servers=results,
            baseline_versions={result.server_id: result.baseline_version for result in results},
        )

    def _detect_server(
        self,
        collection: ServerCollection,
        peer_comparison: PeerComparison,
        *,
        baseline: BaselineV1 | None,
        blocked_reason: str | None = None,
    ) -> DetectionServerResult:
        baseline_fingerprint = (
            fingerprint_config_environment(baseline.expected_config, baseline.expected_environment)
            if baseline is not None
            else None
        )
        baseline_id = baseline.baseline_id if baseline is not None else None
        baseline_version = baseline.baseline_version if baseline is not None else None
        state = collection.state
        if blocked_reason:
            return DetectionServerResult(
                server_id=collection.server_id,
                service_group=state.service if state else None,
                collection_status=collection.status,
                outcome="unable_to_assess",
                baseline_drift_detected=None,
                baseline_fingerprint=baseline_fingerprint,
                observed_fingerprint=collection.fingerprint if state else None,
                findings=[],
                policy_violations=[],
                peer_comparison=peer_comparison,
                collection_error=collection.error,
                assessment_error=blocked_reason,
                baseline_id=baseline_id,
                baseline_version=baseline_version,
            )
        if collection.status != "success" or state is None:
            return DetectionServerResult(
                server_id=collection.server_id,
                service_group=None,
                collection_status="failed",
                outcome="unable_to_assess",
                baseline_drift_detected=None,
                baseline_fingerprint=baseline_fingerprint,
                observed_fingerprint=None,
                findings=[],
                policy_violations=[],
                peer_comparison=peer_comparison,
                collection_error=collection.error,
                assessment_error="collection_failed_or_state_unavailable",
                baseline_id=baseline_id,
                baseline_version=baseline_version,
            )

        observed_fingerprint = collection.fingerprint or fingerprint_state(state)
        if baseline is None:
            return DetectionServerResult(
                server_id=collection.server_id,
                service_group=state.service,
                collection_status="success",
                outcome="pending_baseline",
                baseline_drift_detected=None,
                baseline_fingerprint=None,
                observed_fingerprint=observed_fingerprint,
                findings=[],
                policy_violations=[],
                peer_comparison=peer_comparison,
                assessment_error="baseline_pending_approval",
            )
        state_group = state.service
        if state_group != baseline.service_group:
            return DetectionServerResult(
                server_id=collection.server_id,
                service_group=state_group,
                collection_status="success",
                outcome="unable_to_assess",
                baseline_drift_detected=None,
                baseline_fingerprint=baseline_fingerprint,
                observed_fingerprint=observed_fingerprint,
                findings=[],
                policy_violations=[],
                peer_comparison=peer_comparison,
                assessment_error="service_group_mismatch",
                baseline_id=baseline_id,
                baseline_version=baseline_version,
            )
        if baseline.service_group != self.policy.applies_to.service_group:
            return DetectionServerResult(
                server_id=collection.server_id,
                service_group=state_group,
                collection_status="success",
                outcome="unable_to_assess",
                baseline_drift_detected=None,
                baseline_fingerprint=baseline_fingerprint,
                observed_fingerprint=observed_fingerprint,
                findings=[],
                policy_violations=[],
                peer_comparison=peer_comparison,
                assessment_error="policy_unavailable_for_service_group",
                baseline_id=baseline_id,
                baseline_version=baseline_version,
            )
        if baseline.policy_version != self.policy.policy_version:
            return DetectionServerResult(
                server_id=collection.server_id,
                service_group=state_group,
                collection_status="success",
                outcome="unable_to_assess",
                baseline_drift_detected=None,
                baseline_fingerprint=baseline_fingerprint,
                observed_fingerprint=observed_fingerprint,
                findings=[],
                policy_violations=[],
                peer_comparison=peer_comparison,
                assessment_error="policy_version_mismatch",
                baseline_id=baseline_id,
                baseline_version=baseline_version,
            )

        expected = {
            "config": baseline.expected_config.model_dump(mode="json"),
            "environment": baseline.expected_environment.model_dump(mode="json"),
        }
        actual = {
            "config": state.config.model_dump(mode="json"),
            "environment": state.environment.model_dump(mode="json"),
        }
        differences = compare_structures(expected, actual)
        flat_actual = _flatten(actual)
        rule_violations = policy_violations(flat_actual, self.policy, service_group=state_group)
        violations = [
            PolicyViolation(
                policy_id=item.policy_id,
                field_path=item.field_path,
                severity=item.severity,
                observed=_evidence(item.field_path, item.observed_value),
            )
            for item in rule_violations
        ]
        findings = [_assess_finding(diff, rule_violations, self.policy) for diff in differences]
        return DetectionServerResult(
            server_id=collection.server_id,
            service_group=state_group,
            collection_status="success",
            outcome="drifted" if differences else "clean",
            baseline_drift_detected=bool(differences),
            baseline_fingerprint=baseline_fingerprint,
            observed_fingerprint=observed_fingerprint,
            findings=findings,
            policy_violations=violations,
            peer_comparison=peer_comparison,
            baseline_id=baseline_id,
            baseline_version=baseline_version,
        )


def compare_structures(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> list[FindingEvidence]:
    """Return stable, canonical field-level changes, including absent versus explicit null."""
    expected_flat = _flatten(expected)
    actual_flat = _flatten(actual)
    differences: list[FindingEvidence] = []
    for field_path in sorted(set(expected_flat) | set(actual_flat)):
        expected_value = expected_flat.get(field_path, _MISSING)
        actual_value = actual_flat.get(field_path, _MISSING)
        if expected_value is _MISSING:
            difference_type: DifferenceType = "unexpected_field"
        elif actual_value is _MISSING:
            difference_type = "missing_field"
        elif type(expected_value) is type(actual_value) and expected_value == actual_value:
            continue
        else:
            difference_type = "changed_value"
        differences.append(
            FindingEvidence(
                field_path=field_path,
                difference_type=difference_type,
                expected=_evidence(field_path, expected_value),
                actual=_evidence(field_path, actual_value),
            )
        )
    return differences


def _assess_finding(
    evidence: FindingEvidence,
    violations: list[PolicyRuleViolation],
    policy: DetectionPolicy,
) -> DetectionFinding:
    matching = [item for item in violations if item.field_path == evidence.field_path]
    default_severity = getattr(policy.severity_defaults, evidence.difference_type)
    if matching:
        highest = max(_SEVERITY_RANK[item.severity] for item in matching)
        selected = sorted(
            (item for item in matching if _SEVERITY_RANK[item.severity] == highest),
            key=lambda item: item.policy_id,
        )
        severity: Severity = selected[0].severity
    else:
        severity = default_severity
    return DetectionFinding(
        evidence=evidence,
        assessment=FindingAssessment(
            severity=severity,
            policy_violation=bool(matching),
            policy_ids=sorted(item.policy_id for item in matching),
        ),
    )


def _peer_comparisons(
    collection: Sequence[ServerCollection],
    valid_states: dict[str, ServerStateV1 | None],
    policy: DetectionPolicy,
) -> dict[str, PeerComparison]:
    results = {result.server_id: result for result in collection}
    state_groups: dict[str, list[tuple[str, ServerStateV1]]] = {}
    for server_id, state in valid_states.items():
        if state is not None:
            state_groups.setdefault(state.service, []).append((server_id, state))
    for group in state_groups.values():
        group.sort(key=lambda item: item[0])

    comparisons: dict[str, PeerComparison] = {}
    for server_id in sorted(results):
        collection_result = results[server_id]
        state = valid_states.get(server_id)
        if state is None:
            detail = (
                collection_result.error.kind
                if collection_result.error is not None
                else "validated state unavailable"
            )
            comparisons[server_id] = PeerComparison(
                server_id=server_id,
                service_group=None,
                status="unavailable",
                peer_count=0,
                is_outlier=False,
                consensus_size=0,
                similarities=[],
                excluded_peers=[],
                reason=detail,
            )
            continue

        group = state_groups[state.service]
        peers = [(peer_id, peer) for peer_id, peer in group if peer_id != server_id]
        exclusions = [
            PeerExclusion(
                server_id=other_id,
                reason="collection_failed"
                if other.status == "failed"
                else "missing_validated_state",
                detail=(
                    other.error.kind if other.error is not None else "validated state unavailable"
                ),
            )
            for other_id, other in sorted(results.items())
            if other_id != server_id
            and (other.status != "success" or valid_states.get(other_id) is None)
        ]
        exclusions.extend(
            PeerExclusion(
                server_id=other_id,
                reason="different_service_group",
                detail=f"Server belongs to service group {other_state.service!r}.",
            )
            for other_id, other_state in sorted(
                (item for item in valid_states.items() if item[1] is not None),
                key=lambda item: item[0],
            )
            if other_id != server_id and other_state.service != state.service
        )

        similarities = [_similarity(state, peer_id, peer_state) for peer_id, peer_state in peers]
        signature_counts = Counter(_signature(peer_state) for _, peer_state in group)
        own_count = signature_counts[_signature(state)]
        consensus_size = max(signature_counts.values())
        is_outlier = (
            len(group) >= 3 and consensus_size > len(group) / 2 and own_count < consensus_size
        )
        if len(peers) < 2:
            status: Literal["compared", "insufficient_peers", "unavailable"] = "insufficient_peers"
            reason = "At least two comparable peers are required to identify an outlier."
        else:
            status = "compared"
            reason = None
        if is_outlier:
            similarities.sort(key=lambda item: (item.score, item.peer_server_id))
        else:
            similarities.sort(key=lambda item: item.peer_server_id)
        comparisons[server_id] = PeerComparison(
            server_id=server_id,
            service_group=state.service,
            status=status,
            peer_count=len(peers),
            is_outlier=is_outlier,
            severity=policy.severity_defaults.peer_outlier if is_outlier else None,
            consensus_size=consensus_size,
            similarities=similarities,
            excluded_peers=sorted(exclusions, key=lambda item: item.server_id),
            reason=reason,
        )
    return comparisons


def _similarity(state: ServerStateV1, peer_id: str, peer: ServerStateV1) -> PeerSimilarity:
    current = _flatten(
        {
            "config": state.config.model_dump(mode="json"),
            "environment": state.environment.model_dump(mode="json"),
        }
    )
    other = _flatten(
        {
            "config": peer.config.model_dump(mode="json"),
            "environment": peer.environment.model_dump(mode="json"),
        }
    )
    compared = sorted(set(current) | set(other))
    matching = sum(
        field in current
        and field in other
        and type(current[field]) is type(other[field])
        and current[field] == other[field]
        for field in compared
    )
    return PeerSimilarity(
        peer_server_id=peer_id,
        matching_fields=matching,
        compared_fields=len(compared),
        score=matching / len(compared) if compared else 1.0,
    )


def _signature(state: ServerStateV1) -> str:
    return canonical_json(
        {
            "config": state.config.model_dump(mode="json"),
            "environment": state.environment.model_dump(mode="json"),
        }
    )


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key in sorted(value):
        path = f"{prefix}.{key}" if prefix else str(key)
        child = value[key]
        if isinstance(child, Mapping):
            flattened.update(_flatten(child, path))
        else:
            flattened[path] = child
    return flattened


def _evidence(field_path: str, value: Any) -> EvidenceValue:
    sensitive = bool(_SENSITIVE_PATH.search(field_path))
    if value is _MISSING:
        return EvidenceValue(state="missing", redacted=sensitive)
    if value is None:
        return EvidenceValue(state="null", redacted=sensitive)
    if sensitive:
        return EvidenceValue(state="value", value="[REDACTED]", redacted=True)
    if isinstance(value, str):
        safe_value, count = _SECRET_TEXT.subn(lambda match: f"{match.group(1)}=[REDACTED]", value)
        return EvidenceValue(state="value", value=safe_value, redacted=bool(count))
    return EvidenceValue(state="value", value=value)
