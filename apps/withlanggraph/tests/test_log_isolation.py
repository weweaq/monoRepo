"""测试日志/心跳隔离守卫：pytest 进程绝不写真实 logs/ 与 data/run/（P0 Task 6）。"""

from __future__ import annotations

import os
from pathlib import Path


def test_test_log_root_is_not_project_logs():
    root = Path(os.environ["GACORE_LOG_ROOT"]).resolve()
    project_logs = (Path(__file__).resolve().parents[1] / "logs").resolve()
    assert root != project_logs
    assert project_logs not in root.parents
    assert "GACORE_RUN_DIR" not in os.environ


def test_sink_uses_test_log_root():
    from gacore import jsonl_logger

    root = Path(os.environ["GACORE_LOG_ROOT"]).resolve()
    sink = jsonl_logger._get_sink()
    assert isinstance(sink, jsonl_logger._DailyFileHandler)
    assert Path(sink._log_root).resolve() == root
