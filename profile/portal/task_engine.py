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



# === Log Handler: 捕获 profile.* 日志到内存队列 ===


class PortalLogHandler(logging.Handler):
    """捕获 profile 命名空间的日志，存入内存队列供 SSE 读取。

    日志队列在 detach 后仍保留，可继续被 get_logs 读取；
    detach 仅停止向该队列追加新日志。
    """

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self._queues: dict[int, deque] = {}
        self._active: set[int] = set()
        self._lock = threading.Lock()

    def attach(self, task_id: int) -> None:
        with self._lock:
            self._queues[task_id] = deque(maxlen=500)
            self._active.add(task_id)

    def detach(self, task_id: int) -> None:
        """停止向该任务的队列追加新日志，但保留已有日志供读取。"""
        with self._lock:
            self._active.discard(task_id)

    def get_logs(self, task_id: int, after_idx: int = 0) -> list[dict]:
        with self._lock:
            q = self._queues.get(task_id)
            if q is None:
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
            for task_id in self._active:
                q = self._queues.get(task_id)
                if q is not None:
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

        steps_status: list[dict] = []
        try:
            setup(log_dir=LOG_DIR)
            # setup() 首次调用会 root.handlers.clear()，需重新挂载 PortalLogHandler
            root_logger = logging.getLogger("profile")
            if self._log_handler not in root_logger.handlers:
                root_logger.addHandler(self._log_handler)

            init_db()

            update_task_run(task_id, status="running",
                            started_at=datetime.now().isoformat(timespec="seconds"))

            steps = TASK_STEPS.get(task_type, [])

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
                steps_json=steps_status,
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


