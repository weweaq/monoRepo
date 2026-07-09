"""trae 聊天记录分析，输出 trae画像报告.md"""
from datetime import datetime

from profile.io.trae_reader import TraeReader
from profile.analysis.direction import analyze as analyze_direction
from profile.analysis.activity import analyze as analyze_activity
from profile.analysis.decision import analyze as analyze_decision
from profile.output.writer import write_report, format_report
from profile.config import CLAIMED_DIRECTION


def main():
    reader = TraeReader()
    if not reader.is_available():
        print("[error] trae 数据源不可用")
        return

    records = reader.read()
    print(f"读取 trae 记录: {len(records)} 条")

    dir_result = analyze_direction(records, CLAIMED_DIRECTION)
    act_result = analyze_activity(records)
    dec_result = analyze_decision(records)

    sections = [
        ("方向真实度", _format_direction(dir_result)),
        ("活跃时段", _format_activity(act_result)),
        ("决策行动模式", _format_decision(dec_result)),
    ]

    md = format_report(sections, "trae 画像报告")
    path = write_report("trae画像报告.md", md)
    print(f"已生成: {path}")


def _format_direction(r: dict) -> str:
    if r.get("status") == "样本不足":
        return f"数据不足（{r.get('count', 0)} 条记录），待积累。"
    lines = [
        f"- 漂移度：{r['漂移度判断']}",
        f"- 方向占比：{r['方向提及占比']}",
        f"- 实际 TOP10 主题：",
    ]
    for word, count in r.get("实际TOP10主题", []):
        lines.append(f"  - {word}（{count}次）")
    lines.append(f"- 每周趋势：{r.get('每周趋势', '暂无')}")
    return "\n".join(lines)


def _format_activity(r: dict) -> str:
    if r.get("status") == "样本不足":
        return f"数据不足（{r.get('count', 0)} 条记录），待积累。"
    return "\n".join([
        f"- 峰值时段：{r['峰值时段']}",
        f"- 低谷时段：{r['低谷时段']}",
        f"- 日均活跃次数：{r['日均活跃次数']}",
        f"- 总记录数：{r['总记录数']}",
    ])


def _format_decision(r: dict) -> str:
    if r.get("status") == "样本不足":
        return f"数据不足（{r.get('count', 0)} 条记录），待积累。"
    dist = r.get("处理路径分布", {})
    return "\n".join([
        f"- 调研类：{dist.get('调研类', 0)}%",
        f"- 动手类：{dist.get('动手类', 0)}%",
        f"- 讨论类：{dist.get('讨论类', 0)}%",
        f"- 模式：{r.get('模式判断', '未知')}",
        f"- 想法到动手间隔：{r.get('想法到动手间隔', '暂无')}",
    ])


if __name__ == "__main__":
    main()
