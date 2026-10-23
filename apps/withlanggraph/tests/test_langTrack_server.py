from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gacore.langTrack.server as server_mod
from gacore.langTrack.server import create_app
from gacore.langTrack.storage import Storage


def _read_jsonl_logs() -> list[dict]:
    """All app.jsonl records of this test process (GACORE_LOG_ROOT is isolated by conftest)."""
    root = Path(os.environ["GACORE_LOG_ROOT"])
    out: list[dict] = []
    for path in root.glob("*/app.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


@pytest.fixture(autouse=True)
def _isolated_device_aliases(tmp_path, monkeypatch):
    """Never read the developer's real data/device_aliases.json (machine-state coupling)."""
    from gacore.langTrack import etl as _etl

    alias_file = tmp_path / "device_aliases.json"
    alias_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(_etl, "DEVICE_ALIASES_PATH", alias_file)


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Pin ETL knobs: the lifespan-started loop must only block on stop_event, so no
    # test can ever launch the real ETL subprocess.
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)
    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    with TestClient(app) as c:
        yield c, storage
    storage.close()


def test_health(client):
    c, _ = client
    body = c.get("/health").json()
    # status 保持 "ok" 以兼容客户端存活探针；深度结论走 degraded/reasons。
    assert body["status"] == "ok"
    assert body["degraded"] is False
    assert body["reasons"] == []
    assert set(body["components"]) == {"main", "scheduler", "qq", "langtrack_server"}
    assert body["etl"]["running"] is False
    assert body["ingest"]["last_failure_at"] is None


def test_client_version(client):
    """版本自查接口：返回非空 app_version，且与客户端 build.gradle versionName 一致。"""
    c, _ = client
    body = c.get("/api/client/version").json()
    assert isinstance(body.get("app_version"), str)
    assert body["app_version"]  # 非空
    assert "latest_apk_url" in body


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
    """/etl/status 暴露运行状态与最近一次结果（含触发来源/耗时/错误）。"""
    c, _ = client
    st = c.get("/etl/status").json()
    assert st["running"] is False
    assert set(st) >= {
        "running",
        "last_finished_at",
        "last_ok",
        "last_trigger",
        "last_duration_ms",
        "last_error",
    }


def test_etl_run_endpoint_triggers_and_guards(client, monkeypatch):
    """/etl/run 异步触发 ETL；运行期间重复请求返回 busy（防重入）。"""
    import time as _time

    from gacore.langTrack import server as server_mod

    calls = []

    def fake_once(trigger="manual"):
        calls.append(trigger)
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


