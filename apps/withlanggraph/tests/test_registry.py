"""Tests for the gacore.tools registry: the canonical tool list and its contract.

The registry is the seam between the LLM and the graph: TOOL_NAMES must agree with
what build_tool_list() returns, so binding the model and executing tools never disagree.
"""

from __future__ import annotations

from gacore.config import Config
from gacore.tools import TOOL_NAMES, build_tool_list


def _tools_by_name(tmp_cfg: Config) -> dict[str, object]:
    return {tool.name: tool for tool in build_tool_list(tmp_cfg)}


def test_build_tool_list_matches_tool_names(tmp_cfg: Config) -> None:
    given = build_tool_list(tmp_cfg)

    assert len(given) == len(TOOL_NAMES)
    assert sorted(tool.name for tool in given) == sorted(TOOL_NAMES)


def test_every_tool_exposes_a_valid_args_schema(tmp_cfg: Config) -> None:
    for tool in build_tool_list(tmp_cfg):
        schema = tool.args_schema.model_json_schema()
        assert "properties" in schema


def test_every_tool_name_attribute_matches_registry_names(tmp_cfg: Config) -> None:
    given = _tools_by_name(tmp_cfg)

    assert set(given) == set(TOOL_NAMES)
    for name in TOOL_NAMES:
        assert given[name].name == name


def test_smoke_file_read_missing_file_returns_error_dict(tmp_cfg: Config) -> None:
    given = _tools_by_name(tmp_cfg)

    result = given["file_read"].invoke({"path": str(tmp_cfg.root / "nope.txt")})

    assert isinstance(result, dict)
    assert result["error"] == "not_found"


def test_smoke_web_execute_js_returns_stub_error_dict(tmp_cfg: Config) -> None:
    given = _tools_by_name(tmp_cfg)

    result = given["web_execute_js"].invoke({"script": "document.title"})

    assert result == {
        "error": "web_execute_js is not supported in this reimplementation (TMWebDriver removed)"
    }
