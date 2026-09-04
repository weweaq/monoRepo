# checkSelf Portal 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 checkSelf 个人画像系统构建本地 Portal 页面，提供工作流进展可视化、数据库浏览、LLM 交互展示、画像产出展示和分步触发操作能力。

**Architecture:** 在现有 `profile/` 包内新增 `portal/` 子包，FastAPI 做 API 层，单 HTML + Alpine.js 做前端。任务在进程内通过线程池执行，日志通过 logging Handler 捕获并经 SSE 推送。新增 `task_runs` 和 `llm_calls` 两张表，在 `LLMClient.chat()` 加埋点。现有流水线逻辑零改动。

**Tech Stack:** Python 3.9+, FastAPI, uvicorn, Alpine.js (CDN), marked.js (CDN), SQLite, pytest

## Global Constraints

- Python >=3.9, 纯标准库 + FastAPI/uvicorn 新增依赖
- 数据库路径: `data/profile.db` (现有 `profile.config.DB_PATH`)
- Obsidian 输出目录: `profile.config.OBSIDIAN_OUTPUT_DIR`
- 日志目录: `profile.config.LOG_DIR`
- CLI 入口签名: `main(argv: list[str] | None = None) -> int`
- 任何 CLI 调用前必须先 `setup(log_dir=LOG_DIR)` + `init_db()`
- 日志命名空间: `profile.*`，通过 `profile.log.get_logger(name)` 获取
- 测试框架: pytest，测试文件在 `tests/` 目录
- LLM 客户端: `profile.llm.client.LLMClient`，调用入口为 `chat()` 方法
- 串行锁: 同一时间只允许一个任务运行

---

## File Structure

```
checkSelf/
├── profile/
│   ├── portal/                    # 新增子包
│   │   ├── __init__.py            # 空文件
│   │   ├── app.py                 # FastAPI 应用 + 路由注册 + uvicorn 启动
│   │   ├── task_engine.py         # TaskRunner + LogHandler + 串行锁 + contextvars
│   │   ├── db_store.py            # task_runs + llm_calls 的 CRUD
│   │   ├── routes/
│   │   │   ├── __init__.py        # 空文件
│   │   │   ├── dashboard.py       # GET /api/dashboard
│   │   │   ├── tasks.py           # 任务 CRUD + SSE
│   │   │   ├── data.py            # raw_data + llm_intents 浏览
│   │   │   ├── llm.py             # llm_calls 查询
│   │   │   └── profiles.py        # 画像文件列表 + 结构化渲染
│   │   └── static/
│   │       └── index.html         # 单 HTML (Alpine.js + marked.js CDN)
│   ├── llm/
│   │   └── client.py              # 改动: 加 contextvars + _record_llm_call
│   └── db/
│       └── init_db.py             # 改动: 加 task_runs + llm_calls DDL
├── tests/
│   ├── test_portal_db_store.py    # 新增
│   ├── test_portal_task_engine.py # 新增
│   └── test_portal_routes.py      # 新增
├── pyproject.toml                 # 改动: 加 fastapi, uvicorn
```

---

### Task 1: 依赖与数据库 Schema

**Files:**
- Modify: `pyproject.toml`
- Modify: `profile/db/init_db.py`
- Test: `tests/test_portal_db_schema.py`

**Interfaces:**
- Produces: `task_runs` 表和 `llm_calls` 表存在于 `data/profile.db`，通过 `init_db()` 创建

- [ ] **Step 1: 添加 fastapi, uvicorn 依赖到 pyproject.toml**

修改 `pyproject.toml`，将 `dependencies = []` 改为：

```toml
dependencies = ["fastapi>=0.100", "uvicorn>=0.20"]
```

- [ ] **Step 2: 安装新依赖**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\pip install fastapi uvicorn`
Expected: 成功安装 fastapi 和 uvicorn

- [ ] **Step 3: 在 init_db.py 的 _SCHEMA 中追加 task_runs 和 llm_calls 表 DDL**

在 `profile/db/init_db.py` 的 `_SCHEMA` 字符串中，在 `llm_intents` 表的 `);` 之后追加：

```sql

-- Portal: 任务运行记录
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    steps_json TEXT,
    current_step TEXT,
    params_json TEXT,
    result_json TEXT,
    log_dir TEXT,
    error_message TEXT,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Portal: LLM 调用记录
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_run_id INTEGER,
    step TEXT,
    model TEXT NOT NULL,
    system_prompt TEXT,
    user_prompt TEXT,
    response TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    elapsed_ms INTEGER,
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    called_at DATETIME NOT NULL,
    finished_at DATETIME,
    FOREIGN KEY (task_run_id) REFERENCES task_runs(id)
);
```

- [ ] **Step 4: 编写测试验证新表存在**

创建 `tests/test_portal_db_schema.py`：

```python
import sqlite3
import tempfile
import os
from pathlib import Path
from profile.db.init_db import get_connection, init_db


def test_task_runs_table_exists():
    """init_db 后 task_runs 表应存在"""
    init_db()
    conn = get_connection()
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
        ).fetchone()
        assert result is not None
        assert result["name"] == "task_runs"
    finally:
        conn.close()


def test_llm_calls_table_exists():
    """init_db 后 llm_calls 表应存在"""
    init_db()
    conn = get_connection()
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_calls'"
        ).fetchone()
        assert result is not None
        assert result["name"] == "llm_calls"
    finally:
        conn.close()


def test_llm_calls_columns():
    """llm_calls 表应有正确的列"""
    init_db()
    conn = get_connection()
    try:
        cols = conn.execute("PRAGMA table_info(llm_calls)").fetchall()
        col_names = [c["name"] for c in cols]
        assert "task_run_id" in col_names
        assert "step" in col_names
        assert "model" in col_names
        assert "system_prompt" in col_names
        assert "user_prompt" in col_names
        assert "response" in col_names
        assert "prompt_tokens" in col_names
        assert "completion_tokens" in col_names
        assert "total_tokens" in col_names
        assert "elapsed_ms" in col_names
        assert "success" in col_names
        assert "error_message" in col_names
        assert "called_at" in col_names
        assert "finished_at" in col_names
    finally:
        conn.close()
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_db_schema.py -v`
Expected: 3 个测试全部 PASS

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml profile/db/init_db.py tests/test_portal_db_schema.py
git commit -m "feat(portal): add task_runs and llm_calls tables + fastapi/uvicorn deps"
```

---

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

**Files:**
- Modify: `profile/llm/client.py`
- Create: `profile/portal/task_engine.py` (仅 contextvars 部分，TaskRunner 在 Task 4 补充)
- Test: `tests/test_llm_tracking.py`

**Interfaces:**
- Consumes: `profile.portal.db_store.insert_llm_call`
- Produces: `profile.portal.task_engine.set_task_context(task_run_id, step)` 和 `clear_task_context()`
- Produces: `LLMClient.chat()` 自动记录到 llm_calls 表

- [ ] **Step 1: 创建 task_engine.py 的 contextvars 部分**

创建 `profile/portal/task_engine.py`：

```python
"""Portal 任务引擎：进程内线程池执行 + 日志捕获 + contextvars。"""

import contextvars
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from profile.portal.db_store import (
    insert_task_run, update_task_run, get_task_run,
    query_task_runs, recover_stale_tasks,
)

# === ContextVars: 在任务执行期间传递 task_run_id 和 step ===

_current_task_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_task_run_id", default=None
)
_current_step: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_step", default=None
)


def set_task_context(task_run_id: int, step: str | None = None) -> None:
    """设置当前任务上下文，供 LLMClient 埋点读取。"""
    _current_task_run_id.set(task_run_id)
    _current_step.set(step)


def clear_task_context() -> None:
    """清除任务上下文。"""
    _current_task_run_id.set(None)
    _current_step.set(None)


def get_current_task_run_id() -> int | None:
    return _current_task_run_id.get()


def get_current_step() -> str | None:
    return _current_step.get()
```

