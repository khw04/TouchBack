from fastapi.testclient import TestClient
from app.main import app


def test_health_reports_server_without_agent_readiness():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "agent_ready": False}
        assert client.get("/api/unknown").status_code == 404


def test_frontend_modules_are_served_as_javascript():
    with TestClient(app) as client:
        for path in ("/app.mjs?v=13", "/api.mjs?v=13", "/fixtures.mjs?v=9", "/presentation.mjs?v=13", "/read-gate.mjs"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].split(";")[0] == "text/javascript", path
