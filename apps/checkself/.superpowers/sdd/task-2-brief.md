### Task 2: Portal db_store (task_runs + llm_calls CRUD)

**Files:**
- Create: `profile/portal/__init__.py` (空文件)
- Create: `profile/portal/db_store.py`
- Test: `tests/test_portal_db_store.py`

**Interfaces:**
- Consumes: `profile.db.init_db.get_connection`
- Produces: `insert_task_run()`, `update_task_run()`, `get_task_run()`, `query_task_runs()`, `insert_llm_call()`, `query_llm_calls()`, `get_llm_call()`, `recover_stale_tasks()`

- [ ] **Step 1: 创建 portal 包的 __init__.py**

创建 `profile/portal/__init__.py`，内容为空。

- [ ] **Step 2: 编写 db_store 测试**

创建 `tests/test_portal_db_store.py`：

```python
from datetime import datetime
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
```

- [ ] **Step 3: 运行测试验证失败**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_db_store.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 4: 实现 db_store.py**

创建 `profile/portal/db_store.py`：

```python
"""Portal 数据层：task_runs 和 llm_calls 的 CRUD 操作。"""

import json
from datetime import datetime

from profile.db.init_db import get_connection


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _parse_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


# === task_runs ===

def insert_task_run(task_type: str, status: str, params_json=None,
                    steps_json=None, started_at=None, log_dir=None) -> int:
    started_at = started_at or datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO task_runs (task_type, status, steps_json, params_json, log_dir, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_type, status, _to_json_str(steps_json), _to_json_str(params_json),
             log_dir, str(started_at)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_task_run(task_id: int, **kwargs) -> None:
    if not kwargs:
        return
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in ("steps_json", "params_json", "result_json"):
            v = _to_json_str(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(task_id)
    conn = get_connection()
    try:
        conn.execute(f"UPDATE task_runs SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()


def get_task_run(task_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    d = _row_to_dict(row)
    if d:
        d["steps_json"] = _parse_json(d.get("steps_json"))
        d["params_json"] = _parse_json(d.get("params_json"))
        d["result_json"] = _parse_json(d.get("result_json"))
    return d


def query_task_runs(page: int = 1, page_size: int = 20,
                    status: str = None, task_type: str = None) -> dict:
    sql = "SELECT * FROM task_runs WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if task_type:
        sql += " AND task_type = ?"
        params.append(task_type)
    # count
    conn = get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({sql})", params
        ).fetchone()[0]
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([page_size, (page - 1) * page_size])
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        d = _row_to_dict(row)
        d["steps_json"] = _parse_json(d.get("steps_json"))
        d["params_json"] = _parse_json(d.get("params_json"))
        d["result_json"] = _parse_json(d.get("result_json"))
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def recover_stale_tasks() -> int:
    """将所有 status=running 的任务标记为 failed（portal 重启时调用）。"""
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE task_runs SET status = 'failed', error_message = 'portal 重启中断' "
            "WHERE status = 'running'"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# === llm_calls ===

def insert_llm_call(step: str, model: str, system_prompt=None, user_prompt=None,
                    response=None, prompt_tokens=None, completion_tokens=None,
                    total_tokens=None, elapsed_ms=None, success=1,
                    error_message=None, task_run_id=None,
                    called_at=None, finished_at=None) -> int:
    called_at = called_at or datetime.now().isoformat(timespec="seconds")
    finished_at = finished_at or datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO llm_calls
               (task_run_id, step, model, system_prompt, user_prompt, response,
                prompt_tokens, completion_tokens, total_tokens, elapsed_ms,
                success, error_message, called_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_run_id, step, model, system_prompt, user_prompt, response,
             prompt_tokens, completion_tokens, total_tokens, elapsed_ms,
             success, error_message, str(called_at), str(finished_at)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_llm_call(call_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (call_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row)


def query_llm_calls(page: int = 1, page_size: int = 20,
                    step: str = None, model: str = None,
                    success: int = None) -> dict:
    sql = "SELECT * FROM llm_calls WHERE 1=1"
    params = []
    if step:
        sql += " AND step = ?"
        params.append(step)
    if model:
        sql += " AND model = ?"
        params.append(model)
    if success is not None:
        sql += " AND success = ?"
        params.append(success)
    conn = get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({sql})", params
        ).fetchone()[0]
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([page_size, (page - 1) * page_size])
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items = [_row_to_dict(row) for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _to_json_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_db_store.py -v`
Expected: 6 个测试全部 PASS

- [ ] **Step 6: 提交**

```bash
git add profile/portal/__init__.py profile/portal/db_store.py tests/test_portal_db_store.py
git commit -m "feat(portal): add db_store for task_runs and llm_calls CRUD"
```

---

### Task 3: LLM Client 埋点
