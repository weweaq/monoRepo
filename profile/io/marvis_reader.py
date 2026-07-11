import hashlib
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from profile.config import MARVIS_DATA_DIR
from profile.db.store import upsert_raw_data
from profile.io.base import BaseReader
from profile.log import get_logger, log_error
from profile.models import ChatRecord

logger = get_logger("io.marvis_reader")


class MarvisReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "marvis"

    def is_available(self) -> bool:
        return (MARVIS_DATA_DIR / "data.db").exists()

    def ingest(self) -> int:
        logger.info("开始 marvis 数据入库", extra={
            "extra": {"source_dir": str(MARVIS_DATA_DIR)}
        })

        if not self.is_available():
            logger.warning("marvis 数据源不可用", extra={
                "extra": {"path": str(MARVIS_DATA_DIR)}
            })
            return 0

        rows = self._read_rows()
        logger.info("从 marvis DB 读取到记录", extra={
            "extra": {"row_count": len(rows)}
        })

        count = 0
        skipped_empty = 0
        for row in rows:
            content = row.get("content", "")
            ts = row.get("created_at")
            if not content or not ts:
                skipped_empty += 1
                continue

            source_id = row.get("id") or row.get("rowid")
            if source_id is None:
                digest = hashlib.md5(f"{ts}:{content}".encode("utf-8")).hexdigest()[:12]
                source_id = f"{ts}_{digest}"

            upsert_raw_data(
                source=self.source_name,
                source_id=str(source_id),
                content=str(content),
                actions=None,
                outcome="",
                learned=None,
                timestamp=str(ts),
                raw_json=row,
            )
            count += 1

        logger.info("marvis 入库完成", extra={
            "extra": {
                "source": "marvis",
                "total_ingested": count,
                "skipped_empty": skipped_empty,
                "db_rows_read": len(rows),
            }
        })
        return count

    def read(self) -> list[ChatRecord]:
        records = []
        for row in self._read_rows():
            record = self._to_record(row)
            if record:
                records.append(record)
        logger.info("marvis 读取完成", extra={
            "extra": {"records_count": len(records)}
        })
        return records

    def _read_rows(self) -> list[dict]:
        src = MARVIS_DATA_DIR / "data.db"
        if not src.exists():
            return []

        src_size_kb = round(src.stat().st_size / 1024, 1)
        logger.info("复制 marvis DB", extra={
            "extra": {"src": str(src), "src_size_kb": src_size_kb}
        })

        tmp = Path(tempfile.gettempdir()) / "marvis_data_copy.db"
        try:
            shutil.copy2(str(src), str(tmp))
        except OSError as e:
            log_error(logger, "无法复制 data.db（请先关闭 Marvis）", exc=e, context={
                "src": str(src),
                "tmp": str(tmp),
            })
            return []

        rows = []
        try:
            conn = sqlite3.connect(str(tmp))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM messages WHERE role='user' ORDER BY created_at")
            column_names = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(column_names, row)))
            conn.close()
            logger.info("marvis DB 查询完成", extra={
                "extra": {
                    "columns": column_names,
                    "user_message_count": len(rows),
                }
            })
        except sqlite3.Error as e:
            log_error(logger, "marvis SQLite 读取失败", exc=e, context={
                "db_path": str(tmp),
            })
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        return rows

    def _to_record(self, row: dict) -> ChatRecord | None:
        ts = row.get("created_at")
        content = row.get("content", "")
        if not content or not ts:
            return None
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, OSError):
            return None
        return ChatRecord(
            time=t,
            content=str(content),
            source=self.source_name,
        )
