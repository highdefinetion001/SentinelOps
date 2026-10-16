import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from backend.app.config.baseline import load_baseline

ROOT = Path(__file__).parents[2]


def request_json(url: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def test_compose_fleet_state_drift_and_reset() -> None:
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Engine is not installed; real-container integration test NOT RUN")

    compose_check = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=10
    )
    if compose_check.returncode:
        pytest.skip("Docker Compose v2 is unavailable; real-container integration test NOT RUN")
    daemon_check = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=10)
    if daemon_check.returncode:
        pytest.skip(
            "Docker CLI is installed but the current user cannot access the Docker daemon; "
            "real-container integration test NOT RUN"
        )

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update({"API_PORT": str(port), "DEMO_MODE": "true"})
    compose = [docker, "compose", "-p", "configguard-phase1-smoke"]

    try:
        subprocess.run(
            [*compose, "up", "--build", "-d", "--wait"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=600,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                health = request_json(f"{base}/api/health")
                if health["status"] == "ok":
                    break
            except (OSError, urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

        initial = request_json(f"{base}/api/fleet/states")
        assert initial["status"] == "complete"
        assert initial["successful"] == 3
        assert {entry["server_id"] for entry in initial["servers"]} == {
            "server-01",
            "server-02",
            "server-03",
        }
        initial_fingerprints = {
            entry["server_id"]: entry["fingerprint"] for entry in initial["servers"]
        }
        initial_by_id = {entry["server_id"]: entry["state"] for entry in initial["servers"]}
        baseline = load_baseline(ROOT / "config" / "baseline.json")
        for state in initial_by_id.values():
            assert state["config"] == baseline.expected_config.model_dump(mode="json")
            assert state["environment"] == baseline.expected_environment.model_dump(mode="json")

        initial_scan = request_json(f"{base}/api/scans", method="POST")
        assert initial_scan["status"] == "complete"
        assert initial_scan["drifted"] == 0

        request_json(
            f"{base}/api/fleet/server-03/simulate-drift",
            method="POST",
            body={"scenario": "security_drift"},
        )
        drifted = request_json(f"{base}/api/fleet/states")
        drifted_by_id = {entry["server_id"]: entry for entry in drifted["servers"]}
        assert drifted_by_id["server-03"]["state"]["config"]["debug_mode"] is True
        drift_scan = request_json(f"{base}/api/scans", method="POST")
        detected = next(item for item in drift_scan["servers"] if item["server_id"] == "server-03")
        assert detected["outcome"] == "drifted"
        debug = next(
            item
            for item in detected["findings"]
            if item["evidence"]["field_path"] == "config.debug_mode"
        )
        assert debug["assessment"]["severity"] == "CRITICAL"
        assert drifted_by_id["server-01"]["fingerprint"] == initial_fingerprints["server-01"]
        assert drifted_by_id["server-02"]["fingerprint"] == initial_fingerprints["server-02"]

        reset = request_json(f"{base}/api/fleet/server-03/reset", method="POST")
        assert reset["verified"] is True
        restored = request_json(f"{base}/api/fleet/states")
        restored_by_id = {entry["server_id"]: entry for entry in restored["servers"]}
        assert (
            restored_by_id["server-03"]["state"]["config"] == initial_by_id["server-03"]["config"]
        )
        assert restored_by_id["server-03"]["fingerprint"] == initial_fingerprints["server-03"]
        resolved_scan = request_json(f"{base}/api/scans", method="POST")
        assert resolved_scan["drifted"] == 0
    finally:
        subprocess.run(
            [*compose, "down", "--remove-orphans"],
            cwd=ROOT,
            env=env,
            check=False,
            timeout=120,
        )
