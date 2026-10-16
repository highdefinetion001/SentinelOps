"""FastAPI application for health, fleet collection, and safe demo controls."""

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.collector.fleet import DEFAULT_FLEET_SERVERS, FleetCollector, parse_server_registry
from backend.app.config.baseline import load_baseline
from backend.app.config.policy import load_policy
from backend.app.config.registry import ServerRegistration, load_server_registry
from backend.app.core.detection import DriftDetector
from backend.app.core.fingerprinting import fingerprint_state
from backend.app.core.scheduler import collection_loop
from backend.app.schemas.detection import DetectionScan
from backend.app.schemas.fleet import (
    DriftRequest,
    DriftResponse,
    FleetCollection,
    ResetResponse,
    ServerCollection,
)
from backend.app.schemas.persistence import (
    ApproveBaselineRequest,
    BaselineList,
    BaselineRecord,
    CreateBaselineCandidateRequest,
    DecommissionRequest,
    ScanDetail,
    ScanList,
    ServerDetail,
    ServerInventoryList,
    ServerInventoryRecord,
)
from backend.app.storage.sqlite import SQLiteDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./data/configguard.db")


def _boolean_setting(name: str, *, default: bool) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise RuntimeError(f"{name} must be a boolean value")
    return value in {"true", "1", "yes"}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    database = SQLiteDatabase(_database_url())
    database.initialize()
    registrations = configured_server_registrations()
    if os.getenv("SERVER_REGISTRY_FILE"):
        database.register_servers(registrations)
    application.state.database = database
    application.state.scan_lock = asyncio.Lock()
    scheduler_task: asyncio.Task[None] | None = None
    if _boolean_setting("MONITOR_SCHEDULER_ENABLED", default=True):
        worker_count = int(os.getenv("WEB_CONCURRENCY", "1"))
        if worker_count != 1:
            database.close()
            raise RuntimeError("Scheduled collection requires exactly one Monitor worker process")
        interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
        if not 1 <= interval <= 86_400:
            database.close()
            raise RuntimeError("SCAN_INTERVAL_SECONDS must be between 1 and 86400")

        async def scheduled_scan() -> None:
            async with application.state.scan_lock:
                await _execute_scan(
                    get_fleet_collector(),
                    get_drift_detector(),
                    database,
                    idempotency_key=None,
                )

        scheduler_task = asyncio.create_task(
            collection_loop(scheduled_scan, interval), name="configguard-collection-loop"
        )
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        database.close()


app = FastAPI(title="ConfigGuard AI", version="0.3.0", lifespan=lifespan)


class HealthResponse(BaseModel):
    status: str
    application: str
    dependencies_checked: bool


class ReadyResponse(BaseModel):
    status: str
    database: str
    registered_servers: int


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
    return {item.server_id: item.endpoint for item in configured_server_registrations()}


def configured_server_registrations() -> list[ServerRegistration]:
    registry_file = os.getenv("SERVER_REGISTRY_FILE")
    if registry_file:
        return load_server_registry(registry_file)
    raw = os.getenv("FLEET_SERVERS")
    if raw is None:
        default_path = PROJECT_ROOT / "config" / "servers.json"
        if default_path.exists():
            return load_server_registry(default_path)
        raw = ",".join(f"{server_id}={url}" for server_id, url in DEFAULT_FLEET_SERVERS.items())
    return [
        ServerRegistration(
            server_id=server_id,
            display_name=server_id.replace("-", " ").title(),
            role="application",
            endpoint=url,
        )
        for server_id, url in parse_server_registry(raw).items()
    ]


def demo_controls_enabled() -> bool:
    return os.getenv("DEMO_MODE", "false").strip().lower() in {"true", "1", "yes"}


def get_fleet_collector() -> FleetCollector:
    registrations = configured_server_registrations()
    return FleetCollector(
        {item.server_id: item.endpoint for item in registrations},
        registrations={item.server_id: item for item in registrations},
    )


def get_drift_detector() -> DriftDetector:
    baseline = load_baseline(PROJECT_ROOT / "config" / "baseline.json")
    policy = load_policy(PROJECT_ROOT / "config" / "policies.yaml")
    if baseline.policy_version != policy.policy_version:
        raise ValueError("Approved baseline and detection policy versions do not match")
    return DriftDetector(baseline, policy)


def get_database(request: Request) -> SQLiteDatabase:
    database = getattr(request.app.state, "database", None)
    if database is None:
        database = SQLiteDatabase(_database_url())
        try:
            database.initialize()
            if os.getenv("SERVER_REGISTRY_FILE"):
                database.register_servers(configured_server_registrations())
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise _storage_error() from exc
        request.app.state.database = database
    return database


def _storage_error() -> HTTPException:
    return HTTPException(status_code=503, detail="Persistent storage is unavailable")


