from fastapi.testclient import TestClient

from backend.app.main import app


def test_health_reports_application_without_claiming_dependency_readiness() -> None:
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "application": "configguard-ai",
        "dependencies_checked": False,
    }
