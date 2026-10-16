"""FastAPI application for health, fleet collection, and safe demo controls."""

import asyncio
import logging
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
from backend.app.core.ai_analysis import (
    AIAnalysisFailure,
    OpenAICompatibleAnalysisClient,
    build_drift_evidence,
    build_local_demo_explanation,
)
from backend.app.core.detection import DriftDetector
from backend.app.core.fingerprinting import fingerprint_state
from backend.app.core.scheduler import collection_loop
from backend.app.core.server_templates import load_server_templates, resolve_server_template
from backend.app.provisioner import InternalProvisionRequest, InternalProvisionResponse
from backend.app.schemas.ai_analysis import AIAnalysisRecord
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
from backend.app.schemas.provisioning import (
    ProvisioningResult,
    ProvisionServerRequest,
    ServerTemplateList,
)
from backend.app.storage.sqlite import SQLiteDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
AI_PROVIDER_ID = "openai_compatible"
LOCAL_DEMO_PROVIDER_ID = "local_demo"
LOCAL_DEMO_MODEL_ID = "predefined-template-v1"


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
    application.state.provision_locks = {}
    application.state.ai_analysis_tasks = {}
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
                    application=application,
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
        analysis_tasks = list(application.state.ai_analysis_tasks.values())
        for task in analysis_tasks:
            task.cancel()
        if analysis_tasks:
            await asyncio.gather(*analysis_tasks, return_exceptions=True)
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
    demo_controls_enabled: bool


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


def _collector_with_managed_servers(
    collector: FleetCollector, database: SQLiteDatabase
) -> FleetCollector:
    registrations = dict(collector.registrations)
    urls = dict(collector.server_urls)
    for raw in database.list_managed_registrations():
        try:
            registration = ServerRegistration.model_validate(raw)
        except ValidationError:
            continue
        if registration.server_id not in urls:
            urls[registration.server_id] = registration.endpoint
            registrations[registration.server_id] = registration
    return FleetCollector(
        urls,
        registrations=registrations,
        timeout=collector.timeout,
        transport=collector.transport,
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
            demo_controls_enabled=demo_controls_enabled(),
        )
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.get("/api/server-templates", response_model=ServerTemplateList)
async def server_templates() -> ServerTemplateList:
    try:
        return ServerTemplateList(items=list(load_server_templates().values()))
    except (RuntimeError, ValidationError) as exc:
        raise HTTPException(status_code=503, detail="Server templates are unavailable") from exc


def _provisioning_response(
    request: ProvisionServerRequest, record: dict[str, object]
) -> ProvisioningResult:
    status = record.get("provisioning_status") or "provisioning"
    if status not in {
        "pending_baseline",
        "provisioning",
        "starting",
        "healthy",
        "registered",
        "active",
        "failed",
        "unavailable",
    }:
        status = "provisioning"
    health_status = record.get("health_status") or "unknown"
    if health_status not in {"healthy", "unknown", "unhealthy"}:
        health_status = "unknown"
    return ProvisioningResult(
        request_id=request.request_id,
        server_id=request.server_id,
        display_name=request.display_name,
        role=request.role,
        template_id=request.template_id,
        status=status,
        health_status=health_status,
        container_name=record.get("container_name"),
        baseline_status=(
            "approved"
            if record.get("current_baseline_id") is not None
            else "pending_approval"
            if status == "pending_baseline"
            else "not_created"
        ),
        message=(
            "Server is provisioned and awaiting explicit baseline approval."
            if status == "pending_baseline"
            else "This request is already being provisioned."
        ),
        error_step=record.get("error_step"),
    )


@app.post("/api/servers/provision", response_model=ProvisioningResult, status_code=201)
async def provision_server(
    request: ProvisionServerRequest,
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    detector: Annotated[DriftDetector, Depends(get_drift_detector)],
) -> ProvisioningResult:
    locks = getattr(app.state, "provision_locks", None)
    if locks is None:
        locks = {}
        app.state.provision_locks = locks
    lock = locks.setdefault(request.server_id, asyncio.Lock())
    async with lock:
        return await _provision_server(request, database, detector)


