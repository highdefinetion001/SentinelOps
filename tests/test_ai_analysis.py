import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.core.ai_analysis import (
    AIAnalysisFailure,
    OpenAICompatibleAnalysisClient,
    build_drift_evidence,
)
from backend.app.main import app
from backend.app.schemas.ai_analysis import AIExplanation
from backend.app.schemas.detection import (
    DetectionFinding,
    DetectionScan,
    DetectionServerResult,
    EvidenceValue,
    FindingAssessment,
    FindingEvidence,
    PeerComparison,
)
from backend.app.storage.sqlite import SQLiteDatabase, utc_text


def make_scan(*, outcome: str = "drifted") -> DetectionScan:
    drifted = outcome == "drifted"
    server = DetectionServerResult(
        server_id="server-01",
        service_group="checkout-api",
        collection_status="success",
        outcome=outcome,
        baseline_drift_detected=drifted,
        baseline_fingerprint="a" * 64,
        observed_fingerprint="b" * 64,
        findings=(
            [
                DetectionFinding(
                    evidence=FindingEvidence(
                        field_path="config.debug_mode",
                        difference_type="changed_value",
                        expected=EvidenceValue(state="value", value=False),
                        actual=EvidenceValue(state="value", value=True),
                    ),
                    assessment=FindingAssessment(
                        severity="CRITICAL",
                        policy_violation=True,
                        policy_ids=["production-debug-disabled"],
                    ),
                )
            ]
            if drifted
            else []
        ),
        policy_violations=[],
        peer_comparison=PeerComparison(
            server_id="server-01",
            service_group="checkout-api",
            status="insufficient_peers",
            peer_count=0,
            is_outlier=False,
            consensus_size=1,
            similarities=[],
            excluded_peers=[],
        ),
        baseline_id="baseline-01" if drifted else None,
        baseline_version="1" if drifted else None,
    )
    return DetectionScan(
        scan_id="6c4a1a2f-8e6d-4e38-b02a-4195d392e4fa",
        scanned_at=datetime(2026, 10, 16, tzinfo=UTC),
        status="complete",
        detector_version="1.0.0",
        baseline_id=None,
        baseline_version=None,
        baseline_fingerprint=None,
        policy_version="1.0",
        total=1,
        successful=1,
        failed=0,
        drifted=int(drifted),
        unable_to_assess=0,
        pending_baseline=0,
        servers=[server],
        baseline_versions={"server-01": "1" if drifted else None},
    )


def insert_scan(database: SQLiteDatabase, scan: DetectionScan) -> None:
    now = utc_text()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO scans (
                scan_id, started_at, completed_at, status, detector_version, total,
                successful, failed, drifted, unable_to_assess, pending_baseline,
                response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scan.scan_id,
                now,
                now,
                scan.status,
                scan.detector_version,
                scan.total,
                scan.successful,
                scan.failed,
                scan.drifted,
                scan.unable_to_assess,
                scan.pending_baseline,
                scan.model_dump_json(),
            ),
        )


def test_evidence_contains_only_allowlisted_verified_drift_and_redacts_secrets() -> None:
    scan = make_scan()
    server = scan.servers[0]
    secret_finding = DetectionFinding(
        evidence=FindingEvidence(
            field_path="config.api_key",
            difference_type="changed_value",
            expected=EvidenceValue(state="value", value="old-key", redacted=True),
            actual=EvidenceValue(state="value", value="super-secret-token", redacted=True),
        ),
        assessment=FindingAssessment(
            severity="HIGH", policy_violation=True, policy_ids=["secret-policy"]
        ),
    )
    unsafe = scan.model_copy(
        update={
            "servers": [server.model_copy(update={"findings": [*server.findings, secret_finding]})]
        }
    )

    evidence = build_drift_evidence(unsafe, server_roles={"server-01": "web"})
    serialized = json.dumps(evidence)

    assert evidence["servers"][0]["role"] == "web"
    assert evidence["servers"][0]["baseline_version"] == "1"
    assert [item["field"] for item in evidence["servers"][0]["findings"]] == ["config.debug_mode"]
    assert "super-secret-token" not in serialized
    assert "api_key" not in serialized


