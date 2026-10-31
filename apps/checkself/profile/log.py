"""AI-Debuggable Logging Module.

符合 AGENTS.md 日志规范：
- JSONL 格式，一行一个 JSON 对象
- 每条日志包含 timestamp, level, module, message
- 错误日志附带 error_type, stack_trace, context
- 敏感数据脱敏：API Key/Token/密码/Email/手机号
- 日志按天归档 logs/YYYY-MM-DD/run.jsonl，同一天所有运行追加同一文件；
  每次运行开头写一条 message="运行开始" 的记录，用 extra.run_id 区分运行
- 长驻进程跨天时自动切到新日期目录，不依赖重启

（2026-10-31 之前是「每次运行一个 logs/YYYY-MM-DD-HHmmss/ 目录」，
按用户要求改为按天归档，偏差记录见 docs/ROADMAP.md。）
"""

import json
import logging
import os
import re
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path


_LEVEL_NAMES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _env_level(name: str, default: int) -> int:
    """从环境变量读取日志级别，支持数字或名称。"""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return _LEVEL_NAMES.get(raw.strip().upper(), default)


# 敏感字段名模式（小写匹配）
_SENSITIVE_KEY_PATTERNS = re.compile(
    r"(api_key|apikey|api[-_]?secret|token|password|passwd|secret|email|phone|mobile|authorization)",
    re.IGNORECASE,
)

# 敏感值模式：邮箱、手机号、长 token
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_TOKEN_RE = re.compile(r"(?:Bearer\s+)?[A-Za-z0-9\-_]{20,}")


def _sanitize_value(key: str, value):
    """脱敏处理：根据 key 名或 value 格式判断是否需要脱敏。"""
    if not isinstance(value, str):
        return value
    if _SENSITIVE_KEY_PATTERNS.search(key):
        if len(value) <= 8:
            return "***"
        return value[:4] + "***" + value[-4:]
    if _EMAIL_RE.fullmatch(value):
        local, domain = value.split("@", 1)
        return local[:2] + "***@" + domain
    if _PHONE_RE.fullmatch(value):
        return value[:3] + "****" + value[-4:]
    return value