async def _provision_server(
    request: ProvisionServerRequest,
    database: SQLiteDatabase,
    detector: DriftDetector,
) -> ProvisioningResult:
    try:
        template = resolve_server_template(request.template_id, request.role)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=422, detail="Template is not valid for the selected role"
        ) from exc

    endpoint = f"http://{request.server_id}:8080"
    payload_hash_source = request.model_dump(mode="json")
    try:
        _, is_new = database.reserve_managed_server(
            server_id=request.server_id,
            display_name=request.display_name,
            role=request.role,
            endpoint=endpoint,
            request_id=str(request.request_id),
            request_payload=payload_hash_source,
            template_id=template.template_id,
            template_version=template.version,
            description=request.description,
            config=template.config.model_dump(mode="json"),
        )
        record = database.get_server(request.server_id)
        operation = database.get_managed_operation(request.server_id)
        if record is None or operation is None:
            raise sqlite3.IntegrityError("managed server reservation disappeared")
        if not is_new and operation["provisioning_status"] not in {
            "provisioning",
            "starting",
            "healthy",
            "registered",
            "failed",
        }:
            return _provisioning_response(request, record)
    except ValueError as exc:
        if str(exc) == "server_id_conflict":
            raise HTTPException(status_code=409, detail="Server ID is already registered") from exc
        raise HTTPException(status_code=409, detail="Server could not be reserved") from exc
    except sqlite3.Error as exc:
        raise _storage_error() from exc

    step = "container_provisioning"
    provisioner_url = os.getenv("PROVISIONER_URL", "http://provisioner:8090").rstrip("/")
    provisioner_attempted = False
    try:
        database.update_managed_server(
            request.server_id, status="starting", health_status="unknown"
        )
        internal_request = InternalProvisionRequest(
            request=request,
            service=template.service,
            config_version=template.config_version,
            config=template.config,
            environment=template.environment,
        )
        provisioner_attempted = True
        response = await _create_server_via_provisioner(provisioner_url, internal_request)
        if response.status_code != 201:
            raise HTTPException(
                status_code=502, detail="Provisioning service could not create the server"
            )
        provisioned = InternalProvisionResponse.model_validate(response.json())
        database.update_managed_server(
            request.server_id,
            status="healthy",
            health_status="healthy",
            container_id=provisioned.container_id,
            container_name=provisioned.container_name,
        )

        step = "identity_verification"
        registration = ServerRegistration(
            server_id=request.server_id,
            display_name=request.display_name,
            role=request.role,
            endpoint=provisioned.endpoint,
        )
        statuses = database.register_servers([registration])
        if statuses.get(request.server_id) == "identity_conflict":
            raise HTTPException(
                status_code=409, detail="New server identity conflicts with inventory"
            )

        step = "initial_collection"
        collector = FleetCollector(
            {request.server_id: provisioned.endpoint},
            registrations={request.server_id: registration},
        )
        observed = await collector.collect_server(request.server_id)
        if observed.status != "success" or observed.state is None:
            raise HTTPException(
                status_code=502, detail="New server did not return valid identity and state"
            )
        collection = FleetCollection(
            collected_at=datetime.now(UTC),
            status="complete",
            total=1,
            successful=1,
            failed=0,
            servers=[observed],
        )
        database.update_managed_server(
            request.server_id,
            status="registered",
            health_status="healthy",
            container_id=provisioned.container_id,
            container_name=provisioned.container_name,
        )
        step = "baseline_candidate_creation"
        states = database.reconcile_collection(
            collection, policy_version=detector.policy.policy_version
        )
        if states.get(request.server_id) in {"identity_conflict", "decommissioned"}:
            raise HTTPException(status_code=409, detail="Server identity cannot be enrolled")
        database.create_candidate(
            request.server_id,
            observed.state,
            created_by="sentinelops-provisioner",
            reason=f"Initial enrollment using {template.template_id}",
            policy_version=detector.policy.policy_version,
        )
        database.update_managed_server(
            request.server_id,
            status="pending_baseline",
            health_status="healthy",
            container_id=provisioned.container_id,
            container_name=provisioned.container_name,
        )
        final = database.get_server(request.server_id)
        assert final is not None
        return _provisioning_response(request, final)
    except HTTPException as exc:
        safe_message = str(exc.detail) if isinstance(exc.detail, str) else "Provisioning failed"
        if provisioner_attempted:
            await _cleanup_provisioned_server(provisioner_url, request)
        try:
            database.update_managed_server(
                request.server_id,
                status="failed",
                health_status="unhealthy",
                error_step=step,
                error_summary=safe_message,
            )
        except (KeyError, sqlite3.Error):
            pass
        raise HTTPException(
            status_code=exc.status_code,
            detail=f"Provisioning failed during {step}: {safe_message}",
        ) from exc
    except (httpx.HTTPError, ValidationError, sqlite3.Error, ValueError) as exc:
        if provisioner_attempted:
            await _cleanup_provisioned_server(provisioner_url, request)
        try:
            database.update_managed_server(
                request.server_id,
                status="failed",
                health_status="unhealthy",
                error_step=step,
                error_summary=(
                    "Provisioning failed; inspect Monitor readiness and the provisioning service."
                ),
            )
        except (KeyError, sqlite3.Error):
            pass
        logger.warning("Dynamic server provisioning failed during %s", step)
        raise HTTPException(
            status_code=502,
            detail=f"Provisioning failed during {step}; the server was not enrolled",
        ) from exc