def test_client_discovers_the_single_available_model_and_validates_structured_response() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "discovered-test-model"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Debug mode was enabled on the web server.",
                                    "impact": "This may expose diagnostic details to users.",
                                    "remediation": ["Restore the approved debug setting."],
                                    "prevention": ["Alert on production debug changes."],
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = OpenAICompatibleAnalysisClient(
        base_url="https://ai.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    model, explanation = asyncio.run(client.analyze({"scan_id": "safe-id", "servers": []}))

    assert model == "discovered-test-model"
    assert explanation.format == "structured"
    assert explanation.remediation == ["Restore the approved debug setting."]
    assert [request.url.path for request in requested] == ["/v1/models", "/v1/chat/completions"]
    body = json.loads(requested[1].content)
    assert body["model"] == "discovered-test-model"
    assert body["response_format"] == {"type": "json_object"}


def test_client_falls_back_to_labeled_text_when_json_mode_is_unsupported() -> None:
    completion_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal completion_count
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        completion_count += 1
        if completion_count == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format json_object is unsupported"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Debug mode differs from baseline."}}]},
        )

    client = OpenAICompatibleAnalysisClient(
        base_url="https://ai.example.test/v1", transport=httpx.MockTransport(handler)
    )
    model, explanation = asyncio.run(client.analyze({"scan_id": "safe-id"}))

    assert model == "test-model"
    assert completion_count == 2
    assert explanation.format == "text"
    assert explanation.summary == "Debug mode differs from baseline."


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": "not json"}}]}
            ),
            "malformed structured explanation",
        ),
        (
            lambda request: (_ for _ in ()).throw(
                httpx.ReadTimeout("request timed out", request=request)
            ),
            "timed out",
        ),
        (
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("dns lookup failed", request=request)
            ),
            "could not be reached",
        ),
    ],
)
def test_client_reports_malformed_timeout_and_unreachable_provider_safely(handler, message) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return handler(request)

    client = OpenAICompatibleAnalysisClient(
        base_url="https://ai.example.test/v1", transport=httpx.MockTransport(route)
    )
    with pytest.raises(AIAnalysisFailure, match=message):
        asyncio.run(client.analyze({"scan_id": "safe-id"}))


def test_analysis_persists_without_changing_the_scan_and_cached_request_does_not_call_ai(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ai-analysis.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "true")
    scan = make_scan()
    calls = 0

    class FakeClient:
        async def analyze(self, evidence):
            nonlocal calls
            calls += 1
            assert evidence["servers"][0]["findings"][0]["field"] == "config.debug_mode"
            return "mock-model-v1", AIExplanation(
                summary="Debug mode was enabled.",
                impact="Diagnostic information may be exposed.",
                remediation=["Restore the approved setting."],
                prevention=["Alert on changes to debug mode."],
            )

    monkeypatch.setattr("backend.app.main._ai_client_from_environment", lambda: FakeClient())

    with TestClient(app) as client:
        database = client.app.state.database
        insert_scan(database, scan)
        before = database.get_scan(scan.scan_id)["result"].model_dump_json()
        requested = client.post(f"/api/scans/{scan.scan_id}/analysis")
        assert requested.status_code == 200
        assert requested.json()["status"] == "pending"
        for _ in range(100):
            result = client.get(f"/api/scans/{scan.scan_id}/analysis")
            if result.json()["status"] != "pending":
                break
            asyncio.run(asyncio.sleep(0.01))
        cached = client.post(f"/api/scans/{scan.scan_id}/analysis")
        after = database.get_scan(scan.scan_id)["result"].model_dump_json()

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["model"] == "mock-model-v1"
    assert result.json()["explanation"]["summary"] == "Debug mode was enabled."
    assert cached.json()["status"] == "completed"
    assert calls == 1
    assert before == after


def test_clean_scan_has_no_analysis_record_or_ai_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'clean-scan.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "true")
    scan = make_scan(outcome="clean")

    class UnexpectedClient:
        async def analyze(self, evidence):
            pytest.fail("clean scans must never be sent to the AI provider")

    monkeypatch.setattr("backend.app.main._ai_client_from_environment", lambda: UnexpectedClient())
    with TestClient(app) as client:
        database = client.app.state.database
        insert_scan(database, scan)
        response = client.post(f"/api/scans/{scan.scan_id}/analysis")
        record = database.get_ai_analysis(scan.scan_id)

    assert response.status_code == 409
    assert record is None


