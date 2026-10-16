from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.core.fingerprinting import fingerprint_state
from backend.app.core.server_templates import load_server_templates
from backend.app.main import app
from backend.app.schemas.fleet import CollectionFailure, FleetCollection, ServerCollection
from backend.app.schemas.provisioning import ProvisionServerRequest
from backend.app.schemas.server_state import ServerStateV1


def _request(
    server_id: str = "checkout-worker-04", request_id: str | None = None
) -> dict[str, object]:
    return {
        "request_id": request_id or str(uuid4()),
        "server_id": server_id,
        "display_name": "Checkout Worker 04",
        "role": "web",
        "template_id": "web-default-v1",
        "description": "Provisioning regression test",
        "demo_mode": True,
    }


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'provisioning.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SERVER_REGISTRY_FILE", "")
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def test_templates_are_validated_and_role_specific() -> None:
    templates = load_server_templates()
    assert set(templates) == {
        "web-default-v1",
        "application-default-v1",
        "database-default-v1",
    }
    assert {item.role for item in templates.values()} == {"web", "application", "database"}
    assert all(item.config.debug_mode is False for item in templates.values())
    assert all("password" not in str(item.model_dump()).lower() for item in templates.values())


def test_provisioning_creates_pending_candidate_and_same_request_is_idempotent(monkeypatch) -> None:
    from backend.app import main

    calls = 0

    async def provisioner(_url, payload):
        nonlocal calls
        calls += 1
        assert payload.request.server_id == "checkout-worker-04"
        return httpx.Response(
            201,
            json={
                "server_id": payload.request.server_id,
                "container_id": "a" * 64,
                "container_name": "configguard-ai-sentinel-checkout-worker-04",
                "endpoint": "http://checkout-worker-04:8080",
                "health_status": "healthy",
            },
        )

    class FakeCollector:
        def __init__(self, urls, registrations=None, **kwargs):
            self.urls = urls
            self.server_urls = urls
            self.registrations = registrations or {}
            self.timeout = 5.0
            self.transport = kwargs.get("transport")

        async def collect_server(self, server_id):
            template = load_server_templates()["web-default-v1"]
            state = ServerStateV1(
                schema_version="1.0",
                server_id=server_id,
                service=template.service,
                config_version=template.config_version,
                config=template.config,
                environment=template.environment,
                collected_at=datetime.now(UTC),
            )
            return ServerCollection(
                server_id=server_id,
                status="success",
                fingerprint=fingerprint_state(state),
                state=state,
                display_name="Checkout Worker 04",
                role="web",
                instance_id="stable-instance-123",
            )

        async def collect(self):
            return FleetCollection(
                collected_at=datetime.now(UTC),
                status="failed",
                total=1,
                successful=0,
                failed=1,
                servers=[
                    ServerCollection(
                        server_id="checkout-worker-04",
                        status="failed",
                        error=CollectionFailure(
                            kind="unreachable", message="Server is unreachable"
                        ),
                    )
                ],
            )

    monkeypatch.setattr(main, "_create_server_via_provisioner", provisioner)
    monkeypatch.setattr(main, "FleetCollector", FakeCollector)
    body = _request()
    with TestClient(app) as client:
        created = client.post("/api/servers/provision", json=body)
        candidates = client.get("/api/servers/checkout-worker-04/baseline-candidates")
        detail = client.get("/api/servers/checkout-worker-04")
        retried = client.post("/api/servers/provision", json=body)
        unavailable = client.get("/api/fleet/states")
        unavailable_detail = client.get("/api/servers/checkout-worker-04")

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending_baseline"
    assert created.json()["baseline_status"] == "pending_approval"
    assert candidates.status_code == 200
    assert len(candidates.json()["items"]) == 1
    assert candidates.json()["items"][0]["status"] == "pending"
    assert detail.status_code == 200
    assert detail.json()["current_baseline_id"] is None
    assert retried.status_code == 201
    assert calls == 1
    assert unavailable.status_code == 200
    assert unavailable.json()["servers"][0]["status"] == "failed"
    assert unavailable_detail.json()["status"] == "unavailable"
    assert unavailable_detail.json()["health_status"] == "unhealthy"


