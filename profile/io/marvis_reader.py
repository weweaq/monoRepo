import hashlib
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from profile.config import MARVIS_DATA_DIR
from profile.db.store import upsert_raw_data
from profile.io.base import BaseReader
from profile.models import ChatRecord


class MarvisReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "marvis"

    def is_available(self) -> bool:
        return (MARVIS_DATA_DIR / "data.db").exists()

    def ingest(self) -> int:
        if not self.is_available():
            print("[skip] marvis 数据源不可用")
            return 0

        rows = self._read_rows()
        count = 0
        for row in rows:
            content = row.get("content", "")
            ts = row.get("created_at")
            if not content or not ts:
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

        print(f"[ok] marvis 入库 {count} 条")
        return count

    def read(self) -> list[ChatRecord]:
        records = []
        for row in self._read_rows():
            record = self._to_record(row)
            if record:
                records.append(record)
        return records

    def _read_rows(self) -> list[dict]:
        src = MARVIS_DATA_DIR / "data.db"
        if not src.exists():
            return []

        tmp = Path(tempfile.gettempdir()) / "marvis_data_copy.db"
        try:
            shutil.copy2(str(src), str(tmp))
        except OSError as e:
            print(f"[error] marvis: cannot copy data.db (close Marvis first): {e}")
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
        except sqlite3.Error as e:
            print(f"[error] marvis: SQLite error: {e}")
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
