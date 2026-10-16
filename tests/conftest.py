from datetime import UTC, datetime

import pytest


@pytest.fixture
def valid_state() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "server_id": "checkout-03",
        "service": "checkout-api",
        "config_version": "v1",
        "config": {
            "debug_mode": False,
            "max_connections": 200,
            "log_level": "INFO",
            "tls_min_version": "TLS1.2",
        },
        "environment": {"runtime_version": "3.12", "app_version": "1.4.0"},
        "collected_at": datetime(2026, 10, 16, tzinfo=UTC).isoformat(),
    }
