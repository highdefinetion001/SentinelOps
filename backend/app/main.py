"""FastAPI application for health, fleet collection, and safe demo controls."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.collector.fleet import DEFAULT_FLEET_SERVERS, FleetCollector, parse_server_registry
from backend.app.config.baseline import load_baseline
from backend.app.config.policy import load_policy
from backend.app.core.detection import DriftDetector
from backend.app.core.fingerprinting import fingerprint_state
from backend.app.schemas.detection import DetectionScan
from backend.app.schemas.fleet import DriftRequest, DriftResponse, FleetCollection, ResetResponse

app = FastAPI(title="ConfigGuard AI", version="0.1.0")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class HealthResponse(BaseModel):
    status: str
    application: str
    dependencies_checked: bool


class _DriftAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    server_id: str
    scenario: str
    status: str


class _ResetAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    server_id: str
    status: str
    expected_fingerprint: str


def configured_servers() -> dict[str, str]:
    raw = os.getenv("FLEET_SERVERS")
    if raw is None:
        raw = ",".join(f"{server_id}={url}" for server_id, url in DEFAULT_FLEET_SERVERS.items())
    return parse_server_registry(raw)


def demo_controls_enabled() -> bool:
    return os.getenv("DEMO_MODE", "false").strip().lower() in {"true", "1", "yes"}


def get_fleet_collector() -> FleetCollector:
    return FleetCollector(configured_servers())


def get_drift_detector() -> DriftDetector:
    baseline = load_baseline(PROJECT_ROOT / "config" / "baseline.json")
    policy = load_policy(PROJECT_ROOT / "config" / "policies.yaml")
    if baseline.policy_version != policy.policy_version:
        raise ValueError("Approved baseline and detection policy versions do not match")
    return DriftDetector(baseline, policy)


async def get_upstream_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.5)) as client:
        yield client


def _server_url(server_id: str, registry: dict[str, str]) -> str:
    try:
        return registry[server_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown server ID") from exc


async def _post_demo_control(
    client: httpx.AsyncClient,
    url: str,
    *,
    path: str,
    body: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        request_options = {"json": body} if body is not None else {}
        response = await client.post(f"{url}{path}", **request_options)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Demo control request timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Demo server could not be reached") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=409, detail="Demo controls are disabled on the server")
    if not response.is_success:
        raise HTTPException(status_code=502, detail="Demo server rejected the control request")
    try:
        result = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Demo server returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Demo server returned an invalid response")
    return result


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report process health without implying external dependencies were checked."""
    return HealthResponse(status="ok", application="configguard-ai", dependencies_checked=False)


@app.get("/api/fleet/states", response_model=FleetCollection)
async def fleet_states(
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
) -> FleetCollection:
    """Collect configured servers concurrently, returning partial outcomes when needed."""
    return await collector.collect()


@app.post("/api/scans", response_model=DetectionScan)
async def run_detection_scan(
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
    detector: Annotated[DriftDetector, Depends(get_drift_detector)],
) -> DetectionScan:
    """Collect the current fleet and return deterministic baseline and peer assessments."""
    collection = await collector.collect()
    return detector.detect(collection, scanned_at=collection.collected_at)


@app.post("/api/fleet/{server_id}/simulate-drift", response_model=DriftResponse, status_code=202)
async def simulate_fleet_drift(
    server_id: str,
    request: DriftRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
) -> DriftResponse:
    registry = configured_servers()
    url = _server_url(server_id, registry)
    if not demo_controls_enabled():
        raise HTTPException(status_code=409, detail="Demo controls are disabled")

    result = await _post_demo_control(
        client, url, path="/demo/drift", body={"scenario": request.scenario}
    )
    try:
        acknowledgement = _DriftAcknowledgement.model_validate(result)
    except ValidationError as exc:
        raise HTTPException(
            status_code=502, detail="Demo server returned an invalid acknowledgement"
        ) from exc
    if (
        acknowledgement.server_id != server_id
        or acknowledgement.scenario != request.scenario
        or acknowledgement.status != "applied"
    ):
        raise HTTPException(
            status_code=502, detail="Demo server acknowledgement did not match request"
        )
    return DriftResponse(server_id=server_id, scenario=request.scenario, status="applied")


@app.post("/api/fleet/{server_id}/reset", response_model=ResetResponse)
async def reset_fleet_server(
    server_id: str,
    client: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
) -> ResetResponse:
    registry = configured_servers()
    url = _server_url(server_id, registry)
    if not demo_controls_enabled():
        raise HTTPException(status_code=409, detail="Demo controls are disabled")

    result = await _post_demo_control(client, url, path="/demo/reset")
    try:
        acknowledgement = _ResetAcknowledgement.model_validate(result)
    except ValidationError as exc:
        raise HTTPException(
            status_code=502, detail="Demo server returned an invalid reset response"
        ) from exc
    if acknowledgement.server_id != server_id or acknowledgement.status != "reset":
        raise HTTPException(
            status_code=502, detail="Demo server reset acknowledgement did not match"
        )

    state_result = await collector.collect_server(server_id)
    if state_result.status != "success" or state_result.state is None:
        raise HTTPException(status_code=502, detail="Reset could not be verified from server state")
    if fingerprint_state(state_result.state) != acknowledgement.expected_fingerprint:
        raise HTTPException(
            status_code=502, detail="Reset state did not match the server's initial state"
        )
    return ResetResponse(
        server_id=server_id,
        status="reset",
        verified=True,
        fingerprint=acknowledgement.expected_fingerprint,
    )
