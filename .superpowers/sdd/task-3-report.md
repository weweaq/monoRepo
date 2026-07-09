## Task 3 Report: IO 层抽象基类

**Status:** complete

**Commit:** `67ca0e2` - feat: add BaseReader abstract class

**Files created:**
- `profile/io/base.py` — 18 lines, `BaseReader(ABC)` with three abstract members: `read()`, `is_available()`, `source_name`

**Verification:**
- `python -c "from profile.io.base import BaseReader; print('OK')"` → `OK`

**Concerns:** none
