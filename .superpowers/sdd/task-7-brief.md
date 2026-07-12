### Task 7: Tasks API + SSE

**Files:**
- Create: `profile/portal/routes/tasks.py`
- Test: `tests/test_portal_routes.py` (追加 tasks 测试)

**Interfaces:**
- Consumes: `profile.portal.task_engine.get_runner`, `profile.portal.db_store.query_task_runs`, `get_task_run`
- Produces: `GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks`, `DELETE /api/tasks/{id}`, `GET /api/tasks/{id}/logs`

- [ ] **Step 1: 实现 tasks.py**

创建 `profile/portal/routes/tasks.py`：

```python
"""Tasks API: 任务 CRUD + SSE 日志流。"""

import asyncio
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from profile.portal.task_engine import get_runner
from profile.portal.db_store import query_task_runs, get_task_run

router = APIRouter()


class TaskCreateRequest(BaseModel):
    task_type: str
    params: dict = {}


VALID_TASK_TYPES = {"refresh_all", "ingest", "ingest_single", "extract_intents", "generate_profiles"}


@router.get("/tasks")
def list_tasks(page: int = 1, page_size: int = 20,
               status: str = None, task_type: str = None):
    return query_task_runs(page=page, page_size=page_size, status=status, task_type=task_type)


@router.get("/tasks/{task_id}")
def get_task(task_id: int):
    row = get_task_run(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


@router.post("/tasks")
def create_task(req: TaskCreateRequest):
    if req.task_type not in VALID_TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"无效的任务类型: {req.task_type}")

    runner = get_runner()
    task_id = runner.start_task(req.task_type, req.params)
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有任务在运行中")
    return {"id": task_id, "status": "started"}


@router.delete("/tasks/{task_id}")
def cancel_task(task_id: int):
    runner = get_runner()
    success = runner.cancel(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="无法取消（任务可能已结束或不匹配）")
    return {"id": task_id, "status": "cancelling"}


@router.get("/tasks/{task_id}/logs")
async def stream_logs(task_id: int):
    """SSE 流：实时推送任务日志。"""
    runner = get_runner()

    async def event_stream():
        idx = 0
        while True:
            logs = runner.get_logs(task_id, after_idx=idx)
            for log in logs:
                idx += 1
                yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"

            # 检查任务是否已结束
            task = get_task_run(task_id)
            if task and task["status"] in ("done", "failed", "cancelled"):
                # 推送剩余日志
                remaining = runner.get_logs(task_id, after_idx=idx)
                for log in remaining:
                    idx += 1
                    yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'done', 'status': task['status']}, ensure_ascii=False)}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

- [ ] **Step 2: 追加 tasks API 测试到 test_portal_routes.py**

在 `tests/test_portal_routes.py` 追加：

```python
def test_list_tasks():
    client = TestClient(app)
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_create_invalid_task_type():
    client = TestClient(app)
    resp = client.post("/api/tasks", json={"task_type": "invalid", "params": {}})
    assert resp.status_code == 400


def test_get_nonexistent_task():
    client = TestClient(app)
    resp = client.get("/api/tasks/99999")
    assert resp.status_code == 404
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/tasks.py tests/test_portal_routes.py
git commit -m "feat(portal): tasks API with CRUD and SSE log streaming"
```

---

### Task 8: Data API
