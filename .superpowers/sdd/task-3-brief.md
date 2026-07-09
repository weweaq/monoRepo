### Task 3: IO 层抽象基类

**Files:**
- Create: `profile/io/base.py`

**Produces:** `BaseReader` 抽象基类，定义所有 Reader 的接口契约。

- [ ] **Step 1: 写 base.py**

```python
from abc import ABC, abstractmethod

from profile.models import ChatRecord


class BaseReader(ABC):
    @abstractmethod
    def read(self) -> list[ChatRecord]:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...
```

- [ ] **Step 2: 验证导入**

```powershell
python -c "from profile.io.base import BaseReader; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add profile/io/base.py
git commit -m "feat: add BaseReader abstract class"
```
