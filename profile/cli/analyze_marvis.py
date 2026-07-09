"""marvis 聊天记录分析，输出 marvis画像报告.md"""
from profile.io.marvis_reader import MarvisReader
from profile.analysis.topic import analyze as analyze_topic
from profile.analysis.activity import analyze as analyze_activity
from profile.output.writer import write_report, format_report


def main():
    reader = MarvisReader()
    if not reader.is_available():
        print("[error] marvis 数据源不可用")
        return

    records = reader.read()
    print(f"读取 marvis 记录: {len(records)} 条")

    topic_result = analyze_topic(records)
    act_result = analyze_activity(records)

    sections = [
        ("日常诉求", _format_topic(topic_result)),
        ("活跃时段", _format_activity(act_result)),
    ]

    md = format_report(sections, "marvis 画像报告")
    path = write_report("marvis画像报告.md", md)
    print(f"已生成: {path}")


def _format_topic(r: dict) -> str:
    if r.get("status") == "样本不足":
        return f"数据不足（{r.get('count', 0)} 条记录），待积累。"
    lines = ["### TOP10 诉求主题"]
    for word, count in r.get("主要诉求TOP10", []):
        lines.append(f"- {word}（{count}次）")
    cat = r.get("诉求分类", {})
    lines.append("### 诉求分类")
    for k, v in cat.items():
        lines.append(f"- {k}：{v}%")
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


if __name__ == "__main__":
    main()
