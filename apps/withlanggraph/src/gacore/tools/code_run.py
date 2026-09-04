"""LangChain @tool running python/powershell code in a subprocess with timeout kill.

Mirrors GA's do_code_run: python code runs from a temp .ai.py file with the
code_run_header.py preamble prepended; powershell runs as a -Command one-liner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final, Literal, TypedDict

from langchain_core.tools import tool

from gacore.config import Config
from gacore.jsonl_logger import get_logger

_MAX_OUTPUT_CHARS: Final = 20000
_TRUNCATION_MARKER: Final = "\n...[output truncated]"
_HEADER_FILENAME: Final = "code_run_header.py"
_WINDOWS: Final = os.name == "nt"

logger = get_logger("tools.code_run")
_default_cfg: Final = Config.default()


def _resolve_powershell() -> str:
    """Find the powershell executable, falling back to the well-known System32 path.

    shutil.which("powershell") covers the normal case where WindowsPowerShell is on
    PATH.  Some sandboxed environments (e.g. CI containers, restricted test runners)
    strip System32 from PATH even though the binary exists; the fallback resolves
    it via %SystemRoot% so the tool still works there.
    """
    found = shutil.which("powershell") or shutil.which("powershell.exe")
    if found:
        return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate)


class CodeRunResult(TypedDict, total=False):
    """Result of one code execution: status, captured output and exit info."""

    status: Literal["ok", "timeout", "error"]
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    language: str
    truncated: bool


def _truncate(text: str) -> tuple[str, bool]:
    """Cap output at _MAX_OUTPUT_CHARS, appending a marker when cut."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text, False
    return text[:_MAX_OUTPUT_CHARS] + _TRUNCATION_MARKER, True


def _prepare_python_script(code: str, cfg: Config) -> Path:
    """Write code to a unique temp .ai.py file with the header preamble prepended."""
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    script_path = cfg.temp_dir / f"code_{uuid.uuid4().hex[:8]}.ai.py"
    header_path = cfg.asset_dir / _HEADER_FILENAME
    header = header_path.read_text(encoding="utf-8") if header_path.is_file() else ""
    script_path.write_text(header + code, encoding="utf-8")
    return script_path


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the process and, on Windows, its whole child tree via taskkill /T /F."""
    proc.kill()
    if _WINDOWS and proc.pid is not None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True, check=False)


@tool
def code_run(
    language: str = "python",
    code: str | None = None,
    timeout_seconds: int = 60,
    cwd: str | None = None,
    _cfg: Config | None = None,
) -> CodeRunResult:
    """Execute python or powershell code in a subprocess and return captured stdout/stderr.

    python runs from a temp .ai.py file (with code_run_header.py prepended); powershell runs as a
    -Command one-liner. The process is killed when timeout_seconds elapses, including its child
    tree on Windows. Output is truncated at 20000 chars per stream. The _cfg arg is injected at
    runtime and excluded from the tool schema.
    """
    cfg = _cfg if _cfg is not None else _default_cfg
    if code is None:
        return CodeRunResult(
            status="error",
            stdout="",
            stderr="code parameter is required",
            exit_code=None,
            timed_out=False,
            language=language,
            truncated=False,
        )

    run_dir = cfg.root if cwd is None else (cfg.root / cwd).resolve()
    match language:
        case "python":
            script_path = _prepare_python_script(code, cfg)
            cmd: list[str] = [sys.executable, "-X", "utf8", "-u", str(script_path)]
        case "powershell":
            cmd = [_resolve_powershell(), "-NoProfile", "-Command", code]
        case _:
            logger.warning("code_run rejected unsupported language", language=language)
            return CodeRunResult(
                status="error",
                stdout="",
                stderr=f"unsupported language: {language}",
                exit_code=None,
                timed_out=False,
                language=language,
                truncated=False,
            )

    logger.info("code_run start", language=language, timeout_seconds=timeout_seconds, cwd=str(run_dir))
    try:
        if _WINDOWS:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(run_dir),
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(run_dir),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
    except OSError as e:
        logger.error(
            "code_run failed to spawn process",
            error_type=type(e).__name__,
            stack_trace=str(e),
            context={"language": language, "cwd": str(run_dir)},
        )
        return CodeRunResult(
            status="error",
            stdout="",
            stderr=str(e),
            exit_code=None,
            timed_out=False,
            language=language,
            truncated=False,
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        timed_out = False
    except TimeoutExpired:
        timed_out = True
        logger.warning("code_run timed out, killing process tree", timeout_seconds=timeout_seconds)
        _kill_process_tree(proc)
        stdout, stderr = proc.communicate()

    stdout_text, stdout_truncated = _truncate(stdout or "")
    stderr_text, stderr_truncated = _truncate(stderr or "")
    status: Literal["ok", "timeout"] = "timeout" if timed_out else "ok"
    logger.info(
        "code_run finished",
        status=status,
        exit_code=proc.returncode,
        timed_out=timed_out,
        truncated=stdout_truncated or stderr_truncated,
    )
    return CodeRunResult(
        status=status,
        stdout=stdout_text,
        stderr=stderr_text,
        exit_code=proc.returncode,
        timed_out=timed_out,
        language=language,
        truncated=stdout_truncated or stderr_truncated,
    )