def _server_or_404(database: SQLiteDatabase, server_id: str) -> dict[str, object]:
    record = database.get_server(server_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown server")
    return record


def _include_unconfigured(
    collection: FleetCollection,
    registered_servers: list[dict[str, object]],
    configured_ids: set[str],
) -> FleetCollection:
    seen = {result.server_id for result in collection.servers}
    missing = [
        str(server["server_id"])
        for server in registered_servers
        if server["server_id"] not in configured_ids
        and server["status"] != "decommissioned"
        and server["server_id"] not in seen
    ]
    if not missing:
        return collection
    failures = [
        ServerCollection(
            server_id=server_id,
            status="failed",
            error={
                "kind": "not_configured",
                "message": "Registered server is absent from the configured fleet registry.",
            },
        )
        for server_id in sorted(missing)
    ]
    servers = [*collection.servers, *failures]
    successful = sum(item.status == "success" for item in servers)
    failed = len(servers) - successful
    return FleetCollection(
        collected_at=collection.collected_at,
        status="complete" if failed == 0 else "partial" if successful else "failed",
        total=len(servers),
        successful=successful,
        failed=failed,
        servers=servers,
    )


def _baseline_response(record: dict[str, object]) -> BaselineRecord:
    return BaselineRecord.model_validate(record)


def _server_response(record: dict[str, object], database: SQLiteDatabase) -> ServerDetail:
    current = database.get_current_baseline(str(record["server_id"]))
    return ServerDetail.model_validate(
        {**record, "current_baseline": _baseline_response(current) if current else None}
    )


async def get_upstream_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0, connect=1.5), trust_env=False
    ) as client:
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


