"""合并 trae + marvis 分析，生成 个人画像-v0.1.md"""
from datetime import datetime

from profile.io.trae_reader import TraeReader
from profile.io.marvis_reader import MarvisReader
from profile.analysis.direction import analyze as analyze_direction
from profile.analysis.activity import analyze as analyze_activity
from profile.analysis.decision import analyze as analyze_decision
from profile.analysis.topic import analyze as analyze_topic
from profile.output.writer import write_report
from profile.config import CLAIMED_DIRECTION


def main():
    trae = TraeReader()
    marvis = MarvisReader()

    trae_records = trae.read() if trae.is_available() else []
    marvis_records = marvis.read() if marvis.is_available() else []
    print(f"trae: {len(trae_records)} 条, marvis: {len(marvis_records)} 条")

    all_records = trae_records + marvis_records

    dir_result = analyze_direction(trae_records, CLAIMED_DIRECTION) if trae_records else {"status": "样本不足"}
    dec_result = analyze_decision(trae_records) if trae_records else {"status": "样本不足"}
    act_result = analyze_activity(all_records) if all_records else {"status": "样本不足"}
    topic_result = analyze_topic(marvis_records) if marvis_records else {"status": "样本不足"}

    today = datetime.now().strftime("%Y-%m-%d")
    md = _build_markdown(today, trae_records, marvis_records,
                         dir_result, dec_result, act_result, topic_result)

    path = write_report("个人画像-v0.1.md", md)
    print(f"已生成: {path}")


def _build_markdown(today, trae_records, marvis_records,
                    dir_result, dec_result, act_result, topic_result):
    lines = [
        f"## 个人画像 v0.1 · {today}",
        "",
        "> 自动生成，请勿手动编辑",
        "",
        "### 数据概况",
        "",
        _format_overview(trae_records, marvis_records),
        "",
        "### 方向真实度",
        "",
        _no_data(dir_result)
        if dir_result.get("status") == "样本不足"
        else _format_direction(dir_result),
        "",
        "### 决策行动模式",
        "",
        _no_data(dec_result)
        if dec_result.get("status") == "样本不足"
        else _format_decision(dec_result),
        "",
        "### 活跃时段",
        "",
        _no_data(act_result)
        if act_result.get("status") == "样本不足"
        else _format_activity(act_result),
        "",
        "### 日常诉求（marvis）",
        "",
        _no_data(topic_result)
        if topic_result.get("status") == "样本不足"
        else _format_topic(topic_result),
        "",
    ]
    return "\n".join(lines)


def _no_data(r: dict) -> str:
    return f"> *数据不足（{r.get('count', 0)} 条记录），待积累。*"


def _format_overview(trae_records, marvis_records):
    lines = [
        f"- 数据来源：trae（{len(trae_records)}条）+ marvis（{len(marvis_records)}条）",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if trae_records:
        dates = {r.time.date() for r in trae_records}
        lines.append(f"- trae 时间范围：{min(dates)} 至 {max(dates)}")
    if marvis_records:
        dates = {r.time.date() for r in marvis_records}
        lines.append(f"- marvis 时间范围：{min(dates)} 至 {max(dates)}")
    return "\n".join(lines)


def _format_direction(r: dict) -> str:
    lines = [
        f"- 声称方向：{r.get('声称方向', {})}",
        f"- 漂移度：{r.get('漂移度判断', '未知')}",
        f"- 方向占比：{r.get('方向提及占比', {})}",
        f"- 实际 TOP10 主题：",
    ]
    for word, count in r.get("实际TOP10主题", []):
        lines.append(f"  - {word}（{count}次）")
    return "\n".join(lines)


def _format_activity(r: dict) -> str:
    return "\n".join([
        f"- 峰值时段：{r.get('峰值时段', '未知')}",
        f"- 低谷时段：{r.get('低谷时段', '未知')}",
        f"- 日均活跃次数：{r.get('日均活跃次数', '?')}",
        f"- 总记录数：{r.get('总记录数', '?')} / 天数：{r.get('天数', '?')}",
    ])


def _format_decision(r: dict) -> str:
    dist = r.get("处理路径分布", {})
    return "\n".join([
        f"- 调研类：{dist.get('调研类', 0)}%",
        f"- 动手类：{dist.get('动手类', 0)}%",
        f"- 讨论类：{dist.get('讨论类', 0)}%",
        f"- 模式：{r.get('模式判断', '未知')}",
        f"- 想法到动手间隔：{r.get('想法到动手间隔', '暂无')}",
    ])


def _format_topic(r: dict) -> str:
    lines = []
    top = r.get("主要诉求TOP10", [])
    if top:
        lines.append("**TOP10 诉求主题**")
        for word, count in top:
            lines.append(f"- {word}（{count}次）")
    cat = r.get("诉求分类", {})
    if cat:
        lines.append("**诉求分类**")
        for k, v in cat.items():
            lines.append(f"- {k}：{v}%")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
