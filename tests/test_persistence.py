import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from backend.app.config.baseline import load_baseline
from backend.app.config.policy import load_policy
from backend.app.config.registry import ServerRegistration
from backend.app.core.detection import DriftDetector
from backend.app.core.fingerprinting import fingerprint_state
from backend.app.schemas.fleet import CollectionFailure, FleetCollection, ServerCollection
from backend.app.schemas.server_state import ServerStateV1
from backend.app.storage.sqlite import _SCHEMA, SCHEMA_VERSION, SQLiteDatabase


def make_state(
    server_id: str,
    *,
    max_connections: int = 200,
    service: str = "checkout-api",
) -> ServerStateV1:
    return ServerStateV1.model_validate(
        {
            "schema_version": "1.0",
            "server_id": server_id,
            "service": service,
            "config_version": "v1",
            "config": {
                "debug_mode": False,
                "max_connections": max_connections,
                "log_level": "INFO",
                "tls_min_version": "TLS1.2",
            },
            "environment": {"runtime_version": "3.12", "app_version": "1.4.0"},
            "collected_at": datetime.now(UTC),
        }
    )


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
    success_count = len(states)
    failed_count = len(servers) - success_count
    return FleetCollection(
        collected_at=datetime.now(UTC),
        status="complete" if failed_count == 0 else "partial" if success_count else "failed",
        total=len(servers),
        successful=success_count,
        failed=failed_count,
        servers=servers,
    )


@pytest.fixture
def store(tmp_path) -> SQLiteDatabase:
    database = SQLiteDatabase(f"sqlite:///{tmp_path / 'configguard.db'}")
    database.initialize()
    return database


def enroll(store: SQLiteDatabase, state: ServerStateV1) -> dict:
    statuses = store.reconcile_collection(make_collection(state), policy_version="1.0")
    assert statuses[state.server_id] == "pending_baseline"
    candidates = store.list_baselines(state.server_id, status="pending")
    assert len(candidates) == 1
    return candidates[0]


def approve(store: SQLiteDatabase, server_id: str, candidate: dict) -> dict:
    return store.approve_candidate(
        server_id,
        candidate["baseline_id"],
        approved_by="admin@example.test",
        reason="Reviewed for test",
    )


def detector() -> DriftDetector:
    return DriftDetector(load_baseline("config/baseline.json"), load_policy("config/policies.yaml"))


def test_first_collection_registers_server_and_creates_only_pending_candidate(store) -> None:
    state = make_state("checkout-01")
    candidate = enroll(store, state)

    server = store.get_server(state.server_id)
    assert server["display_name"] == "Checkout 01"
    assert server["status"] == "pending_baseline"
    assert server["current_baseline_id"] is None
    assert candidate["status"] == "pending"
    assert store.approved_baselines() == {}


def test_declared_registration_is_idempotent_and_endpoint_change_conflicts(store) -> None:
    registration = ServerRegistration(
        server_id="checkout-01",
        display_name="Checkout Web",
        role="web",
        endpoint="http://web-server:8080",
    )
    assert store.register_servers([registration])["checkout-01"] == "newly_enrolled"
    assert store.register_servers([registration])["checkout-01"] == "newly_enrolled"
    assert len(store.list_servers()) == 1
    assert store.get_server("checkout-01")["server_type"] == "web"
    replacement = registration.model_copy(update={"endpoint": "http://replacement:8080"})
    assert store.register_servers([replacement])["checkout-01"] == "identity_conflict"
    assert store.get_server("checkout-01")["status"] == "identity_conflict"


def test_replacement_instance_cannot_reuse_enrolled_identity_baseline(store) -> None:
    registration = ServerRegistration(
        server_id="checkout-01",
        display_name="Checkout Web",
        role="web",
        endpoint="http://web-server:8080",
    )
    store.register_servers([registration])
    state = make_state("checkout-01")
    first = ServerCollection(
        server_id="checkout-01",
        status="success",
        state=state,
        fingerprint=fingerprint_state(state),
        display_name="Checkout Web",
        role="web",
        collector_version="1.0",
        instance_id="a" * 32,
    )
    store.reconcile_collection(
        FleetCollection(
            collected_at=datetime.now(UTC),
            status="complete",
            total=1,
            successful=1,
            failed=0,
            servers=[first],
        )
    )
    assert len(store.list_baselines("checkout-01", status="pending")) == 1
    replacement = first.model_copy(update={"instance_id": "b" * 32})
    statuses = store.reconcile_collection(
        FleetCollection(
            collected_at=datetime.now(UTC),
            status="complete",
            total=1,
            successful=1,
            failed=0,
            servers=[replacement],
        )
    )
    assert statuses["checkout-01"] == "identity_conflict"
    assert len(store.list_baselines("checkout-01", status="pending")) == 1


