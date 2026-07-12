### Task 3: LLM Client 埋点

**Files:**
- Modify: `profile/llm/client.py`
- Create: `profile/portal/task_engine.py` (仅 contextvars 部分，TaskRunner 在 Task 4 补充)
- Test: `tests/test_llm_tracking.py`

**Interfaces:**
- Consumes: `profile.portal.db_store.insert_llm_call`
- Produces: `profile.portal.task_engine.set_task_context(task_run_id, step)` 和 `clear_task_context()`
- Produces: `LLMClient.chat()` 自动记录到 llm_calls 表

- [ ] **Step 1: 创建 task_engine.py 的 contextvars 部分**

创建 `profile/portal/task_engine.py`：

```python
"""Portal 任务引擎：进程内线程池执行 + 日志捕获 + contextvars。"""

import contextvars
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from profile.portal.db_store import (
    insert_task_run, update_task_run, get_task_run,
    query_task_runs, recover_stale_tasks,
)

# === ContextVars: 在任务执行期间传递 task_run_id 和 step ===

_current_task_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_task_run_id", default=None
)
_current_step: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_step", default=None
)


def set_task_context(task_run_id: int, step: str | None = None) -> None:
    """设置当前任务上下文，供 LLMClient 埋点读取。"""
    _current_task_run_id.set(task_run_id)
    _current_step.set(step)


def clear_task_context() -> None:
    """清除任务上下文。"""
    _current_task_run_id.set(None)
    _current_step.set(None)


def get_current_task_run_id() -> int | None:
    return _current_task_run_id.get()


def get_current_step() -> str | None:
    return _current_step.get()
```

- [ ] **Step 2: 编写埋点测试**

创建 `tests/test_llm_tracking.py`：

```python
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
```

- [ ] **Step 3: 运行测试验证失败**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_llm_tracking.py -v`
Expected: FAIL (llm_calls 表中没有记录)

- [ ] **Step 4: 在 client.py 中加埋点**

修改 `profile/llm/client.py`，在文件顶部 import 区域追加：

```python
from profile.portal.task_engine import get_current_task_run_id, get_current_step
```

在 `LLMClient` 类中，`chat()` 方法的 `return content.strip()` 之前（即 `logger.debug("LLM 响应全文"...)` 之后），追加成功埋点：

```python
                    # 记录到 llm_calls 表
                    _record_llm_call(
                        model=self.model,
                        system_prompt=system_prompt,
                        user_prompt=prompt,
                        response=content,
                        usage=usage,
                        elapsed_ms=elapsed_ms,
                        success=True,
                    )
```

在 `chat()` 方法的两个 except 块中，在 `raise` 之前追加失败埋点：

HTTPError except 块中，在 `raise RuntimeError(...)` 之前追加：

```python
            _record_llm_call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                response=None,
                usage={},
                elapsed_ms=elapsed_ms,
                success=False,
                error_message=f"HTTP {e.code}: {error_body[:200]}",
            )
```

URLError except 块中，在 `raise RuntimeError(...)` 之前追加：

```python
            _record_llm_call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                response=None,
                usage={},
                elapsed_ms=elapsed_ms,
                success=False,
                error_message=f"网络错误: {e}",
            )
```

在 `LLMClient` 类外部（`class JsonParseError` 之前），追加 `_record_llm_call` 函数：

```python
def _record_llm_call(model, system_prompt, user_prompt, response, usage,
                     elapsed_ms, success, error_message=None):
    """记录 LLM 调用到 llm_calls 表。"""
    try:
        from profile.portal.db_store import insert_llm_call
        insert_llm_call(
            task_run_id=get_current_task_run_id(),
            step=get_current_step(),
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=response,
            prompt_tokens=usage.get("prompt_tokens") if usage else None,
            completion_tokens=usage.get("completion_tokens") if usage else None,
            total_tokens=usage.get("total_tokens") if usage else None,
            elapsed_ms=elapsed_ms,
            success=1 if success else 0,
            error_message=error_message,
        )
    except Exception:
        # 埋点失败不影响主流程
        pass
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_llm_tracking.py -v`
Expected: 2 个测试全部 PASS

- [ ] **Step 6: 提交**

```bash
git add profile/portal/task_engine.py profile/llm/client.py tests/test_llm_tracking.py
git commit -m "feat(portal): add LLM call tracking via contextvars + client.py instrumentation"
```

---

### Task 4: TaskRunner 完整实现
