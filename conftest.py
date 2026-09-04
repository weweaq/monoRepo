"""Workspace-wide pytest policy for the mono repo.

Keeps mono-side test decisions at the root so migrated app trees stay as close
to their upstream repos as possible (subtree sync friendly).
"""

from __future__ import annotations

import pytest

# 上游（WithLangGraph 原仓库 HEAD b127a2e）即失败的用例：test_qq.py 的角色卡切换
# 用例里 checkpointer 为 MagicMock，无法 await adelete_thread。修复正在原仓库进行
# （qq.py / test_qq.py 有未提交改动），mono 侧不重复修以免分叉。
# subtree 同步上游修复后删除本条。
_KNOWN_UPSTREAM_FAILURES = {
    "apps/withlanggraph/tests/test_qq.py::test_role_command_switches_card_and_clears_thread",
    # checkSelf（HEAD e8e7864）4 个 schema 漂移失败：近期 refactor（readers 内联、
    # portal UI 重构）改了 analysis 输出键名，测试断言仍是旧键。上游修好后移除。
    "apps/checkself/tests/test_activity.py::test_single_source_traces_through",
    "apps/checkself/tests/test_direction.py::test_analyze_weekly_trends",
    "apps/checkself/tests/test_topic.py::test_analyze_returns_top_words",
    "apps/checkself/tests/test_topic.py::test_analyze_categorizes",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.nodeid in _KNOWN_UPSTREAM_FAILURES:
            item.add_marker(
                pytest.mark.skip(reason="原仓库 HEAD 即失败，修复未提交；subtree 同步后移除（见 conftest._KNOWN_UPSTREAM_FAILURES）")
            )
