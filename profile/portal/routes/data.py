"""Data API: raw_data 和 llm_intents 浏览。"""

from fastapi import APIRouter, HTTPException

from profile.db.init_db import get_connection
from profile.db.store import query_raw_data, query_intents

router = APIRouter()


@router.get("/data/raw")
def list_raw_data(page: int = 1, page_size: int = 20,
                  source: str = None, start_date: str = None, end_date: str = None,
                  q: str = None):
    all_rows = query_raw_data(source=source, start_date=start_date, end_date=end_date, q=q)
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
                 source: str = None, category: str = None, q: str = None):
    all_rows = query_intents(source=source, q=q)
    # 按 category 筛选
    if category:
        all_rows = [r for r in all_rows if r.get("intent_category") == category]
    total = len(all_rows)
    start = (page - 1) * page_size
    end = start + page_size
    items = all_rows[start:end]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
