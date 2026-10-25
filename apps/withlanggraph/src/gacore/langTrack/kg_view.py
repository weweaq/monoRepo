"""KG 可视化：只读 ``kg_*`` 快照，渲染三个自包含页面。

设计约束（与 demo 原型的区别）：

- **只读**：只执行 SELECT；不建表、不写库、不触发抽取。
- **外挂式**：本模块不依赖 server/dashboard；``server.py`` 只加一条薄路由。
- **自包含**：CSS/JS/cytoscape 全部内联，不请求 CDN；页面可单独导出后双击打开。
- **三视图**：``overview``（全景力导图）/ ``timeline``（按抽取日回放）/ ``audit``（图谱体检）。

入口：

- FastAPI：``GET /kg?view=overview|timeline|audit``（见 ``server.py``）
- 静态导出：``python -m gacore.langTrack.kg_view --out-dir data/kg_view``
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Final

from gacore.langTrack import kg_view_assets as assets

APP_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = APP_ROOT / "data" / "langTrack.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"
_TZ = timezone(timedelta(hours=8))

VIEWS: Final = ("overview", "timeline", "audit")
VIEW_FILES: Final = {
    "overview": "kg_overview.html",
    "timeline": "kg_timeline.html",
    "audit": "kg_audit.html",
}
VIEW_TITLES: Final = {
    "overview": "KG 全景图 · langTrack",
    "timeline": "KG 演化 · langTrack",
    "audit": "KG 体检 · langTrack",
}

TYPE_LABEL = {
    "Person": "人物",
    "Event": "事件",
    "Theme": "主题",
    "Project": "项目",
    "Observation": "观察",
    "Axiom": "信条",
    "Unknown": "未知",
}
TYPE_COLOR = {
    "Person": "#2dd4bf",
    "Event": "#60a5fa",
    "Theme": "#f59e0b",
    "Project": "#f472b6",
    "Observation": "#a78bfa",
    "Axiom": "#e5e7eb",
    "Unknown": "#64748b",
}
TYPE_SHAPE = {
    "Person": "ellipse",
    "Event": "round-rectangle",
    "Theme": "diamond",
    "Project": "hexagon",
    "Observation": "octagon",
    "Axiom": "triangle",
    "Unknown": "ellipse",
}
LEVEL_LABEL = {"L1": "事实", "L2": "观点", "L3": "推断"}


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def load_kg_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read the KG tables into one JSON-serializable snapshot.

    The queries mirror the prototype generator. They are intentionally read-only
    and tolerate missing/unknown values; a missing table is a hard error because
    the route cannot render a meaningful graph without the schema.
    """
    conn.row_factory = sqlite3.Row

    nodes: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT node_id,node_type,label,canonical,aliases,first_day,last_day,src,confidence FROM kg_nodes"
    ):
        nodes.append(
            {
                "id": r["node_id"],
                "type": r["node_type"] or "Unknown",
                "label": r["label"] or r["node_id"],
                "canonical": r["canonical"] or "",
                "aliases": _json(r["aliases"], []),
                "first_day": r["first_day"],
                "last_day": r["last_day"],
                "src": r["src"],
                "confidence": r["confidence"],
            }
        )

    raw_edges: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT edge_id,from_node,to_node,relation,edge_level,attributor,assertion_text,"
        "valid_from,valid_to,invalidated,confidence,day,src FROM kg_edges"
    ):
        raw_edges.append(
            {
                "id": r["edge_id"],
                "from": r["from_node"],
                "to": r["to_node"],
                "relation": r["relation"],
                "level": r["edge_level"],
                "attributor": r["attributor"] or "",
                "assertion": r["assertion_text"] or "",
                "evidence": "",
                "strength": None,
                "valid_from": r["valid_from"],
                "valid_to": r["valid_to"],
                "invalidated": int(r["invalidated"] or 0),
                "day": r["day"],
                "src": r["src"],
                "kind": "event" if r["src"] == "events" else "todo",
            }
        )

    rel_rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT relation_id,from_node,to_node,relation,edge_level,attributor,assertion_text,"
        "evidence,strength,valid_from,valid_to,day FROM kg_relations ORDER BY day, rowid"
    ):
        rel_rows.append(
            {
                "id": r["relation_id"],
                "from": r["from_node"],
                "to": r["to_node"],
                "relation": r["relation"],
                "level": r["edge_level"],
                "attributor": r["attributor"] or "",
                "assertion": r["assertion_text"] or "",
                "evidence": r["evidence"] or "",
                "strength": r["strength"],
                "valid_from": r["valid_from"],
                "valid_to": r["valid_to"],
                "invalidated": 0,
                "day": r["day"],
                "src": "relations",
                "kind": "relation",
            }
        )

    # Stable relations are appended per day; collapse to one row per
    # (from,to,relation) using the latest day so the canvas stays readable.
    rel_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rel_rows:
        key = (row["from"], row["to"], row["relation"])
        prev = rel_map.get(key)
        # The query is ordered by (day,rowid); still compare days explicitly so
        # the retained row is the latest one even if the query changes later.
        if prev is None or (row["day"] or "") >= (prev["day"] or ""):
            rel_map[key] = row
    rel_edges = list(rel_map.values())

    edges = raw_edges + rel_edges

    degree: Counter[str] = Counter()
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
    for n in nodes:
        n["degree"] = degree.get(n["id"], 0)
        n["isolated"] = n["degree"] == 0

    # Chain links: exactly one edge per unordered node pair. Prefer L1, then
    # strength, then the latest day, so a chain shows one readable arrow.
    pair_best: dict[tuple[str, str], dict[str, Any]] = {}
    for e in edges:
        key = tuple(sorted((e["from"], e["to"])))
        score = (1 if e["level"] == "L1" else 0, e["strength"] or 0, e["day"] or "")
        current = pair_best.get(key)
        if current is None:
            pair_best[key] = {**e, "_score": score, "alts": 1}
        elif score > current["_score"]:
            pair_best[key] = {**e, "_score": score, "alts": current["alts"] + 1}
        else:
            current["alts"] += 1
    links = [{k: v for k, v in row.items() if k != "_score"} for row in pair_best.values()]

    alias_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT alias,canonical,method,score,day,run_id,superseded_by FROM kg_aliases"
        )
    ]
    # day 建表 NOT NULL；归一空值只为防御外来库，排序/比较统一安全
    for row in alias_rows:
        row["day"] = row["day"] or ""
    for row in alias_rows:
        row["active"] = not any(
            other["superseded_by"] == f"{row['alias']}|{row['day']}|{row['run_id']}"
            for other in alias_rows
        )

    by_alias: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in alias_rows:
        if row["active"]:
            by_alias[row["alias"]].append(row)

    effective: dict[str, str] = {}
    for alias, rows in by_alias.items():
        for row in sorted(rows, key=lambda x: (x["day"], x["run_id"]), reverse=True):
            score = float(row["score"] or 0)
            if row["method"] == "fuzzy" and score < 0.85:
                continue
            effective[alias] = row["canonical"]
            break

    def ultimate(node_id: str) -> str:
        seen = {node_id}
        cur = node_id
        while cur in effective:
            nxt = effective[cur]
            if nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        return cur

    label_by_id = {n["id"]: n["label"] for n in nodes}
    alias_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for row in alias_rows:
        key = (row["alias"], row["canonical"])
        prev = alias_pairs.get(key)
        if prev is None or row["day"] > prev["day"]:
            alias_pairs[key] = row

    alias_effective = [
        {
            "alias": alias,
            "canonical": canonical,
            "final": ultimate(alias),
            "alias_label": label_by_id.get(alias, alias),
            "canonical_label": label_by_id.get(canonical, canonical),
            "final_label": label_by_id.get(ultimate(alias), ultimate(alias)),
        }
        for alias, canonical in sorted(effective.items())
    ]

    alias_candidates = [
        {
            "alias": row["alias"],
            "canonical": row["canonical"],
            "score": round(float(row["score"] or 0), 3),
            "day": row["day"],
            "alias_label": label_by_id.get(row["alias"], row["alias"]),
            "canonical_label": label_by_id.get(row["canonical"], row["canonical"]),
        }
        for row in alias_pairs.values()
        if row["method"] == "fuzzy" and float(row["score"] or 0) < 0.85
    ]
    alias_candidates.sort(key=lambda x: x["score"], reverse=True)

    dup_groups: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        if n["type"] == "Person":
            dup_groups[n["label"]].append(n["id"])
    degree_by_id = {n["id"]: n["degree"] for n in nodes}
    duplicates = [
        {
            "label": label,
            "ids": ids,
            "degrees": [degree_by_id[i] for i in ids],
        }
        for label, ids in dup_groups.items()
        if len(ids) > 1
    ]
    duplicates.sort(key=lambda x: x["label"])

    runs: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT run_id,day,status,channel_stats,started_at,finished_at FROM kg_runs ORDER BY day,started_at"
    ):
        stats = _json(r["channel_stats"], {})
        if not isinstance(stats, dict):
            stats = {}
        material = stats.get("_material") or {}
        if not isinstance(material, dict):
            material = {}
        channels = []
        for key in ("events", "todos", "relations", "emotions", "patterns", "worldview"):
            st = stats.get(key)
            if not isinstance(st, dict):
                continue
            channels.append(
                {
                    "key": key,
                    "status": st.get("status", ""),
                    "rows": st.get("rows", 0),
                    "error": st.get("error", ""),
                }
            )
        runs.append(
            {
                "run_id": r["run_id"],
                "day": r["day"],
                "status": r["status"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"] or "",
                "material_chars": material.get("chars", 0),
                "sources": material.get("sources", []),
                "channels": channels,
                "extra": [k for k in stats if not k.startswith("_")],
                "reap": (stats.get("_reap") if isinstance(stats.get("_reap"), dict) else {}),
            }
        )

    days = sorted({r["day"] for r in runs})
    new_nodes = Counter(n["first_day"] for n in nodes)
    # Count the deduplicated edge set, not raw relation rows: stable relations
    # are re-appended every day and would otherwise make "new" exceed "total".
    new_edges = Counter(e["day"] for e in edges)
    day_stats = []
    for day in days:
        day_stats.append(
            {
                "day": day,
                "new_nodes": new_nodes.get(day, 0),
                "new_edges": new_edges.get(day, 0),
                "nodes_total": sum(1 for n in nodes if n["first_day"] <= day),
                "edges_total": sum(1 for e in edges if e["day"] <= day),
            }
        )

    run_by_day: dict[str, dict[str, Any]] = {}
    for run in sorted(runs, key=lambda x: x["started_at"]):
        run_by_day[run["day"]] = run

    isolated = [n for n in nodes if n["isolated"]]
    isolated_by_type = Counter(n["type"] for n in isolated)
    type_dist = Counter(n["type"] for n in nodes)
    level_dist = Counter(e["level"] for e in edges)
    relation_dist = Counter(e["relation"] for e in edges)

    return {
        "generated_at": datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M"),
        "source": "langTrack.db",
        "nodes": nodes,
        "edges": edges,
        "links": links,
        "days": days,
        "day_stats": day_stats,
        "runs": [run_by_day[d] for d in days if d in run_by_day],
        "aliases": {
            "rows": len(alias_rows),
            "unique_pairs": len(alias_pairs),
            "effective": alias_effective,
            "candidates": alias_candidates,
            "duplicate_groups": duplicates,
        },
        "audit": {
            "isolated": {
                "count": len(isolated),
                "by_type": dict(isolated_by_type),
                "sample": [
                    {"id": n["id"], "label": n["label"], "type": n["type"], "first_day": n["first_day"]}
                    for n in sorted(isolated, key=lambda x: (x["type"], x["label"]))[:120]
                ],
            },
            "type_dist": dict(type_dist),
            "level_dist": dict(level_dist),
            "relation_dist": dict(relation_dist.most_common(18)),
            "counts": {
                "nodes": len(nodes),
                "edges": len(raw_edges),
                "relation_rows": len(rel_rows),
                "relation_unique": len(rel_edges),
                "isolated": len(isolated),
                "alias_rows": len(alias_rows),
                "alias_pairs": len(alias_pairs),
                "alias_effective": len(alias_effective),
                "alias_candidates": len(alias_candidates),
                "duplicate_groups": len(duplicates),
            },
        },
        "meta": {
            "type_label": TYPE_LABEL,
            "type_color": TYPE_COLOR,
            "type_shape": TYPE_SHAPE,
            "level_label": LEVEL_LABEL,
        },
    }


def _data_tag(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/json" id="kg-data">{raw}</script>'


def _nav(view: str, *, static_links: bool, available_views: tuple[str, ...] | None = None) -> str:
    items = (
        ("overview", "全景图"),
        ("timeline", "时间演化"),
        ("audit", "图谱体检"),
    )
    links = []
    for key, label in items:
        if static_links and available_views is not None and key not in available_views:
            links.append(f'<span class="kgoff" title="本次未导出">{label}</span>')
            continue
        href = VIEW_FILES[key] if static_links else f"/kg?view={key}"
        cls = " class=\"on\"" if key == view else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<nav class="kgnav">' + "".join(links) + "</nav>"


@lru_cache(maxsize=1)
def _cytoscape_js() -> str:
    path = STATIC_DIR / "cytoscape.min.js"
    if not path.exists():
        raise FileNotFoundError(f"missing vendored cytoscape asset: {path}")
    return path.read_text(encoding="utf-8")


def _page(
    *,
    title: str,
    css: str,
    body: str,
    js: str,
    data: dict[str, Any],
    nav: str,
    include_cytoscape: bool,
) -> str:
    graph_lib = f"<script>{_cytoscape_js()}</script>" if include_cytoscape else ""
    rendered_body = body.replace("{{KG_NAV}}", nav)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>{assets.NAV_CSS}{css}</style>
</head>
<body>
{rendered_body}
{graph_lib}
{_data_tag(data)}
<script>{assets.COMMON}{js}</script>
</body>
</html>
"""


def render_kg_html(
    conn: sqlite3.Connection,
    view: str = "overview",
    *,
    static_links: bool = False,
    available_views: tuple[str, ...] | None = None,
) -> str:
    """Render one self-contained KG page.

    ``view`` falls back to ``overview`` for unknown values so a bad query string
    never turns into a 500. ``static_links=True`` uses relative file names for
    the CLI export; the FastAPI route keeps ``/kg?view=...`` links.
    ``available_views`` only affects the static nav: views not exported in this
    run render as a dimmed placeholder instead of a dead link.
    """
    target = view if view in VIEWS else "overview"
    data = load_kg_snapshot(conn)
    nav = _nav(target, static_links=static_links, available_views=available_views)

    if target == "overview":
        return _page(
            title=VIEW_TITLES[target],
            css=assets.D1_CSS,
            body=assets.D1_BODY,
            js=assets.D1_JS,
            data=data,
            nav=nav,
            include_cytoscape=True,
        )
    if target == "timeline":
        return _page(
            title=VIEW_TITLES[target],
            css=assets.D3_CSS,
            body=assets.D3_BODY,
            js=assets.D3_JS,
            data=data,
            nav=nav,
            include_cytoscape=True,
        )
    return _page(
        title=VIEW_TITLES[target],
        css=assets.D4_CSS,
        body=assets.D4_BODY,
        js=assets.D4_JS,
        data=data,
        nav=nav,
        include_cytoscape=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the three KG visualization pages.")
    parser.add_argument("--db", default=str(DB_PATH), help="path to langTrack.db (read-only)")
    parser.add_argument("--out-dir", default=str(APP_ROOT / "data" / "kg_view"), help="output directory")
    parser.add_argument("--view", choices=(*VIEWS, "all"), default="all", help="view to export (default: all)")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}")
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        targets = VIEWS if args.view == "all" else (args.view,)
        for target in targets:
            html = render_kg_html(conn, target, static_links=True, available_views=tuple(targets))
            path = out_dir / VIEW_FILES[target]
            path.write_text(html, encoding="utf-8")
            print(f"{path}  {path.stat().st_size / 1024:.0f} KB")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
