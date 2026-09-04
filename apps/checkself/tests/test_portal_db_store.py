from profile.portal.db_store import (
    insert_task_run, update_task_run, get_task_run,
    query_task_runs, insert_llm_call, query_llm_calls,
    get_llm_call, recover_stale_tasks,
)
from profile.db.init_db import init_db


def setup_function():
    """每个测试前重置数据库"""
    init_db()
    conn = __import__("profile.db.init_db", fromlist=["get_connection"]).get_connection()
    try:
        conn.execute("DELETE FROM llm_calls")
        conn.execute("DELETE FROM task_runs")
        conn.commit()
    finally:
        conn.close()


def test_insert_and_get_task_run():
    task_id = insert_task_run(
        task_type="refresh_all",
        status="pending",
        params_json={"days": 7},
    )
    assert task_id > 0
    row = get_task_run(task_id)
    assert row["task_type"] == "refresh_all"
    assert row["status"] == "pending"


def test_update_task_run():
    task_id = insert_task_run(task_type="ingest", status="pending")
    update_task_run(task_id, status="running", current_step="数据入库")
    row = get_task_run(task_id)
    assert row["status"] == "running"
    assert row["current_step"] == "数据入库"


def test_query_task_runs_pagination():
    for i in range(5):
        insert_task_run(task_type="ingest", status="done")
    result = query_task_runs(page=1, page_size=3)
    assert len(result["items"]) == 3
    assert result["total"] == 5
    result2 = query_task_runs(page=2, page_size=3)
    assert len(result2["items"]) == 2


def test_insert_and_get_llm_call():
    call_id = insert_llm_call(
        step="intent_extract",
        model="LongCat-2.0",
        system_prompt="system",
        user_prompt="user",
        response="resp",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        elapsed_ms=500,
        success=1,
    )
    assert call_id > 0
    row = get_llm_call(call_id)
    assert row["model"] == "LongCat-2.0"
    assert row["prompt_tokens"] == 10


def test_query_llm_calls_filter():
    insert_llm_call(step="intent_extract", model="LongCat-2.0",
                    success=1, response="ok")
    insert_llm_call(step="profile_trae", model="LongCat-2.0",
                    success=0, error_message="timeout")
    result = query_llm_calls(success=0)
    assert result["total"] == 1
    assert result["items"][0]["step"] == "profile_trae"


def test_recover_stale_tasks():
    task_id = insert_task_run(task_type="ingest", status="running")
    recover_stale_tasks()
    row = get_task_run(task_id)
    assert row["status"] == "failed"
    assert "重启" in (row["error_message"] or "")
