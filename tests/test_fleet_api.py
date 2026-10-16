import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.collector.fleet import FleetCollector
from backend.app.core.fingerprinting import fingerprint_state
from backend.app.main import (
    app,
    get_fleet_collector,
    get_upstream_client,
)


def server_state(server_id: str, config: dict[str, object] | None = None) -> dict[str, object]:
    return {
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
        "environment": {"runtime_version": "3.12", "app_version": "1.4.0"},
        "collected_at": datetime.now(UTC).isoformat(),
    }


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")


def test_fleet_states_endpoint_returns_success_and_partial_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "server-02":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=server_state("server-01"), request=request)

    collector = FleetCollector(
        {"server-01": "http://server-01", "server-02": "http://server-02"},
        transport=httpx.MockTransport(handler),
    )
    app.dependency_overrides[get_fleet_collector] = lambda: collector
    try:
        with TestClient(app) as client:
            response = client.get("/api/fleet/states")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["successful"] == 1
    assert response.json()["servers"][1]["error"]["kind"] == "unreachable"


def test_scan_requires_explicit_baseline_approval_before_clean_assessment() -> None:
    state = server_state("server-01")
    collector = FleetCollector(
        {"server-01": "http://server-01"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=state, request=request)
        ),
    )
    app.dependency_overrides[get_fleet_collector] = lambda: collector
    try:
        with TestClient(app) as client:
            response = client.post("/api/scans")
            candidates = client.get("/api/servers/server-01/baseline-candidates")
            candidate = candidates.json()["items"][0]
            approval = client.post(
                f"/api/servers/server-01/baselines/{candidate['baseline_id']}/approve",
                json={"confirm": True, "approved_by": "test-admin"},
            )
            clean_scan = client.post("/api/scans")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["pending_baseline"] == 1
    assert response.json()["servers"][0]["outcome"] == "pending_baseline"
    assert candidates.status_code == 200
    assert approval.status_code == 200
    assert approval.json()["approved_by"] == "test-admin"
    assert clean_scan.json()["successful"] == 1
    assert clean_scan.json()["drifted"] == 0
    assert clean_scan.json()["servers"][0]["outcome"] == "clean"


def test_drift_and_reset_routes_validate_and_verify_remote_changes(monkeypatch) -> None:
    server_id = "server-03"
    url = "http://server-03:8080"
    original = server_state(server_id)
    runtime = {"config": original["config"]}

    def current_state() -> dict[str, object]:
        return server_state(server_id, runtime["config"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/state":
            return httpx.Response(200, json=current_state(), request=request)
        if request.url.path == "/demo/drift":
            scenario = json.loads(request.content)["scenario"]
            config = dict(runtime["config"])
            config["debug_mode"] = True
            runtime["config"] = config
            return httpx.Response(
                200,
                json={"server_id": server_id, "scenario": scenario, "status": "applied"},
                request=request,
            )
        if request.url.path == "/demo/reset":
            runtime["config"] = original["config"]
            expected = fingerprint_state(current_state())
            return httpx.Response(
                200,
                json={
                    "server_id": server_id,
                    "status": "reset",
                    "expected_fingerprint": expected,
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("backend.app.main.configured_servers", lambda: {server_id: url})
    monkeypatch.setattr("backend.app.main.demo_controls_enabled", lambda: True)

    async def mock_client():
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    app.dependency_overrides[get_upstream_client] = mock_client
    app.dependency_overrides[get_fleet_collector] = lambda: FleetCollector(
        {server_id: url}, transport=transport
    )
    try:
        with TestClient(app) as client:
            drift = client.post(
                f"/api/fleet/{server_id}/simulate-drift",
                json={"scenario": "security_drift"},
            )
            drifted = client.get("/api/fleet/states")
            reset = client.post(f"/api/fleet/{server_id}/reset")
            restored = client.get("/api/fleet/states")
    finally:
        app.dependency_overrides.clear()

    assert drift.status_code == 202
    assert drift.json() == {
        "server_id": server_id,
        "scenario": "security_drift",
        "status": "applied",
    }
    assert drifted.json()["servers"][0]["state"]["config"]["debug_mode"] is True
    assert reset.status_code == 200
    assert reset.json()["verified"] is True
    assert restored.json()["servers"][0]["state"]["config"] == original["config"]


def test_unknown_server_id_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.main.configured_servers", lambda: {"server-01": "http://server-01"}
    )
    monkeypatch.setattr("backend.app.main.demo_controls_enabled", lambda: True)
    with TestClient(app) as client:
        response = client.post(
            "/api/fleet/not-configured/simulate-drift", json={"scenario": "security_drift"}
        )
    assert response.status_code == 404


def test_api_demo_controls_require_demo_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.main.configured_servers", lambda: {"server-01": "http://server-01"}
    )
    monkeypatch.setattr("backend.app.main.demo_controls_enabled", lambda: False)
    with TestClient(app) as client:
        response = client.post(
            "/api/fleet/server-01/simulate-drift", json={"scenario": "security_drift"}
        )
    assert response.status_code == 409


def test_server_and_scan_history_endpoints_return_404_and_paginated_data() -> None:
    with TestClient(app) as client:
        unknown = client.get("/api/servers/missing")
        missing_scan = client.get("/api/scans/00000000-0000-0000-0000-000000000000")
        history = client.get("/api/scans?limit=5&offset=0")

    assert unknown.status_code == 404
    assert missing_scan.status_code == 404
    assert history.status_code == 200
    assert history.json() == {"items": [], "limit": 5, "offset": 0}
