### Task 5: FastAPI App Skeleton

**Files:**
- Create: `profile/portal/routes/__init__.py` (空文件)
- Create: `profile/portal/app.py`
- Test: `tests/test_portal_routes.py` (skeleton test)

**Interfaces:**
- Produces: `profile.portal.app.app` (FastAPI 实例), `profile.portal.app.main()` (启动入口)
- Produces: 挂载静态文件服务，`GET /` 返回 index.html

- [ ] **Step 1: 创建 routes 包的 __init__.py**

创建 `profile/portal/routes/__init__.py`，内容为空。

- [ ] **Step 2: 编写 app skeleton 测试**

创建 `tests/test_portal_routes.py`：

```python
from fastapi.testclient import TestClient
from profile.portal.app import app


def test_root_returns_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_dashboard_endpoint_exists():
    client = TestClient(app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_task" in data
    assert "recent_tasks" in data
    assert "data_overview" in data
    assert "llm_overview" in data
```

- [ ] **Step 3: 创建 app.py**

创建 `profile/portal/app.py`：

```python
"""FastAPI 应用入口。

用法:
    python -m profile.portal.app
    python -m profile.portal.app --port 8080
"""

import argparse
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from profile.db.init_db import init_db
from profile.config import LOG_DIR, OBSIDIAN_OUTPUT_DIR, ensure_dirs
from profile.log import setup
from profile.portal.task_engine import get_runner, recover_stale_tasks
from profile.portal.routes import dashboard, tasks, data, llm, profiles

app = FastAPI(title="checkSelf Portal")

# 静态文件
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def on_startup():
    """启动时初始化。"""
    setup(log_dir=LOG_DIR)
    ensure_dirs()
    init_db()
    recover_stale_tasks()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# 注册路由
app.include_router(dashboard.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(data.router, prefix="/api")
app.include_router(llm.router, prefix="/api")
app.include_router(profiles.router, prefix="/api")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="checkSelf Portal")
    parser.add_argument("--port", type=int, default=8000, help="端口号")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if not args.no_browser:
        webbrowser.open(f"http://{args.host}:{args.port}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 创建最小的 index.html（占位，Task 11 完善）**

创建 `profile/portal/static/index.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>checkSelf Portal</title>
</head>
<body>
    <h1>checkSelf Portal</h1>
    <p>Loading...</p>
</body>
</html>
```

- [ ] **Step 5: 运行测试验证失败（路由还没创建）**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: FAIL (ImportError: routes 模块不存在)

- [ ] **Step 6: 提交（skeleton + 占位路由在后续 Task 实现）**

```bash
git add profile/portal/routes/__init__.py profile/portal/app.py profile/portal/static/index.html tests/test_portal_routes.py
git commit -m "feat(portal): FastAPI app skeleton with startup init and static serving"
```

---

### Task 6: Dashboard API
