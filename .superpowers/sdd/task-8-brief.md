### Task 8: TraeReader

**Files:**
- Create: `profile/io/trae_reader.py`

**Interfaces:**
- Produces: `TraeReader` class implementing `BaseReader`

- [ ] **Step 1: Write trae_reader.py**

```python
import glob
import json
from datetime import datetime

from profile.config import TRAE_MEMORY_DIR
from profile.io.base import BaseReader
from profile.models import ChatRecord


class TraeReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "trae"

    def is_available(self) -> bool:
        if not TRAE_MEMORY_DIR.exists():
            return False
        files = glob.glob(str(TRAE_MEMORY_DIR / "*" / "*" / "session_memory_*.jsonl"), recursive=False)
        return len(files) > 0

    def read(self) -> list[ChatRecord]:
        records = []
        pattern = str(TRAE_MEMORY_DIR / "*" / "*" / "session_memory_*.jsonl")
        for filepath in glob.glob(pattern):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            record = self._parse_record(data)
                            if record:
                                records.append(record)
                        except json.JSONDecodeError:
                            import sys
                            print(f"[warn] trae: skip bad JSON line in {filepath}", file=sys.stderr)
            except OSError as e:
                import sys
                print(f"[warn] trae: cannot read {filepath}: {e}", file=sys.stderr)
        return records

    def _parse_record(self, data: dict) -> ChatRecord | None:
        time_str = data.get("message_summary_time", "")
        if not time_str:
            return None
        try:
            t = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
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
```

- [ ] **Step 2: Verify import + is_available**

```powershell
python -c "from profile.io.trae_reader import TraeReader; r = TraeReader(); print('available:', r.is_available())"
```

- [ ] **Step 3: Commit**

```bash
git add profile/io/trae_reader.py
git commit -m "feat: add TraeReader for session memory JSONL"
```
