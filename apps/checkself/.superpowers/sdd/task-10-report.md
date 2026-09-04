### Task 10: Profiles API - 实施报告

**状态:** SUCCESS（已完成）

**分支:** feat/portal

---

## 1. 交付内容

### 文件变更

| 文件 | 类型 | 说明 |
|------|------|------|
| `profile/portal/routes/profiles.py` | 实现 | 将占位 stub 实现为完整 Profiles API 路由 |
| `tests/test_portal_routes.py` | 测试 | 追加 3 个 profiles API 测试用例 |

### 实现的接口

- `GET /api/profiles` — 列出 `OBSIDIAN_OUTPUT_DIR` 下的画像文件（`.md`/`.json`），按文件名倒序排列；每项含 `filename`、`type`、`date`、`size`、`ext`；目录不存在时返回 `{items: [], total: 0}`。
- `GET /api/profiles/{filename}` — 读取单个画像文件内容。`.json` 文件解析成功返回 `format: json`，解析失败降级为 `format: text`；`.md` 文件返回 `format: markdown`；文件不存在返回 404；含路径穿越字符（`/`、`\`、`..`）返回 400。

路由前缀 `/api` 在 `profile/portal/app.py` 中通过 `app.include_router(profiles.router, prefix="/api")` 注册（Tasks 1-9 期间已就绪），故本任务仅填充路由实现即可。

### 实现代码（profile/portal/routes/profiles.py）

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

实现完全遵循 brief 中给出的代码，风格与既有 `routes/tasks.py`、`routes/data.py`、`routes/llm.py` 保持一致。`_parse_filename` 通过文件名子串匹配 `FILE_TYPE_MAP` 推断画像类型，并以正则提取 `YYYY-MM-DD` 日期。

---

## 2. Commits

| commit | subject |
|--------|---------|
| `bded4179b713192d430bea7d73b5f37eb2609105` | `feat(portal): profiles API for file listing and structured rendering` |

提交统计：`2 files changed, 92 insertions(+), 2 deletions(-)`，仅包含本任务相关的 `profile/portal/routes/profiles.py` 与 `tests/test_portal_routes.py`，未夹带其他未提交改动。

---

## 3. 测试结果

运行命令：
```
.venv\Scripts\python.exe -m pytest tests/test_portal_routes.py -v
```

结果：**17 passed, 2 warnings in 1.38s**

逐项明细（全部 PASSED）：

```
test_root_returns_html                       PASSED
test_dashboard_endpoint_exists               PASSED
test_dashboard_returns_all_six_keys          PASSED
test_dashboard_data_overview_is_list         PASSED
test_dashboard_llm_overview_has_stats        PASSED
test_list_tasks                              PASSED
test_create_invalid_task_type                PASSED
test_get_nonexistent_task                    PASSED
test_list_raw_data                           PASSED
test_list_raw_data_with_source_filter        PASSED
test_get_raw_data_not_found                  PASSED
test_list_intents                            PASSED
test_list_llm_calls                          PASSED
test_get_llm_call_not_found                  PASSED
test_list_profiles                           PASSED   <- 新增
test_get_profile_not_found                   PASSED   <- 新增
test_get_profile_path_traversal_blocked      PASSED   <- 新增
```

新增的 3 个用例均通过：
- `test_list_profiles`：`GET /api/profiles` 返回 200，响应体含 `items` 与 `total`。
- `test_get_profile_not_found`：`GET /api/profiles/nonexistent-file.md` 返回 404。
- `test_get_profile_path_traversal_blocked`：`GET /api/profiles/..%2F..%2Fetc%2Fpasswd` 返回 400 或 404（实际命中 400 路径穿越拦截）。

### 警告说明（非本任务引入）
2 条 warnings 均为 `app.py` 中 `@app.on_event("startup")` 的 FastAPI DeprecationWarning，属既有代码（Tasks 1-9 期间已存在），与 Task 10 改动无关。

---

## 4. 关切 / 风险（Concerns）

1. **路径穿越防护为字符串级黑名单**：当前实现通过检查 filename 是否含 `/`、`\`、`..` 来拦截穿越。这是一种黑名单式校验，对 brief 中给出的 `..%2F..%2Fetc%2Fpasswd`（解码后为 `../../etc/passwd`）有效。更稳健的做法是用 `filepath.resolve()` 后校验是否仍位于 `OBSIDIAN_OUTPUT_DIR.resolve()` 之内（白名单/前缀校验）。本次按 brief 原样实现，未额外加固；若后续放宽 filename 字符集（如允许子目录），建议升级为 resolve + 前缀校验。
2. **`list_profiles` 无分页**：与 `tasks`/`data`/`llm` 路由不同，`/profiles` 直接返回全量列表，无 `page`/`page_size`。当画像产出目录文件数量增长时可能产生较大响应体。本次按 brief 原样实现，未引入分页。
3. **`_parse_filename` 类型匹配依赖中文/英文子串**：`FILE_TYPE_MAP` 的 key 含中文（`综合`、`变化报告`），依赖文件名命名约定；若实际产出的文件名不含这些关键词，`type` 将回落为 `unknown`。这是约定式推断，符合 brief 设计。
4. **依赖真实文件系统**：`/profiles` 直接读取 `OBSIDIAN_OUTPUT_DIR`（`d:/AAAmyPrj/gitee/obsidian/我的文档/AI使用/画像产出`），测试在真实环境运行时该目录存在（由 `ensure_dirs()` 创建），故 `test_list_profiles` 返回 200 而非空目录兜底。测试未对目录内容做断言，仅校验结构键，稳健性可接受。
5. **工作区其他未提交改动**：仓库中存在大量与 Task 10 无关的未暂存修改（其他 task brief/report、多个新模块等）。本次提交严格按 brief 仅 add 了 Task 10 的 2 个文件，未污染提交范围。这些遗留改动需由后续任务或统一清理处理。

---

## 5. 报告路径

本报告：`D:\AAAmyprj\github\myrepos\checkSelf\.superpowers\sdd\task-10-report.md`
