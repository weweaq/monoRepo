import glob
import json
from datetime import datetime

from profile.config import TRAE_MEMORY_DIR
from profile.db.store import upsert_raw_data
from profile.io.base import BaseReader
from profile.models import ChatRecord


class TraeReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "trae"

    def is_available(self) -> bool:
        if not TRAE_MEMORY_DIR.exists():
            return False
        files = glob.glob(str(TRAE_MEMORY_DIR / "*" / "*" / "session_memory_*.jsonl"))
        return len(files) > 0

    def ingest(self) -> int:
        if not self.is_available():
            print("[skip] trae 数据源不可用")
            return 0

        count = 0
        for filepath in glob.glob(str(TRAE_MEMORY_DIR / "*" / "*" / "session_memory_*.jsonl")):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            print(f"[warn] trae: skip bad JSON line in {filepath}")
                            continue

                        ts = data.get("message_summary_time", "")
                        if not ts:
                            continue

                        upsert_raw_data(
                            source=self.source_name,
                            source_id=str(data.get("message_id", "")),
                            content=data.get("intent", ""),
                            actions=data.get("actions", []),
                            outcome=data.get("outcome", ""),
                            learned=data.get("learned", []),
                            timestamp=ts,
                            raw_json=data,
                        )
                        count += 1
            except OSError as e:
                print(f"[warn] trae: cannot read {filepath}: {e}")

        print(f"[ok] trae 入库 {count} 条")
        return count

    def read(self) -> list[ChatRecord]:
        records = []
        for filepath in glob.glob(str(TRAE_MEMORY_DIR / "*" / "*" / "session_memory_*.jsonl")):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            record = self._to_record(data)
                            if record:
                                records.append(record)
                        except json.JSONDecodeError:
                            print(f"[warn] trae: skip bad JSON line in {filepath}")
            except OSError as e:
                print(f"[warn] trae: cannot read {filepath}: {e}")
        return records

    def _to_record(self, data: dict) -> ChatRecord | None:
        ts = data.get("message_summary_time", "")
        if not ts:
            return None
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return ChatRecord(
            time=t,
            content=data.get("intent", ""),
            actions=data.get("actions", []),
            outcome=data.get("outcome", ""),
            learned=data.get("learned", []),
            source=self.source_name,
        )