def test_schema_v1_database_migrates_without_losing_server_records(tmp_path) -> None:
    database_file = tmp_path / "phase3.db"
    connection = sqlite3.connect(database_file)
    for statement in _SCHEMA:
        if statement.startswith("CREATE TABLE servers"):
            statement = statement.replace("        server_type TEXT,\n", "")
            statement = statement.replace("        endpoint TEXT,\n", "")
            statement = statement.replace("        instance_id TEXT,\n", "")
        connection.execute(statement)
    connection.execute(
        """INSERT INTO servers (
            server_id, display_name, role, lifecycle_status, reachability, first_seen
        ) VALUES ('legacy-01', 'Legacy 01', 'checkout-api', 'active', 'reachable',
            '2026-10-16T00:00:00Z')"""
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    migrated = SQLiteDatabase(f"sqlite:///{database_file}")
    migrated.initialize()
    assert migrated.get_server("legacy-01")["display_name"] == "Legacy 01"
    assert migrated.get_server("legacy-01")["server_type"] is None
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_repeated_collection_never_auto_approves_or_duplicates_candidate(store) -> None:
    state = make_state("checkout-01")
    candidate = enroll(store, state)
    store.reconcile_collection(make_collection(state))

    candidates = store.list_baselines(state.server_id)
    assert len(candidates) == 1
    assert candidates[0]["baseline_id"] == candidate["baseline_id"]
    assert candidates[0]["status"] == "pending"
    assert store.get_server(state.server_id)["status"] == "pending_baseline"


def test_candidate_cannot_be_approved_for_another_server(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))
    enroll(store, make_state("checkout-02"))

    with pytest.raises(KeyError, match="baseline_not_found"):
        store.approve_candidate("checkout-02", candidate["baseline_id"], approved_by="admin")


def test_approval_sets_current_baseline_and_audit_metadata(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))
    approved = approve(store, "checkout-01", candidate)

    assert approved["status"] == "approved"
    assert approved["approved_by"] == "admin@example.test"
    assert approved["approved_at"] is not None
    assert store.get_current_baseline("checkout-01")["baseline_id"] == candidate["baseline_id"]
    assert store.get_server("checkout-01")["status"] == "active"


def test_second_approval_supersedes_without_mutating_history(store) -> None:
    first = enroll(store, make_state("checkout-01"))
    approve(store, "checkout-01", first)
    second_state = make_state("checkout-01", max_connections=250)
    second = store.create_candidate(
        "checkout-01", second_state, created_by="operator", policy_version="1.0"
    )
    approve(store, "checkout-01", second)

    versions = store.list_baselines("checkout-01")
    assert [record["version"] for record in versions] == [2, 1]
    assert versions[0]["status"] == "approved" and versions[0]["is_current"] is True
    assert versions[1]["status"] == "superseded" and versions[1]["is_current"] is False
    assert versions[1]["snapshot"]["config"]["max_connections"] == 200


def test_two_servers_use_distinct_approved_baselines(store) -> None:
    first = make_state("checkout-01", max_connections=200)
    second = make_state("checkout-02", max_connections=450)
    collection = make_collection(first, second)
    store.reconcile_collection(collection)
    for server_id in (first.server_id, second.server_id):
        approve(store, server_id, store.list_baselines(server_id)[0])

    report = detector().detect(collection, approved_baselines=store.approved_baselines())
    by_id = {result.server_id: result for result in report.servers}
    assert by_id["checkout-01"].outcome == "clean"
    assert by_id["checkout-02"].outcome == "clean"
    assert report.pending_baseline == 0
    assert by_id["checkout-01"].baseline_id != by_id["checkout-02"].baseline_id
    assert report.baseline_versions == {"checkout-01": "1", "checkout-02": "1"}
    assert report.baseline_id is None


def test_pending_baseline_scan_count_is_per_server_assessment(store) -> None:
    states = [make_state(f"checkout-{number:02d}") for number in range(1, 4)]
    collection = make_collection(*states)
    store.reconcile_collection(collection)

    scan = detector().detect(collection, approved_baselines=store.approved_baselines())

    assert scan.pending_baseline == 3
    assert sum(item.outcome == "pending_baseline" for item in scan.servers) == 3
    pending_candidates = sum(
        len(store.list_baselines(state.server_id, status="pending")) for state in states
    )
    assert pending_candidates == 3


def test_fresh_candidate_is_not_auto_approved_by_repeated_scan(store) -> None:
    state = make_state("checkout-01")
    enroll(store, state)
    next_state = make_state("checkout-01", max_connections=500)
    store.reconcile_collection(make_collection(next_state))

    assert store.get_server("checkout-01")["status"] == "pending_baseline"
    assert all(item["status"] == "pending" for item in store.list_baselines("checkout-01"))
    assert store.approved_baselines() == {}


