### Task 8: Data API

**Files:**
- Create: `profile/portal/routes/data.py`
- Test: `tests/test_portal_routes.py` (追加 data 测试)

**Interfaces:**
- Consumes: `profile.db.store.query_raw_data`, `profile.db.store.query_intents`, `profile.db.init_db.get_connection`
- Produces: `GET /api/data/raw`, `GET /api/data/raw/{id}`, `GET /api/data/intents`

- [ ] **Step 1: 实现 data.py**

创建 `profile/portal/routes/data.py`：

```python
"""Data API: raw_data 和 llm_intents 浏览。"""

from fastapi import APIRouter, HTTPException

from profile.db.init_db import get_connection
from profile.db.store import query_raw_data, query_intents

router = APIRouter()


@router.get("/data/raw")
def list_raw_data(page: int = 1, page_size: int = 20,
                  source: str = None, start_date: str = None, end_date: str = None):
    all_rows = query_raw_data(source=source, start_date=start_date, end_date=end_date)
    total = len(all_rows)
    start = (page - 1) * page_size
    end = start + page_size
    items = all_rows[start:end]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/data/raw/{raw_id}")
def get_raw_data(raw_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM raw_data WHERE id = ?", (raw_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    return dict(row)


@router.get("/data/intents")
def list_intents(page: int = 1, page_size: int = 20,
                 source: str = None, category: str = None):
    all_rows = query_intents(source=source)
    # 按 category 筛选
    if category:
        all_rows = [r for r in all_rows if r.get("intent_category") == category]
    total = len(all_rows)
    start = (page - 1) * page_size
    end = start + page_size
    items = all_rows[start:end]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
```

- [ ] **Step 2: 追加 data API 测试**

在 `tests/test_portal_routes.py` 追加：

```python
def test_list_raw_data():
    client = TestClient(app)
    resp = client.get("/api/data/raw")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_list_raw_data_with_source_filter():
    client = TestClient(app)
    resp = client.get("/api/data/raw?source=trae")
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["source"] == "trae"


def test_get_raw_data_not_found():
    client = TestClient(app)
    resp = client.get("/api/data/raw/99999")
    assert resp.status_code == 404


def test_list_intents():
    client = TestClient(app)
    resp = client.get("/api/data/intents")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/data.py tests/test_portal_routes.py
git commit -m "feat(portal): data API for raw_data and llm_intents browsing"
```

---

### Task 9: LLM API
