from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gacore.langTrack.storage import Storage

CST = timezone(timedelta(hours=8))


def _new_storage(tmp_path):
    return Storage(tmp_path / "langTrack.db")


def test_insert_event_and_count(tmp_path):
    s = _new_storage(tmp_path)
    s.upsert_device("dev1", 1000)
    s.insert_event("dev1", 1000, "usage", {"pkg": "com.x", "foreground_ms": 5}, 2000)
    assert s.event_count() == 1
    s.close()


def test_batch_idempotent(tmp_path):
    s = _new_storage(tmp_path)
    assert s.register_batch("b1", "dev1", 1000) is True
    assert s.register_batch("b1", "dev1", 1000) is False  # 重复
    s.close()


def test_duplicate_batch_not_double_insert(tmp_path):
    s = _new_storage(tmp_path)
    s.upsert_device("dev1", 1000)
    assert s.register_batch("b1", "dev1", 2000) is True
    s.insert_event("dev1", 1000, "usage", {"pkg": "com.x"}, 2000)
    assert s.register_batch("b1", "dev1", 2000) is False
    assert s.event_count() == 1  # caller skips insert when register_batch reports duplicate
    s.close()


def test_ingest_batch_inserts_and_dedups(tmp_path):
    s = _new_storage(tmp_path)
    events = [
        (1000, "usage", {"pkg": "com.x", "foreground_ms": 5}),
        (2000, "session", {"kind": "screen_on"}),
    ]
    assert s.ingest_batch("b1", "dev1", 1000, events) is True
    assert s.event_count() == 2
    # 幂等命中：同 batch_id 重试不重复入库（客户端超时重试场景）
    assert s.ingest_batch("b1", "dev1", 1000, events) is False
    assert s.event_count() == 2
    s.close()


def test_ingest_batch_atomic_rollback(tmp_path):
    """事件插入失败时整个批次回滚：批次未登记、事件零条，重试视为首次可正常入库。"""
    s = _new_storage(tmp_path)
    with pytest.raises(TypeError):
        s.ingest_batch("b1", "dev1", 1000, [(1000, "usage", {"x": object()})])
    assert s.event_count() == 0
    # 批次未登记 → 同 batch_id 重试仍为首次，事件可完整入库，不丢也不重复
    assert s.ingest_batch("b1", "dev1", 1000, [(1000, "usage", {"pkg": "com.x"})]) is True
    assert s.event_count() == 1
    s.close()


def test_new_db_auto_fills_timestamps(tmp_path):
    """新库建表自带 DEFAULT，插入自动填东八区可读时间。"""
    s = _new_storage(tmp_path)
    s.upsert_device("dev1", 1000)
    s.ingest_batch("b1", "dev1", 1000, [(1000, "usage", {"pkg": "com.x"})])
    pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
    dev = s._conn.execute("SELECT created_at, updated_at FROM devices WHERE device_id='dev1'").fetchone()
    assert re.match(pattern, dev[0]) and dev[0] == dev[1]
    batch = s._conn.execute("SELECT created_at, updated_at FROM ingested_batches WHERE batch_id='b1'").fetchone()
    assert re.match(pattern, batch[0]) and batch[0] == batch[1]
    ev = s._conn.execute("SELECT created_at, updated_at FROM events").fetchone()
    assert re.match(pattern, ev[0]) and ev[0] == ev[1]
    s.close()


def test_upsert_refreshes_updated_at(tmp_path):
    s = _new_storage(tmp_path)
    s.upsert_device("dev1", 1000)
    first = s._conn.execute("SELECT updated_at FROM devices WHERE device_id='dev1'").fetchone()[0]
    s.upsert_device("dev1", 2000)
    second = s._conn.execute("SELECT updated_at FROM devices WHERE device_id='dev1'").fetchone()[0]
    assert s._conn.execute("SELECT last_seen FROM devices WHERE device_id='dev1'").fetchone()[0] == 2000
    assert second >= first  # 再次上报刷新 updated_at（秒级精度，同秒内可能相等）
    s.close()


def test_old_db_migrated_with_cst_timestamps(tmp_path):
    """旧库（无时间戳列）打开时自动补列，按东八区回填可读时间。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE devices (device_id TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER);
        CREATE TABLE ingested_batches (batch_id TEXT PRIMARY KEY, device_id TEXT, received_at INTEGER);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
          ts INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, received_at INTEGER NOT NULL);
    """)
    ms = 1700000000000  # 2023-11-14 22:13:20 UTC
    conn.execute("INSERT INTO devices(device_id, first_seen, last_seen) VALUES ('d1', ?, ?)", (ms, ms + 60000))
    conn.execute("INSERT INTO ingested_batches(batch_id, device_id, received_at) VALUES ('b1', 'd1', ?)", (ms,))
    conn.execute("INSERT INTO events(device_id, ts, type, payload, received_at) VALUES ('d1', ?, 'usage', '{}', ?)", (ms, ms))
    conn.commit()
    conn.close()

    s = Storage(db)  # 打开时触发迁移
    expected = datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S")
    dev = s._conn.execute("SELECT created_at, updated_at FROM devices WHERE device_id='d1'").fetchone()
    assert dev[0] == expected
    assert dev[1] == datetime.fromtimestamp((ms + 60000) / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S")
    batch = s._conn.execute("SELECT created_at FROM ingested_batches WHERE batch_id='b1'").fetchone()[0]
    assert batch == expected
    ev = s._conn.execute("SELECT created_at FROM events").fetchone()[0]
    assert ev == expected
    s.close()