def _sanitize_dict(data: dict) -> dict:
    """递归脱敏字典中的敏感字段。"""
    sanitized = {}
    for k, v in data.items():
        if isinstance(v, dict):
            sanitized[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            sanitized[k] = [_sanitize_dict(item) if isinstance(item, dict) else _sanitize_value(k, item) for item in v]
        else:
            sanitized[k] = _sanitize_value(k, v)
    return sanitized


class JsonlFormatter(logging.Formatter):
    """JSONL 格式 Formatter。"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        if _RUN_ID:
            log_entry["run_id"] = _RUN_ID

        if record.levelno >= logging.ERROR:
            log_entry["error_type"] = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
            log_entry["stack_trace"] = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            ctx = getattr(record, "context", None)
            if ctx:
                log_entry["context"] = _sanitize_dict(ctx) if isinstance(ctx, dict) else ctx

        extra = getattr(record, "extra", None)
        if extra:
            log_entry["extra"] = _sanitize_dict(extra) if isinstance(extra, dict) else extra

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class _ContextFilter(logging.Filter):
    """自动注入 module 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return True


_LOG_DIR: Path | None = None
_LOG_FILE: Path | None = None
_RUN_ID: str | None = None
_INITIALIZED = False


def _today() -> str:
    """当前本地日期（东八区所在机器的本地时间），作为日志目录名。"""
    return datetime.now().strftime("%Y-%m-%d")


class _DailyFileHandler(logging.FileHandler):
    """按天归档的 JSONL handler：跨天时自动切到新日期目录的 run.jsonl。

    同一进程整夜运行时，0 点后的日志写入新一天的文件，而不是继续污染前一天。
    切换采用「先开新流、成功后再提交状态」，失败时保留旧流并在下一条记录重试，
    不会把 I/O 异常抛给业务调用方（与标准 FileHandler 行为一致）。
    """

    def __init__(self, base: Path, day: str) -> None:
        self._base = base
        self._day = day
        (self._base / day).mkdir(parents=True, exist_ok=True)
        super().__init__(self._base / day / "run.jsonl", mode="a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        day = _today()
        if day != self._day:
            self.acquire()
            try:
                if day != self._day:
                    try:
                        self._switch_day(day)
                    except OSError:
                        # 切换失败：保留旧流，状态不推进，下一条记录会再试
                        self.handleError(record)
                        return
            finally:
                self.release()
        super().emit(record)

    def _switch_day(self, day: str) -> None:
        global _LOG_DIR, _LOG_FILE
        new_dir = self._base / day
        new_dir.mkdir(parents=True, exist_ok=True)
        new_file = new_dir / "run.jsonl"

        old_stream = self.stream
        self.baseFilename = str(new_file)
        self.stream = self._open()  # 失败则直接抛 OSError，_day 保持旧值
        self._day = day
        _LOG_DIR, _LOG_FILE = new_dir, new_file
        if old_stream is not None:
            old_stream.close()


def setup(log_dir: Path | None = None, level: int | None = None, console_level: int | None = None) -> None:
    """初始化日志系统。

    日志按天归档：`logs/YYYY-MM-DD/run.jsonl`，同一天多次运行追加同一文件，
    不覆盖历史日志。每次运行开头写一条 `message="运行开始"` 的记录，
    后续每条日志都带同一个 `run_id`，用于在同一文件内区分运行。

    注意：追加写只在同一进程内严格串行；多个进程（如常驻 portal + 手动 CLI）
    同时写同一天文件时按行为单位尽力追加，Windows 下超长单行理论上可能交错。

    级别参数优先级：显式参数 > 环境变量 > 默认值。
    - `level` (文件 handler) 默认 DEBUG，可用 `PROFILE_LOG_LEVEL` 覆盖
    - `console_level` (控制台 handler) 默认 INFO，可用 `PROFILE_CONSOLE_LEVEL`
      或 `PROFILE_VERBOSE=1` 覆盖（VERBOSE 会把控制台也升到 DEBUG）

    Args:
        log_dir: 日志基目录（默认 logs/），实际会写入其下的 YYYY-MM-DD/ 子目录
        level: 文件 handler 级别（默认 DEBUG，记录最详细信息）
        console_level: 控制台 handler 级别（默认 INFO，避免 DEBUG 刷屏）
    """
    global _LOG_DIR, _LOG_FILE, _RUN_ID, _INITIALIZED
    if _INITIALIZED:
        return

    effective_level = level if level is not None else _env_level("PROFILE_LOG_LEVEL", logging.DEBUG)
    effective_console = console_level if console_level is not None else _env_level("PROFILE_CONSOLE_LEVEL", logging.INFO)
    if os.getenv("PROFILE_VERBOSE", "").lower() in ("1", "true", "yes", "on"):
        effective_console = logging.DEBUG

    base = (Path(log_dir) if log_dir else Path("logs")).resolve()
    day = _today()

    root = logging.getLogger("profile")
    root.setLevel(effective_level)
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)

    # JSONL 文件 handler（按天归档，当天追加，含 DEBUG）
    fh = _DailyFileHandler(base, day)
    fh.setLevel(effective_level)
    fh.setFormatter(JsonlFormatter())
    fh.addFilter(_ContextFilter())
    root.addHandler(fh)

    # 控制台 handler（简洁格式，方便人工看，默认 INFO 不刷 DEBUG）
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(effective_console)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    ch.addFilter(_ContextFilter())
    root.addHandler(ch)

    _LOG_DIR = base / day
    _LOG_FILE = _LOG_DIR / "run.jsonl"
    _RUN_ID = f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    _INITIALIZED = True

    # 运行分界记录直接写文件 handler，绕过级别过滤，保证任何日志级别下都留痕
    record = logging.LogRecord(
        name="profile",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="运行开始",
        args=(),
        exc_info=None,
    )
    record.extra = {
        "run_id": _RUN_ID,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "argv": " ".join(sys.argv),
    }
    fh.emit(record)


def get_logger(name: str) -> logging.Logger:
    """获取 logger，自动挂在 profile 命名空间下。"""
    if not name.startswith("profile."):
        name = f"profile.{name}"
    return logging.getLogger(name)


def log_error(logger: logging.Logger, msg: str, exc: Exception | None = None, context: dict | None = None) -> None:
    """记录错误日志，自动附带 error_type, stack_trace, context。"""
    extra = {}
    if context:
        extra["context"] = _sanitize_dict(context) if isinstance(context, dict) else context
    logger.error(msg, exc_info=exc is not None, extra=extra)


def log_exception(logger: logging.Logger, msg: str, exc: Exception, context: dict | None = None) -> None:
    """记录异常日志（等同于 log_error，语义更明确）。"""
    log_error(logger, msg, exc=exc, context=context)


def get_log_dir() -> Path | None:
    """返回当天日志目录（logs/YYYY-MM-DD/）。"""
    return _LOG_DIR


def get_log_file() -> Path | None:
    """返回当天日志文件（logs/YYYY-MM-DD/run.jsonl）。"""
    return _LOG_FILE