def test_duplicate_identity_and_role_template_mismatch_are_rejected(monkeypatch) -> None:
    from backend.app import main

    calls = 0

    async def provisioner(_url, _payload):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={})

    async def no_cleanup(_url, _request):
        return None

    monkeypatch.setattr(main, "_create_server_via_provisioner", provisioner)
    monkeypatch.setattr(main, "_cleanup_provisioned_server", no_cleanup)
    with TestClient(app) as client:
        mismatched = _request()
        mismatched["role"] = "database"
        mismatch_response = client.post("/api/servers/provision", json=mismatched)
        first = client.post("/api/servers/provision", json=_request())
        duplicate = _request(request_id=str(uuid4()))
        duplicate_response = client.post("/api/servers/provision", json=duplicate)

    assert mismatch_response.status_code == 422
    assert first.status_code == 502
    assert duplicate_response.status_code == 409
    assert calls == 1


def test_invalid_server_id_is_rejected_before_reservation() -> None:
    with TestClient(app) as client:
        response = client.post("/api/servers/provision", json=_request(server_id="Bad Name"))
        servers = client.get("/api/servers?limit=100&offset=0")
    assert response.status_code == 422
    assert servers.json()["items"] == []


def test_internal_provisioner_rejects_configuration_outside_selected_template(monkeypatch) -> None:
    from backend.app.provisioner import app as provisioner_app
    from backend.app.provisioner import engine

    called = False

    async def unexpected_provision(_payload):
        nonlocal called
        called = True
        raise AssertionError("invalid template payload must not reach Docker")

    monkeypatch.setattr(engine, "provision", unexpected_provision)
    template = load_server_templates()["web-default-v1"]
    body = {
        "request": _request(),
        "service": template.service,
        "config_version": template.config_version,
        "config": {**template.config.model_dump(mode="json"), "debug_mode": True},
        "environment": template.environment.model_dump(mode="json"),
    }
    with TestClient(provisioner_app) as client:
        response = client.post("/internal/servers", json=body)

    assert response.status_code == 422
    assert called is False


def test_container_definition_uses_only_fixed_private_and_unprivileged_settings(
    monkeypatch,
) -> None:
    import asyncio

    from backend.app.provisioner import DockerEngine, InternalProvisionRequest

    template = load_server_templates()["web-default-v1"]
    payload = InternalProvisionRequest(
        request=ProvisionServerRequest.model_validate(_request()),
        service=template.service,
        config_version=template.config_version,
        config=template.config,
        environment=template.environment,
    )
    docker = DockerEngine()
    docker.network = "sentinelops_test_monitoring"
    docker.resource_prefix = "sentinelops-test"
    container_configs: list[dict[str, object]] = []
    labels: dict[str, str] = {}

    async def fake_request(method, path, *, json_body=None):
        nonlocal labels
        if method == "GET" and path.endswith("/json") and "containers" in path:
            if "checkout-worker-04" in path:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "Id": "b" * 64,
                    "Config": {"Labels": labels},
                    "State": {"Status": "running", "Health": {"Status": "healthy"}},
                },
            )
        if method == "POST" and path == "/volumes/create":
            labels = json_body["Labels"]
            return httpx.Response(201, json={"Name": json_body["Name"], "Labels": labels})
        if method == "POST" and path.startswith("/containers/create"):
            container_configs.append(json_body)
            labels = json_body["Labels"]
            return httpx.Response(201, json={"Id": "b" * 64})
        if method == "POST" and path.endswith("/start"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected Docker API operation {method} {path}")

    monkeypatch.setattr(docker, "request", fake_request)
    result = asyncio.run(docker.provision(payload))
    config = container_configs[0]

    assert result.server_id == "checkout-worker-04"
    assert config["Image"] == "configguard-fleet:local"
    assert config["ExposedPorts"] == {"8080/tcp": {}}
    assert "PortBindings" not in config["HostConfig"]
    assert config["HostConfig"]["ReadonlyRootfs"] is True
    assert config["HostConfig"]["CapDrop"] == ["ALL"]
    assert config["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
    assert config["HostConfig"]["NetworkMode"] == "sentinelops_test_monitoring"
    assert config["HostConfig"]["Mounts"][0]["Source"] == (
        "sentinelops-test-identity-checkout-worker-04"
    )
    assert all("docker.sock" not in str(value) for value in config.values())
