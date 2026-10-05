"""news_mcp 桥接工具单测（封闭）：注册表 presence / 降级约定 / 参数透传 / 内容格式化。

绝不真启 npx 子进程（真实端到端验证见 ROADMAP v3.4 执行记录）；桥接行为全部 stub。

运行：uv run pytest apps/withlanggraph/tests/test_news_mcp.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.tools import TOOL_NAMES, build_tool_list
from gacore.tools import news_mcp


# --------------------------------------------------------------------------- #
# 注册表：三个新闻工具必须进规范集合（LLM 绑定与图构建同一来源）                #
# --------------------------------------------------------------------------- #
def test_news_tools_registered():
    assert {"news_hot_list", "news_search", "news_list_sources"} <= set(TOOL_NAMES)
    build_list = build_tool_list(None)  # cfg 仅签名占位，工具自配置
    assert {"news_hot_list", "news_search", "news_list_sources"} <= {t.name for t in build_list}


# --------------------------------------------------------------------------- #
# 桥接崩溃 → 可读降级文本，绝不 raise（对话连续性契约）                          #
# --------------------------------------------------------------------------- #
def test_tool_degrades_on_bridge_crash(monkeypatch):
    def _boom(name, args, timeout=None):
        raise RuntimeError("npx 进程崩溃")

    monkeypatch.setattr(news_mcp._bridge, "call_tool", _boom)
    out = news_mcp.news_hot_list.invoke({"source_id": "zhihu", "count": 5})
    assert out.startswith("新闻工具暂不可用：")
    assert "npx 进程崩溃" in out


# --------------------------------------------------------------------------- #
# 参数透传：工具名与参数原样进桥（MCP 工具名/参数名是外部契约，别在工具层改名）  #
# --------------------------------------------------------------------------- #
def test_args_passthrough(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def _fake(name, args, timeout=None):
        captured.append((name, dict(args)))
        return "# 知乎 (zhihu)\n1. 测试条目"

    monkeypatch.setattr(news_mcp._bridge, "call_tool", _fake)
    out = news_mcp.news_hot_list.invoke({"source_id": "zhihu", "count": 5})
    assert out == "# 知乎 (zhihu)\n1. 测试条目"
    assert captured == [("get_hotest_latest_news", {"id": "zhihu", "count": 5})]


def test_search_optional_source_omitted_when_empty(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def _fake(name, args, timeout=None):
        captured.append((name, dict(args)))
        return "- 无匹配"

    monkeypatch.setattr(news_mcp._bridge, "call_tool", _fake)
    news_mcp.news_search.invoke({"keyword": "人工智能"})
    news_mcp.news_search.invoke({"keyword": "科技", "source": "weibo", "count": 5})
    assert captured[0] == ("search_news", {"keyword": "人工智能", "count": 10})
    assert captured[1] == ("search_news", {"keyword": "科技", "source": "weibo", "count": 5})


# --------------------------------------------------------------------------- #
# _format_content：MCP content 块拼接（text 块逐个连接，空块兜底）               #
# --------------------------------------------------------------------------- #
def test_format_content_joins_text_blocks():
    class _Block:
        def __init__(self, text=None):
            self.text = text

    assert news_mcp._format_content([_Block("a"), _Block("b")]) == "a\nb"
    # image 等无 text 的块跳过；全空兜底占位
    assert news_mcp._format_content([_Block(None), _Block("x")]) == "x"
    assert news_mcp._format_content([_Block(None)]) == "（MCP 返回空内容）"
    assert news_mcp._format_content(None) == "（MCP 返回空内容）"
