### Task 9: MarvisReader

**Files:**
- Create: `profile/io/marvis_reader.py`

**Interfaces:**
- Produces: `MarvisReader` class implementing `BaseReader`

- [ ] **Step 1: Write marvis_reader.py**

```python
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from profile.config import MARVIS_DATA_DIR
from profile.io.base import BaseReader
from profile.models import ChatRecord


class MarvisReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "marvis"

    def is_available(self) -> bool:
        db_path = MARVIS_DATA_DIR / "data.db"
        return db_path.exists()

    def read(self) -> list[ChatRecord]:
        src = MARVIS_DATA_DIR / "data.db"
        if not src.exists():
            return []

        tmp_dir = Path(tempfile.gettempdir())
        tmp = tmp_dir / "marvis_data_copy.db"
        try:
            shutil.copy2(str(src), str(tmp))
        except OSError as e:
            import sys
            print(f"[error] marvis: cannot copy data.db (close Marvis first): {e}", file=sys.stderr)
            return []

        records = []
        try:
            conn = sqlite3.connect(str(tmp))
            cur = conn.cursor()
            cur.execute("SELECT * FROM messages ORDER BY timestamp")
            rows = cur.fetchall()
            column_names = [desc[0] for desc in cur.description]
            for row in rows:
                record = self._parse_row(dict(zip(column_names, row)))
                if record:
                    records.append(record)
            conn.close()
        except sqlite3.Error as e:
            import sys
            print(f"[error] marvis: SQLite error: {e}", file=sys.stderr)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        return records

    def _parse_row(self, row: dict) -> ChatRecord | None:
        ts = row.get("timestamp")
        content = row.get("content", "") or row.get("text", "") or row.get("message", "")
        if not content:
            return None

        if isinstance(ts, (int, float)):
            t = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
        elif isinstance(ts, str):
            try:
                t = datetime.fromtimestamp(int(ts) / 1000 if int(ts) > 1e12 else int(ts))
            except (ValueError, OSError):
                return None
        else:
            return None

        return ChatRecord(
            time=t,
            content=str(content),
            source=self.source_name,
        )
```

- [ ] **Step 2: Verify import + is_available**

```powershell
python -c "from profile.io.marvis_reader import MarvisReader; r = MarvisReader(); print('available:', r.is_available())"
```

- [ ] **Step 3: Commit**

```bash
git add profile/io/marvis_reader.py
git commit -m "feat: add MarvisReader for SQLite chat data"
```
