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


_ALLOWED_TASK_RUN_COLUMNS = {"status", "steps_json", "current_step", "params_json",
                             "result_json", "log_dir", "error_message",
                             "started_at", "finished_at"}


def update_task_run(task_id: int, **kwargs) -> None:
    if not kwargs:
        return
    for k in kwargs:
        if k not in _ALLOWED_TASK_RUN_COLUMNS:
            raise ValueError(f"非法列名: {k}")
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
