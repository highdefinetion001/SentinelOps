"""Small simulated application server with safe, opt-in demo state changes."""

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
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
        inline = os.getenv("SERVER_CONFIG_JSON")
        if inline:
            return ServerDefinition.model_validate_json(inline)
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
    display_name: str | None = None,
    role: str = "application",
    identity_file: str | Path | None = None,
) -> FastAPI:
    definition = _load_definition(config_path)
    if expected_server_id is not None and definition.server_id != expected_server_id:
        raise RuntimeError(
            f"Configured server ID {definition.server_id!r} does not match "
            f"SERVER_ID {expected_server_id!r}"
        )

    initial_config = definition.config
    actual_display_name = display_name or definition.server_id
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", role):
        raise RuntimeError("SERVER_ROLE contains invalid characters")
    identity_path = Path(identity_file) if identity_file else None
    instance_id = _load_or_create_instance_id(identity_path) if identity_path else uuid.uuid4().hex
    override_path = identity_path.with_name("demo-config.json") if identity_path else None
    runtime = {"config": _load_demo_override(override_path, initial_config)}
    application = FastAPI(title=f"Simulated {definition.server_id}", version="0.1.0")

    @application.middleware("http")
    async def collector_metadata(request, call_next):
        result: Response = await call_next(request)
        result.headers["X-ConfigGuard-Server-ID"] = definition.server_id
        result.headers["X-ConfigGuard-Display-Name"] = actual_display_name
        result.headers["X-ConfigGuard-Role"] = role
        result.headers["X-ConfigGuard-Collector-Version"] = "1.0"
        result.headers["X-ConfigGuard-Instance-ID"] = instance_id
        return result

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "server_id": definition.server_id,
            "display_name": actual_display_name,
            "role": role,
            "collector_version": "1.0",
        }

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
        _save_demo_override(override_path, runtime["config"])
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
        if override_path is not None:
            override_path.unlink(missing_ok=True)
        return ResetResult(
            server_id=definition.server_id,
            status="reset",
            expected_fingerprint=fingerprint_state(_server_state(definition, initial_config)),
        )

    return application


def _load_or_create_instance_id(path: Path) -> str:
    """Keep a server identity stable across container restarts through its own volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="ascii").strip()
        uuid.UUID(hex=value)
        return value
    except FileNotFoundError:
        value = uuid.uuid4().hex
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(value)
        except FileExistsError:
            return _load_or_create_instance_id(path)
        return value
    except (OSError, ValueError) as exc:
        raise RuntimeError("Server identity file is invalid or unavailable") from exc


def _load_demo_override(path: Path | None, initial: ServerConfig) -> ServerConfig:
    if path is None or not path.exists():
        return initial
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return ServerConfig.model_validate(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Persisted demo configuration is invalid") from exc


def _save_demo_override(path: Path | None, config: ServerConfig) -> None:
    if path is None:
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


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
_display_name = os.getenv("SERVER_DISPLAY_NAME", _server_id)
_role = os.getenv("SERVER_ROLE", "application")
_identity_file = os.getenv("SERVER_IDENTITY_FILE")
app = create_app(
    _config_path,
    expected_server_id=_server_id,
    demo_enabled=_environment_bool("DEMO_MODE"),
    display_name=_display_name,
    role=_role,
    identity_file=_identity_file,
)
