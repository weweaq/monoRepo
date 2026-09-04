### Task 9: LLM API

**Files:**
- Create: `profile/portal/routes/llm.py`
- Test: `tests/test_portal_routes.py` (追加 llm 测试)

**Interfaces:**
- Consumes: `profile.portal.db_store.query_llm_calls`, `get_llm_call`
- Produces: `GET /api/llm/calls`, `GET /api/llm/calls/{id}`

- [ ] **Step 1: 实现 llm.py**

创建 `profile/portal/routes/llm.py`：

```python
"""LLM API: llm_calls 查询。"""

from fastapi import APIRouter, HTTPException

from profile.portal.db_store import query_llm_calls, get_llm_call

router = APIRouter()


@router.get("/llm/calls")
def list_llm_calls(page: int = 1, page_size: int = 20,
                   step: str = None, model: str = None, success: int = None):
    return query_llm_calls(page=page, page_size=page_size, step=step, model=model, success=success)


@router.get("/llm/calls/{call_id}")
def get_llm_call_detail(call_id: int):
    row = get_llm_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="LLM 调用记录不存在")
    return row
```

- [ ] **Step 2: 追加 llm API 测试**

在 `tests/test_portal_routes.py` 追加：

```python
def test_list_llm_calls():
    client = TestClient(app)
    resp = client.get("/api/llm/calls")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_get_llm_call_not_found():
    client = TestClient(app)
    resp = client.get("/api/llm/calls/99999")
    assert resp.status_code == 404
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/llm.py tests/test_portal_routes.py
git commit -m "feat(portal): LLM API for llm_calls query"
```

---

### Task 10: Profiles API
