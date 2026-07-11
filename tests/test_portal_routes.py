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


def test_dashboard_returns_all_six_keys():
    """Dashboard API should return all 6 aggregation keys"""
    client = TestClient(app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_task" in data
    assert "recent_tasks" in data
    assert "data_overview" in data
    assert "llm_overview" in data
    assert "latest_profile" in data
    assert "latest_changes" in data


def test_dashboard_data_overview_is_list():
    """data_overview should be a list of {source, count} dicts"""
    client = TestClient(app)
    resp = client.get("/api/dashboard")
    data = resp.json()
    assert isinstance(data["data_overview"], list)
    for item in data["data_overview"]:
        assert "source" in item
        assert "count" in item


def test_dashboard_llm_overview_has_stats():
    """llm_overview should have total_calls, total_tokens, success_rate"""
    client = TestClient(app)
    resp = client.get("/api/dashboard")
    data = resp.json()
    llm = data["llm_overview"]
    assert "total_calls" in llm
    assert "total_tokens" in llm
    assert "success_rate" in llm


def test_list_tasks():
    client = TestClient(app)
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_create_invalid_task_type():
    client = TestClient(app)
    resp = client.post("/api/tasks", json={"task_type": "invalid", "params": {}})
    assert resp.status_code == 400


def test_get_nonexistent_task():
    client = TestClient(app)
    resp = client.get("/api/tasks/99999")
    assert resp.status_code == 404


def test_list_raw_data():
    client = TestClient(app)
    resp = client.get("/api/data/raw")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_list_raw_data_with_source_filter():
    client = TestClient(app)
    resp = client.get("/api/data/raw?source=trae")
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["source"] == "trae"


def test_get_raw_data_not_found():
    client = TestClient(app)
    resp = client.get("/api/data/raw/99999")
    assert resp.status_code == 404


def test_list_intents():
    client = TestClient(app)
    resp = client.get("/api/data/intents")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


def test_list_llm_calls():
    client = TestClient(app)
    resp = client.get("/api/llm/calls")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_get_llm_call_not_found():
    client = TestClient(app)
    resp = client.get("/api/llm/calls/99999")
    assert resp.status_code == 404
