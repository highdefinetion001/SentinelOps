"""Narrow internal API for creating only validated SentinelOps fleet containers."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.server_templates import resolve_server_template
from backend.app.schemas.provisioning import ProvisionServerRequest
from backend.app.schemas.server_state import EnvironmentMetadata, ServerConfig

DOCKER_API_VERSION = "v1.41"
FLEET_IMAGE = "configguard-fleet:local"
IDENTITY_TARGET = "/var/lib/configguard"
HEALTHCHECK = [
    "CMD",
    "python",
    "-c",
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)",
]


class InternalProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: ProvisionServerRequest
    service: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=64)
    config: ServerConfig
    environment: EnvironmentMetadata


class InternalProvisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    server_id: str
    container_id: str = Field(pattern=r"^[a-f0-9]{12,64}$")
    container_name: str
    endpoint: str
    health_status: Literal["healthy"]


class InternalRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class DockerEngine:
    def __init__(self) -> None:
        self.base_url = os.getenv("DOCKER_API_URL", "http://docker-api:2375").rstrip("/")
        self.network = os.getenv("MONITORING_NETWORK", "configguard-ai_monitoring")
        self.resource_prefix = os.getenv("RESOURCE_PREFIX", "configguard-ai")
        self.timeout = httpx.Timeout(4.0, connect=2.0)

    async def request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            trust_env=False,
        ) as client:
            return await client.request(
                method,
                f"/{DOCKER_API_VERSION}{path}",
                json=json_body,
            )

    def resource_names(self, server_id: str) -> tuple[str, str]:
        return (
            f"{self.resource_prefix}-sentinel-{server_id}",
            f"{self.resource_prefix}-identity-{server_id}",
        )

    async def provision(self, payload: InternalProvisionRequest) -> InternalProvisionResponse:
        request = payload.request
        container_name, volume_name = self.resource_names(request.server_id)
        labels = {
            "com.sentinelops.managed": "true",
            "com.sentinelops.server-id": request.server_id,
            "com.sentinelops.request-id": str(request.request_id),
        }

        existing = await self._inspect_container(container_name)
        if existing is not None:
            current_labels = existing.get("Config", {}).get("Labels", {})
            if all(current_labels.get(key) == value for key, value in labels.items()):
                state = existing.get("State", {})
                if state.get("Status") != "running":
                    start_response = await self.request(
                        "POST", f"/containers/{quote(existing['Id'], safe='')}/start"
                    )
                    if start_response.status_code not in {204, 304}:
                        raise self._docker_error("container restart", start_response)
                await self._wait_healthy(existing["Id"], request.server_id)
                return self._result(existing["Id"], container_name, request.server_id)
            raise HTTPException(status_code=409, detail="Container name is already in use")

        volume_created = False
        container_id: str | None = None
        try:
            volume_response = await self.request(
                "POST",
                "/volumes/create",
                json_body={"Name": volume_name, "Labels": labels},
            )
            if volume_response.status_code not in {200, 201}:
                raise self._docker_error("identity volume creation", volume_response)
            volume_payload = volume_response.json()
            if volume_payload.get("Name") != volume_name or volume_payload.get("Labels") != labels:
                raise HTTPException(status_code=502, detail="Identity volume verification failed")
            volume_created = True

            definition = {
                "schema_version": "1.0",
                "server_id": request.server_id,
                "service": payload.service,
                "config_version": payload.config_version,
                "config": payload.config.model_dump(mode="json"),
                "environment": payload.environment.model_dump(mode="json"),
            }
            container_config = {
                "Image": FLEET_IMAGE,
                "Env": [
                    f"SERVER_ID={request.server_id}",
                    f"SERVER_DISPLAY_NAME={request.display_name}",
                    f"SERVER_ROLE={request.role}",
                    f"SERVER_IDENTITY_FILE={IDENTITY_TARGET}/instance-id",
                    f"SERVER_CONFIG_JSON={_json_compact(definition)}",
                    "DEMO_MODE=true",
                ],
                "Labels": labels,
                "ExposedPorts": {"8080/tcp": {}},
                "Healthcheck": {
                    "Test": HEALTHCHECK,
                    "Interval": 5_000_000_000,
                    "Timeout": 2_000_000_000,
                    "Retries": 12,
                    "StartPeriod": 5_000_000_000,
                },
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Memory": 268_435_456,
                    "NanoCpus": 500_000_000,
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Source": volume_name,
                            "Target": IDENTITY_TARGET,
                        }
                    ],
                    "NetworkMode": self.network,
                },
                "NetworkingConfig": {
                    "EndpointsConfig": {self.network: {"Aliases": [request.server_id]}}
                },
            }
            create_response = await self.request(
                "POST",
                f"/containers/create?name={quote(container_name, safe='')}",
                json_body=container_config,
            )
            if create_response.status_code != 201:
                raise self._docker_error("container creation", create_response)
            created = create_response.json()
            container_id = created.get("Id")
            if not isinstance(container_id, str) or not re.fullmatch(
                r"[a-f0-9]{12,64}", container_id
            ):
                raise HTTPException(
                    status_code=502, detail="Docker returned an invalid container ID"
                )

            start_response = await self.request("POST", f"/containers/{container_id}/start")
            if start_response.status_code not in {204, 304}:
                raise self._docker_error("container start", start_response)
            await self._wait_healthy(container_id, request.server_id)
            return self._result(container_id, container_name, request.server_id)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if container_id is not None:
                await self._remove_container(container_id)
            if volume_created:
                await self._remove_volume(volume_name, labels)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status_code=502, detail="Docker Engine could not provision the server"
            ) from exc
        except HTTPException:
            if container_id is not None:
                await self._remove_container(container_id)
            if volume_created:
                await self._remove_volume(volume_name, labels)
            raise

    async def remove(self, server_id: str, request_id: str) -> None:
        container_name, volume_name = self.resource_names(server_id)
        container = await self._inspect_container(container_name)
        labels = {
            "com.sentinelops.managed": "true",
            "com.sentinelops.server-id": server_id,
            "com.sentinelops.request-id": request_id,
        }
        if container is not None:
            actual = container.get("Config", {}).get("Labels", {})
            if not all(actual.get(key) == value for key, value in labels.items()):
                raise HTTPException(
                    status_code=409, detail="Container is not owned by this operation"
                )
            await self._remove_container(container["Id"])
        await self._remove_volume(volume_name, labels)

    async def _inspect_container(self, identifier: str) -> dict | None:
        response = await self.request("GET", f"/containers/{quote(identifier, safe='')}/json")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._docker_error("container lookup", response)
        return response.json()

    async def _wait_healthy(self, identifier: str, server_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            container = await self._inspect_container(identifier)
            if container is None:
                raise HTTPException(status_code=502, detail="Created container disappeared")
            state = container.get("State", {})
            health = state.get("Health", {}).get("Status")
            if health == "healthy":
                return
            if state.get("Status") in {"exited", "dead"} or health == "unhealthy":
                raise HTTPException(
                    status_code=502, detail="Server container failed its health check"
                )
            await asyncio.sleep(1)
        raise HTTPException(status_code=504, detail="Server container health check timed out")

    async def _remove_container(self, identifier: str) -> None:
        response = await self.request(
            "DELETE", f"/containers/{quote(identifier, safe='')}?force=true&v=false"
        )
        if response.status_code not in {204, 404}:
            raise self._docker_error("failed container cleanup", response)

    async def _remove_volume(self, name: str, expected_labels: dict[str, str]) -> None:
        response = await self.request("GET", f"/volumes/{quote(name, safe='')}")
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise self._docker_error("identity volume lookup", response)
        labels = response.json().get("Labels", {})
        if not all(labels.get(key) == value for key, value in expected_labels.items()):
            return
        response = await self.request("DELETE", f"/volumes/{quote(name, safe='')}")
        if response.status_code not in {204, 404}:
            raise self._docker_error("identity volume cleanup", response)

    async def _remove_managed_container(
        self, identifier: str, expected_labels: dict[str, str]
    ) -> None:
        container = await self._inspect_container(identifier)
        if container is None:
            return
        actual = container.get("Config", {}).get("Labels", {})
        if all(actual.get(key) == value for key, value in expected_labels.items()):
            await self._remove_container(container["Id"])

    def _result(self, identifier: str, name: str, server_id: str) -> InternalProvisionResponse:
        return InternalProvisionResponse(
            server_id=server_id,
            container_id=identifier,
            container_name=name,
            endpoint=f"http://{server_id}:8080",
            health_status="healthy",
        )

    @staticmethod
    def _docker_error(operation: str, response: httpx.Response) -> HTTPException:
        status = 409 if response.status_code == 409 else 502
        return HTTPException(
            status_code=status, detail=f"Docker {operation} failed (HTTP {response.status_code})"
        )


def _json_compact(value: dict) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


engine = DockerEngine()
app = FastAPI(title="SentinelOps Provisioner", version="1.0.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/servers", response_model=InternalProvisionResponse, status_code=201)
async def provision_server(request: InternalProvisionRequest) -> InternalProvisionResponse:
    try:
        template = resolve_server_template(request.request.template_id, request.request.role)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Unsupported provisioning template") from exc
    if (
        request.service != template.service
        or request.config_version != template.config_version
        or request.config != template.config
        or request.environment != template.environment
        or request.request.demo_mode is not True
    ):
        raise HTTPException(
            status_code=422, detail="Provisioning payload differs from its approved template"
        )
    return await engine.provision(request)


@app.delete("/internal/servers/{server_id}", status_code=204, response_class=Response)
async def remove_server(server_id: str, request: InternalRemoveRequest) -> Response:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", server_id):
        raise HTTPException(status_code=422, detail="Invalid server ID")
    await engine.remove(server_id, request.request_id)
    return Response(status_code=204)
