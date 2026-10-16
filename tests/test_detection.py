from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.config.baseline import load_baseline
from backend.app.config.policy import load_policy
from backend.app.core.detection import DriftDetector, compare_structures
from backend.app.core.fingerprinting import fingerprint_state
from backend.app.schemas.fleet import CollectionFailure, FleetCollection, ServerCollection
from backend.app.schemas.server_state import ServerStateV1

ROOT = Path(__file__).parents[1]


@pytest.fixture
def detector() -> DriftDetector:
    baseline = load_baseline(ROOT / "config" / "baseline.json")
    policy = load_policy(ROOT / "config" / "policies.yaml")
    return DriftDetector(baseline, policy)


def make_state(
    server_id: str, *, config: dict | None = None, env: dict | None = None
) -> ServerStateV1:
    raw = {
        "schema_version": "1.0",
        "server_id": server_id,
        "service": "checkout-api",
        "config_version": "v1",
        "config": config
        or {
            "debug_mode": False,
            "max_connections": 200,
            "log_level": "INFO",
            "tls_min_version": "TLS1.2",
        },
        "environment": env or {"runtime_version": "3.12", "app_version": "1.4.0"},
        "collected_at": datetime(2026, 10, 16, tzinfo=UTC),
    }
    return ServerStateV1.model_validate(raw)


def make_collection(
    *states: ServerStateV1, failed: tuple[str, str] | None = None
) -> FleetCollection:
    servers = [
        ServerCollection(
            server_id=state.server_id,
            status="success",
            state=state,
            fingerprint=fingerprint_state(state),
        )
        for state in states
    ]
    if failed:
        servers.append(
            ServerCollection(
                server_id=failed[0],
                status="failed",
                error=CollectionFailure(kind="unreachable", message=failed[1]),
            )
        )
    failed_count = int(failed is not None)
    return FleetCollection(
        collected_at=datetime(2026, 10, 16, tzinfo=UTC),
        status="partial" if failed_count else "complete",
        total=len(servers),
        successful=len(states),
        failed=failed_count,
        servers=servers,
    )


def test_clean_server_and_stable_metadata_are_not_drift(detector: DriftDetector) -> None:
    first = make_state("checkout-01")
    second = first.model_copy(
        update={"server_id": "checkout-02", "collected_at": datetime(2026, 10, 17, tzinfo=UTC)}
    )
    scan = detector.detect(
        make_collection(first, second),
        scan_id="fixed",
        scanned_at=datetime(2026, 10, 17, tzinfo=UTC),
    )
    assert scan.drifted == 0
    assert all(server.outcome == "clean" for server in scan.servers)


def test_debug_violation_is_critical_and_secret_evidence_is_redacted(
    detector: DriftDetector,
) -> None:
    config = {
        "debug_mode": True,
        "max_connections": 200,
        "log_level": "INFO",
        "tls_min_version": "TLS1.2",
    }
    state = make_state("checkout-03", config=config)
    server = detector.detect(make_collection(state)).servers[0]
    assert server.outcome == "drifted"
    debug = next(
        item for item in server.findings if item.evidence.field_path == "config.debug_mode"
    )
    assert debug.assessment.severity == "CRITICAL"
    secret_diff = compare_structures(
        {"config": {"password": "old-value"}},
        {"config": {"password": "dont-leak"}},
    )[0]
    assert secret_diff.actual.value == "[REDACTED]"
    assert "dont-leak" not in secret_diff.model_dump_json()


def test_shared_baseline_drift_still_uses_baseline_truth(detector: DriftDetector) -> None:
    config = {
        "debug_mode": False,
        "max_connections": 300,
        "log_level": "INFO",
        "tls_min_version": "TLS1.2",
    }
    scan = detector.detect(
        make_collection(
            make_state("checkout-01", config=config),
            make_state("checkout-02", config=config),
            make_state("checkout-03"),
        )
    )
    assert scan.drifted == 2
    assert all(server.baseline_drift_detected is True for server in scan.servers[:2])
    assert scan.servers[2].baseline_drift_detected is False
    assert any(server.peer_comparison.is_outlier for server in scan.servers)


def test_environment_change_is_drift_and_policy_violation(detector: DriftDetector) -> None:
    state = make_state("checkout-01", env={"runtime_version": "3.10", "app_version": "1.4.0"})
    result = detector.detect(make_collection(state)).servers[0]
    assert result.outcome == "drifted"
    assert any(
        item.policy_id == "runtime-approved" and item.severity == "HIGH"
        for item in result.policy_violations
    )
    assert (
        next(
            item for item in result.policy_violations if item.policy_id == "runtime-approved"
        ).observed.value
        == "3.10"
    )


def test_unreachable_server_is_failure_not_fabricated_drift(detector: DriftDetector) -> None:
    scan = detector.detect(
        make_collection(make_state("checkout-01"), failed=("checkout-02", "offline"))
    )
    failed = next(item for item in scan.servers if item.server_id == "checkout-02")
    assert failed.outcome == "unable_to_assess"
    assert failed.baseline_drift_detected is None
    assert failed.collection_error.kind == "unreachable"


def test_log_level_change_is_low_severity(detector: DriftDetector) -> None:
    config = {
        "debug_mode": False,
        "max_connections": 200,
        "log_level": "WARNING",
        "tls_min_version": "TLS1.2",
    }
    result = detector.detect(make_collection(make_state("checkout-01", config=config))).servers[0]
    finding = next(
        item for item in result.findings if item.evidence.field_path == "config.log_level"
    )
    assert finding.assessment.severity == "LOW"
