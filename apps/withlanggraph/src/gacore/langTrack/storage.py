"""SQLite 存储层：设备、幂等批次、事件。用标准库 sqlite3，不引 ORM。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  first_seen INTEGER,
  last_seen  INTEGER,
  created_at TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE TABLE IF NOT EXISTS ingested_batches (
  batch_id    TEXT PRIMARY KEY,
  device_id   TEXT,
  received_at INTEGER,
  created_at TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  type        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device_id, ts);
"""


def _add_timestamp_columns(
    conn: sqlite3.Connection,
    table: str,
    created_col: str,
    updated_col: str,
) -> None:
    """给旧库的某张表补 created_at / updated_at 列并回填东八区可读时间。

    新库由 _SCHEMA 的 DEFAULT 自动填充；旧库的表不会被 CREATE IF NOT EXISTS 改动，
    只能 ALTER TABLE 补列。ADD COLUMN 不允许表达式默认值，故先加裸列再回填。
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "created_at" in cols:
        return
    with conn:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN created_at TEXT")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN updated_at TEXT")
        conn.execute(
            f"UPDATE {table} SET "
            f"created_at = datetime({created_col} / 1000, 'unixepoch', '+8 hours'), "
            f"updated_at = datetime({updated_col} / 1000, 'unixepoch', '+8 hours') "
            f"WHERE created_at IS NULL"
        )


class Storage:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """旧库升级：为不含时间戳列的表补 created_at / updated_at（东八区可读）。"""
        _add_timestamp_columns(self._conn, "devices", "first_seen", "last_seen")
        _add_timestamp_columns(self._conn, "ingested_batches", "received_at", "received_at")
        _add_timestamp_columns(self._conn, "events", "received_at", "received_at")

    def register_batch(self, batch_id: str, device_id: str, received_at: int) -> bool:
        """登记一个批次；返回 True=首次，False=已存在(幂等命中)。"""
        cur = self._conn.execute(
            "SELECT 1 FROM ingested_batches WHERE batch_id = ?", (batch_id,)
        )
        if cur.fetchone():
            return False
        self._conn.execute(
            "INSERT INTO ingested_batches(batch_id, device_id, received_at) VALUES (?,?,?)",
            (batch_id, device_id, received_at),
        )
        self._conn.commit()
        return True

    def ingest_batch(
        self,
        batch_id: str,
        device_id: str,
        received_at: int,
        events: list[tuple[int, str, dict]],
    ) -> bool:
        """原子处理一批上报：登记批次 + 插入全部事件，单事务提交，全有或全无。

        返回 True=首次入库；False=batch_id 已存在(幂等命中，事件未插入)。
        事务内任一步失败则整体回滚，杜绝「批次已登记但事件部分/全部未插入」的
        半完成窗口——否则客户端复用 batch_id 重试时会因幂等命中而永久丢失这批事件。
        用 INSERT OR IGNORE 消除「SELECT 检查 + INSERT」的并发竞态。
        """
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ingested_batches(batch_id, device_id, received_at) VALUES (?,?,?)",
                (batch_id, device_id, received_at),
            )
            if cur.rowcount == 0:
                return False
            for ts, type_, payload in events:
                self._conn.execute(
                    "INSERT INTO events(device_id, ts, type, payload, received_at) VALUES (?,?,?,?,?)",
                    (device_id, ts, type_, json.dumps(payload, ensure_ascii=False), received_at),
                )
        return True

    def upsert_device(self, device_id: str, ts: int) -> None:
        self._conn.execute(
            """
            INSERT INTO devices(device_id, first_seen, last_seen) VALUES (?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              last_seen=excluded.last_seen,
              updated_at=datetime('now', '+8 hours')
            """,
            (device_id, ts, ts),
        )
        self._conn.commit()

    def insert_event(self, device_id: str, ts: int, type: str, payload: dict, received_at: int) -> None:
        self._conn.execute(
            "INSERT INTO events(device_id, ts, type, payload, received_at) VALUES (?,?,?,?,?)",
            (device_id, ts, type, json.dumps(payload, ensure_ascii=False), received_at),
        )
        self._conn.commit()

    def event_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
