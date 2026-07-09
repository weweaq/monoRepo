### Task 2: config.py + models.py

**Files:**
- Create: `profile/config.py`
- Create: `profile/models.py`

**Produces:** 路径配置常量和 ChatRecord dataclass，供后续所有模块使用。

- [ ] **Step 1: 写 config.py**

```python
from pathlib import Path

TRAE_MEMORY_DIR = Path("C:/Users/17734/.trae-cn/memory/projects")
MARVIS_DATA_DIR = Path("C:/Users/17734/AppData/Roaming/Tencent/Marvis/User/2E51332FD5D7CCFEA611C89585433078/database")
OBSIDIAN_OUTPUT_DIR = Path("d:/AAAmyPrj/gitee/obsidian/我的文档/AI使用/画像产出")

CLAIMED_DIRECTION = {"主": "Agent", "次": "Memory"}
```

- [ ] **Step 2: 写 models.py**

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChatRecord:
    time: datetime
    content: str
    actions: list[str] = field(default_factory=list)
    outcome: str = ""
    learned: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class ContentRecord:
    time: datetime
    title: str
    category: str = ""
    duration: int = 0
    source: str = ""
```

- [ ] **Step 3: 验证导入**

```powershell
python -c "from profile.config import TRAE_MEMORY_DIR; from profile.models import ChatRecord; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add profile/config.py profile/models.py
git commit -m "feat: add config and data models"
```
