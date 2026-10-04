"""Response capture in llm_requests.jsonl: every line = one complete call unit
(request + response + duration), failed calls record response.error, streaming
aggregates chunks, and non-message inputs (judge string prompts) are wrapped
instead of dropped. Regression for the ops gap: requests were logged without
responses, leaving the "what did the model actually answer" half invisible."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from gacore import llm_request_log
from gacore.jsonl_logger import session_id
from gacore.llm_request_log import install_llm_logging


class _ExplodingModel:
    """Minimal non-pydantic model whose invoke always fails (patch target)."""

    model_name = "exploding-1"

    def invoke(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    def ainvoke(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stream(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
        yield  # pragma: no cover — makes this a generator function

    async def astream(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
        yield  # pragma: no cover

    def bind_tools(self, tools: Any, *args: Any, **kwargs: Any) -> Any:
        return self


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = type("CfgStub", (), {"default": staticmethod(lambda: _for_tests(tmp_path))})
    monkeypatch.setattr(llm_request_log, "Config", cfg)
    return tmp_path / "logs"


def _for_tests(tmp_path: Path) -> Any:
    from gacore.config import Config

    return Config.for_tests(tmp_path)


def _read_lines(logs: Path) -> list[dict]:
    today = sorted(p for p in logs.glob("*/llm_requests.jsonl"))
    assert today, "no llm_requests.jsonl written"
    lines = today[-1].read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _ai(content: str) -> AIMessage:
    return AIMessage(content=content)


class _ChunkyModel:
    """Non-pydantic model whose stream yields many small chunks (aggregation target)."""

    model_name = "chunky-1"

    def invoke(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def ainvoke(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stream(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        for part in ("你", "好", "世", "界"):
            yield AIMessageChunk(content=part)

    async def astream(self, input_: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
        yield  # pragma: no cover

    def bind_tools(self, tools: Any, *args: Any, **kwargs: Any) -> Any:
        return self


def test_invoke_records_response_and_duration(log_dir: Path) -> None:
    model = install_llm_logging(FakeListChatModel(responses=["hello world"]), "fake")
    result = model.invoke([HumanMessage("hi")])
    assert result.content == "hello world"

    records = _read_lines(log_dir)
    assert len(records) == 1
    rec = records[0]
    assert rec["run_kind"] == "invoke"
    assert rec["response"]["content"] == "hello world"
    assert rec["response"]["content_chars"] == len("hello world")
    assert rec["session"] == session_id()
    assert isinstance(rec["duration_ms"], int) and rec["duration_ms"] >= 0
    # request side still intact
    assert rec["messages"][0]["role"] == "human"


def test_invoke_failure_records_error_and_reraises(log_dir: Path) -> None:
    model = install_llm_logging(_ExplodingModel(), "fake")
    with pytest.raises(RuntimeError, match="provider down"):
        model.invoke([HumanMessage("hi")])

    records = _read_lines(log_dir)
    assert len(records) == 1
    assert "provider down" in records[0]["response"]["error"]
    assert isinstance(records[0]["duration_ms"], int)


def test_string_input_wrapped_as_user_message(log_dir: Path) -> None:
    """Judge/structured calls invoke with a plain string — must not record empty."""
    model = install_llm_logging(FakeListChatModel(responses=["ok"]), "fake")
    model.invoke("合并还是新建？")

    records = _read_lines(log_dir)
    assert records[0]["messages"] == [{"role": "user", "content": "合并还是新建？"}]
    assert records[0]["response"]["content"] == "ok"


def test_stream_aggregates_chunks(log_dir: Path) -> None:
    model = install_llm_logging(_ChunkyModel(), "fake")
    chunks = list(model.stream([HumanMessage("hi")]))
    assert len(chunks) == 4

    records = _read_lines(log_dir)
    assert len(records) == 1
    resp = records[0]["response"]
    assert resp["aggregated"] is True
    assert resp["chunks"] == 4
    assert resp["content"] == "你好世界"
    assert resp["content_chars"] == 4
    assert "interrupted" not in resp


def test_stream_interrupted_marks_partial(log_dir: Path) -> None:
    model = install_llm_logging(_ChunkyModel(), "fake")
    gen = model.stream([HumanMessage("hi")])
    next(gen)
    gen.close()

    records = _read_lines(log_dir)
    resp = records[0]["response"]
    assert resp["interrupted"] is True
    assert resp["chunks"] == 1
    assert resp["content"] == "你"
