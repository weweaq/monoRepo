"""抽取通道的共享契约：通道声明、运行上下文、落库结果。

通道 = 一个 prompt 文件 + 一份素材路由 + 一个落库函数。新增通道只需在
``channels/`` 下加一个模块并在 ``channels/__init__.py`` 注册，runner 无需改动。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

# node_id 前缀 → node_type。LLM 漏报节点时按此约定补占位节点，保证无悬空边（§9）。
_NODE_TYPE_BY_PREFIX: Final[dict[str, str]] = {
    "person": "Person",
    "event": "Event",
    "project": "Project",
    "theme": "Theme",
    "obs": "Observation",
    "axiom": "Axiom",
}


def infer_node_type(node_id: str) -> str:
    """由 node_id 前缀推断节点类型；无法识别时回落 ``Unknown``。"""
    prefix = node_id.split("_", 1)[0].lower()
    return _NODE_TYPE_BY_PREFIX.get(prefix, "Unknown")


@dataclass(frozen=True, slots=True)
class ExtractContext:
    """单次抽取运行的上下文（落库函数据此填 day/run_id/src）。"""

    day: str
    run_id: str
    prompt_ver: str


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """一个抽取通道的声明。

    - key：通道名，同时是 ``kg_*`` 行的 ``src`` 值；
    - prompt：``config/kg_prompts/`` 下的 prompt 文件名（改 prompt 不动代码）；
    - sources：素材路由（§3.2），只把该通道需要的源 detail 喂给它；
    - handle：``(conn, ctx, payload) -> (写入行数, 统计)``，在单事务内落库。
    """

    key: str
    prompt: str
    sources: tuple[str, ...]
    handle: Callable[[sqlite3.Connection, ExtractContext, Any], tuple[int, dict[str, Any]]]


@dataclass(slots=True)
class ChannelOutcome:
    """单通道执行结果，落进 ``kg_runs.channel_stats``。"""

    key: str
    status: str = "ok"  # ok | skipped | failed
    rows: int = 0
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "rows": self.rows}
        if self.error:
            out["error"] = self.error
        if self.extra:
            out.update(self.extra)
        return out
