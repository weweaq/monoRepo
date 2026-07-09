### Task 10: Markdown 输出层

**Files:**
- Create: `profile/output/writer.py`

**Interfaces:**
- Produces: `write_report(filename: str, content: str) -> Path`, `format_report(sections: list, title: str) -> str`

- [ ] **Step 1: Write writer.py**

```python
from datetime import datetime
from pathlib import Path

from profile.config import OBSIDIAN_OUTPUT_DIR


def write_report(filename: str, content: str) -> Path:
    OBSIDIAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OBSIDIAN_OUTPUT_DIR / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


def format_report(sections: list[tuple[str, str]], title: str = "个人画像报告") -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# {title}", f"", f"> 生成日期：{today}", f"> 自动生成，请勿手动编辑", ""]
    for heading, body in sections:
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 2: Verify import**

```powershell
python -c "from profile.output.writer import write_report, format_report; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add profile/output/writer.py
git commit -m "feat: add markdown writer for Obsidian output"
```
