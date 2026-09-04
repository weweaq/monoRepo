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
