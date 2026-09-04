"""变化报告生成器。

把 diff 结果写成 Markdown 文件到 Obsidian 输出目录，不写数据库。
"""

from datetime import datetime
from pathlib import Path

from profile.config import OBSIDIAN_OUTPUT_DIR
from profile.log import get_logger

logger = get_logger("refresh.reporter")


def write_change_report(changes: list[dict]) -> Path | None:
    if not changes:
        logger.info("没有变化需要记录")
        return None

    OBSIDIAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = OBSIDIAN_OUTPUT_DIR / f"个人画像-变化报告-{today}.md"

    type_labels = {
        "new": "[新增]",
        "increased": "[上升]",
        "decreased": "[下降]",
        "disappeared": "[消失]",
        "changed": "[变化]",
    }

    # 统计变化类型
    type_counts = {}
    for c in changes:
        ct = c.get("change_type", "unknown")
        type_counts[ct] = type_counts.get(ct, 0) + 1

    logger.info("生成变化报告", extra={
        "extra": {
            "total_changes": len(changes),
            "type_counts": type_counts,
            "affected_profiles": list(set(c.get("profile_type", "") for c in changes)),
        }
    })

    lines = [
        f"# 个人画像变化报告 · {today}",
        "",
        "## 本周变化",
        "",
    ]

    for c in changes:
        label = type_labels.get(c["change_type"], "[变化]")
        lines.append(f"- {label} {c['change_summary']}")

    lines.extend([
        "",
        "## 详情",
        "",
        "| 类型 | 画像 | 字段 | 旧值 | 新值 |",
        "|------|------|------|------|------|",
    ])
    for c in changes:
        lines.append(
            f"| {c['change_type']} | {c['profile_type']} | `{c['field_name']}` | {c['old_value'][:40]} | {c['new_value'][:40]} |"
        )

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("变化报告已保存", extra={
        "extra": {"path": str(path), "changes_count": len(changes)}
    })
    return path