- [ ] **Step 2: 编写埋点测试**

创建 `tests/test_llm_tracking.py`：

```python
from unittest.mock import patch, MagicMock
from profile.portal.task_engine import set_task_context, clear_task_context
from profile.portal.db_store import query_llm_calls
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


def test_llm_call_recorded_on_success():
    """chat() 成功后应记录到 llm_calls 表"""
    from profile.llm.client import LLMClient

    client = LLMClient(api_key="test-key", api_url="http://fake", model="test-model")
    set_task_context(task_run_id=None, step="test_step")

    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"choices":[{"message":{"content":"hello"}}],"usage":{"prompt_tokens":5,"completion_tokens":10,"total_tokens":15}}'
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = client.chat("test prompt", system_prompt="sys prompt")

    assert result == "hello"
    calls = query_llm_calls()
    assert calls["total"] == 1
    call = calls["items"][0]
    assert call["model"] == "test-model"
    assert call["user_prompt"] == "test prompt"
    assert call["system_prompt"] == "sys prompt"
    assert call["response"] == "hello"
    assert call["prompt_tokens"] == 5
    assert call["completion_tokens"] == 10
    assert call["total_tokens"] == 15
    assert call["success"] == 1
    assert call["step"] == "test_step"
    clear_task_context()


def test_llm_call_recorded_on_failure():
    """chat() 失败后也应记录到 llm_calls 表"""
    import urllib.error
    from profile.llm.client import LLMClient

    client = LLMClient(api_key="test-key", api_url="http://fake", model="test-model")
    set_task_context(task_run_id=None, step="fail_step")

    error = urllib.error.HTTPError("http://fake", 500, "Server Error", {}, None)
    with patch("urllib.request.urlopen", side_effect=error):
        try:
            client.chat("test", system_prompt="sys")
        except RuntimeError:
            pass

    calls = query_llm_calls(success=0)
    assert calls["total"] == 1
    call = calls["items"][0]
    assert call["success"] == 0
    assert call["error_message"] is not None
    assert call["step"] == "fail_step"
    clear_task_context()
```

- [ ] **Step 3: 运行测试验证失败**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_llm_tracking.py -v`
Expected: FAIL (llm_calls 表中没有记录)

- [ ] **Step 4: 在 client.py 中加埋点**

修改 `profile/llm/client.py`，在文件顶部 import 区域追加：

```python
from profile.portal.task_engine import get_current_task_run_id, get_current_step
```

在 `LLMClient` 类中，`chat()` 方法的 `return content.strip()` 之前（即 `logger.debug("LLM 响应全文"...)` 之后），追加成功埋点：

```python
                    # 记录到 llm_calls 表
                    _record_llm_call(
                        model=self.model,
                        system_prompt=system_prompt,
                        user_prompt=prompt,
                        response=content,
                        usage=usage,
                        elapsed_ms=elapsed_ms,
                        success=True,
                    )
```

在 `chat()` 方法的两个 except 块中，在 `raise` 之前追加失败埋点：

HTTPError except 块中，在 `raise RuntimeError(...)` 之前追加：

```python
            _record_llm_call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                response=None,
                usage={},
                elapsed_ms=elapsed_ms,
                success=False,
                error_message=f"HTTP {e.code}: {error_body[:200]}",
            )
```

URLError except 块中，在 `raise RuntimeError(...)` 之前追加：

```python
            _record_llm_call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                response=None,
                usage={},
                elapsed_ms=elapsed_ms,
                success=False,
                error_message=f"网络错误: {e}",
            )
```

在 `LLMClient` 类外部（`class JsonParseError` 之前），追加 `_record_llm_call` 函数：

```python
def _record_llm_call(model, system_prompt, user_prompt, response, usage,
                     elapsed_ms, success, error_message=None):
    """记录 LLM 调用到 llm_calls 表。"""
    try:
        from profile.portal.db_store import insert_llm_call
        insert_llm_call(
            task_run_id=get_current_task_run_id(),
            step=get_current_step(),
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=response,
            prompt_tokens=usage.get("prompt_tokens") if usage else None,
            completion_tokens=usage.get("completion_tokens") if usage else None,
            total_tokens=usage.get("total_tokens") if usage else None,
            elapsed_ms=elapsed_ms,
            success=1 if success else 0,
            error_message=error_message,
        )
    except Exception:
        # 埋点失败不影响主流程
        pass
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_llm_tracking.py -v`
Expected: 2 个测试全部 PASS

- [ ] **Step 6: 提交**

```bash
git add profile/portal/task_engine.py profile/llm/client.py tests/test_llm_tracking.py
git commit -m "feat(portal): add LLM call tracking via contextvars + client.py instrumentation"
```

---

### Task 4: TaskRunner 完整实现

**Files:**
- Modify: `profile/portal/task_engine.py` (追加 TaskRunner + LogHandler)
- Test: `tests/test_portal_task_engine.py`

**Interfaces:**
- Consumes: `profile.cli.ingest.main`, `profile.cli.extract_intents.main`, `profile.cli.generate_profiles.main`, `profile.cli.refresh_all.main`
- Produces: `TaskRunner` 类，方法 `start_task()`, `get_status()`, `cancel()`, `get_logs()`

- [ ] **Step 1: 编写 TaskRunner 测试**

创建 `tests/test_portal_task_engine.py`：

```python
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
```

- [ ] **Step 2: 在 task_engine.py 中追加 TaskRunner 和 LogHandler**

在 `profile/portal/task_engine.py` 文件末尾追加：

```python
# === Log Handler: 捕获 profile.* 日志到内存队列 ===


class PortalLogHandler(logging.Handler):
    """捕获 profile 命名空间的日志，存入内存队列供 SSE 读取。"""

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self._queues: dict[int, deque] = {}
        self._lock = threading.Lock()

    def attach(self, task_id: int) -> None:
        with self._lock:
            self._queues[task_id] = deque(maxlen=500)

    def detach(self, task_id: int) -> None:
        with self._lock:
            self._queues.pop(task_id, None)

    def get_logs(self, task_id: int, after_idx: int = 0) -> list[dict]:
        with self._lock:
            q = self._queues.get(task_id)
            if not q:
                return []
            return list(q)[after_idx:]

    def emit(self, record: logging.LogRecord) -> None:
        # 只处理 profile.* 命名空间
        if not record.name.startswith("profile"):
            return
        entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if extra:
            entry["extra"] = extra if isinstance(extra, dict) else str(extra)
        with self._lock:
            for task_id, q in self._queues.items():
                q.append(entry)


# === Task Step Definitions ===

TASK_STEPS = {
    "refresh_all": [
        ("读旧画像", lambda: _noop_step()),
        ("增量入库", lambda: _run_cli("profile.cli.ingest", [])),
        ("提取意图", lambda: _run_cli("profile.cli.extract_intents", [])),
        ("生成画像", lambda: _run_cli("profile.cli.generate_profiles", ["--days", "7"])),
        ("生成变化报告", lambda: _noop_step()),
    ],
    "ingest": [
        ("数据入库", lambda: _run_cli("profile.cli.ingest", [])),
    ],
    "ingest_single": [
        ("入库", lambda source: _run_cli("profile.cli.ingest", [source])),
    ],
    "extract_intents": [
        ("意图提取", lambda: _run_cli("profile.cli.extract_intents", [])),
    ],
    "generate_profiles": [
        ("生成画像", lambda: _run_cli("profile.cli.generate_profiles", ["--days", "7"])),
    ],
}


