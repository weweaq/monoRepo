"""langTrack Web 仪表盘：基于事实卡片（fact_card）渲染深色仪表盘 HTML（与 App 主版一致）。

用法：FastAPI 挂载 GET /dashboard 即调用 render_dashboard_html(conn, day)。
数据源：sessions / daily_stats / places（ETL 产出），并在 persona 卡片上方插入
「事实审查」块（来自 fact_card 注入的紧凑事实卡），保证数据口径与消息拼装一致。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import urllib.parse
from html import escape
from pathlib import Path

from gacore.langTrack import fact_card
from gacore.langTrack import location_reader as lr

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"
_TZ = datetime.timezone(datetime.timedelta(hours=8))

PAGE_CSS = """
:root{--bg:#05060A;--card:#11151F;--line:#1C2333;--ink:#E9EDF6;--ink2:#96A0B4;
--ink3:#5A6378;--cyan:#22D3EE;--green:#3FBF8F;--rose:#F26B5C;--amber:#F2A65A;--violet:#8A7AD8}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);padding:24px;max-width:860px;margin:0 auto}
h1{font-size:22px;font-weight:800;margin-bottom:4px}
.sub{color:var(--ink3);font-size:13px;margin-bottom:20px}
.nav{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}
.nav a{color:var(--ink2);text-decoration:none;font-size:13px;padding:6px 14px;border-radius:9px;
background:var(--card);border:1px solid var(--line)}
.nav a.on{color:var(--cyan);border-color:rgba(34,211,238,.4);background:rgba(34,211,238,.08)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.stat .v{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.stat .l{font-size:11px;color:var(--ink3);margin-top:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px}
.card h2{font-size:13px;color:var(--ink2);letter-spacing:.05em;margin-bottom:12px;font-weight:700}
.row{display:flex;align-items:center;padding:6px 0;font-size:13px}
.row .name{flex:1;color:var(--ink)}
.row .val{color:var(--ink2);font-variant-numeric:tabular-nums}
.bar-wrap{flex:1;height:8px;background:#0D1220;border-radius:4px;margin-left:12px;overflow:hidden}
.bar{height:100%;border-radius:4px}
.hbar{display:flex;gap:2px;height:44px;align-items:flex-end;margin-top:8px}
.hcell{flex:1;border-radius:3px 3px 0 0;position:relative}
.hcell:hover::after{content:attr(data-n);position:absolute;top:-20px;left:50%;transform:translateX(-50%);
background:#1A2234;padding:2px 6px;border-radius:6px;font-size:10px;color:var(--ink);white-space:nowrap}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11px;color:var(--ink3)}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{color:var(--ink3);text-align:left;font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px}
.tbl td{padding:7px 8px;border-bottom:1px solid #161D2E}
.empty{color:var(--ink3);font-size:13px;padding:20px;text-align:center}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.chip{display:inline-block;padding:3px 9px;border-radius:8px;font-size:12px;border:1px solid var(--line);background:#0D1220}
.compact-pre{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12px;
color:var(--cyan);background:#0B0F18;border:1px solid rgba(34,211,238,.25);border-radius:10px;
padding:12px;line-height:1.6;margin-top:8px}
.meta{font-size:11px;color:var(--ink3);line-height:1.8;margin-bottom:8px}
.meta b{color:var(--ink2);font-weight:600}
.review-note{font-size:11px;color:var(--ink3);margin:-6px 0 10px}
.warn{font-size:12px;color:var(--amber);background:rgba(242,166,90,.08);
border:1px solid rgba(242,166,90,.35);border-radius:10px;padding:8px 12px;margin-bottom:12px}
.ev{font-size:11px;color:var(--ink2);margin-top:8px;line-height:1.7;
background:#0B0F18;border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.ev .evrow{color:var(--ink3)}
.evrow+.evrow{margin-top:2px}
.ev b{color:var(--green);font-weight:600}
.tagb{display:inline-block;color:var(--cyan);font-size:11px}
.nd{color:var(--amber);font-size:12px}
.dim{color:var(--ink3);font-size:11px}
.mig-note{font-size:11px;color:var(--ink3);margin-top:6px}
.evtr td{background:none;border:0;padding:2px 8px 10px}
.etl-row{display:flex;gap:12px;align-items:center;margin:0 0 20px}
#etl-btn{background:var(--card);border:1px solid var(--line);color:var(--cyan);
padding:6px 14px;border-radius:9px;font-size:13px;cursor:pointer}
#etl-btn:disabled{color:var(--ink3);cursor:default}
#etl-btn:hover:not(:disabled){border-color:rgba(34,211,238,.4);background:rgba(34,211,238,.08)}
"""


def _fmt_dur(ms: int) -> str:
    if ms is None:
        return "-"
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "-"
    h, rem = divmod(ms // 1000, 3600)
    m = rem // 60
    return f"{h}小时{m}分" if h else f"{m}分钟"


def _fmt_time(ms: int) -> str:
    """本地时区 HH:MM（用于原 dashboard 会话列表）。"""
    return datetime.datetime.fromtimestamp(ms / 1000, tz=_TZ).strftime("%H:%M")


def _app_color(idx: int) -> str:
    palette = ["#22D3EE", "#F26B5C", "#3FBF8F", "#F2A65A", "#8A7AD8",
               "#E58FC4", "#7FB0E6", "#4ADE80", "#F87171", "#60A5FA"]
    return palette[idx % len(palette)]


def _collect_days(conn: sqlite3.Connection) -> list[str]:
    """日期导航候选：合并 daily_stats / stays / anomalies 的 day 并集，降序。"""
    days: set[str] = set()
    for table in ("daily_stats", "stays", "anomalies"):
        try:
            for r in conn.execute(f"SELECT DISTINCT day FROM {table}"):
                if r["day"]:
                    days.add(r["day"])
        except sqlite3.OperationalError:
            continue
    return sorted(days, reverse=True)


def _hours_activity(conn: sqlite3.Connection, device_id: str, day: str) -> list[int]:
    """24h 亮屏活动分布：按 snapshot.screen=true 聚合（按设备过滤）。"""
    hours = [0] * 24
    rows = conn.execute(
        "SELECT strftime('%H', ts/1000,'unixepoch','+8 hours') h "
        "FROM events WHERE type='snapshot' AND device_id=? AND "
        "date(ts/1000,'unixepoch','+8 hours')=? AND payload LIKE '%\"screen\":true%'",
        (device_id, day),
    ).fetchall()
    for (h,) in rows:
        try:
            hours[int(h)] += 1
        except (ValueError, TypeError):
            pass
    return hours


def _render_coverage(conn: sqlite3.Connection) -> str:
    """A① 采集覆盖卡片：把 contract_coverage 全量渲染为按状态着色的 chip。"""
    rows = conn.execute(
        "SELECT type, desc, status FROM contract_coverage ORDER BY "
        "(CASE status WHEN 'missing' THEN 1 WHEN 'stale' THEN 2 "
        "WHEN 'unexpected' THEN 3 ELSE 4 END), type"
    ).fetchall()
    if not rows:
        return ""
    color_map = {
        "ok": "var(--green)", "stale": "var(--amber)",
        "missing": "var(--rose)", "unexpected": "var(--ink2)",
    }
    sym_map = {
        "ok": "OK", "stale": "STALE", "missing": "MISS", "unexpected": "UNEX",
    }
    chips = ""
    for r in rows:
        c = color_map.get(r["status"], "var(--ink2)")
        sym = sym_map.get(r["status"], "?")
        chips += (
            f'<span class="chip" title="{escape(r["desc"] or r["type"])}" '
            f'style="border-color:{c};color:{c}">{sym} {escape(r["type"])}</span>'
        )
    return (
        f'<div class="card"><h2>采集覆盖（契约 {len(rows)} 类）</h2>'
        f'<div class="chips">{chips}</div></div>'
    )


def _render_persona(p: dict) -> str:
    """C1 人物画像卡片：直接渲染 fact_card 注入的 persona（不再二次查询）。"""
    if not p or not p.get("available"):
        return '<div class="card"><h2>人物画像</h2><div class="empty">近 7 日无数据</div></div>'
    traits = p.get("traits", [])
    trait_chips = "".join(
        f'<span class="chip" style="border-color:var(--violet);color:var(--violet)">{escape(t)}</span>'
        for t in traits
    ) or '<span class="chip">数据较少</span>'
    cats = p.get("category_usage", [])[:5]
    cat_rows = "".join(
        f'<div class="row"><span class="name">{escape(c["category"])}</span>'
        f'<span class="val">{c["hours"]}h · {c["pct"]}%</span></div>'
        for c in cats
    )
    sh = p.get("screen_health", {})
    rh = p.get("rhythm", {})
    return (
        f'<div class="card"><h2>人物画像（近 7 天）</h2>'
        f'<div class="chips">{trait_chips}</div>'
        f'<div class="row"><span class="name">日均屏幕</span>'
        f'<span class="val">{sh.get("avg_hours", 0)}h · {sh.get("note", "")}</span></div>'
        f'<div class="row"><span class="name">深夜活跃</span>'
        f'<span class="val">{rh.get("night_pct", 0)}% · {"夜猫子" if rh.get("night_owl") else "正常"}</span></div>'
        f'<div style="margin-top:8px;font-size:12px;color:var(--ink2)">{escape(p.get("card", ""))}</div>'
        f'<div style="margin-top:10px"><div class="row"><span class="name" style="color:var(--ink3);font-size:11px">分类时长 Top</span></div>{cat_rows}</div>'
        f'</div>'
    )


def _render_card_meta(card: dict) -> str:
    """事实审查旁注：card_fp / 生成时刻 / 数据水位 / 裁剪明细。"""
    rows: list[str] = []
    om = card.get("compact_omitted") or {}
    om_txt = "；".join(f"{k}:{v}" for k, v in om.items()) or "-"
    meta_items = [
        ("card_fp", card.get("card_fp", "")),
        ("generated_at", card.get("generated_at", "")),
        ("etl_watermark", card.get("etl_watermark", "")),
        ("data_as_of", f'{card.get("data_as_of", "")}（{card.get("data_as_of_source", "")}）'),
        ("location_as_of", card.get("location_as_of", "")),
        ("data_age_min", card.get("data_age_min", "")),
        ("compact_lines", "、".join(card.get("compact_lines", [])) or "-"),
        ("compact_omitted", om_txt),
    ]
    for k, v in meta_items:
        rows.append(f'<div><b>{escape(k)}</b>：{escape(str(v))}</div>')
    return f'<div class="meta">数据水位{""} <br/>{"".join(rows)}</div>'


def _render_timeline(card: dict) -> str:
    """今日轨迹 / 当前已知。"""
    stays = card.get("stays") or []
    ck = card.get("current_known")
    parts: list[str] = []
    if stays:
        seq = " → ".join(f'{escape(s["label"])} {s["start_hhmm"]}-{s["end_hhmm"]}' for s in stays)
        parts.append(
            f'<div class="row"><span class="name">今日轨迹</span>'
            f'<span class="val">{seq}</span></div>'
        )
    if ck:
        txt = f'{escape(ck["label"])} {ck["since_hhmm"]}-{ck["observed_until_hhmm"]}'
        if ck.get("poi"):
            txt += f' · {escape(ck["poi"])}'
        if ck.get("district"):
            txt += f' · {escape(ck["district"])}'
        parts.append(
            f'<div class="row"><span class="name">当前已知</span>'
            f'<span class="val">{txt}</span></div>'
        )
    return "".join(parts)


def _render_stay_trip_anomaly(card: dict) -> str:
    """当日停留 / 移动 / 异常三张表。"""
    stays = card.get("stays") or []
    trips = card.get("trips") or []
    anoms = card.get("anomalies") or []

    stay_rows = "".join(
        f'<tr><td>{escape(s["label"])}</td><td>{s["start_hhmm"]}-{s["end_hhmm"]}</td></tr>'
        for s in stays
    ) or '<tr><td colspan="2" class="empty">无</td></tr>'

    trip_rows = "".join(
        f'<tr><td>{escape(t["from_label"])} → {escape(t["to_label"])}</td>'
        f'<td>{t["start_hhmm"]}-{t["end_hhmm"]}</td><td>{t["dist_m"]} m</td></tr>'
        for t in trips
    ) or '<tr><td colspan="3" class="empty">无</td></tr>'

    anom_rows = "".join(
        f'<tr><td>{escape(a["kind"])}</td><td>{escape(a["poi"])}</td>'
        f'<td>{escape(a["detail"])}</td></tr>'
        for a in anoms
    ) or '<tr><td colspan="3" class="empty">无</td></tr>'

    return f"""
    <div style="margin-top:10px;font-size:11px;color:var(--ink3)">当日停留</div>
    <table class="tbl"><tr><th>位置</th><th>时段</th></tr>{stay_rows}</table>
    <div style="margin-top:10px;font-size:11px;color:var(--ink3)">当日移动（端点直距，非导航距离）</div>
    <table class="tbl"><tr><th>区间</th><th>时段</th><th>距离</th></tr>{trip_rows}</table>
    <div style="margin-top:10px;font-size:11px;color:var(--ink3)">当日系统标记 / 异常</div>
    <table class="tbl"><tr><th>类型</th><th>地点</th><th>详情</th></tr>{anom_rows}</table>
    """


def _render_day_summary(card: dict) -> str:
    """当日汇总：停留累计 + 常驻点（全历史 location 点数，不谎称到访次数）。"""
    sm = card.get("stay_minutes") or {}
    stay_min_txt = " · ".join(
        f"{k} {v // 60}h{v % 60}m" for k, v in sorted(sm.items()) if v
    ) or "-"
    places = card.get("places") or []
    place_rows = "".join(
        f'<tr><td>{escape(p.get("label", ""))}</td><td>{escape(p.get("poi", ""))}</td>'
        f'<td>{p.get("visits", 0)}</td></tr>'
        for p in places
    ) or '<tr><td colspan="3" class="empty">无</td></tr>'
    return f"""
    <div style="margin-top:10px;font-size:11px;color:var(--ink3)">当日汇总</div>
    <div class="row"><span class="name">屏幕 / 通知</span>
      <span class="val">{card.get("screen_hours", 0):.1f}h · 通知 {card.get("notification_count", 0)}（点击 {card.get("notification_clicked", 0)}）</span></div>
    <div class="row"><span class="name">停留累计</span><span class="val">{escape(stay_min_txt)}</span></div>
    <div style="margin-top:10px;font-size:11px;color:var(--ink3)">常驻点（全历史 location 点数）</div>
    <table class="tbl"><tr><th>位置</th><th>POI</th><th>点数</th></tr>{place_rows}</table>
    """


def _render_arrivals(conn: sqlite3.Connection, device_id: str, day: str) -> str:
    """当日到达（采集体检）：events 按 type 计数（只读 SQL，不属于 FactCard）。"""
    rows = conn.execute(
        "SELECT type, COUNT(*) n FROM events "
        "WHERE device_id=? AND date(ts/1000,'unixepoch','+8 hours')=? "
        "GROUP BY type ORDER BY n DESC",
        (device_id, day),
    ).fetchall()
    if not rows:
        return ""
    body_rows = "".join(
        f'<tr><td>{escape(r["type"])}</td><td>{r["n"]}</td></tr>' for r in rows
    )
    return (
        f'<div class="card"><h2>当日到达（采集体检）</h2>'
        f'<table class="tbl"><tr><th>类型</th><th>条数</th></tr>{body_rows}</table></div>'
    )


def _render_fact_review(card: dict) -> str:
    """事实审查块：compact 预览 + 旁注 + 轨迹/当前已知 + 三表 + 当日汇总。"""
    compact = card.get("compact") or ""
    if compact:
        pre = f'<pre class="compact-pre">{escape(compact)}</pre>'
    else:
        pre = '<div class="empty">当日拼不出 compact（数据不足）</div>'
    return (
        f'<div class="card" style="border-color:rgba(63,191,143,.35)">'
        f'<h2>事实审查 · {escape(card.get("day", ""))}</h2>'
        f'<div class="review-note">只含时序事实、统计与系统 tag，不含人格句（未注入 LLM）</div>'
        f'{_render_card_meta(card)}{pre}{_render_timeline(card)}'
        f'{_render_stay_trip_anomaly(card)}{_render_day_summary(card)}'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Task 9：位置画像关键指标 + 迁移审查（§2.11）——只渲染，不重算画像
# ---------------------------------------------------------------------------


def _place_display(place_name, user_tag=""):
    """统一地点文案：place_name〔tag〕；两者皆无 → 未知地点。"""
    name = (place_name or "").strip()
    tag = (user_tag or "").strip()
    if name and tag:
        return f"{name}〔{tag}〕"
    if name:
        return name
    if tag:
        return tag
    return "未知地点"


def _render_evidence(ev, *, metric="") -> str:
    """渲染 Evidence components；None → “数据不足”，不以 0 冒充事实。"""
    if not ev:
        return '<div class="ev">数据不足（无样本窗口）</div>'
    level = ev.get("confidence_level") or "low"
    color = {"high": "var(--green)", "medium": "var(--amber)", "low": "var(--rose)"}.get(level, "var(--amber)")
    cov = ev.get("coverage_ratio")
    cov_txt = f"{cov * 100:.0f}%" if isinstance(cov, (int, float)) else "-"
    rows = [
        f"窗口 {ev.get('window_days', '-')} 天（请求 {ev.get('requested_window_days', '-')} / 可用 {ev.get('available_window_days', '-')}）",
        f"覆盖率 {cov_txt}（{ev.get('observed_bins', 0)} / {ev.get('expected_bins', 0)} 格）",
        f"样本 {ev.get('sample_count', 0)} / 需求 {ev.get('required_samples', 0)}",
        "解析 {parse:.2f} · 精度已知 {acc:.2f} · 质量 {q:.2f}".format(
            parse=ev.get("parse_validity_score", 0) or 0,
            acc=ev.get("accuracy_known_score", 0) or 0,
            q=ev.get("quality_score", 0) or 0,
        ),
    ]
    low_note = (
        '<div class="evrow" style="color:var(--amber)">数据不足（置信度低，不作确定结论）</div>'
        if level == "low" else ""
    )
    return (
        f'<div class="ev"><span class="badge" '
        f'style="border:1px solid {color};color:{color}">{escape(level)}</span>'
        f'<div class="evrow">' + "</div><div class=\"evrow\">".join(escape(r) for r in rows)
        + f"</div>{low_note}</div>"
    )


def _coord_system_counts(conn: sqlite3.Connection, device_id: str, day: str) -> dict:
    """所选日坐标制集合及点数：按事件 ts 经 etl_config 时间段匹配解析。"""
    counts: dict[str, int] = {}
    try:
        from gacore.langTrack.etl_config import load_coord_systems, resolve_coord_system

        cfg = load_coord_systems()
    except Exception:  # noqa: BLE001 - 配置缺失/损坏降级 unknown
        cfg = {"default": "unknown", "periods": []}
    try:
        rows = conn.execute(
            "SELECT ts FROM events WHERE type='location' AND device_id=? AND "
            "date(ts/1000,'unixepoch','+8 hours')=?",
            (device_id, day),
        ).fetchall()
    except sqlite3.OperationalError:
        return counts
    for (ts,) in rows:
        cs = resolve_coord_system(device_id, int(ts or 0), cfg)
        counts[cs] = counts.get(cs, 0) + 1
    return counts


def _render_location_health(conn: sqlite3.Connection, device_id: str, day: str) -> str:
    """定位健康卡：points / coverage / accuracy 三档 / provider / 采样间隔 / 坐标制。"""
    try:
        q = lr.read_daily_quality(conn, device_id=device_id, day=day)
    except Exception:  # noqa: BLE001
        q = None
    coords = _coord_system_counts(conn, device_id, day)

    warn_parts: list[str] = []
    if q is None:
        warn_parts.append("无定位质量日表")
    if "unknown" in coords or not coords:
        warn_parts.append("坐标制未知")
    elif len(coords) > 1:
        warn_parts.append("同日多坐标制")
    warn = (
        f'<div class="warn">{escape("；".join(warn_parts))}</div>' if warn_parts else ""
    )

    # 覆盖率：observed/expected 半小时格；expected 只计到当日数据水位，不把未来算缺测
    observed = int(q.get("observed_half_hour_bins") or 0) if q else 0
    expected = 1
    try:
        day0_ms = int(datetime.datetime(
            *[int(x) for x in day.split("-")], tzinfo=_TZ
        ).timestamp() * 1000)
        wm = None
        row = conn.execute(
            "SELECT last_event_ts FROM etl_state WHERE device_id=?", (device_id,)
        ).fetchone()
        if row:
            wm = row["last_event_ts"]
        end_ms = min(wm or (day0_ms + 86400000 - 1), day0_ms + 86400000 - 1)
        expected = max(1, int((max(end_ms, day0_ms) - day0_ms) // 1_800_000))
    except Exception:  # noqa: BLE001
        expected = 1
    cov = min(1.0, observed / expected)

    rows = ""
    if q:
        rows += (
            f'<div class="row"><span class="name">有效点 / 总点</span>'
            f'<span class="val">{q.get("points_valid", 0)} / {q.get("points_total", 0)}</span></div>'
            f'<div class="row"><span class="name">30 分钟覆盖格</span>'
            f'<span class="val">{observed} / {expected}（{cov * 100:.0f}%）</span></div>'
            f'<div class="row"><span class="name">精度 ≤50m / 51–150m / >150m</span>'
            f'<span class="val">{q.get("accuracy_le_50", 0)} / {q.get("accuracy_51_150", 0)} / {q.get("accuracy_gt_150", 0)}'
            f'（已知 {q.get("accuracy_known", 0)}）</span></div>'
            f'<div class="row"><span class="name">采样间隔中位数（所选日）</span>'
            f'<span class="val">{_fmt_sec(q.get("median_interval_sec"))}</span></div>'
        )
        try:
            prov = json.loads(q.get("providers_json") or "{}")
        except (ValueError, TypeError):
            prov = {}
        if prov:
            prov_txt = "、".join(f"{escape(str(k))}×{v}" for k, v in prov.items())
            rows += f'<div class="row"><span class="name">provider</span><span class="val">{prov_txt}</span></div>'
    else:
        rows += '<div class="empty">今日无定位质量数据</div>'

    if coords:
        coord_txt = "、".join(
            f"{escape(str(k))}×{v}" for k, v in sorted(coords.items())
        )
        rows += (
            f'<div class="row"><span class="name">坐标制及点数</span>'
            f'<span class="val">{coord_txt}</span></div>'
        )
    return (
        f'<div class="card"><h2>定位健康 · {escape(day)}</h2>{warn}{rows}'
        f'<div class="mig-note">坐标制集合按事件 ts 时间段解析；unknown 或同日多坐标制时黄色警告。</div></div>'
    )


def _fmt_sec(sec) -> str:
    if sec is None:
        return "-"
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "-"
    if sec < 1:
        return f"{sec * 1000:.0f} 毫秒"
    if sec < 60:
        return f"{sec:.1f} 秒"
    return f"{int(sec // 60)}分{int(sec % 60)}秒"


def _has_home_work(sp) -> bool:
    home_work_rhythm = (sp or {}).get("home_work_rhythm")
    commute = (sp or {}).get("commute_profile")
    return bool(home_work_rhythm or commute)


def _has_spatial_data(sp) -> bool:
    """是否具备任何位置画像内容：空骨架（全空）与“画像存在但缺家/公司”语义分离。"""
    if not sp:
        return False
    return bool(
        sp.get("frequent_places") or sp.get("spatial_extent")
        or sp.get("commute_profile") or sp.get("home_work_rhythm")
        or sp.get("scene_exposure") or sp.get("place_change")
    )


def _render_kpi30(sp: dict | None) -> str:
    """30 天关键指标卡：覆盖率 / 有停留地点数 / 常去 Top1 / P90 生活半径 / 通勤 / 家·公司时长。"""
    # 空骨架（全空）整卡走“数据不足”；仅画像存在且缺 home/work tag 才算“尚未确认家/公司”
    if not sp or not _has_spatial_data(sp):
        return (
            '<div class="card"><h2>30 天关键指标</h2>'
            '<div class="empty">数据不足，无法构建长期画像。</div></div>'
        )
    ext = sp.get("spatial_extent") or {}
    freq = sp.get("frequent_places") or []
    freq30 = [p for p in freq if 30 in (p.get("windows") or [])]
    top = freq30[0] if freq30 else None
    commute = sp.get("commute_profile")
    rhythm = sp.get("home_work_rhythm")

    pw = (sp.get("per_window") or {}).get("30") or {}
    cov_obs = pw.get("observed_bins")
    cov_exp = pw.get("expected_bins")
    if not cov_exp:
        ec = (ext.get("evidence") or {})
        cov_obs, cov_exp = ec.get("observed_bins"), ec.get("expected_bins")
    cov_txt = (
        f"{cov_obs} / {cov_exp} 格（{cov_obs / cov_exp * 100:.0f}%）"
        if cov_exp else "-"
    )

    home_d = ext.get("home_distance")
    if not ext:
        radius_txt = "数据不足"
    elif not home_d or home_d.get("p90_m") is None:
        radius_txt = "尚未确认家/公司"
    else:
        radius_txt = _fmt_km(home_d.get("p90_m"))

    comm_rows = ""
    if commute:
        comm_rows += (
            f'<div class="row"><span class="name">家→公司有效通勤天数</span>'
            f'<span class="val">{commute.get("valid_days", 0)} 天'
            f'（工作日 {commute.get("weekday_valid_days", 0)} / 周末 {commute.get("weekend_valid_days", 0)}）</span></div>'
            f'<div class="row"><span class="name">通勤耗时中位 / IQR</span>'
            f'<span class="val">{_fmt_dur(commute.get("duration_ms_median"))} / {_fmt_dur(commute.get("duration_ms_iqr"))}</span></div>'
            f'<div class="row"><span class="name">端点直距 / 路线距</span>'
            f'<span class="val">{_fmt_km(commute.get("endpoint_dist_m"))} / {'-' if commute.get("route_dist_m") is None else _fmt_km(commute.get("route_dist_m"))}</span></div>'
        )
    if rhythm:
        wk = rhythm.get("weekday") or {}
        home_ms_med = (wk.get("home_ms") or {}).get("median_ms")
        work_ms_med = (wk.get("work_ms") or {}).get("median_ms")
        comm_rows += (
            f'<div class="row"><span class="name">工作日在家时长中位</span>'
            f'<span class="val">{_fmt_dur(home_ms_med) if home_ms_med is not None else "无有效样本日"}</span></div>'
            f'<div class="row"><span class="name">工作日公司时长中位</span>'
            f'<span class="val">{_fmt_dur(work_ms_med) if work_ms_med is not None else "无有效样本日"}</span></div>'
        )
    if not comm_rows:
        comm_rows = (
            f'<div class="row"><span class="name">通勤 / 家·公司时长</span>'
            f'<span style="color:var(--amber);font-size:12px">{"尚未确认家/公司" if not _has_home_work(sp) else "数据不足"}</span></div>'
        )

    items = [
        ("可观测覆盖率（30 天）", cov_txt),
        ("有停留地点数（30 天）", str(ext.get("place_count")) if ext.get("place_count") is not None else "-"),
        ("常去地点 Top 1", _place_display(top.get("place_name"), top.get("user_tag")) if top else "数据不足"),
        ("相对家 P90 生活半径", radius_txt),
    ]
    stat_rows = "".join(
        f'<div class="row"><span class="name">{escape(k)}</span>'
        f'<span class="val">{escape(str(v))}</span></div>'
        for k, v in items
    )
    ev = ext.get("evidence")
    ev_html = (
        _render_evidence(ev, metric="30天画像")
        if ev else '<div class="ev">数据不足（无画像样本）</div>'
    )
    return (
        f'<div class="card"><h2>30 天关键指标</h2>'
        f'{stat_rows}{comm_rows}{ev_html}'
        f'</div>'
    )


def _fmt_km(m) -> str:
    if m is None:
        return "-"
    try:
        m = float(m)
    except (TypeError, ValueError):
        return "-"
    return f"{m / 1000:.1f} 公里" if m >= 1000 else f"{m:.0f} 米"


def _last_seen_str(ms) -> str:
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000, tz=_TZ).strftime("%Y-%m-%d")


def _place_point_counts(conn: sqlite3.Connection, device_id: str) -> dict[str, int]:
    """按 place_id 补齐全历史原始点数（places.point_count 静态统计），缺列降级 {}。"""
    try:
        return {
            str(r["place_id"]): int(r["point_count"] or 0)
            for r in conn.execute(
                "SELECT place_id, point_count FROM places "
                "WHERE device_id=? AND place_id IS NOT NULL",
                (device_id,),
            ).fetchall()
        }
    except (sqlite3.OperationalError, TypeError):
        return {}


def _render_frequent_places(conn: sqlite3.Connection, card: dict, sp: dict | None, window: int) -> str:
    """常去地点表：地点场景 / tag / 原始点数 / 到访段数 / 停留时长 / 最近到访 / 分布。"""
    if not sp:
        return '<div class="card"><h2>常去地点</h2><div class="empty">数据不足</div></div>'
    freq = sp.get("frequent_places") or []
    items = [p for p in freq if window in (p.get("windows") or [])]
    if not items:
        return (
            f'<div class="card"><h2>常去地点表 · {window} 天</h2>'
            '<div class="empty">近窗口内无达到“常去”门槛的地点</div></div>'
        )
    # 原始点数来源：按 place_id 全表补齐，避免因“常去地点仅保留 top N”而漏掉其余地点
    by_pid = {
        (p.get("place_id") or ""): dict(p)
        for p in (card.get("places") or []) if p.get("place_id")
    }
    for pid, c in _place_point_counts(conn, card.get("device_id") or "").items():
        by_pid.setdefault(pid, {})["point_count"] = c
    rows = ""
    for p in items:
        pid = p.get("place_id") or ""
        full = by_pid.get(pid) or {}
        pc = full.get("point_count")
        pc_txt = str(pc) if pc is not None else "-"
        week_n = p.get("weekday_visits") or 0
        end_n = p.get("weekend_visits") or 0
        wd_dist = (
            f"{week_n} / {end_n}"
            if (week_n or end_n)
            else "-"
        )
        rows += (
            f'<tr><td>{escape(_place_display(p.get("place_name"), p.get("user_tag")))}</td>'
            f'<td>{escape(p.get("user_tag") or "") or "-"}</td>'
            f'<td>{pc_txt}</td>'
            f'<td>{p.get("visit_days", 0)}</td>'
            f'<td>{p.get("visit_episodes", 0)}</td>'
            f'<td>{_fmt_dur(p.get("stay_ms"))} / {_fmt_dur(p.get("median_stay_ms"))}</td>'
            f'<td>{_last_seen_str(p.get("last_seen_ms"))}</td>'
            f'<td>{wd_dist}</td></tr>'
            # 每行挂该项自身 Evidence，不拿单个顶层样本代替全部指标
            f'<tr class="evtr"><td colspan="8">'
            f'{_render_evidence(p.get("evidence"), metric="常去地点")}'
            f'</td></tr>'
        )
    body = (
        f'<div class="card"><h2>常去地点表 · {window} 天</h2>'
        f'<table class="tbl"><tr>'
        f'<th>地点场景</th><th>tag</th><th>原始点数</th><th>到访天数</th>'
        f'<th>到访段数</th><th>停留时长（总 / 中位）</th><th>最近到访</th><th>工作日 / 周末（天）</th>'
        f'</tr>{rows}</table>'
        f'<div class="mig-note">原始点数=全历史 location 点数（按 place 全表补齐）；到访段数/停留时长按所选窗口聚合；'
        f'低于“常去”门槛的地点请见完整地点表。</div>'
        f'</div>'
    )
    return body


def _render_rhythm(sp: dict | None) -> str:
    """家/公司节奏：首次离家 / 到公司 / 离公司 / 最后回家 median/IQR 与有效样本日。"""
    if not _has_home_work(sp):
        return '<div class="card"><h2>家 / 公司节奏</h2><div class="empty">尚未确认家/公司</div></div>'
    rhythm = (sp or {}).get("home_work_rhythm")
    if not rhythm:
        return '<div class="card"><h2>家 / 公司节奏</h2><div class="empty">数据不足，无有效样本日</div></div>'
    wk = rhythm.get("weekday") or {}

    def _row(label, key):
        med = (wk.get(key) or {}).get("median_hhmm") or "-"
        iqr = (wk.get(key) or {}).get("iqr_hhmm") or "-"
        return f'<div class="row"><span class="name">{label}</span><span class="val">{med} ± {iqr}</span></div>'

    rows = "".join([
        _row("首次离家", "first_leave"),
        _row("到公司", "arrive_work"),
        _row("离公司", "leave_work"),
        _row("最后回家", "last_back"),
    ])
    home_med = (wk.get("home_ms") or {}).get("median_ms")
    work_med = (wk.get("work_ms") or {}).get("median_ms")
    rows += (
        f'<div class="row"><span class="name">在家时长中位</span>'
        f'<span class="val">{_fmt_dur(home_med) if home_med is not None else "-"}</span></div>'
        f'<div class="row"><span class="name">公司时长中位</span>'
        f'<span class="val">{_fmt_dur(work_med) if work_med is not None else "-"}</span></div>'
        f'<div class="row"><span class="name">有效样本日 / 缺测日</span>'
        f'<span class="val">{rhythm.get("anchor_days", 0)} / {rhythm.get("missing_days", 0)}</span></div>'
    )
    ev = rhythm.get("evidence") or {}
    wd = ev.get("window_days") or 30
    return (
        f'<div class="card"><h2>家 / 公司节奏（工作日 · {wd} 天）</h2>'
        f'{rows}{_render_evidence(rhythm.get("evidence"), metric="家/公司节奏")}'
        f'<div class="mig-note">{escape(rhythm.get("calendar_basis") or "")}</div></div>'
    )


def _render_scene(sp: dict | None) -> str:
    """场景暴露变化：poi_l1 当前窗口 vs 前窗口；旧窗口为 0 时不显示百分比。"""
    items = (sp or {}).get("scene_exposure") or []
    if not items:
        return '<div class="card"><h2>场景暴露变化</h2><div class="empty">数据不足，无场景暴露样本</div></div>'
    rows = ""
    for it in items:
        chg = it.get("change_pct")
        chg_txt = (
            f'{chg:+.0f}%'
            if chg is not None else "旧窗口为 0（不显示百分比）"
        )
        rows += (
            f'<tr><td>{escape(it.get("poi_l1") or "unknown")}</td>'
            f'<td>{it.get("cur_visit_days", 0)}</td>'
            f'<td>{it.get("cur_episodes", 0)}</td>'
            f'<td>{_fmt_dur(it.get("cur_stay_ms"))}</td>'
            f'<td>{it.get("prev_visit_days", 0)}</td>'
            f'<td>{_fmt_dur(it.get("prev_stay_ms"))}</td>'
            f'<td>{chg_txt}</td></tr>'
            # 每行挂该项自身 Evidence
            f'<tr class="evtr"><td colspan="7">'
            f'{_render_evidence(it.get("evidence"), metric="场景暴露")}'
            f'</td></tr>'
        )
    return (
        f'<div class="card"><h2>场景暴露变化（当前 30 天 vs 前 30 天）</h2>'
        f'<table class="tbl"><tr>'
        f'<th>地点场景（poi_l1）</th><th>当前到访天数</th><th>当前段数</th>'
        f'<th>当前时长</th><th>前窗口天数</th><th>前窗口时长</th><th>变化</th>'
        f'</tr>{rows}</table>'
        f'<div class="mig-note">归类口径=当前 places 语义（classification_basis=current_place_semantics），不作历史快照伪装。</div>'
        f'</div>'
    )


def _render_place_change(sp: dict | None) -> str:
    """地点变化：新 canonical 地点数 / 重复到访率 / 地点集合 Jaccard。"""
    pc = (sp or {}).get("place_change")
    if not pc:
        return '<div class="card"><h2>地点变化</h2><div class="empty">数据不足，无当前窗口地点样本</div></div>'
    jac = pc.get("place_set_jaccard")
    jac_txt = f"{jac:.2f}" if jac is not None else "-"
    rows = (
        f'<div class="row"><span class="name">新 canonical 地点数</span>'
        f'<span class="val">{pc.get("new_place_count", 0)}</span></div>'
        f'<div class="row"><span class="name">重复到访率</span>'
        f'<span class="val">{pc.get("repeat_visit_ratio", 0) * 100:.1f}%</span></div>'
        f'<div class="row"><span class="name">地点集合 Jaccard</span>'
        f'<span class="val">{jac_txt}</span></div>'
        f'<div class="row"><span class="name">当前 / 前窗口地点数</span>'
        f'<span class="val">{pc.get("cur_places", 0)} / {pc.get("prev_places", 0)}</span></div>'
    )
    return (
        f'<div class="card"><h2>地点变化</h2>{rows}'
        f'{_render_evidence(pc.get("evidence"), metric="地点变化")}</div>'
    )


def _render_time_space(sp: dict | None) -> str:
    """生活轨迹 · 时段分布（小时 × 地点类，30 天；Task 12d）。

    口径与 spatial_profile._time_space_matrix 一致：单小时 ≥15 分钟计入该地点、
    跨 midnight stay 按自然日分摊、无数据小时如实显示不隐藏。
    """
    ts = (sp or {}).get("time_space") or {}
    hours = ts.get("hours") or []
    if not hours:
        return (
            '<div class="card"><h2>生活轨迹 · 时段分布</h2>'
            '<div class="empty">数据不足，无法构建时段分布。</div></div>'
        )
    n_days = int(ts.get("days") or 0) or 1

    def _pct(v: int) -> str:
        p = v / n_days * 100
        return f"{p:.0f}%" if p else '<span class="dim">·</span>'

    rows = ""
    for it in hours:
        rows += (
            f'<tr><td>{it["hour"]:02d}:00</td>'
            f'<td style="color:var(--violet)">{_pct(it["home"])}</td>'
            f'<td style="color:var(--cyan)">{_pct(it["work"])}</td>'
            f'<td style="color:var(--amber)">{_pct(it["other"])}</td>'
            f'<td class="dim">{_pct(it["no_data"])}</td></tr>'
        )
    ev = _render_evidence(ts.get("evidence"), metric="时段分布")
    return (
        f'<div class="card"><h2>生活轨迹 · 时段分布（{ts.get("window_days", 30)} 天）</h2>'
        f'<table class="tbl"><tr><th>时段</th><th>家</th><th>公司</th><th>其他</th>'
        f'<th>无数据</th></tr>{rows}</table>'
        f'<div class="mig-note">每格 = 该小时处于此地点的天数占比；单小时 ≥15 分钟计入，'
        f'跨午夜 stay 按自然日分摊；同一小时可同时计入两类（地点转移过渡）；无数据不隐藏。</div>'
        f'{ev}</div>'
    )


def _render_migration(conn: sqlite3.Connection, device_id: str) -> str:
    """迁移审查（shadow）：mapping 旧→新、孤儿 stay、tag 冲突、geocode 失效、metrics。"""
    mapping_rows = ""
    try:
        maps = conn.execute(
            "SELECT run_id, old_place_id, new_place_id, match_reason, jaccard, distance_m "
            "FROM location_place_mapping WHERE old_device_id=? "
            "ORDER BY run_id DESC, old_place_id LIMIT 50",
            (device_id,),
        ).fetchall()
        for m in maps:
            jac_txt = "-" if m["jaccard"] is None else f'{m["jaccard"]:.2f}'
            mapping_rows += (
                f'<tr><td>{escape(m["old_place_id"])}</td>'
                f'<td>{escape(m["new_place_id"] or "（未匹配）")}</td>'
                f'<td>{escape(m["match_reason"] or "-")}</td>'
                f'<td>{jac_txt} / {_fmt_km(m["distance_m"])}</td></tr>'
            )
    except sqlite3.OperationalError:
        pass

    issues: dict[str, int] = {}
    orphan_n: int | None = None
    try:
        for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM location_migration_issues "
            "WHERE resolution_status='open' AND (device_id=? OR device_id IS NULL) "
            "GROUP BY kind",
            (device_id,),
        ):
            issues[r["kind"]] = r["n"]
    except sqlite3.OperationalError:
        pass
    # 孤儿 stay：正式 stays 引用了不存在 place 的 stay 数（NOT EXISTS 避免全表扫）
    try:
        orphan_n = conn.execute(
            "SELECT COUNT(*) FROM stays s WHERE s.place_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM places p WHERE p.place_id = s.place_id)"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        orphan_n = None

    metrics: dict[str, int] = {}
    try:
        for r in conn.execute(
            "SELECT metric, value FROM location_migration_metrics WHERE run_id = "
            "(SELECT run_id FROM location_migration_metrics ORDER BY rowid DESC LIMIT 1)"
        ):
            metrics[r["metric"]] = r["value"]
    except sqlite3.OperationalError:
        pass

    issue_txt = "、".join(f"{escape(k)}×{v}" for k, v in sorted(issues.items())) or "无"
    metric_txt = "、".join(
        f"{escape(k)}={v}" for k, v in sorted(metrics.items())
    ) or "无"
    orphan_txt = "未知（无 stays.place_id 列）" if orphan_n is None else str(orphan_n)

    if not mapping_rows and not issues and not metrics:
        return (
            '<div class="card"><h2>迁移审查（shadow）</h2>'
            '<div class="empty">无 shadow 迁移记录（尚未执行位置迁移）</div></div>'
        )
    return (
        f'<div class="card"><h2>迁移审查（shadow）</h2>'
        f'<div class="row"><span class="name">旧→新地点映射（前 50）</span>'
        f'<span class="val">{"见下表" if mapping_rows else "无"}</span></div>'
        f'<table class="tbl"><tr><th>旧 place_id</th><th>新 place_id</th>'
        f'<th>匹配依据（match_reason）</th><th>Jaccard / 距离</th></tr>'
        f'{mapping_rows or "<tr><td colspan=4 class=\"empty\">无映射</td></tr>"}</table>'
        f'<div class="row"><span class="name">孤儿 stay</span><span class="val">{orphan_txt}</span></div>'
        f'<div class="row"><span class="name">未解决迁移 issue</span><span class="val">{issue_txt}</span></div>'
        f'<div class="row"><span class="name">迁移 metrics（最新 run）</span><span class="val">{metric_txt}</span></div>'
        f'</div>'
    )


def render_dashboard_html(
    conn: sqlite3.Connection,
    day: str | None = None,
    device_id: str | None = None,
    window: int = 30,
) -> str:
    conn.row_factory = sqlite3.Row

    today = datetime.datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    requested = day or today
    if window not in (7, 30, 90):
        window = 30

    # 日期导航：合并 daily_stats / stays / anomalies；显式请求日即使不在列表也保留
    days = _collect_days(conn)
    nav_days = days
    if requested not in nav_days:
        nav_days = [requested] + nav_days

    def _q(**extra) -> str:
        params = {"day": requested, "window": window}
        if device_id:
            params["device_id"] = device_id
        params.update(extra)
        return "&".join(
            f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items()
        )

    nav = "".join(
        f'<a href="/dashboard?{_q(day=d)}" class="{"on" if d == requested else ""}">{escape(d)}</a>'
        for d in nav_days
    )
    win_nav = "".join(
        f'<a href="/dashboard?{_q(window=w)}" class="{"on" if w == window else ""}">{w} 天</a>'
        for w in (7, 30, 90)
    )
    nav += f'<span class="dim" style="align-self:center">窗口</span>{win_nav}'

    # 事实卡片：一次 build，全程不触发 ETL；spatial_profile 由 fact_card 注入，不重复计算
    try:
        card = fact_card.build(
            conn=conn, day=requested, device_id=device_id,
            detail="full", outlet="dashboard",
        )
    except Exception:  # noqa: BLE001 - 缺库/损坏时降级为读取失败
        card = None

    if card is None:
        body = ('<div class="card"><h2>读取失败</h2>'
                '<div class="empty">数据读取失败，请稍后重试。</div></div>')
    elif card.get("ambiguous_device"):
        cpart = "、".join(escape(str(c)) for c in card.get("candidate_device_ids") or [])
        links = "".join(
            f'<a href="/dashboard?{_q(device_id=str(c))}">{escape(str(c))}</a> &nbsp;'
            for c in (card.get("candidate_device_ids") or [])
        ) or cpart
        body = (
            f'<div class="card"><h2>多设备</h2>'
            f'<div class="empty">检测到多台设备，未指定 device_id：不合并画像。'
            f'请选择设备后查看：{links}</div></div>'
        )
    else:
        dev = card.get("device_id") or ""
        sp = card.get("spatial_profile") or {}
        body = _render_fact_review(card)

        # 唯一设备确定后才执行设备级统计查询（全部按 device_id 过滤）
        total_h = card.get("screen_hours", 0)
        ncount = card.get("notification_count", 0)
        nclick = card.get("notification_clicked", 0)
        click_rate = nclick / max(1, ncount) * 100
        stats = f"""
        <div class="grid">
          <div class="stat"><div class="v" style="color:var(--cyan)">{total_h:.2f}h</div><div class="l">屏幕时间</div></div>
          <div class="stat"><div class="v" style="color:var(--amber)">{ncount}</div><div class="l">通知 / 点击率 {click_rate:.0f}%</div></div>
          <div class="stat"><div class="v" style="color:var(--green)">{card.get("unlock_count", 0)}</div><div class="l">解锁次数</div></div>
          <div class="stat"><div class="v" style="color:var(--violet)">{card.get("switch_count", 0)}</div><div class="l">切换次数</div></div>
        </div>"""

        ranking = card.get("top_apps") or []
        max_ms = ranking[0]["ms"] if ranking else 1
        app_rows = ""
        for i, a in enumerate(ranking[:8]):
            pct = a["ms"] / max_ms * 100
            app_rows += (
                f'<div class="row"><span class="name">{escape(a["app"])}</span>'
                f'<div class="bar-wrap"><div class="bar" style="width:{pct:.0f}%;background:{_app_color(i)}"></div></div>'
                f'<span class="val" style="margin-left:12px;width:70px;text-align:right">{_fmt_dur(a["ms"])}</span></div>'
            )

        notif_apps = card.get("top_notification_apps") or []
        notif_rows = "".join(
            f'<div class="row"><span class="name">{escape(a["app"])}</span>'
            f'<span class="val">{a["n"]} 条</span></div>' for a in notif_apps
        ) or '<div class="empty">无通知</div>'

        hours = _hours_activity(conn, dev, requested)
        max_h = max(hours) if any(hours) else 1
        hcells = ""
        for hi, n in enumerate(hours):
            hgt = max(3, n / max_h * 100)
            color = "var(--cyan)" if n > 0 else "#161D2E"
            hcells += (
                f'<div class="hcell" data-n="{hi}:00 {n}条" '
                f'style="height:{hgt:.0f}%;background:{color}"></div>'
            )

        recent = conn.execute(
            "SELECT app, duration_ms, start_ms FROM sessions "
            "WHERE day=? AND device_id=? ORDER BY start_ms DESC LIMIT 8",
            (requested, dev),
        ).fetchall()
        recent_rows = "".join(
            f'<tr><td>{_fmt_time(r["start_ms"])}</td><td>{escape(r["app"])}</td>'
            f'<td>{_fmt_dur(r["duration_ms"])}</td></tr>' for r in recent
        ) or '<tr><td colspan="3">无</td></tr>'

        body += f"""
        {stats}
        <div class="card"><h2>24 小时活动</h2><div class="hbar">{hcells}</div></div>
        <div class="grid" style="grid-template-columns:1fr 1fr">
          <div class="card"><h2>App 使用排行</h2>{app_rows}</div>
          <div><div class="card"><h2>通知来源</h2>{notif_rows}</div>
            <div class="card"><h2>最近会话</h2>
              <table class="tbl"><tr><th>时间</th><th>App</th><th>时长</th></tr>{recent_rows}</table>
            </div>
          </div>
        </div>
        {_render_arrivals(conn, dev, requested)}
        {_render_persona(card.get("persona"))}
        {_render_coverage(conn)}
        {_render_location_health(conn, dev, requested)}
        {_render_time_space(sp)}
        {_render_kpi30(sp)}
        {_render_frequent_places(conn, card, sp, window)}
        {_render_rhythm(sp)}
        {_render_scene(sp)}
        {_render_place_change(sp)}
        {_render_migration(conn, dev)}
        """

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>langTrack · {escape(requested)}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>langTrack 数据仪表盘</h1>
<div class="sub">自用数据监控 · 数据源: events + ETL 事实表</div>
<div class="nav">{nav}</div>
<div class="etl-row"><button id="etl-btn" onclick="runEtl(this)">立即转换 (ETL)</button><span id="etl-status" class="dim"></span></div>
<script>
function runEtl(btn){{
  var s = document.getElementById('etl-status');
  btn.disabled = true;
  s.textContent = 'ETL 启动中…';
  fetch('/etl/run', {{method: 'POST'}}).then(function(r){{return r.json();}}).then(function(j){{
    if (j.status === 'busy') {{ s.textContent = '已有 ETL 在运行…'; }}
    var t = setInterval(function(){{
      fetch('/etl/status').then(function(r){{return r.json();}}).then(function(st){{
        if (!st.running) {{
          clearInterval(t);
          s.textContent = '上次 ETL ' + (st.last_finished_at || '') +
            (st.last_ok === true ? ' 成功' : (st.last_ok === false ? ' 失败' : '')) + '，刷新页面…';
          location.reload();
        }} else {{ s.textContent = 'ETL 运行中…'; }}
      }});
    }}, 2000);
  }}).catch(function(){{ btn.disabled = false; s.textContent = '触发失败，请重试'; }});
}}
</script>
{body}
<div class="sub" style="margin-top:30px">© 场景标签为 ETL 逆地理编码结果</div>
</body></html>"""


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    print(render_dashboard_html(conn))
    conn.close()
