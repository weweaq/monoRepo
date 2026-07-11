from fastapi.testclient import TestClient
from profile.portal.app import app


def test_root_returns_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_dashboard_endpoint_exists():
    client = TestClient(app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_task" in data
    assert "recent_tasks" in data
    assert "data_overview" in data
    assert "llm_overview" in data
