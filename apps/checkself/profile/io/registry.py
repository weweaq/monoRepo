"""信息源读取器注册表（自动发现）。

扫描 profile.io 包下所有模块，收集 BaseReader 的子类，实现"丢一个 reader 文件即生效"的
外挂式信息源架构。

设计要点：
  - 自动发现：新增源只需在 profile/io/ 放一个 BaseReader 子类文件，无需改任何中央注册代码。
  - 容错：单个插件模块导入失败不影响其他源的发现。
  - 稳定排序：用 _PRIORITY 保证已知源的展示/处理顺序与重构前一致，未知源追加到末尾。
  - 元数据自描述：每个 reader 通过 analysis_type / profile_target 声明分析方式与画像归属，
    生成流水线据此分派，不再硬编码 if source == "trae"。
"""

import importlib
import pkgutil
from typing import TYPE_CHECKING

from profile.io.base import BaseReader

if TYPE_CHECKING:
    pass

# 已知源的稳定排序兜底；未知源追加到末尾，保证输出顺序与重构前一致。
_PRIORITY = ["trae", "marvis", "bilibili", "netease"]

_SKIP_MODULES = {"base", "__init__"}

_DISCOVERED: dict[str, type[BaseReader]] | None = None


def _walk_subclasses(cls: type) -> set[type]:
    """递归收集所有（间接）子类，避免多层继承漏掉。"""
    result: set[type] = set()
    for sub in cls.__subclasses__():
        result.add(sub)
        result |= _walk_subclasses(sub)
    return result


def _discover() -> dict[str, type[BaseReader]]:
    import profile.io as io_pkg

    for mod in pkgutil.iter_modules(io_pkg.__path__):
        name = mod.name
        if name in _SKIP_MODULES:
            continue
        try:
            importlib.import_module(f"profile.io.{name}")
        except Exception:
            # 单个插件损坏不应连累整体发现
            continue

    found: dict[str, type[BaseReader]] = {}
    for cls in _walk_subclasses(BaseReader):
        try:
            # 用 source_name 作为注册键，需实例化取属性（只读属性，无副作用）
            sample = cls()
            key = sample.source_name
        except Exception:
            continue
        if not key:
            continue
        found[key] = cls
    return found


def _ensure() -> dict[str, type[BaseReader]]:
    global _DISCOVERED
    if _DISCOVERED is None:
        _DISCOVERED = _discover()
    return _DISCOVERED


def _ordered(names: list[str]) -> list[str]:
    ranked = sorted(names, key=lambda n: _PRIORITY.index(n) if n in _PRIORITY else len(_PRIORITY))
    return ranked


def get_reader_class(name: str) -> type[BaseReader] | None:
    return _ensure().get(name)


def get_reader(name: str) -> BaseReader | None:
    cls = get_reader_class(name)
    if cls is None:
        return None
    return cls()


def list_reader_names() -> list[str]:
    return _ordered(list(_ensure().keys()))


def channel_reader_names() -> list[str]:
    """profile_target == 'channel' 的源（各自生成独立 channel 画像）。"""
    result = []
    for name in list_reader_names():
        cls = get_reader_class(name)
        if cls is None:
            continue
        try:
            if cls().profile_target == "channel":
                result.append(name)
        except Exception:
            continue
    return result


def consumption_reader_names() -> list[str]:
    """profile_target == 'consumption' 的源（汇入 content_consumption 合成画像）。"""
    result = []
    for name in list_reader_names():
        cls = get_reader_class(name)
        if cls is None:
            continue
        try:
            if cls().profile_target == "consumption":
                result.append(name)
        except Exception:
            continue
    return result