def test_client_version_reads_config_file(client, tmp_path, monkeypatch):
    """/api/client/version 优先读 data/client_version.json（打包脚本自动写入）。"""
    from gacore.langTrack import server as server_mod

    cfg_dir = tmp_path / "data"
    cfg_dir.mkdir()
    (cfg_dir / "client_version.json").write_text(
        '{"app_version":"9.9","latest_apk_url":"/static/app-debug.apk"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_mod, "_PROJECT_ROOT", tmp_path)

    c, _ = client
    body = c.get("/api/client/version").json()
    assert body["app_version"] == "9.9"
    assert body["latest_apk_url"] == "/static/app-debug.apk"


def test_ingest_emits_completed_log(client):
    """Given a normal batch, When POST /ingest, Then one `ingest completed` line is written."""
    c, _ = client
    payload = {
        "device_id": "dev1", "batch_id": "log-b1", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    assert c.post("/ingest", json=payload).status_code == 200
    hit = [x for x in _read_jsonl_logs() if x.get("message") == "ingest completed" and x.get("batch_id") == "log-b1"]
    assert len(hit) == 1
    assert hit[0]["event_count"] == 1
    assert hit[0]["inserted"] == 1
    assert hit[0]["deduplicated"] is False
    assert hit[0]["client_device_id"] is None
    assert "duration_ms" in hit[0]
    # 不记事件 payload：只有条数与 ID，正文（com.x）绝不能进日志。
    assert "com.x" not in json.dumps(hit[0], ensure_ascii=False)


def test_ingest_alias_logs_client_device_id(client, tmp_path, monkeypatch):
    """别名设备批次：device_id 写主设备，client_device_id 保留客户端原始值。"""
    import json as _json

    from gacore.langTrack import etl as _etl

    alias_file = tmp_path / "device_aliases.json"
    alias_file.write_text(_json.dumps({"dev2": "dev1"}), encoding="utf-8")
    monkeypatch.setattr(_etl, "DEVICE_ALIASES_PATH", alias_file)

    c, _ = client
    payload = {
        "device_id": "dev2", "batch_id": "log-alias", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    assert c.post("/ingest", json=payload).status_code == 200
    hit = [x for x in _read_jsonl_logs() if x.get("message") == "ingest completed" and x.get("batch_id") == "log-alias"]
    assert hit[-1]["device_id"] == "dev1"
    assert hit[-1]["client_device_id"] == "dev2"


def test_ingest_failure_logs_once(tmp_path, monkeypatch):
    """入库异常：路由打一行 ingest failed（含真栈），中间件不重复打 http request crashed。"""
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)
    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(storage, "ingest_batch", boom)
    payload = {
        "device_id": "dev1", "batch_id": "log-fail", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/ingest", json=payload)
    assert r.status_code == 500

    logs = _read_jsonl_logs()
    failed = [x for x in logs if x.get("message") == "ingest failed" and x.get("batch_id") == "log-fail"]
    crashed = [x for x in logs if x.get("message") == "http request crashed" and x.get("path") == "/ingest"]
    assert len(failed) == 1
    assert crashed == []
    assert "Traceback (most recent call last)" in failed[-1]["stack_trace"]
    assert failed[-1]["error_type"] == "RuntimeError"
    assert failed[-1]["client_device_id"] == "dev1"
    assert "duration_ms" in failed[-1]
    storage.close()


def test_ingest_failure_before_storage_still_logs_once(tmp_path, monkeypatch):
    """设备归一化阶段就崩时同样要留下一行（老坑：try 之外抛异常 → 整批零日志）。"""
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)

    def boom(device_id: str) -> str:
        raise RuntimeError("alias boom")

    monkeypatch.setattr(server_mod, "canonical_device_id", boom)
    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    payload = {
        "device_id": "dev1", "batch_id": "log-alias-boom", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/ingest", json=payload)
    assert r.status_code == 500

    logs = _read_jsonl_logs()
    failed = [x for x in logs if x.get("message") == "ingest failed" and x.get("batch_id") == "log-alias-boom"]
    crashed = [x for x in logs if x.get("message") == "http request crashed" and x.get("path") == "/ingest"]
    assert len(failed) == 1
    assert crashed == []
    assert failed[-1]["error_type"] == "RuntimeError"
    storage.close()


def test_ingest_broken_alias_file_degrades_gracefully(tmp_path, monkeypatch):
    """别名文件被写坏（根节点不是对象）时降级为“无别名”，不能让整批 500 或零日志。"""
    from gacore.langTrack import etl as _etl

    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)
    alias_file = tmp_path / "device_aliases.json"
    alias_file.write_text('["broken"]', encoding="utf-8")
    monkeypatch.setattr(_etl, "DEVICE_ALIASES_PATH", alias_file)

    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    payload = {
        "device_id": "dev1", "batch_id": "log-alias-broken", "client_ts": 1000,
        "events": [{"type": "usage", "ts": 1000, "data": {"pkg": "com.x"}}],
    }
    with TestClient(app) as c:
        r = c.post("/ingest", json=payload)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1

    logs = _read_jsonl_logs()
    hit = [x for x in logs if x.get("message") == "ingest completed" and x.get("batch_id") == "log-alias-broken"]
    assert len(hit) == 1
    storage.close()


def test_ingest_422_logs_http_failed(client):
    """校验失败（422）：走 http request failed 分支，带状态码。"""
    c, _ = client
    assert c.post("/ingest", json={"bad": "payload"}).status_code == 422
    hit = [x for x in _read_jsonl_logs() if x.get("message") == "http request failed" and x.get("path") == "/ingest"]
    assert hit and hit[-1]["status"] == 422


def test_repeated_404_logs_once_then_debug(client, monkeypatch):
    """同一路径的重复 404（如 /v1/models 轮询）只在首次记 WARNING，之后降 DEBUG 并带计数。"""
    c, _ = client
    monkeypatch.setattr(server_mod, "_FAILED_SEEN", {})  # 计数是进程级状态，测试内隔离
    assert [c.get("/v1/models").status_code for _ in range(3)] == [404, 404, 404]

    hits = [x for x in _read_jsonl_logs() if x.get("path") == "/v1/models"]
    assert [h["seen"] for h in hits] == [1, 2, 3]
    assert [h["level"] for h in hits] == ["WARNING", "DEBUG", "DEBUG"]
    assert hits[0]["status"] == 404
    # 首次那条要能回答"是谁在轮询"
    assert hits[0]["user_agent"] == "testclient"
    assert hits[0]["client"] in {"testclient", "127.0.0.1", "testserver"}


def test_failure_log_level_policy_keeps_5xx_loud():
    """降噪只针对重复的 4xx；5xx 是故障，重复也必须留在 WARNING。"""
    assert server_mod._warn_on_failure(404, 1) is True
    assert server_mod._warn_on_failure(404, 2) is False
    assert server_mod._warn_on_failure(500, 99) is True


def test_etl_timeout_default_leaves_headroom():
    """2026-10-22 实测 121.9s 被 120s 砍掉 → 默认超时至少 180s。"""
    if "LANGTRACK_ETL_TIMEOUT_SECONDS" in os.environ:
        pytest.skip("LANGTRACK_ETL_TIMEOUT_SECONDS override in effect")
    assert server_mod._ETL_TIMEOUT_SECONDS >= 180


def test_lifespan_writes_server_heartbeat(tmp_path, monkeypatch):
    """lifespan 期间心跳为 running，退出后为 stopped。"""
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)
    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    with TestClient(app):
        data = json.loads((tmp_path / "run" / "langtrack_server.json").read_text(encoding="utf-8"))
        assert data["state"] == "running"
        assert data["component"] == "langtrack_server"
    data = json.loads((tmp_path / "run" / "langtrack_server.json").read_text(encoding="utf-8"))
    assert data["state"] == "stopped"
    storage.close()


def test_etl_timeout_logs_attribution(tmp_path, monkeypatch):
    """ETL 子进程超时被 kill：父进程留下权威的 `ETL timeout` 行（trigger/timeout_sec/stderr_tail）。"""
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 7)

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="etl", timeout=7, output=b"", stderr=b"boom stderr")

    # 只替换 server 模块用到的 subprocess 表面，不动 stdlib 模块本身。
    monkeypatch.setattr(
        server_mod,
        "subprocess",
        types.SimpleNamespace(
            run=raise_timeout,
            CREATE_NO_WINDOW=subprocess.CREATE_NO_WINDOW,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    assert server_mod._run_etl_once(trigger="periodic") is False

    hit = [x for x in _read_jsonl_logs() if x.get("message") == "ETL timeout"]
    assert hit, "expected an `ETL timeout` line"
    last = hit[-1]
    assert last["trigger"] == "periodic"
    assert last["timeout_sec"] == 7
    assert last["duration_ms"] >= 0
    assert "boom stderr" in last["stderr_tail"]
    assert "timeout" in (server_mod._ETL_STATE["last_error"] or "")
