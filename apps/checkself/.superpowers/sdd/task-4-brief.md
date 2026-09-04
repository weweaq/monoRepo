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
