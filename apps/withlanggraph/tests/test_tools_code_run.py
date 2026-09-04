"""Tests for the gacore code_run LangChain tool (wave 1 of the GA reimplementation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gacore.config import Config
from gacore.tools.code_run import _MAX_OUTPUT_CHARS, _TRUNCATION_MARKER, code_run


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """Isolated Config rooted at the pytest tmp_path; never touches real project dirs."""
    return Config.for_tests(tmp_path)


def test_python_prints_stdout_when_success(cfg: Config) -> None:
    result = code_run.func(language="python", code="print('hi')", _cfg=cfg)

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert "hi" in result["stdout"]


def test_timeout_kills_process_and_returns(cfg: Config) -> None:
    result = code_run.func(language="python", code="import time; time.sleep(100)", timeout_seconds=2, _cfg=cfg)

    assert result["timed_out"] is True
    assert result["status"] == "timeout"


def test_python_script_gets_header_prepended(cfg: Config) -> None:
    cfg.asset_dir.mkdir(parents=True, exist_ok=True)
    (cfg.asset_dir / "code_run_header.py").write_text("HEADER_COMMENT_X\n", encoding="utf-8")

    result = code_run.func(language="python", code="print('ok')", _cfg=cfg)

    assert result["status"] == "ok"
    scripts = list(cfg.temp_dir.glob("*.ai.py"))
    assert len(scripts) == 1
    content = scripts[0].read_text(encoding="utf-8")
    assert content.startswith("HEADER_COMMENT_X")
    assert content.endswith("print('ok')")


def test_python_runs_when_header_missing(cfg: Config) -> None:
    result = code_run.func(language="python", code="print('no-header')", _cfg=cfg)

    assert result["status"] == "ok"
    assert "no-header" in result["stdout"]


def test_nonzero_exit_code_reported(cfg: Config) -> None:
    result = code_run.func(language="python", code="import sys; sys.exit(3)", _cfg=cfg)

    assert result["status"] == "ok"
    assert result["exit_code"] == 3


def test_powershell_output(cfg: Config) -> None:
    result = code_run.func(language="powershell", code='Write-Output "hello-ps"', _cfg=cfg)

    assert result["status"] == "ok"
    assert "hello-ps" in result["stdout"]


def test_unknown_language_returns_error(cfg: Config) -> None:
    result = code_run.func(language="ruby", code="puts 1", _cfg=cfg)

    assert result["status"] == "error"
    assert result["exit_code"] is None


def test_missing_code_returns_error(cfg: Config) -> None:
    result = code_run.func(language="python", code=None, _cfg=cfg)

    assert result["status"] == "error"


def test_stdout_truncated_when_large(cfg: Config) -> None:
    result = code_run.func(language="python", code="print('x' * 30000)", _cfg=cfg)

    assert result["truncated"] is True
    assert len(result["stdout"]) <= _MAX_OUTPUT_CHARS + len(_TRUNCATION_MARKER)
    assert _TRUNCATION_MARKER in result["stdout"]


def test_cwd_resolved_relative_to_config_root(cfg: Config) -> None:
    subdir = cfg.root / "subdir"
    subdir.mkdir(parents=True)

    result = code_run.func(language="python", code="import os; print(os.getcwd())", cwd="subdir", _cfg=cfg)

    assert result["status"] == "ok"
    assert str(subdir.resolve()).lower() in result["stdout"].lower()


def test_injection_arg_excluded_from_tool_schema() -> None:
    fields = set(code_run.args_schema.model_fields)

    assert "_cfg" not in fields
    assert {"language", "code", "timeout_seconds", "cwd"} <= fields
