import glob
import json
from datetime import datetime
from pathlib import Path

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
        logger.info("开始 trae 数据入库", extra={
            "extra": {"source_dir": str(TRAE_MEMORY_DIR)}
        })

        if not self.is_available():
            logger.warning("trae 数据源不可用", extra={"extra": {"path": str(TRAE_MEMORY_DIR)}})
            return 0

        filepaths = glob.glob(str(TRAE_MEMORY_DIR / "*" / "*" / "session_memory_*.jsonl"))
        logger.info("发现文件", extra={
            "extra": {"file_count": len(filepaths), "files": [str(Path(f).name) for f in filepaths[:10]]}
        })

        total_count = 0
        total_skipped = 0
        for filepath in filepaths:
            file_count = 0
            file_skipped = 0
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            file_skipped += 1
                            logger.warning("跳过无效 JSON 行", extra={
                                "extra": {"file": filepath, "line_preview": line[:100]}
                            })
                            continue

                        ts = data.get("message_summary_time", "")
                        if not ts:
                            file_skipped += 1
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
                        file_count += 1
            except OSError as e:
                log_error(logger, f"无法读取文件 {filepath}", exc=e, context={
                    "file": filepath,
                })

            total_count += file_count
            total_skipped += file_skipped
            if file_count > 0 or file_skipped > 0:
                logger.info("文件处理完成", extra={
                    "extra": {
                        "file": filepath,
                        "ingested": file_count,
                        "skipped": file_skipped,
                    }
                })

        logger.info("trae 入库完成", extra={
            "extra": {
                "source": "trae",
                "total_ingested": total_count,
                "total_skipped": total_skipped,
                "file_count": len(filepaths),
            }
        })
        return total_count

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
        logger.info("trae 读取完成", extra={
            "extra": {"records_count": len(records)}
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
