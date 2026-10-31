"""测试隔离：日志、数据库、画像输出目录固定写到本次用例的 tmp_path。

背景（2026-10-31）：`apps/checkself/logs` 下 164 个目录全部是 pytest 产物——
`task_engine._run_task` 里 `setup(log_dir=LOG_DIR)` 用的是真实 `profile.config.LOG_DIR`，
而测试直接调用 `TaskRunner.start_task()`，于是每跑一次 pytest 就新建一个日志目录。
同一批用例还直接用真实 `data/profile.db` 并 `DELETE FROM llm_calls/task_runs`。

注意：多个模块在**导入期**就把 `LOG_DIR` / `OBSIDIAN_OUTPUT_DIR` 绑成了模块属性，
只改 `profile.config` 不够，必须把每个绑定点一起改写（见 `_BINDINGS`）。
"""

from __future__ import annotations

import importlib
import logging

import pytest

import profile.db.init_db as db_module
import profile.log as log_module

# 每个模块在导入期绑定的运行期目录属性；conftest 统一改成 tmp_path
_BINDINGS = {
    "profile.config": ("LOG_DIR", "DB_PATH", "OBSIDIAN_OUTPUT_DIR"),
    "profile.db.init_db": ("DB_PATH",),
    "profile.portal.app": ("LOG_DIR",),
    "profile.output.writer": ("OBSIDIAN_OUTPUT_DIR",),
    "profile.refresh.reporter": ("OBSIDIAN_OUTPUT_DIR",),
    "profile.portal.routes.dashboard": ("OBSIDIAN_OUTPUT_DIR",),
    "profile.portal.routes.profiles": ("OBSIDIAN_OUTPUT_DIR",),
    "profile.cli.ingest": ("LOG_DIR",),
    "profile.cli.extract_intents": ("LOG_DIR",),
    "profile.cli.generate_profiles": ("LOG_DIR",),
    "profile.cli.refresh_all": ("LOG_DIR",),
}


def _reset_logging() -> None:
    """关掉并移除 profile 命名空间的文件 handler，重置日志模块级状态。"""
    root = logging.getLogger("profile")
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
            root.removeHandler(handler)
    log_module._INITIALIZED = False
    log_module._LOG_DIR = None
    log_module._LOG_FILE = None
    log_module._RUN_ID = None


@pytest.fixture(autouse=True)
def isolate_runtime_dirs(tmp_path, monkeypatch):
    """把日志目录、数据库、画像输出目录全部指向 tmp_path。"""
    values = {
        "LOG_DIR": tmp_path / "logs",
        "DB_PATH": tmp_path / "data" / "profile.db",
        "OBSIDIAN_OUTPUT_DIR": tmp_path / "obsidian",
    }
    values["DB_PATH"].parent.mkdir(parents=True, exist_ok=True)
    values["OBSIDIAN_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)

    for module_name, attrs in _BINDINGS.items():
        module = importlib.import_module(module_name)
        for attr in attrs:
            monkeypatch.setattr(module, attr, values[attr], raising=False)

    _reset_logging()
    db_module.init_db()
    yield
    _reset_logging()
