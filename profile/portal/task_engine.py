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
