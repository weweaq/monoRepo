from unittest.mock import patch, MagicMock
from profile.portal.task_engine import set_task_context, clear_task_context
from profile.portal.db_store import query_llm_calls
from profile.db.init_db import init_db, get_connection


def setup_function():
    init_db()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM llm_calls")
        conn.execute("DELETE FROM task_runs")
        conn.commit()
    finally:
        conn.close()


def test_llm_call_recorded_on_success():
    """chat() 成功后应记录到 llm_calls 表"""
    from profile.llm.client import LLMClient

    client = LLMClient(api_key="test-key", api_url="http://fake", model="test-model")
    set_task_context(task_run_id=None, step="test_step")

    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"choices":[{"message":{"content":"hello"}}],"usage":{"prompt_tokens":5,"completion_tokens":10,"total_tokens":15}}'
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = client.chat("test prompt", system_prompt="sys prompt")

    assert result == "hello"
    calls = query_llm_calls()
    assert calls["total"] == 1
    call = calls["items"][0]
    assert call["model"] == "test-model"
    assert call["user_prompt"] == "test prompt"
    assert call["system_prompt"] == "sys prompt"
    assert call["response"] == "hello"
    assert call["prompt_tokens"] == 5
    assert call["completion_tokens"] == 10
    assert call["total_tokens"] == 15
    assert call["success"] == 1
    assert call["step"] == "test_step"
    clear_task_context()


def test_llm_call_recorded_on_failure():
    """chat() 失败后也应记录到 llm_calls 表"""
    import urllib.error
    from profile.llm.client import LLMClient

    client = LLMClient(api_key="test-key", api_url="http://fake", model="test-model")
    set_task_context(task_run_id=None, step="fail_step")

    error = urllib.error.HTTPError("http://fake", 500, "Server Error", {}, None)
    with patch("urllib.request.urlopen", side_effect=error):
        try:
            client.chat("test", system_prompt="sys")
        except RuntimeError:
            pass

    calls = query_llm_calls(success=0)
    assert calls["total"] == 1
    call = calls["items"][0]
    assert call["success"] == 0
    assert call["error_message"] is not None
    assert call["step"] == "fail_step"
    clear_task_context()
