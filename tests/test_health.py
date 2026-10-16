import asyncio
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import app


def test_health_reports_application_without_claiming_dependency_readiness() -> None:
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "application": "configguard-ai",
        "dependencies_checked": False,
    }


def test_readiness_checks_sqlite_without_changing_health_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ready.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "false")
    with TestClient(app) as client:
        health = client.get("/api/health")
        ready = client.get("/api/ready")
    assert health.json()["dependencies_checked"] is False
    assert ready.status_code == 200
    assert ready.json()["database"] == "ready"
    assert ready.json()["demo_controls_enabled"] is False


def test_readiness_reports_demo_controls_setting_without_changing_health(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo-ready.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    with TestClient(app) as client:
        ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json()["demo_controls_enabled"] is True


def test_health_stays_responsive_while_startup_scan_is_running(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'scan.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "3600")
    scan_started = threading.Event()

    async def slow_scan(*args, **kwargs) -> None:
        scan_started.set()
        await asyncio.sleep(0.5)

    monkeypatch.setattr("backend.app.main._execute_scan", slow_scan)
    with TestClient(app) as client:
        assert scan_started.wait(timeout=2)
        started = time.monotonic()
        response = client.get("/api/health")
        elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 0.4


def test_startup_reconciles_declared_inventory_without_approving_baselines(
    tmp_path, monkeypatch
) -> None:
    root = Path(__file__).parents[1]
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'registry.db'}")
    monkeypatch.setenv("SERVER_REGISTRY_FILE", str(root / "config" / "servers.json"))
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    for _ in range(2):
        with TestClient(app) as client:
            inventory = client.get("/api/servers").json()["items"]
    assert len(inventory) == 3
    assert {server["server_type"] for server in inventory} == {
        "web",
        "application",
        "database",
    }
    assert all(server["status"] == "newly_enrolled" for server in inventory)
    assert all(server["current_baseline_id"] is None for server in inventory)
    assert all("endpoint" not in server and "instance_id" not in server for server in inventory)
