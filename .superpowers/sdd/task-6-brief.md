### Task 6: Dashboard API

**Files:**
- Create: `profile/portal/routes/dashboard.py`
- Test: `tests/test_portal_routes.py` (追加 dashboard 测试)

**Interfaces:**
- Consumes: `profile.portal.db_store.query_task_runs`, `profile.portal.task_engine.get_runner`
- Consumes: `profile.db.store.query_raw_data`, `profile.db.store.query_intents`
- Consumes: `profile.output.writer.load_latest_json`
- Produces: `GET /api/dashboard`

- [ ] **Step 1: 实现 dashboard.py**

创建 `profile/portal/routes/dashboard.py`：

```python
"""Dashboard API: 首页聚合数据。"""

from fastapi import APIRouter

from profile.portal.task_engine import get_runner
from profile.portal.db_store import query_task_runs, query_llm_calls
from profile.db.init_db import get_connection
from profile.config import OBSIDIAN_OUTPUT_DIR
from profile.output.writer import load_latest_json

router = APIRouter()


@router.get("/dashboard")
def get_dashboard():
    # 1. 当前运行任务
    runner = get_runner()
    current_task = None
    if runner.is_busy():
        tasks = query_task_runs(page=1, page_size=1, status="running")
        if tasks["items"]:
            t = tasks["items"][0]
            current_task = {
                "id": t["id"],
                "task_type": t["task_type"],
                "status": t["status"],
                "current_step": t.get("current_step"),
            }

    # 2. 最近5次历史任务
    recent = query_task_runs(page=1, page_size=5)
    recent_tasks = []
    for t in recent["items"]:
        recent_tasks.append({
            "id": t["id"],
            "task_type": t["task_type"],
            "status": t["status"],
            "started_at": t.get("started_at"),
            "finished_at": t.get("finished_at"),
            "result_json": t.get("result_json"),
        })

    # 3. 数据概览
    data_overview = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM raw_data GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        for r in rows:
            data_overview.append({"source": r["source"], "count": r["cnt"]})
    finally:
        conn.close()

    # 4. LLM 概览
    llm_calls = query_llm_calls(page=1, page_size=1)
    total_calls = llm_calls["total"]
    llm_overview = {"total_calls": total_calls, "total_tokens": 0, "success_rate": 1.0}
    if total_calls > 0:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT SUM(total_tokens) as tokens, "
                "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END)*1.0/COUNT(*) as rate "
                "FROM llm_calls"
            ).fetchone()
            llm_overview["total_tokens"] = row["tokens"] or 0
            llm_overview["success_rate"] = round(row["rate"], 4)
        finally:
            conn.close()

    # 5. 最新综合画像快照
    latest_profile = None
    global_json = load_latest_json("个人画像-综合")
    if global_json:
        latest_profile = {
            "date": global_json.get("date", ""),
            "summary": global_json.get("summary", ""),
            "direction_alignment": (global_json.get("direction_truth") or {}).get("alignment", ""),
        }

    # 6. 最新变化报告（从文件读取）
    latest_changes = None
    import json
    from datetime import datetime
    if OBSIDIAN_OUTPUT_DIR.exists():
        reports = sorted(OBSIDIAN_OUTPUT_DIR.glob("个人画像-变化报告-*.md"), reverse=True)
        if reports:
            date_str = reports[0].stem.replace("个人画像-变化报告-", "")
            content = reports[0].read_text(encoding="utf-8")
            # 简单统计变化类型
            new_count = content.count("[新增]")
            inc_count = content.count("[上升]")
            dec_count = content.count("[下降]")
            dis_count = content.count("[消失]")
            latest_changes = {
                "date": date_str,
                "total_changes": new_count + inc_count + dec_count + dis_count,
                "new": new_count,
                "increased": inc_count,
                "decreased": dec_count,
                "disappeared": dis_count,
            }

    return {
        "current_task": current_task,
        "recent_tasks": recent_tasks,
        "data_overview": data_overview,
        "llm_overview": llm_overview,
        "latest_profile": latest_profile,
        "latest_changes": latest_changes,
    }
```

- [ ] **Step 2: 运行测试验证通过**

Run: `cd D:\AAAmyprj\github\myrepos\checkSelf; .venv\Scripts\python -m pytest tests/test_portal_routes.py::test_dashboard_endpoint_exists -v`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add profile/portal/routes/dashboard.py
git commit -m "feat(portal): dashboard API with aggregated overview data"
```

---

### Task 7: Tasks API + SSE
