"""数据库初始化与建表脚本。

只保留 raw_data 和 llm_intents 两张表。
画像和变化报告不进数据库，直接输出为 md + json 文件。
"""

import sqlite3

from profile.config import DB_PATH, ensure_dirs

_SCHEMA = """
-- 归一化原始数据
CREATE TABLE IF NOT EXISTS raw_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT,
    content TEXT NOT NULL,
    actions TEXT,
    outcome TEXT,
    learned TEXT,
    timestamp DATETIME NOT NULL,
    raw_json TEXT,
    ingested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, source_id)
);

-- LLM提取的用户意图
CREATE TABLE IF NOT EXISTS llm_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_data_id INTEGER NOT NULL,
    intent_category TEXT,
    intent_summary TEXT,
    keywords TEXT,
    sentiment TEXT,
    priority TEXT,
    project_name TEXT,
    llm_model TEXT,
    extracted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (raw_data_id) REFERENCES raw_data(id)
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    from profile.log import get_logger
    logger = get_logger("db.init_db")
    logger.info("DB 初始化完成", extra={"extra": {"db_path": str(DB_PATH)}})