@app.get("/api/ready", response_model=ReadyResponse)
async def readiness(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> ReadyResponse:
    """Check local database readiness for the Compose healthcheck."""
    try:
        with database.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return ReadyResponse(
            status="ready",
            database="ready",
            registered_servers=len(database.list_servers(limit=10000)),
        )
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/fleet/states", response_model=FleetCollection)
async def fleet_states(
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> FleetCollection:
    """Collect configured servers concurrently, returning partial outcomes when needed."""
    collection = await collector.collect()
    try:
        collection = _include_unconfigured(
            collection,
            database.list_servers(limit=10000),
            set(collector.server_urls),
        )
        policy = load_policy(PROJECT_ROOT / "config" / "policies.yaml")
        database.reconcile_collection(collection, policy_version=policy.policy_version)
    except sqlite3.Error as exc:
        raise _storage_error() from exc
    return collection


@app.post("/api/scans", response_model=DetectionScan)
async def run_detection_scan(
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
    detector: Annotated[DriftDetector, Depends(get_drift_detector)],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ] = None,
) -> DetectionScan:
    """Persist an on-demand deterministic scan with baseline-version provenance."""
    lock = getattr(request.app.state, "scan_lock", None)
    if lock is None:
        lock = asyncio.Lock()
    async with lock:
        if idempotency_key:
            try:
                previous = database.get_scan_by_idempotency_key(idempotency_key)
            except sqlite3.Error as exc:
                raise _storage_error() from exc
            if previous:
                return previous["result"]
        return await _execute_scan(collector, detector, database, idempotency_key=idempotency_key)


async def _execute_scan(
    collector: FleetCollector,
    detector: DriftDetector,
    database: SQLiteDatabase,
    *,
    idempotency_key: str | None,
) -> DetectionScan:
    """Collect, reconcile, detect, and persist one scan with one resolved baseline set."""
    started_at = datetime.now(UTC)
    collection = await collector.collect()
    try:
        collection = _include_unconfigured(
            collection,
            database.list_servers(limit=10000),
            set(collector.server_urls),
        )
        statuses = database.reconcile_collection(
            collection, policy_version=detector.policy.policy_version
        )
        approved = database.approved_baselines()
        blocked = {
            server_id: status
            for server_id, status in statuses.items()
            if status in {"identity_conflict", "decommissioned"}
        }
        scan = detector.detect(
            collection,
            scanned_at=collection.collected_at,
            approved_baselines=approved,
            blocked_servers=blocked,
        )
        completed_at = datetime.now(UTC)
        stored_scan_id = database.save_scan(
            scan,
            started_at=started_at,
            completed_at=completed_at,
            idempotency_key=idempotency_key,
        )
        if stored_scan_id != scan.scan_id:
            previous = database.get_scan(stored_scan_id)
            if previous:
                return previous["result"]
    except sqlite3.Error as exc:
        raise _storage_error() from exc
    return scan


@app.get("/api/servers", response_model=ServerInventoryList)
async def list_servers(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ServerInventoryList:
    try:
        records = database.list_servers(limit=limit, offset=offset)
        return ServerInventoryList(
            items=[ServerInventoryRecord.model_validate(record) for record in records],
            limit=limit,
            offset=offset,
        )
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/servers/{server_id}", response_model=ServerDetail)
async def get_server(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> ServerDetail:
    try:
        return _server_response(_server_or_404(database, server_id), database)
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/servers/{server_id}/baseline", response_model=BaselineRecord)
async def current_baseline(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> BaselineRecord:
    try:
        _server_or_404(database, server_id)
        record = database.get_current_baseline(server_id)
        if record is None:
            raise HTTPException(status_code=404, detail="No approved baseline exists")
        return _baseline_response(record)
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/servers/{server_id}/baselines", response_model=BaselineList)
async def baseline_history(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> BaselineList:
    try:
        _server_or_404(database, server_id)
        records = database.list_baselines(server_id)
        return BaselineList(
            server_id=server_id,
            items=[_baseline_response(record) for record in records],
        )
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/servers/{server_id}/baseline-candidates", response_model=BaselineList)
async def baseline_candidates(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> BaselineList:
    try:
        _server_or_404(database, server_id)
        records = database.list_baselines(server_id, status="pending")
        return BaselineList(
            server_id=server_id,
            items=[_baseline_response(record) for record in records],
        )
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.post(
    "/api/servers/{server_id}/baseline-candidates", response_model=BaselineRecord, status_code=201
)
async def create_baseline_candidate(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    request: CreateBaselineCandidateRequest,
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
    detector: Annotated[DriftDetector, Depends(get_drift_detector)],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> BaselineRecord:
    _server_url(server_id, configured_servers())
    observed = await collector.collect_server(server_id)
    if observed.status != "success" or observed.state is None:
        raise HTTPException(
            status_code=502, detail="A validated server state could not be collected"
        )
    collection = FleetCollection(
        collected_at=datetime.now(UTC),
        status="complete",
        total=1,
        successful=1,
        failed=0,
        servers=[observed],
    )
    try:
        states = database.reconcile_collection(
            collection, policy_version=detector.policy.policy_version
        )
        if states.get(server_id) == "identity_conflict":
            raise HTTPException(
                status_code=409, detail="Server identity conflicts with its enrollment"
            )
        if states.get(server_id) == "decommissioned":
            raise HTTPException(
                status_code=409, detail="Decommissioned servers cannot be enrolled again"
            )
        record = database.create_candidate(
            server_id,
            observed.state,
            created_by=request.requested_by,
            reason=request.reason,
            policy_version=detector.policy.policy_version,
        )
        return _baseline_response(database.get_baseline(server_id, record["baseline_id"]) or record)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown server") from exc
    except ValueError as exc:
        if str(exc) == "server_decommissioned":
            raise HTTPException(
                status_code=409, detail="Decommissioned servers cannot be enrolled again"
            ) from exc
        if str(exc) == "identity_conflict":
            raise HTTPException(
                status_code=409, detail="Server identity conflicts with its enrollment"
            ) from exc
        raise HTTPException(
            status_code=409, detail="Baseline candidate could not be created"
        ) from exc
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.post("/api/servers/{server_id}/baselines/{baseline_id}/approve", response_model=BaselineRecord)
async def approve_baseline(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    baseline_id: UUID,
    request: ApproveBaselineRequest,
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> BaselineRecord:
    try:
        record = database.approve_candidate(
            server_id,
            str(baseline_id),
            approved_by=request.approved_by,
            reason=request.reason,
        )
        record = database.get_baseline(server_id, str(baseline_id)) or record
        return _baseline_response(record)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Server or baseline candidate not found"
        ) from exc
    except ValueError as exc:
        if str(exc) == "server_decommissioned":
            raise HTTPException(
                status_code=409, detail="Decommissioned servers cannot approve baselines"
            ) from exc
        if str(exc) == "identity_conflict":
            raise HTTPException(
                status_code=409, detail="Server identity must be reconciled first"
            ) from exc
        raise HTTPException(
            status_code=409, detail="Baseline candidate is not pending approval"
        ) from exc
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.post("/api/servers/{server_id}/decommission", response_model=ServerDetail)
async def decommission_server(
    server_id: Annotated[
        str, ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    request: DecommissionRequest,
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> ServerDetail:
    del request
    try:
        return _server_response(database.decommission(server_id), database)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown server") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Server is already decommissioned") from exc
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/scans", response_model=ScanList)
async def scan_history(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ScanList:
    try:
        return ScanList(
            items=[*database.list_scans(limit=limit, offset=offset)], limit=limit, offset=offset
        )
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/scans/{scan_id}", response_model=ScanDetail)
async def scan_detail(
    scan_id: UUID,
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> ScanDetail:
    try:
        record = database.get_scan(str(scan_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown scan")
        return ScanDetail.model_validate(record)
    except sqlite3.Error as exc:
        raise _storage_error() from exc


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