def test_invalid_persisted_candidate_snapshot_cannot_be_approved(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))
    with store.transaction() as connection:
        connection.execute(
            "UPDATE baselines SET snapshot_json = ? WHERE baseline_id = ?",
            (
                json.dumps(
                    {"schema_version": "1.0", "service_group": "checkout-api", "config": {}}
                ),
                candidate["baseline_id"],
            ),
        )

    with pytest.raises(ValueError, match="invalid_baseline_snapshot"):
        approve(store, "checkout-01", candidate)
    assert store.get_baseline("checkout-01", candidate["baseline_id"])["status"] == "pending"


def test_concurrent_approval_requests_create_one_current_version(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))

    def attempt() -> str:
        try:
            store.approve_candidate("checkout-01", candidate["baseline_id"], approved_by="admin")
            return "approved"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["approved", "conflict"]
    assert (
        len([item for item in store.list_baselines("checkout-01") if item["status"] == "approved"])
        == 1
    )


def test_decommission_preserves_baselines_and_prevents_reactivation(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))
    approve(store, "checkout-01", candidate)
    old_history = store.list_baselines("checkout-01")
    store.decommission("checkout-01")
    status = store.reconcile_collection(make_collection(make_state("checkout-01")))

    assert status["checkout-01"] == "decommissioned"
    assert store.get_server("checkout-01")["status"] == "decommissioned"
    assert store.list_baselines("checkout-01") == old_history
    assert store.approved_baselines() == {}


def test_identity_role_conflict_cannot_use_or_replace_existing_baseline(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))
    approve(store, "checkout-01", candidate)
    conflict = make_state("checkout-01", service="different-service")
    status = store.reconcile_collection(make_collection(conflict))

    assert status["checkout-01"] == "identity_conflict"
    assert store.get_server("checkout-01")["status"] == "identity_conflict"
    assert store.approved_baselines() == {}
    assert len(store.list_baselines("checkout-01")) == 1


def test_partial_collection_scan_keeps_success_and_failure(store) -> None:
    state = make_state("checkout-01")
    collection = make_collection(state, failed=("checkout-02", "offline"))
    store.reconcile_collection(collection)
    scan = detector().detect(collection, approved_baselines=store.approved_baselines())
    scan_id = store.save_scan(scan, started_at=datetime.now(UTC), completed_at=datetime.now(UTC))

    persisted = store.get_scan(scan_id)["result"]
    outcomes = {result.server_id: result.outcome for result in persisted.servers}
    assert outcomes == {"checkout-01": "pending_baseline", "checkout-02": "unable_to_assess"}
    assert persisted.pending_baseline == 1
    assert persisted.failed == 1


def test_scan_history_and_baseline_version_survive_reconnection(store, tmp_path) -> None:
    state = make_state("checkout-01")
    candidate = enroll(store, state)
    approve(store, "checkout-01", candidate)
    collection = make_collection(state)
    report = detector().detect(collection, approved_baselines=store.approved_baselines())
    scan_id = store.save_scan(report, started_at=datetime.now(UTC), completed_at=datetime.now(UTC))
    database_url = f"sqlite:///{tmp_path / 'configguard.db'}"

    reopened = SQLiteDatabase(database_url)
    reopened.initialize()
    saved = reopened.get_scan(scan_id)["result"]
    assert saved.servers[0].baseline_id == candidate["baseline_id"]
    assert saved.servers[0].baseline_version == "1"
    assert reopened.get_current_baseline("checkout-01")["version"] == 1


def test_repeated_initialization_does_not_reset_records(store) -> None:
    candidate = enroll(store, make_state("checkout-01"))
    store.initialize()
    store.initialize()

    assert store.get_baseline("checkout-01", candidate["baseline_id"])["status"] == "pending"


def test_idempotency_key_returns_existing_scan_without_duplicate_history(store) -> None:
    state = make_state("checkout-01")
    collection = make_collection(state)
    store.reconcile_collection(collection)
    report = detector().detect(collection, approved_baselines={})
    started = datetime.now(UTC)
    scan_id = store.save_scan(
        report, started_at=started, completed_at=datetime.now(UTC), idempotency_key="retry-1"
    )
    retry = store.save_scan(
        report.model_copy(update={"scan_id": "different"}),
        started_at=started,
        completed_at=datetime.now(UTC),
        idempotency_key="retry-1",
    )

    assert retry == scan_id
    assert len(store.list_scans(limit=100)) == 1


def test_missing_registered_server_is_reported_as_not_configured(store) -> None:
    state = make_state("checkout-01")
    enroll(store, state)
    # A subsequent collector reconciliation records the explicit failure outcome.
    failure = ServerCollection(
        server_id="checkout-01",
        status="failed",
        error=CollectionFailure(kind="not_configured", message="not in registry"),
    )
    collection = FleetCollection(
        collected_at=datetime.now(UTC),
        status="failed",
        total=1,
        successful=0,
        failed=1,
        servers=[failure],
    )
    store.reconcile_collection(collection)

    assert store.get_server("checkout-01")["status"] == "not_configured"
