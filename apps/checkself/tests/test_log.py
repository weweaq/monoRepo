"""日志按天归档：logs/YYYY-MM-DD/run.jsonl，同一天追加，跨天自动切目录。"""

from __future__ import annotations

import logging
import re

import profile.log as log_module


def _reset_logging() -> None:
    root = logging.getLogger("profile")
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
            root.removeHandler(handler)
    log_module._INITIALIZED = False
    log_module._LOG_DIR = None
    log_module._LOG_FILE = None
    log_module._RUN_ID = None


def _file_handler() -> logging.FileHandler:
    for handler in logging.getLogger("profile").handlers:
        if isinstance(handler, logging.FileHandler):
            return handler
    raise AssertionError("未挂载文件 handler")


def test_setup_writes_into_day_dir(tmp_path):
    """日志落在 logs/YYYY-MM-DD/run.jsonl，而不是带时分秒的运行目录。"""
    _reset_logging()
    log_module.setup(log_dir=tmp_path)
    log_module.get_logger("t").info("hello-day-dir")
    run_id = log_module._RUN_ID

    day_dir = log_module.get_log_dir()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name)
    assert day_dir == tmp_path / day_dir.name
    assert log_module.get_log_file() == day_dir / "run.jsonl"

    lines = (day_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("hello-day-dir" in line for line in lines)
    assert all(run_id in line for line in lines)  # 每条日志都带同一个 run_id
    _reset_logging()


def test_same_day_runs_append_to_one_file(tmp_path):
    """同一天两次运行追加同一文件，各写一条「运行开始」且 run_id 不同。"""
    _reset_logging()
    log_module.setup(log_dir=tmp_path)
    log_module.get_logger("t").info("run-one")
    run_id_1 = log_module._RUN_ID
    _reset_logging()

    log_module.setup(log_dir=tmp_path)
    log_module.get_logger("t").info("run-two")
    run_id_2 = log_module._RUN_ID
    _reset_logging()

    assert run_id_1 != run_id_2

    day_dir = tmp_path / log_module._today()
    assert [p.name for p in day_dir.iterdir()] == ["run.jsonl"]

    text = (day_dir / "run.jsonl").read_text(encoding="utf-8")
    assert text.count("运行开始") == 2
    assert "run-one" in text
    assert "run-two" in text
    assert text.count(run_id_2) >= 2  # 运行开始 + 该运行的业务日志


def test_cross_day_switches_to_new_dir(tmp_path, monkeypatch):
    """整夜运行的进程跨 0 点后切到新日期目录，旧文件保留切换前的日志。"""
    _reset_logging()
    monkeypatch.setattr(log_module, "_today", lambda: "2026-10-30")
    log_module.setup(log_dir=tmp_path)
    log_module.get_logger("t").info("before-midnight")

    monkeypatch.setattr(log_module, "_today", lambda: "2026-10-31")
    log_module.get_logger("t").info("after-midnight")
    assert log_module.get_log_dir() == tmp_path / "2026-10-31"
    assert log_module.get_log_file() == tmp_path / "2026-10-31" / "run.jsonl"
    _reset_logging()

    old_text = (tmp_path / "2026-10-30" / "run.jsonl").read_text(encoding="utf-8")
    new_text = (tmp_path / "2026-10-31" / "run.jsonl").read_text(encoding="utf-8")
    assert "before-midnight" in old_text
    assert "after-midnight" not in old_text
    assert "after-midnight" in new_text


def test_switch_failure_does_not_raise_and_retries(tmp_path, monkeypatch):
    """切天失败（如磁盘满/权限）不能打扰业务，且下一条记录会重试。"""
    _reset_logging()
    monkeypatch.setattr(logging, "raiseExceptions", False)  # 静音 handleError 的 stderr 输出
    monkeypatch.setattr(log_module, "_today", lambda: "2026-10-30")
    log_module.setup(log_dir=tmp_path)
    logger = log_module.get_logger("t")
    logger.info("day-one")

    handler = _file_handler()
    real_switch = handler._switch_day
    calls = {"n": 0}

    def flaky_switch(day: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk busy")
        real_switch(day)

    monkeypatch.setattr(handler, "_switch_day", flaky_switch)
    monkeypatch.setattr(log_module, "_today", lambda: "2026-10-31")

    logger.info("lost-on-failure")  # 不抛异常
    assert handler._day == "2026-10-30"  # 状态未推进
    assert not (tmp_path / "2026-10-31").exists()

    logger.info("retry-ok")
    assert handler._day == "2026-10-31"
    new_text = (tmp_path / "2026-10-31" / "run.jsonl").read_text(encoding="utf-8")
    assert "retry-ok" in new_text
    _reset_logging()


def test_run_start_written_even_when_level_filters_info(tmp_path):
    """文件级别设成 WARNING 时，「运行开始」仍要落盘，保证运行分界不丢。"""
    _reset_logging()
    log_module.setup(log_dir=tmp_path, level=logging.WARNING, console_level=logging.CRITICAL)
    log_module.get_logger("t").info("filtered-out")
    _reset_logging()

    text = (tmp_path / log_module._today() / "run.jsonl").read_text(encoding="utf-8")
    assert text.count("运行开始") == 1
    assert "filtered-out" not in text


def test_runtime_dir_bindings_point_to_tmp(tmp_path):
    """所有在导入期绑定运行期目录的模块都必须被隔离，防止偷偷写真实目录。"""
    import importlib

    expected = {
        "LOG_DIR": tmp_path / "logs",
        "DB_PATH": tmp_path / "data" / "profile.db",
        "OBSIDIAN_OUTPUT_DIR": tmp_path / "obsidian",
    }
    bindings = {
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

    for module_name, attrs in bindings.items():
        module = importlib.import_module(module_name)
        for attr in attrs:
            assert getattr(module, attr) == expected[attr], f"{module_name}.{attr} 未隔离"
