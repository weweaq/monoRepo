# -*- coding: utf-8 -*-
"""Generate 3 self-contained HTML demos that visualize LLM runs (llm-view-demos).

Usage (cwd = apps/withlanggraph):
    uv run python docs/llm-view-demos/build_demos.py [--date 2026-10-03]

数据重建统一走 gacore.llm_runview.run_view——与 review_server 的 /llm-requests 页共用
单一实现，本文件只负责把该 JSON 内嵌进自包含 HTML（双击即开，无需服务器）。

入口关系（2026-10-04 选型落地后）：
- 正式入口：review_server 的 GET /llm-requests（运行回放，评审选定型 A 的服务端移植）
- 本目录产物为离线快照：demo_b_table.html / demo_c_inspector.html 可由本脚本重建；
  demo_a_timeline.html 为选型期快照（A 已移植，脚本不再重建，需要时从 git 历史取
  template_a 版本），demo_a 的功能以后续 /llm-requests 页为准。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from gacore.config import Config
from gacore.llm_runview import run_view

OUT_DIR = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-03")
    args = ap.parse_args()

    data = run_view(Config.default(), args.date)
    data["generated"] = datetime.now().isoformat(timespec="seconds")
    data["source"] = (f"logs/{args.date}/llm_requests.jsonl + app.jsonl（系统动作/ERROR）"
                      f" + scheduled/（产出存档）——经 gacore.llm_runview.run_view 重建")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    for name, tpl in TEMPLATES.items():
        html = tpl.replace("__DATA__", blob).replace("__DATE__", args.date)
        path = OUT_DIR / name
        path.write_text(html, encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size // 1024}KB)")
    return 0


def _load_tmpl(name: str) -> str:
    return (OUT_DIR / name).read_text(encoding="utf-8")


TEMPLATES = {
    "demo_b_table.html": _load_tmpl("template_b_table.tmpl"),
    "demo_c_inspector.html": _load_tmpl("template_c_inspector.tmpl"),
}

if __name__ == "__main__":
    raise SystemExit(main())
