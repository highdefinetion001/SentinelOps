import asyncio
from datetime import UTC, datetime

import httpx

from backend.app.collector.fleet import FleetCollector, parse_server_registry
from backend.app.config.registry import ServerRegistration
from backend.app.core.fingerprinting import fingerprint_state


def state_payload(server_id: str = "server-01") -> dict[str, object]:
    return {
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
        "collected_at": datetime.now(UTC).isoformat(),
    }


def test_collector_accepts_valid_state_and_computes_fingerprint() -> None:
    payload = state_payload()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    result = asyncio.run(
        FleetCollector({"server-01": "http://server-01:8080"}, transport=transport).collect()
    )

    assert result.status == "complete"
    assert result.successful == 1
    assert result.servers[0].state is not None
    assert result.servers[0].state.server_id == "server-01"
    assert result.servers[0].fingerprint == fingerprint_state(result.servers[0].state)


def test_registered_collector_metadata_is_verified_and_returned() -> None:
    registration = ServerRegistration(
        server_id="server-01",
        display_name="Checkout Web",
        role="web",
        endpoint="http://server-01:8080",
    )
    headers = {
        "X-ConfigGuard-Server-ID": "server-01",
        "X-ConfigGuard-Display-Name": "Checkout Web",
        "X-ConfigGuard-Role": "web",
        "X-ConfigGuard-Collector-Version": "1.0",
        "X-ConfigGuard-Instance-ID": "a" * 32,
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=state_payload(), headers=headers)
    )
    result = asyncio.run(
        FleetCollector(
            {"server-01": registration.endpoint},
            registrations={"server-01": registration},
            transport=transport,
        ).collect()
    )
    server = result.servers[0]
    assert server.status == "success"
    assert server.display_name == "Checkout Web"
    assert server.role == "web"
    assert server.collector_version == "1.0"
    assert server.instance_id == "a" * 32


def test_collector_rejects_registered_identity_mismatch() -> None:
    registration = ServerRegistration(
        server_id="server-01",
        display_name="Checkout Web",
        role="web",
        endpoint="http://server-01:8080",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=state_payload(),
            headers={
                "X-ConfigGuard-Server-ID": "replacement",
                "X-ConfigGuard-Display-Name": "Checkout Web",
                "X-ConfigGuard-Role": "web",
                "X-ConfigGuard-Collector-Version": "1.0",
                "X-ConfigGuard-Instance-ID": "b" * 32,
            },
        )
    )
    result = asyncio.run(
        FleetCollector(
            {"server-01": registration.endpoint},
            registrations={"server-01": registration},
            transport=transport,
        ).collect()
    )
    assert result.servers[0].status == "failed"
    assert result.servers[0].error.kind == "identity_conflict"


def test_collector_bounds_response_size() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b" " * (64 * 1024 + 1), request=request)
    )
    result = asyncio.run(
        FleetCollector({"server-01": "http://server-01"}, transport=transport).collect()
    )
    assert result.servers[0].status == "failed"
    assert result.servers[0].error.kind == "response_too_large"


def test_collector_returns_classified_partial_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if host == "unreachable":
            raise httpx.ConnectError("refused", request=request)
        if host == "http-error":
            return httpx.Response(503, request=request)
        if host == "invalid-json":
            return httpx.Response(200, content=b"{malformed", request=request)
        if host == "invalid-state":
            return httpx.Response(200, json={"schema_version": "9.0"}, request=request)
        return httpx.Response(200, json=state_payload("healthy"), request=request)

    registry = {
        "timeout": "http://timeout:8080",
        "unreachable": "http://unreachable:8080",
        "http-error": "http://http-error:8080",
        "invalid-json": "http://invalid-json:8080",
        "invalid-state": "http://invalid-state:8080",
        "healthy": "http://healthy:8080",
    }
    result = asyncio.run(FleetCollector(registry, transport=httpx.MockTransport(handler)).collect())
    by_id = {server.server_id: server for server in result.servers}

    assert result.status == "partial"
    assert result.total == 6
    assert result.successful == 1
    assert result.failed == 5
    assert by_id["timeout"].error.kind == "timeout"
    assert by_id["unreachable"].error.kind == "unreachable"
    assert by_id["http-error"].error.kind == "http_error"
    assert by_id["http-error"].error.upstream_status == 503
    assert by_id["invalid-json"].error.kind == "invalid_json"
    assert by_id["invalid-state"].error.kind == "invalid_state"
    assert by_id["timeout"].state is None


def test_collector_marks_all_failed_without_fabricating_state() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(502, request=request))
    result = asyncio.run(
        FleetCollector({"server-01": "http://server-01"}, transport=transport).collect()
    )
    assert result.status == "failed"
    assert result.servers[0].state is None
    assert result.servers[0].fingerprint is None


def test_collector_rejects_state_with_wrong_server_identity() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=state_payload("another-server"), request=request)
    )
    result = asyncio.run(
        FleetCollector({"server-01": "http://server-01"}, transport=transport).collect()
    )
    assert result.servers[0].status == "failed"
    assert result.servers[0].error.kind == "identity_conflict"


def test_server_registry_requires_explicit_valid_entries() -> None:
    assert parse_server_registry("server-01=http://server-01:8080") == {
        "server-01": "http://server-01:8080"
    }
    try:
        parse_server_registry("server-01=javascript:alert(1)")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported server URL must be rejected")

    try:
        parse_server_registry("not a server=http://server:8080")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid server ID must be rejected")
