"""Profiles API: 画像文件列表 + 内容读取。"""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from profile.config import OBSIDIAN_OUTPUT_DIR

router = APIRouter()

# 文件类型映射
FILE_TYPE_MAP = {
    "综合": "global",
    "trae": "trae",
    "marvis": "marvis",
    "content_consumption": "content_consumption",
    "变化报告": "change_report",
}


def _parse_filename(filename: str) -> dict:
    """从文件名解析类型和日期。"""
    name = Path(filename).stem
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    date_str = date_match.group(1) if date_match else ""

    file_type = "unknown"
    for key, val in FILE_TYPE_MAP.items():
        if key in name:
            file_type = val
            break

    return {"filename": filename, "type": file_type, "date": date_str}


@router.get("/profiles")
def list_profiles():
    if not OBSIDIAN_OUTPUT_DIR.exists():
        return {"items": [], "total": 0}

    files = sorted(OBSIDIAN_OUTPUT_DIR.iterdir(), reverse=True)
    items = []
    for f in files:
        if f.is_file() and f.suffix in (".md", ".json"):
            info = _parse_filename(f.name)
            info["size"] = f.stat().st_size
            info["ext"] = f.suffix
            items.append(info)

    return {"items": items, "total": len(items)}


@router.get("/profiles/{filename}")
def get_profile(filename: str):
    # 安全检查：防止路径穿越
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    filepath = OBSIDIAN_OUTPUT_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    content = filepath.read_text(encoding="utf-8")

    if filepath.suffix == ".json":
        try:
            data = json.loads(content)
            return {"filename": filename, "format": "json", "data": data}
        except json.JSONDecodeError:
            return {"filename": filename, "format": "text", "data": content}
    else:
        return {"filename": filename, "format": "markdown", "data": content}
