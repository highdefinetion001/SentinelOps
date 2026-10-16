import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleet.app import create_app


def write_definition(path: Path, server_id: str) -> None:
    definition = {
        "schema_version": "1.0",
        "server_id": server_id,
        "service": "checkout-api",
        "config_version": "v1",
        "config": {
            "debug_mode": False,
            "max_connections": 200,
            "log_level": "INFO",
            "tls_min_version": "TLS1.2",
        },
        "environment": {"runtime_version": "3.12", "app_version": "1.4.0"},
    }
    path.write_text(json.dumps(definition), encoding="utf-8")


def test_state_uses_contract_identity_and_dynamic_timestamp(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    write_definition(config, "server-01")
    with TestClient(create_app(config, expected_server_id="server-01")) as client:
        first = client.get("/state").json()
        second = client.get("/state").json()

    assert first["server_id"] == "server-01"
    assert first["config"]["debug_mode"] is False
    assert first["environment"]["app_version"] == "1.4.0"
    assert first["collected_at"] != second["collected_at"]
    assert first["collected_at"].endswith("Z") or "+00:00" in first["collected_at"]


def test_predefined_drift_changes_only_selected_server_and_reset_restores_config(
    tmp_path: Path,
) -> None:
    first_config = tmp_path / "server-01.json"
    second_config = tmp_path / "server-02.json"
    write_definition(first_config, "server-01")
    write_definition(second_config, "server-02")

    with (
        TestClient(create_app(first_config, demo_enabled=True)) as first,
        TestClient(create_app(second_config, demo_enabled=True)) as second,
    ):
        original_first = first.get("/state").json()
        original_second = second.get("/state").json()

        drift = first.post("/demo/drift", json={"scenario": "security_drift"})
        drifted_first = first.get("/state").json()
        untouched_second = second.get("/state").json()
        reset = first.post("/demo/reset")
        restored_first = first.get("/state").json()

    assert drift.status_code == 200
    assert drift.json()["status"] == "applied"
    assert drifted_first["config"]["debug_mode"] is True
    assert untouched_second["config"] == original_second["config"]
    assert reset.status_code == 200
    assert reset.json()["expected_fingerprint"]
    assert restored_first["config"] == original_first["config"]


def test_performance_scenario_is_allowlisted(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    write_definition(config, "server-01")
    with TestClient(create_app(config, demo_enabled=True)) as client:
        response = client.post("/demo/drift", json={"scenario": "performance_drift"})
        state = client.get("/state").json()

    assert response.status_code == 200
    assert state["config"]["max_connections"] == 10


def test_unknown_scenario_rejected(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    write_definition(config, "server-01")
    with TestClient(create_app(config, demo_enabled=True)) as client:
        response = client.post("/demo/drift", json={"scenario": "arbitrary_patch"})
    assert response.status_code == 422


def test_demo_control_disabled_by_default(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    write_definition(config, "server-01")
    with TestClient(create_app(config)) as client:
        response = client.post("/demo/drift", json={"scenario": "security_drift"})
    assert response.status_code == 404


def test_server_id_mismatch_prevents_startup(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    write_definition(config, "server-01")
    with pytest.raises(RuntimeError, match="does not match SERVER_ID"):
        create_app(config, expected_server_id="server-03")


def test_restart_preserves_instance_identity_and_allowlisted_demo_drift(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    identity = tmp_path / "state" / "instance-id"
    write_definition(config, "server-01")
    options = {
        "expected_server_id": "server-01",
        "demo_enabled": True,
        "display_name": "Checkout Web",
        "role": "web",
        "identity_file": identity,
    }
    with TestClient(create_app(config, **options)) as first:
        initial = first.get("/state")
        first_id = initial.headers["x-configguard-instance-id"]
        assert initial.headers["x-configguard-role"] == "web"
        assert first.post("/demo/drift", json={"scenario": "security_drift"}).status_code == 200

    with TestClient(create_app(config, **options)) as restarted:
        after_restart = restarted.get("/state")
        assert after_restart.headers["x-configguard-instance-id"] == first_id
        assert after_restart.json()["config"]["debug_mode"] is True
        reset = restarted.post("/demo/reset")
        assert reset.status_code == 200
        assert restarted.get("/state").json()["config"]["debug_mode"] is False


def test_collector_metadata_is_not_added_to_state_json(tmp_path: Path) -> None:
    config = tmp_path / "server.json"
    write_definition(config, "server-01")
    with TestClient(create_app(config, role="database")) as client:
        response = client.get("/state")
    assert response.json()["schema_version"] == "1.0"
    assert "role" not in response.json()
    assert response.headers["x-configguard-role"] == "database"
