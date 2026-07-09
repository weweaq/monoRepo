### Task 2 Report: config.py + models.py

**Status:** DONE

**Commits created:**
- `1a5e625` feat: add config and data models

**Verification:**
`python -c "from profile.config import TRAE_MEMORY_DIR; from profile.models import ChatRecord; print('OK')"` → `OK`

**Self-review:**
- `profile/config.py` — 4 constants (3 Path, 1 dict), zero dependencies beyond stdlib `pathlib`
- `profile/models.py` — 2 dataclasses (`ChatRecord`, `ContentRecord`), zero dependencies beyond stdlib `dataclasses` and `datetime`
- Both files match the task brief exactly
- CRLF line-ending warning is cosmetic (Windows default Git behavior)

**Concerns:** None
