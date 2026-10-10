from fastapi.testclient import TestClient
from app.main import app


def test_health_reports_server_without_agent_readiness():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "agent_ready": False}
        assert client.post("/api/runs", json={}).status_code == 404