async def _cleanup_provisioned_server(base_url: str, request: ProvisionServerRequest) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            await client.request(
                "DELETE",
                f"{base_url}/internal/servers/{request.server_id}",
                json={"request_id": str(request.request_id)},
            )
    except httpx.HTTPError:
        logger.warning("Provisioner cleanup could not be confirmed for a failed request")


async def _create_server_via_provisioner(
    base_url: str, request: InternalProvisionRequest
) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(75.0, connect=2.0), trust_env=False
    ) as client:
        return await client.post(
            f"{base_url}/internal/servers", json=request.model_dump(mode="json")
        )


@app.get("/api/fleet/states", response_model=FleetCollection)
async def fleet_states(
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> FleetCollection:
    """Collect configured servers concurrently, returning partial outcomes when needed."""
    collector = _collector_with_managed_servers(collector, database)
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
        return await _execute_scan(
            collector,
            detector,
            database,
            idempotency_key=idempotency_key,
            application=request.app,
        )


async def _execute_scan(
    collector: FleetCollector,
    detector: DriftDetector,
    database: SQLiteDatabase,
    *,
    idempotency_key: str | None,
    application: FastAPI | None = None,
) -> DetectionScan:
    """Collect, reconcile, detect, and persist one scan with one resolved baseline set."""
    collector = _collector_with_managed_servers(collector, database)
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
        if _scan_has_ai_evidence(scan):
            try:
                _ensure_scan_analysis(scan, database, application)
            except sqlite3.Error:
                logger.warning("AI analysis status could not be persisted for a completed scan.")
    except sqlite3.Error as exc:
        raise _storage_error() from exc
    return scan


def _ai_is_enabled() -> bool:
    try:
        return _boolean_setting("AI_ENABLED", default=False)
    except RuntimeError:
        return False


def _configured_ai_model() -> str | None:
    return os.getenv("AI_MODEL", "").strip() or None


def _scan_has_ai_evidence(scan: DetectionScan) -> bool:
    return any(
        server.outcome == "drifted"
        and server.baseline_drift_detected is True
        and bool(server.findings)
        for server in scan.servers
    )


def _ensure_scan_analysis(
    scan: DetectionScan,
    database: SQLiteDatabase,
    application: FastAPI | None,
    *,
    explicit_retry: bool = False,
) -> AIAnalysisRecord:
    drifted_server_ids = [
        server.server_id
        for server in scan.servers
        if server.outcome == "drifted"
        and server.baseline_drift_detected is True
        and server.findings
    ]
    record = database.create_or_retry_ai_analysis(
        scan.scan_id,
        enabled=_ai_is_enabled(),
        provider=AI_PROVIDER_ID,
        model=_configured_ai_model(),
        server_ids=drifted_server_ids,
        explicit_retry=explicit_retry,
    )
    if _ai_is_enabled() and record.status == "pending" and application is not None:
        _start_ai_analysis(application, scan, database)
    return record


def _start_ai_analysis(application: FastAPI, scan: DetectionScan, database: SQLiteDatabase) -> None:
    tasks: dict[str, asyncio.Task[None]] = application.state.ai_analysis_tasks
    current = tasks.get(scan.scan_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        _complete_ai_analysis(scan, database), name=f"ai-analysis-{scan.scan_id}"
    )
    tasks[scan.scan_id] = task
    task.add_done_callback(lambda completed: tasks.pop(scan.scan_id, None))


def _ai_client_from_environment() -> OpenAICompatibleAnalysisClient:
    try:
        timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))
        return OpenAICompatibleAnalysisClient(
            base_url=os.getenv(
                "AI_BASE_URL", "https://caused-camps-consortium-midi.trycloudflare.com/v1"
            ),
            api_key=os.getenv("AI_API_KEY", ""),
            model=os.getenv("AI_MODEL", ""),
            timeout_seconds=timeout,
        )
    except (ValueError, AIAnalysisFailure) as exc:
        if isinstance(exc, AIAnalysisFailure):
            raise
        raise AIAnalysisFailure("AI configuration is invalid.") from exc


