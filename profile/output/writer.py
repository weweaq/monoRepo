"""画像 Markdown + JSON 输出。

每个画像同时输出 md（人看）和 json（diff 用），按日期命名。
"""

import json
from datetime import datetime
from pathlib import Path

from profile.config import OBSIDIAN_OUTPUT_DIR
from profile.log import get_logger

logger = get_logger("output.writer")


def write_channel(profile: dict, source: str, date_str: str | None = None) -> tuple[Path, Path]:
    """输出分 channel 画像的 md 和 json，返回 (md_path, json_path)。"""
    OBSIDIAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    md_path = OBSIDIAN_OUTPUT_DIR / f"个人画像-{source}-{date_str}.md"
    json_path = OBSIDIAN_OUTPUT_DIR / f"个人画像-{source}-{date_str}.json"

    period = profile.get("period", {})
    lines = [
        f"# {source} 画像",
        "",
        f"> 周期：{period.get('start', '')} ~ {period.get('end', '')}",
        "",
        _dict_to_md(profile),
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("画像已输出", extra={
        "extra": {"source": source, "md": md_path.name, "json": json_path.name}
    })
    return md_path, json_path


def write_global(profile: dict, date_str: str | None = None) -> tuple[Path, Path]:
    """输出综合画像的 md 和 json。"""
    OBSIDIAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    md_path = OBSIDIAN_OUTPUT_DIR / f"个人画像-综合-{date_str}.md"
    json_path = OBSIDIAN_OUTPUT_DIR / f"个人画像-综合-{date_str}.json"

    lines = [
        "# 个人综合画像",
        "",
        f"> 生成时间：{date_str}",
        "",
        "## 一句话总结",
        "",
        profile.get("summary", "暂无"),
        "",
        "## 方向真实度",
        "",
        _dict_to_md(profile.get("direction_truth", {})),
        "",
        "## 知识兴趣光谱",
        "",
        _dict_to_md(profile.get("knowledge_interest", {})),
        "",
        "## 作息精力模式",
        "",
        _dict_to_md(profile.get("activity_pattern", {})),
        "",
        "## 决策行动模式",
        "",
        _dict_to_md(profile.get("decision_style", {})),
        "",
        "## 情绪审美倾向",
        "",
        _dict_to_md(profile.get("emotional_tendency", {})),
        "",
        "## 建议",
        "",
    ]
    for s in profile.get("suggestions", []):
        lines.append(f"- {s}")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("综合画像已输出", extra={
        "extra": {"md": md_path.name, "json": json_path.name}
    })
    return md_path, json_path


def load_latest_json(name_prefix: str) -> dict | None:
    """加载输出目录中指定前缀的最新 json 文件。

    name_prefix 如 "个人画像-trae" 或 "个人画像-综合"。
    """
    if not OBSIDIAN_OUTPUT_DIR.exists():
        return None

    files = sorted(OBSIDIAN_OUTPUT_DIR.glob(f"{name_prefix}-*.json"), reverse=True)
    if not files:
        return None

    return json.loads(files[0].read_text(encoding="utf-8"))


def _dict_to_md(data, depth: int = 0) -> str:
    if isinstance(data, list):
        return "\n".join(f"- {_value_to_str(item)}" for item in data)

    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{'###' if depth == 0 else '####'} {key}")
            lines.append(_dict_to_md(value, depth + 1))
        elif isinstance(value, list):
            lines.append(f"- **{key}**：")
            for item in value:
                lines.append(f"  - {_value_to_str(item)}")
        else:
            lines.append(f"- **{key}**：{_value_to_str(value)}")
    return "\n".join(lines)


def _value_to_str(value) -> str:
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)
