### Task 10: Profiles API

**Files:**
- Create: `profile/portal/routes/profiles.py`
- Test: `tests/test_portal_routes.py` (追加 profiles 测试)

**Interfaces:**
- Consumes: `profile.config.OBSIDIAN_OUTPUT_DIR`
- Produces: `GET /api/profiles`, `GET /api/profiles/{filename}`

- [ ] **Step 1: 实现 profiles.py**

创建 `profile/portal/routes/profiles.py`：

```python
"""Profiles API: 画像文件列表 + 内容读取。"""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from profile.config import OBSIDIAN_OUTPUT_DIR

router = APIRouter()

# 文件类型映射
FILE_TYPE_MAP = {
    "综合": "global",
    "trae": "trae",
    "marvis": "marvis",
    "content_consumption": "content_consumption",
    "变化报告": "change_report",
}


def _parse_filename(filename: str) -> dict:
    """从文件名解析类型和日期。"""
    name = Path(filename).stem
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    date_str = date_match.group(1) if date_match else ""

    file_type = "unknown"
    for key, val in FILE_TYPE_MAP.items():
        if key in name:
            file_type = val
            break

    return {"filename": filename, "type": file_type, "date": date_str}


@router.get("/profiles")
def list_profiles():
    if not OBSIDIAN_OUTPUT_DIR.exists():
        return {"items": [], "total": 0}

    files = sorted(OBSIDIAN_OUTPUT_DIR.iterdir(), reverse=True)
    items = []
    for f in files:
        if f.is_file() and f.suffix in (".md", ".json"):
            info = _parse_filename(f.name)
            info["size"] = f.stat().st_size
            info["ext"] = f.suffix
            items.append(info)

    return {"items": items, "total": len(items)}


@router.get("/profiles/{filename}")
def get_profile(filename: str):
    # 安全检查：防止路径穿越
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    filepath = OBSIDIAN_OUTPUT_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    content = filepath.read_text(encoding="utf-8")

    if filepath.suffix == ".json":
        try:
            data = json.loads(content)
            return {"filename": filename, "format": "json", "data": data}
        except json.JSONDecodeError:
            return {"filename": filename, "format": "text", "data": content}
    else:
        return {"filename": filename, "format": "markdown", "data": content}
```

- [ ] **Step 2: 追加 profiles API 测试**

在 `tests/test_portal_routes.py` 追加：

```python
def test_list_profiles():
    client = TestClient(app)
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_get_profile_not_found():
    client = TestClient(app)
    resp = client.get("/api/profiles/nonexistent-file.md")
    assert resp.status_code == 404


def test_get_profile_path_traversal_blocked():
    client = TestClient(app)
    resp = client.get("/api/profiles/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add profile/portal/routes/profiles.py tests/test_portal_routes.py
git commit -m "feat(portal): profiles API for file listing and structured rendering"
```

---

### Task 11: 前端 index.html
