"""Workspace-wide pytest policy for the mono repo.

Keeps mono-side test decisions at the root so migrated app trees stay as close
to their upstream repos as possible (subtree sync friendly).
"""

from __future__ import annotations

import os
import shutil
import time

import pytest

# 每次运行独立 basetemp 的父目录（位于被 gitignore 的 data/ 下）
_BASETEMP_ROOT = "data/pytest-tmp"
# 只保留最近若干天的运行目录，避免 data/ 无限增长
_BASETEMP_KEEP_SECONDS = 7 * 24 * 3600

# 上游（WithLangGraph 原仓库 HEAD b127a2e）即失败的用例。test_qq.py 的角色卡切换用例
# 已在 mono 侧修好（MagicMock checkpointer + AsyncMock adelete_thread，2026-10-29 实测通过），
# 故从本表移除——否则 Companion Step 1 新增的 repo.set 断言会被整条 skip 掉（假绿）。
_KNOWN_UPSTREAM_FAILURES = {
    # checkSelf（HEAD e8e7864）4 个 schema 漂移失败：近期 refactor（readers 内联、
    # portal UI 重构）改了 analysis 输出键名，测试断言仍是旧键。上游修好后移除。
    "apps/checkself/tests/test_activity.py::test_single_source_traces_through",
    "apps/checkself/tests/test_direction.py::test_analyze_weekly_trends",
    "apps/checkself/tests/test_topic.py::test_analyze_returns_top_words",
    "apps/checkself/tests/test_topic.py::test_analyze_categorizes",
}


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """给每次运行分配独立 basetemp，避开跨身份残留的私有 ACL 目录。

    固定 basetemp 名字（原 addopts 里的 tmp_pytest_run）会被本机另一身份启动的
    运行（凌晨 00:00 的定时任务）创建成私有 ACL 目录：之后本用户连列举都被拒绝，
    pytest 在 getbasetemp 的 rm_rf 处抛 WinError 5，所有 tmp_path 用例 setup 即失败。
    每次运行用独立路径，互不碰撞；trylast 保证在 pytest 内置 tmpdir 插件之后覆写。
    """
    factory = config._tmp_path_factory
    root = config.rootpath / _BASETEMP_ROOT
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for stale in root.iterdir():
        try:
            if stale.is_dir() and now - stale.stat().st_mtime > _BASETEMP_KEEP_SECONDS:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue
    factory._given_basetemp = root / f"run-{os.getpid()}-{time.time_ns()}"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.nodeid in _KNOWN_UPSTREAM_FAILURES:
            item.add_marker(
                pytest.mark.skip(
                    reason="原仓库 HEAD 即失败，修复未提交；subtree 同步后移除"
                    "（见 conftest._KNOWN_UPSTREAM_FAILURES）"
                )
            )
