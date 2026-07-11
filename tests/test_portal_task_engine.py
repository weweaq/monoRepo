import time
from profile.portal.task_engine import TaskRunner
from profile.portal.db_store import get_task_run, query_task_runs
from profile.db.init_db import init_db, get_connection


def setup_function():
    init_db()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM llm_calls")
        conn.execute("DELETE FROM task_runs")
        conn.commit()
    finally:
        conn.close()


def test_start_and_complete_simple_task():
    """启动一个 ingest_single 任务，应完成并记录到 task_runs"""
    runner = TaskRunner()
    task_id = runner.start_task("ingest_single", {"source": "trae"})

    # 等待任务完成（最多 30 秒）
    for _ in range(60):
        status = runner.get_status(task_id)
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.5)

    assert status["status"] in ("done", "failed")
    row = get_task_run(task_id)
    assert row is not None
    assert row["task_type"] == "ingest_single"


def test_concurrent_task_returns_conflict():
    """串行锁：第二个任务应返回 None（409 场景）"""
    runner = TaskRunner()
    # 启动一个长任务
    task_id1 = runner.start_task("refresh_all", {"days": 7})

    # 立即尝试启动第二个
    task_id2 = runner.start_task("ingest", {})
    assert task_id2 is None  # 被拒绝

    # 等待第一个完成
    for _ in range(120):
        status = runner.get_status(task_id1)
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.5)


def test_get_logs_returns_entries():
    """任务执行后应有日志条目"""
    runner = TaskRunner()
    task_id = runner.start_task("ingest_single", {"source": "trae"})

    for _ in range(60):
        status = runner.get_status(task_id)
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.5)

    logs = runner.get_logs(task_id)
    assert len(logs) > 0
    assert "timestamp" in logs[0]
    assert "level" in logs[0]
    assert "message" in logs[0]