async def _complete_ai_analysis(scan: DetectionScan, database: SQLiteDatabase) -> None:
    try:
        server_roles = {
            server.server_id: (database.get_server(server.server_id) or {}).get("role")
            for server in scan.servers
            if server.outcome == "drifted"
        }
        evidence = build_drift_evidence(scan, server_roles=server_roles)
        model, explanation = await _ai_client_from_environment().analyze(evidence)
        database.complete_ai_analysis(
            scan.scan_id, model=model, provider=AI_PROVIDER_ID, explanation=explanation
        )
    except AIAnalysisFailure as exc:
        try:
            if _local_demo_fallback_enabled():
                evidence = build_drift_evidence(
                    scan,
                    server_roles={
                        server.server_id: (database.get_server(server.server_id) or {}).get("role")
                        for server in scan.servers
                        if server.outcome == "drifted"
                    },
                )
                database.complete_ai_analysis(
                    scan.scan_id,
                    model=LOCAL_DEMO_MODEL_ID,
                    provider=LOCAL_DEMO_PROVIDER_ID,
                    explanation=build_local_demo_explanation(evidence),
                )
            else:
                database.fail_ai_analysis(scan.scan_id, error_summary=str(exc))
        except sqlite3.Error:
            logger.warning("AI analysis failure status could not be persisted.")
    except sqlite3.Error:
        logger.warning("AI analysis could not read or update persistent scan data.")


def _local_demo_fallback_enabled() -> bool:
    try:
        return _boolean_setting("AI_DEMO_FALLBACK", default=True)
    except RuntimeError:
        return False


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
    collector = _collector_with_managed_servers(collector, database)
    _server_url(server_id, collector.server_urls)
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


@app.get("/api/scans/{scan_id}/analysis", response_model=AIAnalysisRecord)
async def scan_ai_analysis(
    scan_id: UUID,
    request: Request,
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> AIAnalysisRecord:
    """Retrieve a cached explanation, scheduling one for an older drift scan if needed."""
    try:
        stored = database.get_scan(str(scan_id))
        if stored is None:
            raise HTTPException(status_code=404, detail="Unknown scan")
        scan = stored["result"]
        if not _scan_has_ai_evidence(scan):
            raise HTTPException(status_code=409, detail="This scan has no eligible drift findings")
        return _ensure_scan_analysis(scan, database, request.app)
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.post("/api/scans/{scan_id}/analysis", response_model=AIAnalysisRecord)
async def request_scan_ai_analysis(
    scan_id: UUID,
    request: Request,
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> AIAnalysisRecord:
    """Request or retry advisory analysis without changing the deterministic scan."""
    try:
        stored = database.get_scan(str(scan_id))
        if stored is None:
            raise HTTPException(status_code=404, detail="Unknown scan")
        scan = stored["result"]
        if not _scan_has_ai_evidence(scan):
            raise HTTPException(status_code=409, detail="This scan has no eligible drift findings")
        return _ensure_scan_analysis(scan, database, request.app, explicit_retry=True)
    except sqlite3.Error as exc:
        raise _storage_error() from exc


@app.post("/api/fleet/{server_id}/simulate-drift", response_model=DriftResponse, status_code=202)
async def simulate_fleet_drift(
    server_id: str,
    request: DriftRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_upstream_client)],
    collector: Annotated[FleetCollector, Depends(get_fleet_collector)],
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> DriftResponse:
    collector = _collector_with_managed_servers(collector, database)
    registry = collector.server_urls
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
    database: Annotated[SQLiteDatabase, Depends(get_database)],
) -> ResetResponse:
    collector = _collector_with_managed_servers(collector, database)
    registry = collector.server_urls
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
