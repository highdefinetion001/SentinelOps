from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_compose_declares_three_private_fleet_services_and_backend() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"server-01", "server-02", "server-03", "backend"} <= services.keys()
    fleet_services = [services[f"server-{number:02d}"] for number in (1, 2, 3)]
    assert len({service["image"] for service in fleet_services}) == 1
    assert all("ports" not in service for service in fleet_services)
    assert all(service["networks"] == ["fleet"] for service in fleet_services)
    assert all("SERVER_CONFIG" in service["environment"] for service in fleet_services)
    assert all(
        service["condition"] == "service_healthy"
        for service in services["backend"]["depends_on"].values()
    )
    assert compose["networks"]["fleet"]["driver"] == "bridge"
    assert compose["networks"]["fleet"]["internal"] is True
    assert "fleet" in services["backend"]["networks"]
