"""SQLite persistence for inventory, immutable baseline versions, and scan history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from backend.app.config.baseline import BaselineV1
from backend.app.core.fingerprinting import fingerprint_config_environment
from backend.app.schemas.ai_analysis import AIAnalysisRecord, AIExplanation
from backend.app.schemas.detection import DetectionScan
from backend.app.schemas.fleet import FleetCollection
from backend.app.schemas.server_state import ServerStateV1

if TYPE_CHECKING:
    from backend.app.config.registry import ServerRegistration

SCHEMA_VERSION = 4


def utc_text(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Database timestamps must be timezone-aware")
    return current.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def database_path(database_url: str) -> Path | None:
    """Resolve local SQLite URLs; remote database schemes are intentionally unsupported."""
    if database_url == ":memory:":
        return None
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("DATABASE_URL must be sqlite:///path or :memory:")
    raw_path = database_url[len(prefix) :]
    if not raw_path:
        raise ValueError("DATABASE_URL must include a database path")
    return Path(raw_path)


class SQLiteDatabase:
    """Connections are opened per operation, with WAL and bounded lock waits for files."""

    def __init__(self, database_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.path = database_path(database_url)
        self.timeout_seconds = timeout_seconds
        self._memory_uri = (
            f"file:configguard-{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self.path is None
            else None
        )
        self._memory_anchor = self._new_connection() if self._memory_uri else None

    def _new_connection(self) -> sqlite3.Connection:
        if self._memory_uri:
            connection = sqlite3.connect(
                self._memory_uri,
                uri=True,
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
        else:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        with self.connection() as connection:
            if self.path is not None:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}"
                    )
                if version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    version = SCHEMA_VERSION
                if version == 1:
                    connection.execute("ALTER TABLE servers ADD COLUMN server_type TEXT")
                    connection.execute("ALTER TABLE servers ADD COLUMN endpoint TEXT")
                    connection.execute("ALTER TABLE servers ADD COLUMN instance_id TEXT")
                    version = 2
                if version == 2:
                    connection.execute(_AI_ANALYSES_TABLE)
                    version = 3
                if version == 3:
                    connection.execute(_MANAGED_SERVERS_TABLE)
                    version = 4
                connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def register_servers(self, registrations: list[ServerRegistration]) -> dict[str, str]:
        """Reconcile operator declarations without reactivating or replacing identities."""
        now = utc_text()
        statuses: dict[str, str] = {}
        with self.transaction() as connection:
            for registration in registrations:
                server_id = registration.server_id
                row = connection.execute(
                    "SELECT * FROM servers WHERE server_id = ?", (server_id,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        """INSERT INTO servers (
                            server_id, display_name, role, server_type, endpoint,
                            lifecycle_status, reachability, first_seen
                        ) VALUES (?, ?, NULL, ?, ?, 'newly_enrolled', 'unknown', ?)""",
                        (
                            server_id,
                            registration.display_name,
                            registration.role,
                            registration.endpoint.rstrip("/"),
                            now,
                        ),
                    )
                    statuses[server_id] = "newly_enrolled"
                    continue
                if row["lifecycle_status"] == "decommissioned":
                    statuses[server_id] = "decommissioned"
                    continue
                endpoint = registration.endpoint.rstrip("/")
                conflicts = (row["server_type"] and row["server_type"] != registration.role) or (
                    row["endpoint"] and row["endpoint"] != endpoint
                )
                if conflicts:
                    connection.execute(
                        "UPDATE servers SET lifecycle_status = 'identity_conflict' "
                        "WHERE server_id = ?",
                        (server_id,),
                    )
                    statuses[server_id] = "identity_conflict"
                    continue
                connection.execute(
                    """UPDATE servers SET display_name = ?,
                        server_type = COALESCE(server_type, ?),
                        endpoint = COALESCE(endpoint, ?) WHERE server_id = ?""",
                    (registration.display_name, registration.role, endpoint, server_id),
                )
                statuses[server_id] = row["lifecycle_status"]
        return statuses

    def reserve_managed_server(
        self,
        *,
        server_id: str,
        display_name: str,
        role: str,
        endpoint: str,
        request_id: str,
        request_payload: dict[str, Any],
        template_id: str,
        template_version: str,
        description: str | None,
        config: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Reserve a unique identity and return (record, is_new) transactionally."""
        now = utc_text()
        request_hash = hashlib.sha256(_json(request_payload).encode()).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT m.*, s.endpoint, s.server_type FROM managed_servers m
                    JOIN servers s ON s.server_id = m.server_id WHERE m.server_id = ?""",
                (server_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_id"] == request_id
                    and existing["request_hash"] == request_hash
                ):
                    if existing["provisioning_status"] == "failed":
                        connection.execute(
                            """UPDATE managed_servers SET provisioning_status = 'provisioning',
                                error_step = NULL, error_summary = NULL, updated_at = ?
                                WHERE server_id = ?""",
                            (now, server_id),
                        )
                    return dict(existing), False
                raise ValueError("server_id_conflict")
            static = connection.execute(
                "SELECT 1 FROM servers WHERE server_id = ?", (server_id,)
            ).fetchone()
            if static is not None:
                raise ValueError("server_id_conflict")
            connection.execute(
                """INSERT INTO servers (
                    server_id, display_name, role, server_type, endpoint,
                    lifecycle_status, reachability, first_seen
                ) VALUES (?, ?, NULL, ?, ?, 'newly_enrolled', 'unknown', ?)""",
                (server_id, display_name, role, endpoint, now),
            )
            connection.execute(
                """INSERT INTO managed_servers (
                    server_id, request_id, request_hash, template_id, template_version,
                    description, config_json, provisioning_status, health_status,
                    error_step, error_summary, container_id, container_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning', 'unknown', NULL, NULL,
                    NULL, NULL, ?, ?)""",
                (
                    server_id,
                    request_id,
                    request_hash,
                    template_id,
                    template_version,
                    description,
                    _json(config),
                    now,
                    now,
                ),
            )
        record = self.get_server(server_id)
        assert record is not None
        return record, True

    def update_managed_server(
        self,
        server_id: str,
        *,
        status: str,
        health_status: str,
        container_id: str | None = None,
        container_name: str | None = None,
        error_step: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        if status not in {
            "provisioning",
            "starting",
            "healthy",
            "registered",
            "pending_baseline",
            "active",
            "failed",
            "unavailable",
            "decommissioned",
        }:
            raise ValueError("invalid_provisioning_status")
        if health_status not in {"unknown", "healthy", "unhealthy"}:
            raise ValueError("invalid_health_status")
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE managed_servers SET provisioning_status = ?, health_status = ?,
                    container_id = COALESCE(?, container_id),
                    container_name = COALESCE(?, container_name),
                    error_step = ?, error_summary = ?, updated_at = ? WHERE server_id = ?""",
                (
                    status,
                    health_status,
                    container_id,
                    container_name,
                    error_step,
                    error_summary[:500] if error_summary else None,
                    utc_text(),
                    server_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError("managed_server_not_found")

    def list_managed_registrations(self) -> list[dict[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.server_id, s.display_name, s.server_type AS role, s.endpoint
                    FROM servers s JOIN managed_servers m ON m.server_id = s.server_id
                    WHERE s.lifecycle_status != 'decommissioned'
                    AND m.provisioning_status != 'failed' ORDER BY s.server_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_collection(
        self, collection: FleetCollection, *, policy_version: str = "1.0"
    ) -> dict[str, str]:
        """Upsert discovered identities and create first-enrollment candidates without approval."""
        states: dict[str, str] = {}
        attempt = utc_text(collection.collected_at)
        with self.transaction() as connection:
            for result in collection.servers:
                row = connection.execute(
                    "SELECT * FROM servers WHERE server_id = ?", (result.server_id,)
                ).fetchone()
                state = result.state if result.status == "success" else None
                reported_conflict = bool(result.error and result.error.kind == "identity_conflict")
                if row is None:
                    role = state.service if state else None
                    display_name = result.display_name or _display_name(result.server_id)
                    lifecycle = "identity_conflict" if reported_conflict else "newly_enrolled"
                    connection.execute(
                        """INSERT INTO servers (
                            server_id, display_name, role, server_type, instance_id,
                            lifecycle_status, reachability,
                            first_seen, last_seen, last_attempt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            result.server_id,
                            display_name,
                            role,
                            result.role,
                            result.instance_id,
                            lifecycle,
                            "reachable" if state else "unreachable",
                            attempt,
                            attempt if state else None,
                            attempt,
                        ),
                    )
                else:
                    lifecycle = row["lifecycle_status"]
                    if lifecycle == "decommissioned":
                        states[result.server_id] = "decommissioned"
                        continue
                    if lifecycle == "identity_conflict":
                        connection.execute(
                            "UPDATE servers SET last_attempt = ?, reachability = ? "
                            "WHERE server_id = ?",
                            (attempt, "reachable" if state else "unreachable", result.server_id),
                        )
                        states[result.server_id] = "identity_conflict"
                        continue
                    if state and (
                        (row["server_type"] and result.role and row["server_type"] != result.role)
                        or (
                            row["instance_id"]
                            and result.instance_id
                            and row["instance_id"] != result.instance_id
                        )
                    ):
                        connection.execute(
                            "UPDATE servers SET lifecycle_status = 'identity_conflict', "
                            "reachability = 'reachable', last_attempt = ? WHERE server_id = ?",
                            (attempt, result.server_id),
                        )
                        states[result.server_id] = "identity_conflict"
                        continue
                    if reported_conflict:
                        connection.execute(
                            "UPDATE servers SET lifecycle_status = 'identity_conflict', "
                            "last_attempt = ?, reachability = 'unreachable' WHERE server_id = ?",
                            (attempt, result.server_id),
                        )
                        states[result.server_id] = "identity_conflict"
                        continue
                    if state and row["role"] and state.service != row["role"]:
                        connection.execute(
                            """UPDATE servers SET lifecycle_status = 'identity_conflict',
                                reachability = 'reachable', last_attempt = ?
                                WHERE server_id = ?""",
                            (attempt, result.server_id),
                        )
                        states[result.server_id] = "identity_conflict"
                        continue
                    if state:
                        lifecycle = "active" if row["current_baseline_id"] else "pending_baseline"
                        connection.execute(
                            """UPDATE servers SET role = COALESCE(role, ?),
                                server_type = COALESCE(server_type, ?),
                                instance_id = COALESCE(instance_id, ?),
                                display_name = COALESCE(?, display_name),
                                lifecycle_status = ?, reachability = 'reachable',
                                last_seen = ?, last_attempt = ? WHERE server_id = ?""",
                            (
                                state.service,
                                result.role,
                                result.instance_id,
                                result.display_name,
                                lifecycle,
                                attempt,
                                attempt,
                                result.server_id,
                            ),
                        )
                    else:
                        connection.execute(
                            "UPDATE servers SET reachability = ?, last_attempt = ? "
                            "WHERE server_id = ?",
                            (
                                "not_configured"
                                if result.error and result.error.kind == "not_configured"
                                else "unreachable",
                                attempt,
                                result.server_id,
                            ),
                        )

                if state:
                    baseline_count = connection.execute(
                        "SELECT COUNT(*) FROM baselines WHERE server_id = ?",
                        (result.server_id,),
                    ).fetchone()[0]
                    if baseline_count == 0:
                        self._insert_candidate(
                            connection,
                            result.server_id,
                            state,
                            result.fingerprint,
                            created_by="system:initial-enrollment",
                            reason="Initial observed configuration; approval required.",
                            created_at=attempt,
                            policy_version=policy_version,
                        )
                        lifecycle = "pending_baseline"
                        connection.execute(
                            "UPDATE servers SET lifecycle_status = ? WHERE server_id = ?",
                            (lifecycle, result.server_id),
                        )
                managed_status = (
                    "active"
                    if state and row is not None and row["current_baseline_id"]
                    else "pending_baseline"
                    if state
                    else "unavailable"
                )
                connection.execute(
                    """UPDATE managed_servers SET provisioning_status = ?, health_status = ?,
                        updated_at = ? WHERE server_id = ?""",
                    (
                        managed_status,
                        "healthy" if state else "unhealthy",
                        utc_text(),
                        result.server_id,
                    ),
                )
                states[result.server_id] = lifecycle
        return states

    def create_candidate(
        self,
        server_id: str,
        state: ServerStateV1,
        *,
        created_by: str,
        reason: str | None = None,
        policy_version: str = "1.0",
    ) -> dict[str, Any]:
        """Append an immutable pending candidate; repeated identical requests return it."""
        fingerprint = _state_fingerprint(state)
        created_at = utc_text()
        conflict = False
        candidate_record: dict[str, Any] | None = None
        with self.transaction() as connection:
            server = connection.execute(
                "SELECT * FROM servers WHERE server_id = ?", (server_id,)
            ).fetchone()
            if server is None:
                raise KeyError("server_not_found")
            if server["lifecycle_status"] == "decommissioned":
                raise ValueError("server_decommissioned")
            if server["lifecycle_status"] == "identity_conflict":
                raise ValueError("identity_conflict")
            if server["role"] and server["role"] != state.service:
                connection.execute(
                    "UPDATE servers SET lifecycle_status = 'identity_conflict' WHERE server_id = ?",
                    (server_id,),
                )
                conflict = True
            else:
                existing = connection.execute(
                    """SELECT * FROM baselines WHERE server_id = ? AND status = 'pending'
                       AND fingerprint = ? ORDER BY version DESC LIMIT 1""",
                    (server_id, fingerprint),
                ).fetchone()
                if existing is not None:
                    candidate_record = dict(existing)
                else:
                    candidate_record = self._insert_candidate(
                        connection,
                        server_id,
                        state,
                        fingerprint,
                        created_by=created_by,
                        reason=reason,
                        created_at=created_at,
                        policy_version=policy_version,
                    )
                connection.execute(
                    """UPDATE servers SET role = COALESCE(role, ?),
                        lifecycle_status = CASE WHEN current_baseline_id IS NULL
                            THEN 'pending_baseline' ELSE lifecycle_status END,
                        reachability = 'reachable', last_seen = ?, last_attempt = ?
                        WHERE server_id = ?""",
                    (state.service, created_at, created_at, server_id),
                )
        if conflict:
            raise ValueError("identity_conflict")
        assert candidate_record is not None
        return candidate_record

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection,
        server_id: str,
        state: ServerStateV1,
        fingerprint: str | None,
        *,
        created_by: str,
        reason: str | None,
        created_at: str,
        policy_version: str,
    ) -> dict[str, Any]:
        if not fingerprint:
            fingerprint = _state_fingerprint(state)
        version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM baselines WHERE server_id = ?",
            (server_id,),
        ).fetchone()[0]
        baseline_id = str(uuid.uuid4())
        snapshot = {
            "schema_version": state.schema_version,
            "service_group": state.service,
            "config": state.config.model_dump(mode="json"),
            "environment": state.environment.model_dump(mode="json"),
        }
        connection.execute(
            """INSERT INTO baselines (
                baseline_id, server_id, version, status, snapshot_json, fingerprint,
                created_at, created_by, created_reason, policy_version
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (
                baseline_id,
                server_id,
                version,
                _json(snapshot),
                fingerprint,
                created_at,
                created_by,
                reason,
                policy_version,
            ),
        )
        row = connection.execute(
            "SELECT * FROM baselines WHERE baseline_id = ?", (baseline_id,)
        ).fetchone()
        return dict(row)

    def approve_candidate(
        self,
        server_id: str,
        baseline_id: str,
        *,
        approved_by: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Atomically promote a pending candidate and preserve its former version."""
        approved_at = utc_text()
        with self.transaction() as connection:
            server = connection.execute(
                "SELECT * FROM servers WHERE server_id = ?", (server_id,)
            ).fetchone()
            if server is None:
                raise KeyError("server_not_found")
            if server["lifecycle_status"] == "decommissioned":
                raise ValueError("server_decommissioned")
            if server["lifecycle_status"] == "identity_conflict":
                raise ValueError("identity_conflict")
            candidate = connection.execute(
                "SELECT * FROM baselines WHERE baseline_id = ? AND server_id = ?",
                (baseline_id, server_id),
            ).fetchone()
            if candidate is None:
                raise KeyError("baseline_not_found")
            if candidate["status"] != "pending":
                raise ValueError("baseline_not_pending")
            try:
                snapshot = json.loads(candidate["snapshot_json"])
                ServerStateV1.model_validate_json(
                    json.dumps(
                        {
                            "schema_version": snapshot["schema_version"],
                            "server_id": server_id,
                            "service": snapshot["service_group"],
                            "config_version": "approved-baseline",
                            "config": snapshot["config"],
                            "environment": snapshot["environment"],
                            "collected_at": approved_at,
                        }
                    )
                )
            except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
                raise ValueError("invalid_baseline_snapshot") from exc
            previous_id = server["current_baseline_id"]
            if previous_id:
                connection.execute(
                    "UPDATE baselines SET status = 'superseded' "
                    "WHERE baseline_id = ? AND status = 'approved'",
                    (previous_id,),
                )
            connection.execute(
                """UPDATE baselines SET status = 'approved', approved_at = ?, approved_by = ?,
                    approval_reason = ? WHERE baseline_id = ? AND status = 'pending'""",
                (approved_at, approved_by, reason, baseline_id),
            )
            connection.execute(
                """UPDATE servers SET current_baseline_id = ?, lifecycle_status = 'active'
                    WHERE server_id = ?""",
                (baseline_id, server_id),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM baselines WHERE baseline_id = ?", (baseline_id,)
                ).fetchone()
            )

    def decommission(self, server_id: str) -> dict[str, Any]:
        timestamp = utc_text()
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE servers SET lifecycle_status = 'decommissioned',
                    reachability = 'unknown', decommissioned_at = ? WHERE server_id = ?
                    AND lifecycle_status != 'decommissioned'""",
                (timestamp, server_id),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT * FROM servers WHERE server_id = ?", (server_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("server_not_found")
                raise ValueError("server_decommissioned")
            connection.execute(
                "UPDATE managed_servers SET provisioning_status = 'decommissioned', "
                "updated_at = ? WHERE server_id = ?",
                (timestamp, server_id),
            )
        return self.get_server(server_id) or {}

    def get_managed_operation(self, server_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM managed_servers WHERE server_id = ?", (server_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT s.*, b.version AS current_baseline_version,
                    b.fingerprint AS current_baseline_fingerprint,
                    m.template_id, m.template_version, m.provisioning_status,
                    m.health_status, m.error_step, m.error_summary AS provisioning_error,
                    m.container_id, m.container_name, m.description
                    FROM servers s LEFT JOIN baselines b ON b.baseline_id = s.current_baseline_id
                    LEFT JOIN managed_servers m ON m.server_id = s.server_id
                    WHERE s.server_id = ?""",
                (server_id,),
            ).fetchone()
        return _server_record(row) if row else None

    def list_servers(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.*, b.version AS current_baseline_version,
                    b.fingerprint AS current_baseline_fingerprint,
                    m.template_id, m.template_version, m.provisioning_status,
                    m.health_status, m.error_step, m.error_summary AS provisioning_error,
                    m.container_id, m.container_name, m.description
                    FROM servers s LEFT JOIN baselines b ON b.baseline_id = s.current_baseline_id
                    LEFT JOIN managed_servers m ON m.server_id = s.server_id
                    ORDER BY s.server_id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [_server_record(row) for row in rows]

    def get_baseline(self, server_id: str, baseline_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM baselines WHERE server_id = ? AND baseline_id = ?",
                (server_id, baseline_id),
            ).fetchone()
        if row is None:
            return None
        record = _baseline_record(row)
        server = self.get_server(server_id)
        record["is_current"] = bool(server and server["current_baseline_id"] == baseline_id)
        return record

    def get_current_baseline(self, server_id: str) -> dict[str, Any] | None:
        server = self.get_server(server_id)
        if not server or not server["current_baseline_id"]:
            return None
        return self.get_baseline(server_id, server["current_baseline_id"])

    def list_baselines(self, server_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM baselines WHERE server_id = ? AND status = ? "
                    "ORDER BY version DESC",
                    (server_id, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM baselines WHERE server_id = ? ORDER BY version DESC",
                    (server_id,),
                ).fetchall()
        server = self.get_server(server_id)
        current_id = server["current_baseline_id"] if server else None
        records = [_baseline_record(row) for row in rows]
        for record in records:
            record["is_current"] = record["baseline_id"] == current_id
        return records

    def approved_baselines(self) -> dict[str, BaselineV1]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT b.* FROM baselines b JOIN servers s
                    ON s.current_baseline_id = b.baseline_id
                    WHERE b.status = 'approved' AND s.lifecycle_status = 'active'"""
            ).fetchall()
        baselines: dict[str, BaselineV1] = {}
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            baselines[row["server_id"]] = BaselineV1.model_validate_json(
                json.dumps(
                    {
                        "baseline_id": row["baseline_id"],
                        "service_group": snapshot["service_group"],
                        "baseline_version": str(row["version"]),
                        "schema_version": snapshot["schema_version"],
                        "approval": {
                            "approved_by": row["approved_by"],
                            "approved_at": row["approved_at"],
                            "change_reference": row["approval_reason"] or row["baseline_id"],
                        },
                        "expected_config": snapshot["config"],
                        "expected_environment": snapshot["environment"],
                        "policy_version": row["policy_version"],
                    }
                )
            )
        return baselines

    def save_scan(
        self,
        scan: DetectionScan,
        *,
        started_at: datetime,
        completed_at: datetime,
        idempotency_key: str | None = None,
    ) -> str:
        with self.transaction() as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT scan_id FROM scans WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return existing["scan_id"]
            connection.execute(
                """INSERT INTO scans (
                    scan_id, started_at, completed_at, status, detector_version, total,
                    successful, failed, drifted, unable_to_assess, pending_baseline,
                    idempotency_key, response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan.scan_id,
                    utc_text(started_at),
                    utc_text(completed_at),
                    scan.status,
                    scan.detector_version,
                    scan.total,
                    scan.successful,
                    scan.failed,
                    scan.drifted,
                    scan.unable_to_assess,
                    scan.pending_baseline,
                    idempotency_key,
                    scan.model_dump_json(),
                ),
            )
            for server in scan.servers:
                connection.execute(
                    """INSERT INTO scan_servers (
                        scan_id, server_id, collection_status, outcome, baseline_id,
                        baseline_version, baseline_fingerprint, observed_fingerprint,
                        baseline_drift_detected, findings_json, policy_violations_json,
                        peer_comparison_json, collection_error_json, assessment_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        scan.scan_id,
                        server.server_id,
                        server.collection_status,
                        server.outcome,
                        server.baseline_id,
                        int(server.baseline_version) if server.baseline_version else None,
                        server.baseline_fingerprint,
                        server.observed_fingerprint,
                        None
                        if server.baseline_drift_detected is None
                        else int(server.baseline_drift_detected),
                        _json([item.model_dump(mode="json") for item in server.findings]),
                        _json([item.model_dump(mode="json") for item in server.policy_violations]),
                        _json(server.peer_comparison.model_dump(mode="json")),
                        _json(server.collection_error.model_dump(mode="json"))
                        if server.collection_error
                        else None,
                        server.assessment_error,
                    ),
                )
        return scan.scan_id

    def get_scan_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT scan_id FROM scans WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self.get_scan(row["scan_id"]) if row else None

    def list_scans(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM scans ORDER BY completed_at DESC, scan_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_scan_summary(row) for row in rows]

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            scan = connection.execute(
                "SELECT * FROM scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
            if scan is None:
                return None
        return {
            "started_at": _parse_time(scan["started_at"]),
            "completed_at": _parse_time(scan["completed_at"]),
            "result": DetectionScan.model_validate_json(scan["response_json"]),
        }

    def create_or_retry_ai_analysis(
        self,
        scan_id: str,
        *,
        enabled: bool,
        provider: str,
        model: str | None,
        server_ids: list[str],
        explicit_retry: bool = False,
    ) -> AIAnalysisRecord:
        """Create one scan analysis or requeue a failed/disabled analysis explicitly."""
        now = utc_text()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ai_analyses WHERE scan_id = ?", (scan_id,)
            ).fetchone()
            if row is None:
                status = "pending" if enabled else "disabled"
                error = None if enabled else "AI analysis is disabled by configuration."
                connection.execute(
                    """INSERT INTO ai_analyses (
                        analysis_id, scan_id, server_ids_json, provider, model, status,
                        explanation_json, created_at, updated_at, error_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        scan_id,
                        _json(sorted(set(server_ids))),
                        provider,
                        model,
                        status,
                        now,
                        now,
                        error,
                    ),
                )
            elif (
                explicit_retry
                and enabled
                and (
                    row["status"] in {"failed", "disabled"}
                    or (row["status"] == "completed" and row["provider"] == "local_demo")
                )
            ):
                connection.execute(
                    """UPDATE ai_analyses SET status = 'pending', model = ?,
                        explanation_json = NULL, error_summary = NULL, updated_at = ?
                        WHERE scan_id = ?""",
                    (model, now, scan_id),
                )
            elif not enabled and row["status"] == "pending":
                connection.execute(
                    """UPDATE ai_analyses SET status = 'disabled', error_summary = ?,
                        updated_at = ? WHERE scan_id = ?""",
                    ("AI analysis is disabled by configuration.", now, scan_id),
                )
            result = connection.execute(
                "SELECT * FROM ai_analyses WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        return _ai_analysis_record(result)

    def get_ai_analysis(self, scan_id: str) -> AIAnalysisRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM ai_analyses WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        return _ai_analysis_record(row) if row else None

    def complete_ai_analysis(
        self,
        scan_id: str,
        *,
        model: str,
        explanation: AIExplanation,
        provider: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE ai_analyses SET status = 'completed', model = ?,
                    provider = COALESCE(?, provider), explanation_json = ?,
                    error_summary = NULL, updated_at = ?
                    WHERE scan_id = ? AND status = 'pending'""",
                (model, provider, explanation.model_dump_json(), utc_text(), scan_id),
            )

    def fail_ai_analysis(self, scan_id: str, *, error_summary: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE ai_analyses SET status = 'failed', error_summary = ?,
                    updated_at = ? WHERE scan_id = ? AND status = 'pending'""",
                (error_summary[:500], utc_text(), scan_id),
            )

    def close(self) -> None:
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None


def _state_fingerprint(state: ServerStateV1) -> str:
    return fingerprint_config_environment(state.config, state.environment)


def _display_name(server_id: str) -> str:
    return " ".join(part.capitalize() for part in server_id.replace("_", "-").split("-"))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _server_record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("endpoint", None)
    result.pop("instance_id", None)
    result["managed"] = result.get("template_id") is not None
    for name in ("first_seen", "last_seen", "last_attempt", "decommissioned_at"):
        result[name] = _parse_time(result[name])
    result["status"] = _public_server_status(result)
    return result


def _public_server_status(server: dict[str, Any]) -> str:
    if server["lifecycle_status"] == "decommissioned":
        return "decommissioned"
    if server["lifecycle_status"] == "identity_conflict":
        return "identity_conflict"
    if server.get("provisioning_status") in {
        "provisioning",
        "starting",
        "healthy",
        "failed",
        "unavailable",
    }:
        return server["provisioning_status"]
    if server["reachability"] == "not_configured":
        return "not_configured"
    if server["reachability"] == "unreachable":
        return "unreachable"
    return server["lifecycle_status"]


def _baseline_record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    for name in ("created_at", "approved_at"):
        result[name] = _parse_time(result[name])
    return result


def _scan_summary(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("idempotency_key", None)
    result.pop("response_json", None)
    result["started_at"] = _parse_time(result["started_at"])
    result["completed_at"] = _parse_time(result["completed_at"])
    return result


def _scan_server_record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for name in (
        "findings_json",
        "policy_violations_json",
        "peer_comparison_json",
        "collection_error_json",
    ):
        value = result.pop(name)
        result[name.removesuffix("_json")] = json.loads(value) if value is not None else None
    if result["baseline_drift_detected"] is not None:
        result["baseline_drift_detected"] = bool(result["baseline_drift_detected"])
    return result


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


_AI_ANALYSES_TABLE = """CREATE TABLE IF NOT EXISTS ai_analyses (
    analysis_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL UNIQUE REFERENCES scans(scan_id) ON DELETE CASCADE,
    server_ids_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed', 'disabled')),
    explanation_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_summary TEXT,
    CHECK ((status = 'completed' AND explanation_json IS NOT NULL)
        OR (status != 'completed' AND explanation_json IS NULL))
)"""

_MANAGED_SERVERS_TABLE = """CREATE TABLE IF NOT EXISTS managed_servers (
    server_id TEXT PRIMARY KEY REFERENCES servers(server_id),
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    template_id TEXT NOT NULL,
    template_version TEXT NOT NULL,
    description TEXT,
    config_json TEXT NOT NULL,
    provisioning_status TEXT NOT NULL CHECK (provisioning_status IN (
        'provisioning', 'starting', 'healthy', 'registered', 'pending_baseline',
        'active', 'failed', 'unavailable', 'decommissioned')),
    health_status TEXT NOT NULL CHECK (health_status IN ('unknown', 'healthy', 'unhealthy')),
    error_step TEXT,
    error_summary TEXT,
    container_id TEXT,
    container_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""


_SCHEMA = (
    """CREATE TABLE servers (
        server_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        role TEXT,
        server_type TEXT,
        endpoint TEXT,
        instance_id TEXT,
        lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN (
            'newly_enrolled', 'pending_baseline', 'active', 'identity_conflict', 'decommissioned')),
        reachability TEXT NOT NULL CHECK (reachability IN (
            'unknown', 'reachable', 'unreachable', 'not_configured')),
        first_seen TEXT NOT NULL,
        last_seen TEXT,
        last_attempt TEXT,
        current_baseline_id TEXT REFERENCES baselines(baseline_id),
        decommissioned_at TEXT
    )""",
    """CREATE TABLE baselines (
        baseline_id TEXT PRIMARY KEY,
        server_id TEXT NOT NULL REFERENCES servers(server_id),
        version INTEGER NOT NULL CHECK (version > 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'superseded', 'rejected')),
        snapshot_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_reason TEXT,
        policy_version TEXT NOT NULL,
        approved_at TEXT,
        approved_by TEXT,
        approval_reason TEXT,
        UNIQUE (server_id, version)
    )""",
    "CREATE INDEX baselines_server_status_version ON baselines(server_id, status, version DESC)",
    "CREATE UNIQUE INDEX one_approved_baseline_per_server ON baselines(server_id) "
    "WHERE status = 'approved'",
    """CREATE TABLE scans (
        scan_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
        detector_version TEXT NOT NULL,
        total INTEGER NOT NULL CHECK (total >= 0),
        successful INTEGER NOT NULL CHECK (successful >= 0),
        failed INTEGER NOT NULL CHECK (failed >= 0),
        drifted INTEGER NOT NULL CHECK (drifted >= 0),
        unable_to_assess INTEGER NOT NULL CHECK (unable_to_assess >= 0),
        pending_baseline INTEGER NOT NULL DEFAULT 0 CHECK (pending_baseline >= 0),
        idempotency_key TEXT UNIQUE,
        response_json TEXT NOT NULL
    )""",
    "CREATE INDEX scans_completed_at ON scans(completed_at DESC)",
    """CREATE TABLE scan_servers (
        scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
        server_id TEXT NOT NULL REFERENCES servers(server_id),
        collection_status TEXT NOT NULL CHECK (collection_status IN ('success', 'failed')),
        outcome TEXT NOT NULL CHECK (outcome IN (
            'clean', 'drifted', 'pending_baseline', 'unable_to_assess')),
        baseline_id TEXT REFERENCES baselines(baseline_id),
        baseline_version INTEGER,
        baseline_fingerprint TEXT,
        observed_fingerprint TEXT,
        baseline_drift_detected INTEGER,
        findings_json TEXT NOT NULL,
        policy_violations_json TEXT NOT NULL,
        peer_comparison_json TEXT NOT NULL,
        collection_error_json TEXT,
        assessment_error TEXT,
        PRIMARY KEY (scan_id, server_id)
    )""",
    "CREATE INDEX scan_servers_server ON scan_servers(server_id, scan_id)",
    _AI_ANALYSES_TABLE,
    _MANAGED_SERVERS_TABLE,
)


def _ai_analysis_record(row: sqlite3.Row) -> AIAnalysisRecord:
    return AIAnalysisRecord(
        analysis_id=row["analysis_id"],
        scan_id=row["scan_id"],
        server_ids=json.loads(row["server_ids_json"]),
        provider=row["provider"],
        model=row["model"],
        status=row["status"],
        explanation=(
            AIExplanation.model_validate_json(row["explanation_json"])
            if row["explanation_json"]
            else None
        ),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        error_summary=row["error_summary"],
    )
