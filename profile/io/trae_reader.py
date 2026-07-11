import glob
import json
from datetime import datetime

from profile.config import TRAE_MEMORY_DIR
from profile.db.store import upsert_raw_data
from profile.io.base import BaseReader
from profile.log import get_logger, log_error
from profile.models import ChatRecord

logger = get_logger("io.trae_reader")


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
            logger.warning("trae 数据源不可用", extra={"extra": {"path": str(TRAE_MEMORY_DIR)}})
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
                            logger.warning("跳过无效 JSON 行", extra={
                                "extra": {"file": filepath}
                            })
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
                log_error(logger, f"无法读取文件 {filepath}", exc=e, context={
                    "file": filepath,
                })

        logger.info("trae 入库完成", extra={
            "extra": {"count": count, "source": "trae"}
        })
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
                            logger.warning("跳过无效 JSON 行", extra={
                                "extra": {"file": filepath}
                            })
            except OSError as e:
                log_error(logger, f"无法读取文件 {filepath}", exc=e, context={
                    "file": filepath,
                })
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