def test_failed_analysis_is_cached_until_explicit_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'retry-analysis.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_DEMO_FALLBACK", "false")
    scan = make_scan()
    calls = 0

    class RetryClient:
        async def analyze(self, evidence):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AIAnalysisFailure("AI provider could not be reached.")
            return "retry-model", AIExplanation(
                summary="The approved debug setting differs.",
                impact="The change may expose diagnostic output.",
                remediation=["Restore the approved setting."],
                prevention=["Alert when this setting changes."],
            )

    monkeypatch.setattr("backend.app.main._ai_client_from_environment", lambda: RetryClient())
    with TestClient(app) as client:
        insert_scan(client.app.state.database, scan)
        initial = client.post(f"/api/scans/{scan.scan_id}/analysis")
        for _ in range(100):
            result = client.get(f"/api/scans/{scan.scan_id}/analysis")
            if result.json()["status"] != "pending":
                break
            asyncio.run(asyncio.sleep(0.01))
        cached = client.get(f"/api/scans/{scan.scan_id}/analysis")
        retried = client.post(f"/api/scans/{scan.scan_id}/analysis")
        for _ in range(100):
            result = client.get(f"/api/scans/{scan.scan_id}/analysis")
            if result.json()["status"] != "pending":
                break
            asyncio.run(asyncio.sleep(0.01))

    assert initial.json()["status"] == "pending"
    assert cached.json()["status"] == "failed"
    assert retried.json()["status"] == "pending"
    assert result.json()["status"] == "completed"
    assert calls == 2


def test_provider_failure_persists_an_explicitly_labeled_local_demo_explanation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'local-demo.db'}")
    monkeypatch.setenv("MONITOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_DEMO_FALLBACK", "true")
    scan = make_scan()
    attempts = 0

    class UnavailableClient:
        async def analyze(self, evidence):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise AIAnalysisFailure("AI provider could not be reached.")
            return "ollama-qwen2.5:1.5b", AIExplanation(
                summary="The model explained the detected debug setting change.",
                impact="The policy marks this setting as critical in production.",
                remediation=["Restore the approved debug setting."],
                prevention=["Monitor this setting for future changes."],
            )

    monkeypatch.setattr("backend.app.main._ai_client_from_environment", lambda: UnavailableClient())
    with TestClient(app) as client:
        insert_scan(client.app.state.database, scan)
        client.post(f"/api/scans/{scan.scan_id}/analysis")
        for _ in range(100):
            response = client.get(f"/api/scans/{scan.scan_id}/analysis")
            if response.json()["status"] != "pending":
                break
            asyncio.run(asyncio.sleep(0.01))
        assert response.json()["provider"] == "local_demo"
        retry = client.post(f"/api/scans/{scan.scan_id}/analysis")
        assert retry.json()["status"] == "pending"
        for _ in range(100):
            response = client.get(f"/api/scans/{scan.scan_id}/analysis")
            if response.json()["status"] != "pending":
                break
            asyncio.run(asyncio.sleep(0.01))

    record = response.json()
    assert record["status"] == "completed"
    assert record["provider"] == "openai_compatible"
    assert record["model"] == "ollama-qwen2.5:1.5b"
    assert "model explained" in record["explanation"]["summary"]


def test_ai_analysis_storage_survives_reopen_and_returns_disabled_status(tmp_path) -> None:
    path = tmp_path / "persisted-analysis.db"
    first = SQLiteDatabase(f"sqlite:///{path}")
    first.initialize()
    insert_scan(first, make_scan())
    disabled = first.create_or_retry_ai_analysis(
        make_scan().scan_id,
        enabled=False,
        provider="openai_compatible",
        model=None,
        server_ids=["server-01"],
    )
    assert disabled.status == "disabled"
    first.close()

    reopened = SQLiteDatabase(f"sqlite:///{path}")
    reopened.initialize()
    pending = reopened.create_or_retry_ai_analysis(
        make_scan().scan_id,
        enabled=True,
        provider="openai_compatible",
        model=None,
        server_ids=["server-01"],
        explicit_retry=True,
    )
    assert pending.analysis_id == disabled.analysis_id
    assert pending.status == "pending"
    completed = AIExplanation(
        summary="The setting changed.",
        impact="This could affect operations.",
        remediation=["Restore the approved value."],
        prevention=["Monitor configuration changes."],
    )
    reopened.complete_ai_analysis(
        make_scan().scan_id, model="persisted-model", explanation=completed
    )
    reopened.close()

    final = SQLiteDatabase(f"sqlite:///{path}")
    final.initialize()
    record = final.get_ai_analysis(make_scan().scan_id)
    assert record is not None
    assert record.status == "completed"
    assert record.model == "persisted-model"
    assert record.server_ids == ["server-01"]
    assert record.explanation == completed
    final.close()


def test_schema_v2_database_migrates_ai_analysis_table(tmp_path) -> None:
    path = tmp_path / "schema-v2.db"
    database = SQLiteDatabase(f"sqlite:///{path}")
    database.initialize()
    with database.connection() as connection:
        connection.execute("DROP TABLE ai_analyses")
        connection.execute("PRAGMA user_version = 2")
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'ai_analyses'"
            ).fetchone()[0]
            == "ai_analyses"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'managed_servers'"
            ).fetchone()[0]
            == "managed_servers"
        )
    database.close()
