"""CRUD 操作封装。

只保留 raw_data 和 llm_intents 的操作。
画像和变化报告不进数据库，直接输出为文件。
"""

import json
from datetime import datetime

from profile.db.init_db import get_connection


def _to_json_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _from_json_str(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


# === raw_data ===

def upsert_raw_data(source, source_id, content, actions=None, outcome="", learned=None, timestamp=None, raw_json=None) -> int:
    if timestamp is None:
        timestamp = datetime.now().isoformat(timespec="seconds")
    elif isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat(timespec="seconds")

    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT OR REPLACE INTO raw_data
               (source, source_id, content, actions, outcome, learned, timestamp, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, source_id, content, _to_json_str(actions), outcome,
             _to_json_str(learned), str(timestamp), _to_json_str(raw_json)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def query_raw_data(source=None, start_date=None, end_date=None, q=None) -> list[dict]:
    sql = "SELECT * FROM raw_data WHERE 1=1"
    params = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    if start_date:
        sql += " AND timestamp >= ?"
        params.append(str(start_date))
    if end_date:
        sql += " AND timestamp <= ?"
        params.append(str(end_date))
    if q:
        sql += " AND content LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY timestamp DESC"

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        d["actions"] = _from_json_str(d.get("actions"))
        d["learned"] = _from_json_str(d.get("learned"))
        d["raw_json"] = _from_json_str(d.get("raw_json"))
        result.append(d)
    return result


def get_unprocessed_raw_data(source=None) -> list[dict]:
    sql = """SELECT r.* FROM raw_data r
             LEFT JOIN llm_intents i ON r.id = i.raw_data_id
             WHERE i.id IS NULL"""
    params = []
    if source:
        sql += " AND r.source = ?"
        params.append(source)
    sql += " ORDER BY r.timestamp ASC"

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        d["actions"] = _from_json_str(d.get("actions"))
        d["learned"] = _from_json_str(d.get("learned"))
        d["raw_json"] = _from_json_str(d.get("raw_json"))
        result.append(d)
    return result


# === llm_intents ===

def insert_intent(raw_data_id, intent_category, intent_summary, keywords, sentiment, priority, project_name, llm_model) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO llm_intents
               (raw_data_id, intent_category, intent_summary, keywords,
                sentiment, priority, project_name, llm_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_data_id, intent_category, intent_summary, _to_json_str(keywords),
             sentiment, priority, project_name, llm_model),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def query_intents(source=None, start_date=None, end_date=None, q=None) -> list[dict]:
    sql = """SELECT i.*, r.source, r.source_id, r.content AS raw_content, r.timestamp AS raw_timestamp
             FROM llm_intents i
             JOIN raw_data r ON i.raw_data_id = r.id
             WHERE 1=1"""
    params = []
    if source:
        sql += " AND r.source = ?"
        params.append(source)
    if start_date:
        sql += " AND r.timestamp >= ?"
        params.append(str(start_date))
    if end_date:
        sql += " AND r.timestamp <= ?"
        params.append(str(end_date))
    if q:
        sql += " AND (i.intent_summary LIKE ? OR i.intent_category LIKE ? OR r.content LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    sql += " ORDER BY i.extracted_at DESC"

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        d["keywords"] = _from_json_str(d.get("keywords"))
        result.append(d)
    return result
