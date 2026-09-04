### Task 9: LLM API - 实施报告

**状态:** SUCCESS（已完成）

**分支:** feat/portal

---

## 1. 交付内容

### 文件变更

| 文件 | 类型 | 说明 |
|------|------|------|
| `profile/portal/routes/llm.py` | 实现 | 将占位 stub 实现为完整 LLM API 路由 |
| `tests/test_portal_routes.py` | 测试 | 追加 2 个 llm API 测试用例 |

### 实现的接口

- `GET /api/llm/calls` — 分页查询 llm_calls 记录，支持 `page`、`page_size`、`step`、`model`、`success` 过滤参数；委托 `profile.portal.db_store.query_llm_calls`。
- `GET /api/llm/calls/{call_id}` — 按 id 查询单条 LLM 调用详情；委托 `profile.portal.db_store.get_llm_call`，不存在时返回 404。

路由前缀 `/api` 在 `profile/portal/app.py` 中通过 `app.include_router(llm.router, prefix="/api")` 注册（Tasks 1-8 期间已就绪），故本任务仅填充路由实现即可。

### 实现代码（profile/portal/routes/llm.py）

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

实现完全遵循 brief 中给出的代码，风格与既有 `routes/tasks.py`、`routes/data.py` 保持一致（query 函数返回 `{items, total, page, page_size}` 字典；get 函数在记录不存在时抛 404）。

---

## 2. Commits

| commit | subject |
|--------|---------|
| `09d9b2080f71f9b1360946a999a72c848a6d8448` | `feat(portal): LLM API for llm_calls query` |

提交统计：`2 files changed, 33 insertions(+), 2 deletions(-)`，仅包含本任务相关的 `profile/portal/routes/llm.py` 与 `tests/test_portal_routes.py`，未夹带其他未提交改动。

---

## 3. 测试结果

运行命令：
```
.venv\Scripts\python.exe -m pytest tests/test_portal_routes.py -v
```

结果：**14 passed, 2 warnings in 1.30s**

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
test_list_llm_calls                          PASSED   <- 新增
test_get_llm_call_not_found                  PASSED   <- 新增
```

新增的 2 个用例均通过：
- `test_list_llm_calls`：`GET /api/llm/calls` 返回 200，响应体含 `items` 与 `total`。
- `test_get_llm_call_not_found`：`GET /api/llm/calls/99999` 返回 404。

### 警告说明（非本任务引入）
2 条 warnings 均为 `app.py` 中 `@app.on_event("startup")` 的 FastAPI DeprecationWarning，属既有代码（Tasks 1-8 期间已存在），与 Task 9 改动无关。

---

## 4. 关切 / 风险（Concerns）

1. **查询参数无校验边界**：`page`、`page_size` 未做下界/上界校验（如 `page=0` 或 `page_size=100000`）。当 `page < 1` 时 `OFFSET = (page-1)*page_size` 会得到负数，SQLite 会将负 OFFSET 当作 0 处理而不会报错，但行为不直观。此问题同样存在于既有的 `tasks`、`data` 路由，属于全局风格一致性问题，本次按 brief 原样实现，未额外加固。
2. **`success` 形参无类型约束**：brief 中签名 `success: int = None`，实际传 `?success=0/1` 工作正常；若传入非整数字符串会触发 FastAPI 的 422 校验错误（可接受行为）。
3. **路由前缀依赖既有注册**：本任务未改动 `app.py`，依赖前序任务已完成的 `app.include_router(llm.router, prefix="/api")` 注册；该注册确实已存在，测试通过即可佐证。
4. **工作区其他未提交改动**：仓库中存在大量与 Task 9 无关的未暂存修改（其他 task brief/report、多个新模块如 `profile/db/store.py`、`profile/llm/prompts.py` 等）。本次提交严格按 brief 仅 add 了 Task 9 的 2 个文件，未污染提交范围。这些遗留改动需由后续任务或统一清理处理。

---

## 5. 报告路径

本报告：`D:\AAAmyprj\github\myrepos\checkSelf\.superpowers\sdd\task-9-report.md`
