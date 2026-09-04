from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gacore.langTrack.server import create_app
from gacore.langTrack.storage import Storage


@pytest.fixture
def client(tmp_path):
    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage)
    c = TestClient(app)
    yield c, storage
    storage.close()


def test_health(client):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}


def test_ingest_inserts(client):
    c, storage = client
    payload = {
        "device_id": "dev1", "batch_id": "b1", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x", "foreground_ms": 5}}],
    }
    r = c.post("/ingest", json=payload)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    assert storage.event_count() == 1


def test_ingest_idempotent(client):
    c, storage = client
    payload = {
        "device_id": "dev1", "batch_id": "b1", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    c.post("/ingest", json=payload)
    r2 = c.post("/ingest", json=payload)
    assert r2.status_code == 200
    assert r2.json()["deduplicated"] is True
    assert storage.event_count() == 1


def test_ingest_new_event_types(client):
    """采集扩展的新事件类型（snapshot/notification）可正常上报落库。"""
    c, storage = client
    payload = {
        "device_id": "dev1", "batch_id": "b2", "client_ts": 1000,
        "events": [
            {"type": "snapshot", "ts": 1000, "data": {"fg_pkg": "com.x", "battery": 73}},
            {"type": "notification", "ts": 1001, "data": {"pkg": "com.x", "clicked": False}},
        ],
    }
    r = c.post("/ingest", json=payload)
    assert r.status_code == 200
    assert r.json()["inserted"] == 2
    assert storage.event_count() == 2


def test_ingest_invalid_422(client):
    c, _ = client
    # type 不再限制枚举，422 改为缺 events / 缺 ts 等结构性错误
    r = c.post("/ingest", json={"device_id": "d", "batch_id": "b", "client_ts": 1, "events": [{"type": "nope", "data": {}}]})
    assert r.status_code == 422


def test_ingest_normalizes_alias_device(client, tmp_path, monkeypatch):
    """ingest 层别名归一：alias device_id 入库即写主设备（原始层不再带别名）。"""
    import json as _json

    from gacore.langTrack import etl as _etl

    alias_file = tmp_path / "device_aliases.json"
    alias_file.write_text(
        _json.dumps({"dev2": "dev1"}), encoding="utf-8"
    )
    monkeypatch.setattr(_etl, "DEVICE_ALIASES_PATH", alias_file)

    c, storage = client
    payload = {
        "device_id": "dev2", "batch_id": "b1", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    r = c.post("/ingest", json=payload)
    assert r.status_code == 200

    conn = storage._conn
    devs = [r[0] for r in conn.execute("SELECT DISTINCT device_id FROM events")]
    assert devs == ["dev1"]


def test_etl_status_shape(client):
    """/etl/status 暴露 running/last_finished_at/last_ok 三字段。"""
    c, _ = client
    st = c.get("/etl/status").json()
    assert st["running"] is False
    assert set(st) >= {"running", "last_finished_at", "last_ok"}


def test_etl_run_endpoint_triggers_and_guards(client, monkeypatch):
    """/etl/run 异步触发 ETL；运行期间重复请求返回 busy（防重入）。"""
    import time as _time

    from gacore.langTrack import server as server_mod

    calls = []

    def fake_once():
        calls.append(1)
        _time.sleep(0.3)
        return True

    monkeypatch.setattr(server_mod, "_run_etl_once", fake_once)

    c, _ = client
    r1 = c.post("/etl/run")
    assert r1.status_code == 200
    assert r1.json()["status"] == "started"

    r2 = c.post("/etl/run")
    assert r2.status_code == 200
    assert r2.json()["status"] == "busy"

    deadline = _time.time() + 5
    while _time.time() < deadline:
        st = c.get("/etl/status").json()
        if not st["running"]:
            break
        _time.sleep(0.05)
    assert st["running"] is False
    assert st["last_ok"] is True
    assert st["last_finished_at"]
    assert len(calls) == 1
