"""Concurrent, partial-result collector for configured simulated servers."""

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from backend.app.core.fingerprinting import fingerprint_state
from backend.app.schemas.fleet import (
    CollectionFailure,
    FleetCollection,
    ServerCollection,
)
from backend.app.schemas.server_state import ServerStateV1

DEFAULT_FLEET_SERVERS = {
    "server-01": "http://server-01:8080",
    "server-02": "http://server-02:8080",
    "server-03": "http://server-03:8080",
}


def parse_server_registry(raw: str) -> dict[str, str]:
    """Parse an operator-controlled comma list of explicit `server-id=url` entries."""
    registry: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            raise ValueError("FLEET_SERVERS entries must use server-id=http(s)://host:port")
        server_id, url = (part.strip() for part in item.split("=", maxsplit=1))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", server_id) or server_id in registry:
            raise ValueError("FLEET_SERVERS contains an empty or duplicate server ID")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"Invalid port for configured server {server_id!r}") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or port == 0
        ):
            raise ValueError(f"Invalid base URL for configured server {server_id!r}")
        registry[server_id] = url.rstrip("/")
    if not registry:
        raise ValueError("FLEET_SERVERS must configure at least one server")
    return registry


class FleetCollector:
    """Fetch all configured server states concurrently and preserve individual failures."""

    def __init__(
        self,
        server_urls: Mapping[str, str],
        *,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.server_urls = dict(server_urls)
        self.timeout = timeout or httpx.Timeout(4.0, connect=1.5)
        self.transport = transport

    async def collect(self) -> FleetCollection:
        collected_at = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            servers = await asyncio.gather(
                *(
                    self._collect_one(client, server_id, url)
                    for server_id, url in self.server_urls.items()
                )
            )

        successful = sum(server.status == "success" for server in servers)
        failed = len(servers) - successful
        status = "complete" if failed == 0 else "partial" if successful else "failed"
        return FleetCollection(
            collected_at=collected_at,
            status=status,
            total=len(servers),
            successful=successful,
            failed=failed,
            servers=servers,
        )

    async def collect_server(self, server_id: str) -> ServerCollection:
        """Collect one explicitly configured server, used to verify reset operations."""
        if server_id not in self.server_urls:
            raise ValueError(f"Server {server_id!r} is not configured")
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            return await self._collect_one(client, server_id, self.server_urls[server_id])

    async def _collect_one(
        self, client: httpx.AsyncClient, server_id: str, base_url: str
    ) -> ServerCollection:
        try:
            response = await client.get(f"{base_url}/state")
        except httpx.TimeoutException:
            return self._failure(server_id, "timeout", "State request timed out.")
        except httpx.ConnectError:
            return self._failure(server_id, "unreachable", "Server could not be reached.")
        except httpx.RequestError:
            return self._failure(server_id, "request_error", "State request failed.")

        if not response.is_success:
            return self._failure(
                server_id,
                "http_error",
                "Server returned an unsuccessful HTTP response.",
                upstream_status=response.status_code,
            )

        try:
            json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._failure(server_id, "invalid_json", "Server response was not valid JSON.")

        try:
            state = ServerStateV1.model_validate_json(response.content)
        except ValidationError:
            return self._failure(
                server_id, "invalid_state", "Server response did not match schema version 1.0."
            )

        if state.server_id != server_id:
            return self._failure(
                server_id,
                "invalid_state",
                "Server identity did not match the configured server ID.",
            )

        return ServerCollection(
            server_id=server_id,
            status="success",
            fingerprint=fingerprint_state(state),
            state=state,
        )

    @staticmethod
    def _failure(
        server_id: str,
        kind: str,
        message: str,
        *,
        upstream_status: int | None = None,
    ) -> ServerCollection:
        return ServerCollection(
            server_id=server_id,
            status="failed",
            error=CollectionFailure(kind=kind, message=message, upstream_status=upstream_status),
        )
