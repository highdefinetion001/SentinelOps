"""Small simulated application server with safe, opt-in demo state changes."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.core.fingerprinting import fingerprint_state
from backend.app.schemas.server_state import EnvironmentMetadata, ServerConfig, ServerStateV1


class ServerDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"]
    server_id: str
    service: str
    config_version: str
    config: ServerConfig
    environment: EnvironmentMetadata


class DemoScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scenario: Literal["security_drift", "performance_drift"]


class DriftResult(BaseModel):
    server_id: str
    scenario: Literal["security_drift", "performance_drift"]
    status: Literal["applied"]


class ResetResult(BaseModel):
    server_id: str
    status: Literal["reset"]
    expected_fingerprint: str


def _load_definition(config_path: str | Path) -> ServerDefinition:
    try:
        return ServerDefinition.model_validate_json(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise RuntimeError(f"Unable to load simulated server configuration: {exc}") from exc


def _server_state(definition: ServerDefinition, config: ServerConfig) -> ServerStateV1:
    return ServerStateV1(
        schema_version=definition.schema_version,
        server_id=definition.server_id,
        service=definition.service,
        config_version=definition.config_version,
        config=config,
        environment=definition.environment,
        collected_at=datetime.now(UTC),
    )


def create_app(
    config_path: str | Path,
    *,
    expected_server_id: str | None = None,
    demo_enabled: bool = False,
) -> FastAPI:
    definition = _load_definition(config_path)
    if expected_server_id is not None and definition.server_id != expected_server_id:
        raise RuntimeError(
            f"Configured server ID {definition.server_id!r} does not match "
            f"SERVER_ID {expected_server_id!r}"
        )

    initial_config = definition.config
    runtime = {"config": initial_config}
    application = FastAPI(title=f"Simulated {definition.server_id}", version="0.1.0")

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "server_id": definition.server_id}

    @application.get("/state", response_model=ServerStateV1)
    async def state() -> ServerStateV1:
        return _server_state(definition, runtime["config"])

    @application.post("/demo/drift", response_model=DriftResult)
    async def simulate_drift(request: DemoScenario) -> DriftResult:
        if not demo_enabled:
            raise HTTPException(status_code=404, detail="Demo controls are disabled")
        scenario_values = {
            "security_drift": {"debug_mode": True},
            "performance_drift": {"max_connections": 10},
        }
        updated = runtime["config"].model_dump()
        updated.update(scenario_values[request.scenario])
        runtime["config"] = ServerConfig.model_validate(updated)
        return DriftResult(
            server_id=definition.server_id,
            scenario=request.scenario,
            status="applied",
        )

    @application.post("/demo/reset", response_model=ResetResult)
    async def reset() -> ResetResult:
        if not demo_enabled:
            raise HTTPException(status_code=404, detail="Demo controls are disabled")
        runtime["config"] = initial_config
        return ResetResult(
            server_id=definition.server_id,
            status="reset",
            expected_fingerprint=fingerprint_state(_server_state(definition, initial_config)),
        )

    return application


def _environment_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise RuntimeError(f"{name} must be a boolean value")
    return value in {"true", "1", "yes"}


_server_id = os.getenv("SERVER_ID", "server-01")
_config_path = os.getenv(
    "SERVER_CONFIG",
    str(Path(__file__).parent / "server_configs" / f"{_server_id}.json"),
)
app = create_app(
    _config_path,
    expected_server_id=_server_id,
    demo_enabled=_environment_bool("DEMO_MODE"),
)