def _run_cli(module_path: str, argv: list[str]) -> dict:
    """动态导入并调用 CLI 模块的 main()。"""
    import importlib
    module = importlib.import_module(module_path)
    code = module.main(argv)
    return {"exit_code": code}


def _noop_step() -> dict:
    """空步骤（如 refresh_all 中读旧画像由 main 内部处理）。"""
    return {"status": "skipped"}


# === TaskRunner ===


class TaskRunner:
    """管理任务的串行执行、步骤追踪和日志捕获。"""

    def __init__(self):
        self._lock = threading.Lock()  # 串行锁
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._log_handler = PortalLogHandler()
        self._current_task_id: int | None = None
        self._cancel_flag = threading.Event()

    def start_task(self, task_type: str, params: dict) -> int | None:
        """启动任务。如果已有任务在运行，返回 None（409）。"""
        if not self._lock.acquire(blocking=False):
            return None

        task_id = insert_task_run(
            task_type=task_type,
            status="pending",
            params_json=params,
        )
        self._current_task_id = task_id
        self._cancel_flag.clear()
        self._log_handler.attach(task_id)

        # 确保 log_handler 已挂载
        root_logger = logging.getLogger("profile")
        if self._log_handler not in root_logger.handlers:
            root_logger.addHandler(self._log_handler)

        self._executor.submit(self._run_task, task_id, task_type, params)
        return task_id

    def _run_task(self, task_id: int, task_type: str, params: dict):
        """在后台线程中执行任务。"""
        from profile.log import setup
        from profile.config import LOG_DIR
        from profile.db.init_db import init_db

        try:
            setup(log_dir=LOG_DIR)
            init_db()

            update_task_run(task_id, status="running",
                            started_at=datetime.now().isoformat(timespec="seconds"))

            steps = TASK_STEPS.get(task_type, [])
            steps_status = []

            for idx, (step_name, step_fn) in enumerate(steps):
                if self._cancel_flag.is_set():
                    steps_status.append({"name": step_name, "status": "cancelled"})
                    break

                step_started = datetime.now().isoformat(timespec="seconds")
                set_task_context(task_run_id=task_id, step=_step_key(task_type, idx))
                update_task_run(task_id, current_step=f"步骤{idx+1}/{len(steps)}: {step_name}")

                try:
                    if task_type == "ingest_single":
                        result = step_fn(params.get("source", ""))
                    elif task_type == "refresh_all":
                        if idx == 0:
                            # refresh_all 是一整个 main() 调用，步骤 0 直接跑
                            result = _run_cli("profile.cli.refresh_all",
                                              ["--days", str(params.get("days", 7))])
                            steps_status.append({"name": "全链路刷新", "status": "done",
                                                "started_at": step_started,
                                                "finished_at": datetime.now().isoformat(timespec="seconds")})
                            break
                        else:
                            result = step_fn()
                    else:
                        result = step_fn()

                    step_finished = datetime.now().isoformat(timespec="seconds")
                    steps_status.append({"name": step_name, "status": "done",
                                        "started_at": step_started,
                                        "finished_at": step_finished})
                except Exception as e:
                    step_finished = datetime.now().isoformat(timespec="seconds")
                    steps_status.append({"name": step_name, "status": "failed",
                                        "started_at": step_started,
                                        "finished_at": step_finished,
                                        "error": str(e)})
                    raise

            clear_task_context()

            final_status = "cancelled" if self._cancel_flag.is_set() else "done"
            update_task_run(
                task_id,
                status=final_status,
                steps_json=steps_status,
                current_step=None,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                result_json={"steps_count": len(steps_status)},
            )

        except Exception as e:
            clear_task_context()
            update_task_run(
                task_id,
                status="failed",
                steps_json=steps_status if "steps_status" in dir() else [],
                error_message=str(e),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        finally:
            self._log_handler.detach(task_id)
            self._current_task_id = None
            self._lock.release()

    def get_status(self, task_id: int) -> dict | None:
        return get_task_run(task_id)

    def cancel(self, task_id: int) -> bool:
        """软取消任务。"""
        if self._current_task_id == task_id:
            self._cancel_flag.set()
            return True
        return False

    def get_logs(self, task_id: int, after_idx: int = 0) -> list[dict]:
        return self._log_handler.get_logs(task_id, after_idx)

    def is_busy(self) -> bool:
        return self._current_task_id is not None


def _step_key(task_type: str, idx: int) -> str:
    """返回 LLM 埋点用的 step 名称。"""
    step_keys = {
        "refresh_all": ["refresh", "ingest", "intent_extract", "profile_gen", "diff"],
        "ingest": ["ingest"],
        "ingest_single": ["ingest"],
        "extract_intents": ["intent_extract"],
        "generate_profiles": ["profile_gen"],
    }
    keys = step_keys.get(task_type, [])
    if idx < len(keys):
        return keys[idx]
    return task_type


# 全局单例
_runner: TaskRunner | None = None


def get_runner() -> TaskRunner:
    global _runner
    if _runner is None:
        _runner = TaskRunner()
    return _runner
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_task_engine.py -v -x`
Expected: 3 个测试全部 PASS（可能需要较长时间，因为实际执行了入库）

- [ ] **Step 4: 提交**

```bash
git add profile/portal/task_engine.py tests/test_portal_task_engine.py
git commit -m "feat(portal): implement TaskRunner with serial lock, log capture, step tracking"
```

---

### Task 5: FastAPI App Skeleton

**Files:**
- Create: `profile/portal/routes/__init__.py` (空文件)
- Create: `profile/portal/app.py`
- Test: `tests/test_portal_routes.py` (skeleton test)

**Interfaces:**
- Produces: `profile.portal.app.app` (FastAPI 实例), `profile.portal.app.main()` (启动入口)
- Produces: 挂载静态文件服务，`GET /` 返回 index.html

- [ ] **Step 1: 创建 routes 包的 __init__.py**

创建 `profile/portal/routes/__init__.py`，内容为空。

- [ ] **Step 2: 编写 app skeleton 测试**

创建 `tests/test_portal_routes.py`：

```python
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
```

- [ ] **Step 3: 创建 app.py**

创建 `profile/portal/app.py`：

```python
"""FastAPI 应用入口。

用法:
    python -m profile.portal.app
    python -m profile.portal.app --port 8080
"""

import argparse
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from profile.db.init_db import init_db
from profile.config import LOG_DIR, OBSIDIAN_OUTPUT_DIR, ensure_dirs
from profile.log import setup
from profile.portal.task_engine import get_runner, recover_stale_tasks
from profile.portal.routes import dashboard, tasks, data, llm, profiles

app = FastAPI(title="checkSelf Portal")

# 静态文件
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def on_startup():
    """启动时初始化。"""
    setup(log_dir=LOG_DIR)
    ensure_dirs()
    init_db()
    recover_stale_tasks()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# 注册路由
app.include_router(dashboard.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(data.router, prefix="/api")
app.include_router(llm.router, prefix="/api")
app.include_router(profiles.router, prefix="/api")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="checkSelf Portal")
    parser.add_argument("--port", type=int, default=8000, help="端口号")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if not args.no_browser:
        webbrowser.open(f"http://{args.host}:{args.port}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 创建最小的 index.html（占位，Task 11 完善）**

创建 `profile/portal/static/index.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>checkSelf Portal</title>
</head>
<body>
    <h1>checkSelf Portal</h1>
    <p>Loading...</p>
</body>
</html>
```

- [ ] **Step 5: 运行测试验证失败（路由还没创建）**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: FAIL (ImportError: routes 模块不存在)

- [ ] **Step 6: 提交（skeleton + 占位路由在后续 Task 实现）**

```bash
git add profile/portal/routes/__init__.py profile/portal/app.py profile/portal/static/index.html tests/test_portal_routes.py
git commit -m "feat(portal): FastAPI app skeleton with startup init and static serving"
```

---

### Task 6: Dashboard API

**Files:**
- Create: `profile/portal/routes/dashboard.py`
- Test: `tests/test_portal_routes.py` (追加 dashboard 测试)

**Interfaces:**
- Consumes: `profile.portal.db_store.query_task_runs`, `profile.portal.task_engine.get_runner`
- Consumes: `profile.db.store.query_raw_data`, `profile.db.store.query_intents`
- Consumes: `profile.output.writer.load_latest_json`
- Produces: `GET /api/dashboard`

- [ ] **Step 1: 实现 dashboard.py**

创建 `profile/portal/routes/dashboard.py`：

```python
"""Dashboard API: 首页聚合数据。"""

from fastapi import APIRouter

from profile.portal.task_engine import get_runner
from profile.portal.db_store import query_task_runs, query_llm_calls
from profile.db.init_db import get_connection
from profile.config import OBSIDIAN_OUTPUT_DIR
from profile.output.writer import load_latest_json

router = APIRouter()


@router.get("/dashboard")
def get_dashboard():
    # 1. 当前运行任务
    runner = get_runner()
    current_task = None
    if runner.is_busy():
        tasks = query_task_runs(page=1, page_size=1, status="running")
        if tasks["items"]:
            t = tasks["items"][0]
            current_task = {
                "id": t["id"],
                "task_type": t["task_type"],
                "status": t["status"],
                "current_step": t.get("current_step"),
            }

    # 2. 最近5次历史任务
    recent = query_task_runs(page=1, page_size=5)
    recent_tasks = []
    for t in recent["items"]:
        recent_tasks.append({
            "id": t["id"],
            "task_type": t["task_type"],
            "status": t["status"],
            "started_at": t.get("started_at"),
            "finished_at": t.get("finished_at"),
            "result_json": t.get("result_json"),
        })

    # 3. 数据概览
    data_overview = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM raw_data GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        for r in rows:
            data_overview.append({"source": r["source"], "count": r["cnt"]})
    finally:
        conn.close()

    # 4. LLM 概览
    llm_calls = query_llm_calls(page=1, page_size=1)
    total_calls = llm_calls["total"]
    llm_overview = {"total_calls": total_calls, "total_tokens": 0, "success_rate": 1.0}
    if total_calls > 0:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT SUM(total_tokens) as tokens, "
                "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END)*1.0/COUNT(*) as rate "
                "FROM llm_calls"
            ).fetchone()
            llm_overview["total_tokens"] = row["tokens"] or 0
            llm_overview["success_rate"] = round(row["rate"], 4)
        finally:
            conn.close()

    # 5. 最新综合画像快照
    latest_profile = None
    global_json = load_latest_json("个人画像-综合")
    if global_json:
        latest_profile = {
            "date": global_json.get("date", ""),
            "summary": global_json.get("summary", ""),
            "direction_alignment": (global_json.get("direction_truth") or {}).get("alignment", ""),
        }

    # 6. 最新变化报告（从文件读取）
    latest_changes = None
    import json
    from datetime import datetime
    if OBSIDIAN_OUTPUT_DIR.exists():
        reports = sorted(OBSIDIAN_OUTPUT_DIR.glob("个人画像-变化报告-*.md"), reverse=True)
        if reports:
            date_str = reports[0].stem.replace("个人画像-变化报告-", "")
            content = reports[0].read_text(encoding="utf-8")
            # 简单统计变化类型
            new_count = content.count("[新增]")
            inc_count = content.count("[上升]")
            dec_count = content.count("[下降]")
            dis_count = content.count("[消失]")
            latest_changes = {
                "date": date_str,
                "total_changes": new_count + inc_count + dec_count + dis_count,
                "new": new_count,
                "increased": inc_count,
                "decreased": dec_count,
                "disappeared": dis_count,
            }

    return {
        "current_task": current_task,
        "recent_tasks": recent_tasks,
        "data_overview": data_overview,
        "llm_overview": llm_overview,
        "latest_profile": latest_profile,
        "latest_changes": latest_changes,
    }
```

- [ ] **Step 2: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py::test_dashboard_endpoint_exists -v`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add profile/portal/routes/dashboard.py
git commit -m "feat(portal): dashboard API with aggregated overview data"
```

---

### Task 7: Tasks API + SSE

**Files:**
- Create: `profile/portal/routes/tasks.py`
- Test: `tests/test_portal_routes.py` (追加 tasks 测试)

**Interfaces:**
- Consumes: `profile.portal.task_engine.get_runner`, `profile.portal.db_store.query_task_runs`, `get_task_run`
- Produces: `GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks`, `DELETE /api/tasks/{id}`, `GET /api/tasks/{id}/logs`

- [ ] **Step 1: 实现 tasks.py**

创建 `profile/portal/routes/tasks.py`：

```python
"""Tasks API: 任务 CRUD + SSE 日志流。"""

import asyncio
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from profile.portal.task_engine import get_runner
from profile.portal.db_store import query_task_runs, get_task_run

router = APIRouter()


class TaskCreateRequest(BaseModel):
    task_type: str
    params: dict = {}


VALID_TASK_TYPES = {"refresh_all", "ingest", "ingest_single", "extract_intents", "generate_profiles"}


@router.get("/tasks")
def list_tasks(page: int = 1, page_size: int = 20,
               status: str = None, task_type: str = None):
    return query_task_runs(page=page, page_size=page_size, status=status, task_type=task_type)


@router.get("/tasks/{task_id}")
def get_task(task_id: int):
    row = get_task_run(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


@router.post("/tasks")
def create_task(req: TaskCreateRequest):
    if req.task_type not in VALID_TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"无效的任务类型: {req.task_type}")

    runner = get_runner()
    task_id = runner.start_task(req.task_type, req.params)
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有任务在运行中")
    return {"id": task_id, "status": "started"}


@router.delete("/tasks/{task_id}")
def cancel_task(task_id: int):
    runner = get_runner()
    success = runner.cancel(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="无法取消（任务可能已结束或不匹配）")
    return {"id": task_id, "status": "cancelling"}


@router.get("/tasks/{task_id}/logs")
async def stream_logs(task_id: int):
    """SSE 流：实时推送任务日志。"""
    runner = get_runner()

    async def event_stream():
        idx = 0
        while True:
            logs = runner.get_logs(task_id, after_idx=idx)
            for log in logs:
                idx += 1
                yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"

            # 检查任务是否已结束
            task = get_task_run(task_id)
            if task and task["status"] in ("done", "failed", "cancelled"):
                # 推送剩余日志
                remaining = runner.get_logs(task_id, after_idx=idx)
                for log in remaining:
                    idx += 1
                    yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'done', 'status': task['status']}, ensure_ascii=False)}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

- [ ] **Step 2: 追加 tasks API 测试到 test_portal_routes.py**

在 `tests/test_portal_routes.py` 追加：

```python
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
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/tasks.py tests/test_portal_routes.py
git commit -m "feat(portal): tasks API with CRUD and SSE log streaming"
```

---

### Task 8: Data API

**Files:**
- Create: `profile/portal/routes/data.py`
- Test: `tests/test_portal_routes.py` (追加 data 测试)

**Interfaces:**
- Consumes: `profile.db.store.query_raw_data`, `profile.db.store.query_intents`, `profile.db.init_db.get_connection`
- Produces: `GET /api/data/raw`, `GET /api/data/raw/{id}`, `GET /api/data/intents`

- [ ] **Step 1: 实现 data.py**

创建 `profile/portal/routes/data.py`：

```python
"""Data API: raw_data 和 llm_intents 浏览。"""

from fastapi import APIRouter, HTTPException

from profile.db.init_db import get_connection
from profile.db.store import query_raw_data, query_intents

router = APIRouter()


@router.get("/data/raw")
def list_raw_data(page: int = 1, page_size: int = 20,
                  source: str = None, start_date: str = None, end_date: str = None):
    all_rows = query_raw_data(source=source, start_date=start_date, end_date=end_date)
    total = len(all_rows)
    start = (page - 1) * page_size
    end = start + page_size
    items = all_rows[start:end]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/data/raw/{raw_id}")
def get_raw_data(raw_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM raw_data WHERE id = ?", (raw_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    return dict(row)


@router.get("/data/intents")
def list_intents(page: int = 1, page_size: int = 20,
                 source: str = None, category: str = None):
    all_rows = query_intents(source=source)
    # 按 category 筛选
    if category:
        all_rows = [r for r in all_rows if r.get("intent_category") == category]
    total = len(all_rows)
    start = (page - 1) * page_size
    end = start + page_size
    items = all_rows[start:end]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
```

- [ ] **Step 2: 追加 data API 测试**

在 `tests/test_portal_routes.py` 追加：

```python
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
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/data.py tests/test_portal_routes.py
git commit -m "feat(portal): data API for raw_data and llm_intents browsing"
```

---

### Task 9: LLM API

**Files:**
- Create: `profile/portal/routes/llm.py`
- Test: `tests/test_portal_routes.py` (追加 llm 测试)

**Interfaces:**
- Consumes: `profile.portal.db_store.query_llm_calls`, `get_llm_call`
- Produces: `GET /api/llm/calls`, `GET /api/llm/calls/{id}`

- [ ] **Step 1: 实现 llm.py**

创建 `profile/portal/routes/llm.py`：

```python
"""LLM API: llm_calls 查询。"""

from fastapi import APIRouter, HTTPException

from profile.portal.db_store import query_llm_calls, get_llm_call

router = APIRouter()


@router.get("/llm/calls")
def list_llm_calls(page: int = 1, page_size: int = 20,
                   step: str = None, model: str = None, success: int = None):
    return query_llm_calls(page=page, page_size=page_size, step=step, model=model, success=success)


@router.get("/llm/calls/{call_id}")
def get_llm_call_detail(call_id: int):
    row = get_llm_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="LLM 调用记录不存在")
    return row
```

- [ ] **Step 2: 追加 llm API 测试**

在 `tests/test_portal_routes.py` 追加：

```python
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
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/llm.py tests/test_portal_routes.py
git commit -m "feat(portal): LLM API for llm_calls query"
```

---

### Task 10: Profiles API

**Files:**
- Create: `profile/portal/routes/profiles.py`
- Test: `tests/test_portal_routes.py` (追加 profiles 测试)

**Interfaces:**
- Consumes: `profile.config.OBSIDIAN_OUTPUT_DIR`
- Produces: `GET /api/profiles`, `GET /api/profiles/{filename}`

- [ ] **Step 1: 实现 profiles.py**

创建 `profile/portal/routes/profiles.py`：

```python
"""Profiles API: 画像文件列表 + 内容读取。"""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from profile.config import OBSIDIAN_OUTPUT_DIR

router = APIRouter()

# 文件类型映射
FILE_TYPE_MAP = {
    "综合": "global",
    "trae": "trae",
    "marvis": "marvis",
    "content_consumption": "content_consumption",
    "变化报告": "change_report",
}


def _parse_filename(filename: str) -> dict:
    """从文件名解析类型和日期。"""
    name = Path(filename).stem
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    date_str = date_match.group(1) if date_match else ""

    file_type = "unknown"
    for key, val in FILE_TYPE_MAP.items():
        if key in name:
            file_type = val
            break

    return {"filename": filename, "type": file_type, "date": date_str}


@router.get("/profiles")
def list_profiles():
    if not OBSIDIAN_OUTPUT_DIR.exists():
        return {"items": [], "total": 0}

    files = sorted(OBSIDIAN_OUTPUT_DIR.iterdir(), reverse=True)
    items = []
    for f in files:
        if f.is_file() and f.suffix in (".md", ".json"):
            info = _parse_filename(f.name)
            info["size"] = f.stat().st_size
            info["ext"] = f.suffix
            items.append(info)

    return {"items": items, "total": len(items)}


@router.get("/profiles/{filename}")
def get_profile(filename: str):
    # 安全检查：防止路径穿越
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    filepath = OBSIDIAN_OUTPUT_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    content = filepath.read_text(encoding="utf-8")

    if filepath.suffix == ".json":
        try:
            data = json.loads(content)
            return {"filename": filename, "format": "json", "data": data}
        except json.JSONDecodeError:
            return {"filename": filename, "format": "text", "data": content}
    else:
        return {"filename": filename, "format": "markdown", "data": content}
```

- [ ] **Step 2: 追加 profiles API 测试**

在 `tests/test_portal_routes.py` 追加：

```python
def test_list_profiles():
    client = TestClient(app)
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_get_profile_not_found():
    client = TestClient(app)
    resp = client.get("/api/profiles/nonexistent-file.md")
    assert resp.status_code == 404


def test_get_profile_path_traversal_blocked():
    client = TestClient(app)
    resp = client.get("/api/profiles/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/profiles.py tests/test_portal_routes.py
git commit -m "feat(portal): profiles API for file listing and structured rendering"
```

---

### Task 11: 前端 index.html

**Files:**
- Modify: `profile/portal/static/index.html` (替换占位内容)

**Interfaces:**
- Consumes: 所有 `/api/*` 端点
- Produces: 完整的 4 视图单页应用

**注意:** 此 Task 不写自动化测试，通过手动验证。

- [ ] **Step 1: 实现完整的 index.html**

将 `profile/portal/static/index.html` 替换为完整的单页应用。文件内容较长，包含：
- 顶部导航栏（Dashboard / 数据库 / LLM / 画像）
- Dashboard 视图：当前任务卡片 + 操作按钮组 + 数据概览卡片
- 数据库浏览视图：tab 切换 + 分页表格 + 详情展开
- LLM 交互视图：筛选 + 列表 + prompt/response 展开
- 画像产出视图：时间线 + 结构化卡片 + 变化报告 diff 表格

核心 HTML 结构和 Alpine.js 逻辑：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>checkSelf Portal</title>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, "PingFang SC", "Segoe UI", sans-serif; background: #f5f5f5; color: #333; }
        .nav { background: #1a1a2e; padding: 12px 24px; display: flex; gap: 24px; align-items: center; }
        .nav h1 { color: #fff; font-size: 18px; margin-right: auto; }
        .nav button { background: transparent; border: none; color: #aaa; cursor: pointer; font-size: 14px; padding: 6px 12px; border-radius: 6px; }
        .nav button.active, .nav button:hover { color: #fff; background: rgba(255,255,255,0.1); }
        .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
        .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
        .card h2 { font-size: 16px; margin-bottom: 12px; color: #1a1a2e; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; }
        .stat { text-align: center; }
        .stat .num { font-size: 28px; font-weight: 600; color: #4B3FE3; }
        .stat .label { font-size: 12px; color: #999; margin-top: 4px; }
        .btn { background: #4B3FE3; color: #fff; border: none; padding: 8px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; }
        .btn:hover { background: #3C2ECA; }
        .btn-secondary { background: #e0e0e0; color: #333; }
        .btn-danger { background: #E8463A; }
        .btn-sm { padding: 4px 12px; font-size: 12px; }
        .progress { height: 8px; background: #e0e0e0; border-radius: 4px; margin: 8px 0; overflow: hidden; }
        .progress-bar { height: 100%; background: #4B3FE3; transition: width 0.3s; }
        .log-box { background: #1a1a2e; color: #0f0; padding: 12px; border-radius: 8px; font-family: "JetBrains Mono", monospace; font-size: 12px; max-height: 300px; overflow-y: auto; }
        .log-box div { padding: 2px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; }
        th { background: #f9f9f9; font-weight: 600; color: #666; }
        tr:hover { background: #f5f5ff; cursor: pointer; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
        .tag-new { background: #dcfce7; color: #166534; }
        .tag-increased { background: #dcfce7; color: #166534; }
        .tag-decreased { background: #fee2e2; color: #991b1b; }
        .tag-disappeared { background: #f3f4f6; color: #6b7280; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; }
        .badge-done { background: #dcfce7; color: #166534; }
        .badge-failed { background: #fee2e2; color: #991b1b; }
        .badge-running { background: #fef3c7; color: #92400e; }
        .badge-cancelled { background: #f3f4f6; color: #6b7280; }
        .pagination { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
        pre { white-space: pre-wrap; word-break: break-all; background: #f9f9f9; padding: 12px; border-radius: 8px; font-size: 12px; overflow-x: auto; }
        .detail-panel { background: #f9f9ff; border-radius: 8px; padding: 16px; margin-top: 12px; }
        .dim-card { background: #f9f9ff; border-left: 3px solid #4B3FE3; padding: 12px 16px; border-radius: 0 8px 8px 0; margin-bottom: 12px; }
        .dim-card h3 { font-size: 14px; color: #4B3FE3; margin-bottom: 8px; }
        [x-cloak] { display: none !important; }
    </style>
</head>
<body x-data="portalApp()" x-init="init()">
    <!-- 导航栏 -->
    <div class="nav">
        <h1>checkSelf Portal</h1>
        <button :class="{active: view==='dashboard'}" @click="view='dashboard'">Dashboard</button>
        <button :class="{active: view==='data'}" @click="view='data'; loadData()">数据库</button>
        <button :class="{active: view==='llm'}" @click="view='llm'; loadLLM()">LLM 交互</button>
        <button :class="{active: view==='profiles'}" @click="view='profiles'; loadProfiles()">画像产出</button>
    </div>

    <!-- Dashboard 视图 -->
    <div class="container" x-show="view==='dashboard'" x-cloak>
        <!-- 工作流进展 -->
        <div class="card">
            <h2>工作流进展</h2>
            <template x-if="dash.current_task">
                <div>
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span x-text="dash.current_task.task_type"></span>
                        <span class="badge badge-running" x-text="dash.current_task.status"></span>
                    </div>
                    <p x-text="dash.current_task.current_step" style="color:#666; margin:8px 0;"></p>
                    <div class="progress"><div class="progress-bar" style="width:60%"></div></div>
                    <button class="btn btn-danger btn-sm" @click="cancelTask(dash.current_task.id)">取消</button>
                </div>
            </template>
            <template x-if="!dash.current_task">
                <div>
                    <p style="color:#999; margin-bottom:12px;">无运行中的任务</p>
                    <div style="display:flex; gap:8px; flex-wrap:wrap;">
                        <button class="btn" @click="startTask('refresh_all', {days:7})">立即刷新</button>
                        <button class="btn btn-secondary btn-sm" @click="startTask('ingest', {})">入库</button>
                        <button class="btn btn-secondary btn-sm" @click="startTask('extract_intents', {})">意图提取</button>
                        <button class="btn btn-secondary btn-sm" @click="startTask('generate_profiles', {days:7})">生成画像</button>
                    </div>
                </div>
            </template>
        </div>

        <!-- 最近任务 -->
        <div class="card" x-show="dash.recent_tasks && dash.recent_tasks.length > 0">
            <h2>最近任务</h2>
            <table>
                <thead><tr><th>ID</th><th>类型</th><th>状态</th><th>开始时间</th><th>结束时间</th></tr></thead>
                <tbody>
                    <template x-for="t in dash.recent_tasks" :key="t.id">
                        <tr @click="view='tasks_detail'; loadTaskDetail(t.id)">
                            <td x-text="t.id"></td>
                            <td x-text="t.task_type"></td>
                            <td><span class="badge" :class="'badge-'+t.status" x-text="t.status"></span></td>
                            <td x-text="t.started_at"></td>
                            <td x-text="t.finished_at"></td>
                        </tr>
                    </template>
                </tbody>
            </table>
        </div>

        <!-- 数据概览 -->
        <div class="card">
            <h2>数据概览</h2>
            <div class="grid">
                <template x-for="d in dash.data_overview" :key="d.source">
                    <div class="stat">
                        <div class="num" x-text="d.count"></div>
                        <div class="label" x-text="d.source"></div>
                    </div>
                </template>
            </div>
        </div>

        <!-- LLM 概览 + 最新画像 -->
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
            <div class="card">
                <h2>LLM 调用</h2>
                <div class="grid">
                    <div class="stat"><div class="num" x-text="dash.llm_overview?.total_calls || 0"></div><div class="label">总调用</div></div>
                    <div class="stat"><div class="num" x-text="dash.llm_overview?.total_tokens || 0"></div><div class="label">总 tokens</div></div>
                    <div class="stat"><div class="num" x-text="((dash.llm_overview?.success_rate || 1)*100).toFixed(0)+'%'"></div><div class="label">成功率</div></div>
                </div>
            </div>
            <div class="card">
                <h2>最新画像</h2>
                <template x-if="dash.latest_profile">
                    <div>
                        <p style="font-size:14px; margin-bottom:8px;" x-text="dash.latest_profile.summary"></p>
                        <p style="font-size:12px; color:#999;">方向对齐度: <span x-text="dash.latest_profile.direction_alignment"></span></p>
                        <p style="font-size:12px; color:#999;" x-text="'日期: '+dash.latest_profile.date"></p>
                    </div>
                </template>
                <template x-if="!dash.latest_profile"><p style="color:#999;">暂无画像</p></template>
            </div>
        </div>

        <!-- 最新变化报告 -->
        <div class="card" x-show="dash.latest_changes">
            <h2>最新变化报告</h2>
            <template x-if="dash.latest_changes">
                <div style="display:flex; gap:16px;">
                    <div class="stat"><div class="num" x-text="dash.latest_changes.new"></div><div class="label">新增</div></div>
                    <div class="stat"><div class="num" x-text="dash.latest_changes.increased"></div><div class="label">上升</div></div>
                    <div class="stat"><div class="num" x-text="dash.latest_changes.decreased"></div><div class="label">下降</div></div>
                    <div class="stat"><div class="num" x-text="dash.latest_changes.disappeared"></div><div class="label">消失</div></div>
                </div>
            </template>
        </div>
    </div>

    <!-- 数据库浏览视图 -->
    <div class="container" x-show="view==='data'" x-cloak>
        <div class="card">
            <div style="display:flex; gap:12px; margin-bottom:16px; align-items:center;">
                <h2>数据库浏览</h2>
                <div style="display:flex; gap:4px;">
                    <button class="btn btn-sm" :class="dataTab==='raw' ? '' : 'btn-secondary'" @click="dataTab='raw'; loadData()">raw_data</button>
                    <button class="btn btn-sm" :class="dataTab==='intents' ? '' : 'btn-secondary'" @click="dataTab='intents'; loadData()">llm_intents</button>
                </div>
            </div>
            <div style="display:flex; gap:8px; margin-bottom:12px;">
                <select x-model="dataFilter.source" @change="loadData()" style="padding:4px 8px; border-radius:6px; border:1px solid #ddd;">
                    <option value="">全部来源</option>
                    <option value="trae">trae</option>
                    <option value="marvis">marvis</option>
                    <option value="bilibili">bilibili</option>
                    <option value="netease">netease</option>
                </select>
            </div>
            <table>
                <thead>
                    <template x-if="dataTab==='raw'"><tr><th>ID</th><th>来源</th><th>内容</th><th>时间</th></tr></template>
                    <template x-if="dataTab==='intents'"><tr><th>ID</th><th>分类</th><th>摘要</th><th>模型</th><th>时间</th></tr></template>
                </thead>
                <tbody>
                    <template x-for="item in dataItems" :key="item.id">
                        <tr @click="selectedData=item">
                            <template x-if="dataTab==='raw'">
                                <td colspan="4" style="padding:0;">
                                    <div style="display:flex; gap:12px; padding:8px 12px;">
                                        <span x-text="item.id" style="min-width:40px;"></span>
                                        <span x-text="item.source" style="min-width:60px;"></span>
                                        <span x-text="item.content?.substring(0,60)" style="flex:1;"></span>
                                        <span x-text="item.timestamp" style="min-width:140px; color:#999;"></span>
                                    </div>
                                </td>
                            </template>
                            <template x-if="dataTab==='intents'">
                                <td colspan="5" style="padding:0;">
                                    <div style="display:flex; gap:12px; padding:8px 12px;">
                                        <span x-text="item.id" style="min-width:40px;"></span>
                                        <span x-text="item.intent_category" style="min-width:80px;"></span>
                                        <span x-text="item.intent_summary?.substring(0,50)" style="flex:1;"></span>
                                        <span x-text="item.llm_model" style="min-width:100px; color:#999;"></span>
                                        <span x-text="item.extracted_at" style="min-width:140px; color:#999;"></span>
                                    </div>
                                </td>
                            </template>
                        </tr>
                    </template>
                </tbody>
            </table>
            <div class="pagination">
                <button class="btn btn-secondary btn-sm" @click="dataPage--; loadData()" x-show="dataPage>1">上一页</button>
                <span x-text="'第 '+dataPage+' 页 / 共 '+Math.ceil(dataTotal/20)+' 页'" style="font-size:13px; color:#666;"></span>
                <button class="btn btn-secondary btn-sm" @click="dataPage++; loadData()" x-show="dataPage*20 < dataTotal">下一页</button>
            </div>
            <template x-if="selectedData">
                <div class="detail-panel">
                    <h3>详情</h3>
                    <pre x-text="JSON.stringify(selectedData, null, 2)"></pre>
                    <button class="btn btn-secondary btn-sm" @click="selectedData=null" style="margin-top:8px;">关闭</button>
                </div>
            </template>
        </div>
    </div>

    <!-- LLM 交互视图 -->
    <div class="container" x-show="view==='llm'" x-cloak>
        <div class="card">
            <h2>LLM 交互</h2>
            <div style="display:flex; gap:8px; margin-bottom:12px;">
                <select x-model="llmFilter.step" @change="loadLLM()" style="padding:4px 8px; border-radius:6px; border:1px solid #ddd;">
                    <option value="">全部步骤</option>
                    <option value="intent_extract">意图提取</option>
                    <option value="profile_trae">trae画像</option>
                    <option value="profile_marvis">marvis画像</option>
                    <option value="profile_global">综合画像</option>
                </select>
                <select x-model="llmFilter.success" @change="loadLLM()" style="padding:4px 8px; border-radius:6px; border:1px solid #ddd;">
                    <option value="">全部</option>
                    <option value="1">成功</option>
                    <option value="0">失败</option>
                </select>
            </div>
            <table>
                <thead><tr><th>ID</th><th>步骤</th><th>模型</th><th>耗时</th><th>Tokens</th><th>状态</th></tr></thead>
                <tbody>
                    <template x-for="c in llmItems" :key="c.id">
                        <tr @click="selectedLLM = (selectedLLM?.id === c.id) ? null : c">
                            <td x-text="c.id"></td>
                            <td x-text="c.step"></td>
                            <td x-text="c.model"></td>
                            <td x-text="c.elapsed_ms ? c.elapsed_ms+'ms' : '-'"></td>
                            <td x-text="c.total_tokens || '-'"></td>
                            <td><span class="badge" :class="c.success ? 'badge-done' : 'badge-failed'" x-text="c.success ? '成功' : '失败'"></span></td>
                        </tr>
                    </template>
                </tbody>
            </table>
            <div class="pagination">
                <button class="btn btn-secondary btn-sm" @click="llmPage--; loadLLM()" x-show="llmPage>1">上一页</button>
                <span x-text="'第 '+llmPage+' 页 / 共 '+Math.ceil(llmTotal/20)+' 页'" style="font-size:13px; color:#666;"></span>
                <button class="btn btn-secondary btn-sm" @click="llmPage++; loadLLM()" x-show="llmPage*20 < llmTotal">下一页</button>
            </div>
            <template x-if="selectedLLM">
                <div class="detail-panel">
                    <h3>调用详情 #<span x-text="selectedLLM.id"></span></h3>
                    <details style="margin-bottom:8px;"><summary style="cursor:pointer; font-weight:500;">System Prompt</summary><pre x-text="selectedLLM.system_prompt"></pre></details>
                    <details style="margin-bottom:8px;"><summary style="cursor:pointer; font-weight:500;">User Prompt</summary><pre x-text="selectedLLM.user_prompt"></pre></details>
                    <p style="font-weight:500; margin-bottom:4px;">Response</p>
                    <pre x-text="selectedLLM.response"></pre>
                    <template x-if="selectedLLM.error_message"><p style="color:#E8463A; margin-top:8px;" x-text="'Error: '+selectedLLM.error_message"></p></template>
                    <button class="btn btn-secondary btn-sm" @click="selectedLLM=null" style="margin-top:8px;">关闭</button>
                </div>
            </template>
        </div>
    </div>

    <!-- 画像产出视图 -->
    <div class="container" x-show="view==='profiles'" x-cloak>
        <div class="card">
            <h2>画像产出</h2>
            <div style="display:flex; gap:8px; margin-bottom:12px;">
                <button class="btn btn-sm" :class="profileViewMode==='structured' ? '' : 'btn-secondary'" @click="profileViewMode='structured'">结构化</button>
                <button class="btn btn-sm" :class="profileViewMode==='json' ? '' : 'btn-secondary'" @click="profileViewMode='json'">原始 JSON</button>
            </div>
            <table>
                <thead><tr><th>文件名</th><th>类型</th><th>日期</th><th>大小</th></tr></thead>
                <tbody>
                    <template x-for="p in profileItems" :key="p.filename">
                        <tr @click="loadProfileDetail(p.filename)">
                            <td x-text="p.filename"></td>
                            <td x-text="p.type"></td>
                            <td x-text="p.date"></td>
                            <td x-text="(p.size/1024).toFixed(1)+'KB'"></td>
                        </tr>
                    </template>
                </tbody>
            </table>
            <template x-if="selectedProfile">
                <div class="detail-panel">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                        <h3 x-text="selectedProfile.filename"></h3>
                        <button class="btn btn-secondary btn-sm" @click="selectedProfile=null">关闭</button>
                    </div>
                    <template x-if="profileViewMode==='json'">
                        <pre x-text="JSON.stringify(selectedProfile.data, null, 2)"></pre>
                    </template>
                    <template x-if="profileViewMode==='structured'">
                        <div>
                            <template x-if="selectedProfile.data?.direction_truth">
                                <div class="dim-card">
                                    <h3>方向真实度</h3>
                                    <p><strong>声称方向:</strong> <span x-text="selectedProfile.data.direction_truth.claimed"></span></p>
                                    <p><strong>实际关注:</strong> <span x-text="selectedProfile.data.direction_truth.actual_focus"></span></p>
                                    <p><strong>对齐度:</strong> <span x-text="selectedProfile.data.direction_truth.alignment"></span></p>
                                    <p x-text="selectedProfile.data.direction_truth.description"></p>
                                </div>
                            </template>
                            <template x-if="selectedProfile.data?.knowledge_interest">
                                <div class="dim-card">
                                    <h3>知识兴趣光谱</h3>
                                    <p x-text="selectedProfile.data.knowledge_interest.description"></p>
                                </div>
                            </template>
                            <template x-if="selectedProfile.data?.activity_pattern">
                                <div class="dim-card">
                                    <h3>作息精力模式</h3>
                                    <p><strong>高效时段:</strong> <span x-text="selectedProfile.data.activity_pattern.peak_hours"></span></p>
                                    <p><strong>日均:</strong> <span x-text="selectedProfile.data.activity_pattern.daily_avg"></span></p>
                                </div>
                            </template>
                            <template x-if="selectedProfile.data?.decision_style">
                                <div class="dim-card">
                                    <h3>决策行动模式</h3>
                                    <p x-text="selectedProfile.data.decision_style.description"></p>
                                </div>
                            </template>
                            <template x-if="selectedProfile.data?.emotional_tendency">
                                <div class="dim-card">
                                    <h3>情绪审美倾向</h3>
                                    <p x-text="selectedProfile.data.emotional_tendency.description"></p>
                                </div>
                            </template>
                            <template x-if="selectedProfile.data?.summary">
                                <div class="dim-card">
                                    <h3>总结</h3>
                                    <p x-text="selectedProfile.data.summary"></p>
                                </div>
                            </template>
                            <template x-if="selectedProfile.data?.suggestions">
                                <div class="dim-card">
                                    <h3>建议</h3>
                                    <ul>
                                        <template x-for="s in selectedProfile.data.suggestions" :key="s">
                                            <li x-text="s"></li>
                                        </template>
                                    </ul>
                                </div>
                            </template>
                            <template x-if="selectedProfile.format==='markdown'">
                                <div x-html="marked.parse(selectedProfile.data)"></div>
                            </template>
                        </div>
                    </template>
                </div>
            </template>
        </div>
    </div>

    <script>
        function portalApp() {
            return {
                view: 'dashboard',
                dash: {},
                // data
                dataTab: 'raw',
                dataItems: [],
                dataTotal: 0,
                dataPage: 1,
                dataFilter: { source: '' },
                selectedData: null,
                // llm
                llmItems: [],
                llmTotal: 0,
                llmPage: 1,
                llmFilter: { step: '', success: '' },
                selectedLLM: null,
                // profiles
                profileItems: [],
                selectedProfile: null,
                profileViewMode: 'structured',
                // task
                currentEventSource: null,

                async init() {
                    await this.loadDashboard();
                    // 轮询
                    setInterval(() => {
                        if (this.view === 'dashboard' && !this.dash.current_task) {
                            this.loadDashboard();
                        }
                    }, 10000);
                },

                async loadDashboard() {
                    const resp = await fetch('/api/dashboard');
                    this.dash = await resp.json();
                },

                async startTask(taskType, params) {
                    const resp = await fetch('/api/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ task_type: taskType, params }),
                    });
                    if (resp.ok) {
                        const data = await resp.json();
                        alert('任务已启动: #' + data.id);
                        this.loadDashboard();
                    } else if (resp.status === 409) {
                        alert('已有任务在运行中');
                    } else {
                        alert('启动失败: ' + resp.statusText);
                    }
                },

                async cancelTask(taskId) {
                    await fetch('/api/tasks/' + taskId, { method: 'DELETE' });
                    this.loadDashboard();
                },

                async loadData() {
                    const params = new URLSearchParams({ page: this.dataPage, page_size: 20 });
                    if (this.dataFilter.source) params.set('source', this.dataFilter.source);
                    const endpoint = this.dataTab === 'raw' ? '/api/data/raw' : '/api/data/intents';
                    const resp = await fetch(endpoint + '?' + params);
                    const data = await resp.json();
                    this.dataItems = data.items || [];
                    this.dataTotal = data.total || 0;
                },

                async loadLLM() {
                    const params = new URLSearchParams({ page: this.llmPage, page_size: 20 });
                    if (this.llmFilter.step) params.set('step', this.llmFilter.step);
                    if (this.llmFilter.success !== '') params.set('success', this.llmFilter.success);
                    const resp = await fetch('/api/llm/calls?' + params);
                    const data = await resp.json();
                    this.llmItems = data.items || [];
                    this.llmTotal = data.total || 0;
                },

                async loadProfiles() {
                    const resp = await fetch('/api/profiles');
                    const data = await resp.json();
                    this.profileItems = data.items || [];
                },

                async loadProfileDetail(filename) {
                    const resp = await fetch('/api/profiles/' + encodeURIComponent(filename));
                    this.selectedProfile = await resp.json();
                },
            };
        }
    </script>
</body>
</html>
```

- [ ] **Step 2: 手动验证 — 启动 portal**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m profile.portal.app`
Expected: 浏览器自动打开 `http://127.0.0.1:8000`，显示 Dashboard 页面

验证项：
1. Dashboard 页面显示数据概览（4 个数据源条数）
2. 点击"数据库"tab，能看到 raw_data 表数据
3. 点击"LLM 交互"tab，能看到 llm_calls 记录
4. 点击"画像产出"tab，能看到画像文件列表
5. 点击"立即刷新"按钮，任务启动并显示运行状态

- [ ] **Step 3: 提交**

```bash
git add profile/portal/static/index.html
git commit -m "feat(portal): complete frontend with 4 views - dashboard, data, LLM, profiles"
```

---

## Self-Review 检查

### Spec 覆盖检查

| Spec 要求 | 对应 Task |
|-----------|-----------|
| task_runs 表 | Task 1 |
| llm_calls 表 | Task 1 |
| db_store CRUD | Task 2 |
| LLM client 埋点 | Task 3 |
| TaskRunner + 串行锁 + LogHandler | Task 4 |
| FastAPI app + 启动恢复 | Task 5 |
| Dashboard API | Task 6 |
| Tasks API + SSE | Task 7 |
| Data API | Task 8 |
| LLM API | Task 9 |
| Profiles API | Task 10 |
| 前端 4 视图 | Task 11 |
| contextvars 传递 task_run_id 和 step | Task 3 + Task 4 |
| 软取消机制 | Task 4 |
| 启动恢复（running→failed） | Task 2 (recover_stale_tasks) + Task 5 (on_startup 调用) |
| 路径穿越安全检查 | Task 10 |

### 占位符检查

无 TBD/TODO/占位符。所有代码步骤均包含完整实现。

### 类型一致性检查

- `insert_task_run(task_type, status, params_json, ...)`: Task 2 定义，Task 4 使用 ✓
- `update_task_run(task_id, **kwargs)`: Task 2 定义，Task 4 使用 ✓
- `get_task_run(task_id)`: Task 2 定义，Task 4/6/7 使用 ✓
- `query_task_runs(page, page_size, status, task_type)`: Task 2 定义，Task 6/7 使用 ✓
- `insert_llm_call(step, model, ...)`: Task 2 定义，Task 3 使用 ✓
- `query_llm_calls(page, page_size, step, model, success)`: Task 2 定义，Task 6/9 使用 ✓
- `get_llm_call(call_id)`: Task 2 定义，Task 9 使用 ✓
- `recover_stale_tasks()`: Task 2 定义，Task 5 使用 ✓
- `get_runner()`: Task 4 定义，Task 5/6/7 使用 ✓
- `set_task_context(task_run_id, step)`: Task 3 定义，Task 4 使用 ✓
