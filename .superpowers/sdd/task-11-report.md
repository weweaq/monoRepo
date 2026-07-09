### Task 11 Report: CLI 入口

**Status:** Done

**Commit:** `0f5d136` — `feat: add CLI entry points for trae, marvis, and combined report`

**Files created:**
- `profile/cli/analyze_trae.py` — trae 聊天记录分析，输出 trae画像报告.md
- `profile/cli/analyze_marvis.py` — marvis 聊天记录分析，输出 marvis画像报告.md
- `profile/cli/generate.py` — 合并 trae + marvis 分析，生成 个人画像-v0.1.md

**Verification:**
- Import check passed: `python -c "from profile.cli import analyze_trae, analyze_marvis, generate; print('OK')"` → OK

**Concerns:** None
