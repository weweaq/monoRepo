"""langTrack 结构化知识图谱（KG）抽取层。

对外入口：``run_extract(day, ...)``（跑一次抽取）、``export_day(day, ...)``（--dump 导出）。
CLI 见 ``python -m gacore.langTrack.extract --help``。
"""

from __future__ import annotations

from gacore.langTrack.extract.runner import RunResult, export_day, run_extract

__all__ = ["RunResult", "export_day", "run_extract"]
