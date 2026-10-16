from pathlib import Path

import pytest
import yaml

from backend.app.config.registry import ServerRegistration

ROOT = Path(__file__).parents[1]


def test_compose_declares_monitor_and_three_private_role_servers() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"server-01", "server-02", "server-03", "monitor"} <= services.keys()
    fleet_services = [services[f"server-{number:02d}"] for number in (1, 2, 3)]
    assert len({service["image"] for service in fleet_services}) == 1
    assert all("ports" not in service for service in fleet_services)
    assert all(service["networks"] == ["monitoring"] for service in fleet_services)
    assert all("SERVER_CONFIG" in service["environment"] for service in fleet_services)
    assert [service["environment"]["SERVER_ROLE"] for service in fleet_services] == [
        "web",
        "application",
        "database",
    ]
    assert all(service["volumes"][0].endswith("/var/lib/configguard") for service in fleet_services)
    monitor = services["monitor"]
    assert monitor["networks"] == ["monitoring", "api", "ai", "provisioning"]
    assert set(monitor["depends_on"]) == {"server-01", "server-02", "server-03", "provisioner"}
    assert monitor["depends_on"]["provisioner"]["condition"] == "service_healthy"
    assert all(
        monitor["depends_on"][name]["condition"] == "service_started"
        for name in ("server-01", "server-02", "server-03")
    )
    assert monitor["healthcheck"]["test"][-1].endswith("/api/ready', timeout=2)")
    assert len(monitor["ports"]) == 1
    assert monitor["read_only"] is True
    assert monitor["cap_drop"] == ["ALL"]
    assert compose["networks"]["monitoring"]["driver"] == "bridge"
    assert compose["networks"]["monitoring"]["internal"] is True
    assert "monitoring" in monitor["networks"]
    provisioner = services["provisioner"]
    assert "ports" not in provisioner
    assert provisioner["networks"] == ["provisioning", "docker_api"]
    assert not any("docker.sock" in str(volume) for volume in provisioner.get("volumes", []))
    assert "docker-api" in services
    assert services["docker-api"]["networks"] == ["docker_api"]
    assert services["docker-api"]["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert not any("docker.sock" in str(volume) for volume in monitor.get("volumes", []))
    assert len(services) == len(set(services))
    assert monitor["environment"]["DATABASE_URL"] == (
        "${DATABASE_URL:-sqlite:////data/configguard.db}"
    )
    assert "configguard_data:/data" in monitor["volumes"]
    assert monitor["environment"]["SERVER_REGISTRY_FILE"] == "/app/config/servers.json"
    assert monitor["environment"]["SCAN_INTERVAL_SECONDS"] == "${SCAN_INTERVAL_SECONDS:-60}"
    assert monitor["environment"]["AI_BASE_URL"] == "${AI_BASE_URL:-http://ollama:11434/v1}"
    ollama = services["ollama"]
    assert ollama["profiles"] == ["ai"]
    assert ollama["networks"] == ["ai"]
    assert "ports" not in ollama
    assert "ollama_models:/root/.ollama" in ollama["volumes"]
    assert "configguard_data" in compose["volumes"]
    assert "name" not in (compose["volumes"]["configguard_data"] or {})


def test_registry_config_has_unique_explicit_identities_and_distinct_roles() -> None:
    import json

    registry = json.loads((ROOT / "config" / "servers.json").read_text(encoding="utf-8"))
    entries = registry["servers"]
    assert len(entries) == 3
    assert len({entry["server_id"] for entry in entries}) == 3
    assert {entry["role"] for entry in entries} == {"web", "application", "database"}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:password@server-01:8080",
        "https://public.example.com/state",
        "http://8.8.8.8:8080",
        "http://server-01:8080/path",
    ],
)
def test_registration_rejects_credentials_public_hosts_and_arbitrary_paths(endpoint: str) -> None:
    with pytest.raises(ValueError):
        ServerRegistration(
            server_id="server-01",
            display_name="Checkout Web",
            role="web",
            endpoint=endpoint,
        )
