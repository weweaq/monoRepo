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
