"""实体消解（S2）：把同一实体的不同写法收敛到 canonical，只追加、绝不改旧记录。

三级（计划 §5 S2）：
① exact  —— 名称/别名精确相同 → 直接建映射（method='exact'）；
② fuzzy  —— ``weighted_score = 0.4*jaro_winkler + 0.3*context_overlap + 0.2*temporal_cooccurrence
   + 0.1*same_surname``，``≥0.85`` 自动合并（生效），``0.6~0.85`` 写候选（不生效）；
③ manual —— 人工确认（method='manual'，始终生效）。

追加式（修正设计债②）：``kg_aliases`` 只 INSERT，绝不 UPDATE/DELETE 旧行。
撤销 = 追加一条 ``superseded_by`` 指向被撤销旧行 key 的新记录（反向指针，见 ``schema.py`` 注释）。
**生效判定**：某行生效 ⟺ 没有其它行通过 ``superseded_by`` 引用它；同一 alias 取 (day, run_id) 最新。

本模块只维护映射表，不改写 ``kg_nodes``/``kg_edges``——消解结果由查询侧按需消费。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

AUTO_MERGE: Final = 0.85
CANDIDATE_MIN: Final = 0.6

_WEIGHT_JW: Final = 0.4
_WEIGHT_CONTEXT: Final = 0.3
_WEIGHT_TEMPORAL: Final = 0.2
_WEIGHT_SURNAME: Final = 0.1

# 某行生效 ⟺ 不被任何其它行的 superseded_by 引用（superseded_by 是反向指针）。
_ACTIVE_SQL: Final = """
SELECT canonical, method, score, day, run_id
FROM kg_aliases a
WHERE a.alias = ?
  AND NOT EXISTS (
    SELECT 1 FROM kg_aliases b
    WHERE b.superseded_by = a.alias || '|' || a.day || '|' || a.run_id
  )
ORDER BY a.day DESC, a.run_id DESC
"""

_INSERT_ALIAS: Final = """
INSERT OR IGNORE INTO kg_aliases (alias, canonical, method, score, day, src, superseded_by, run_id)
VALUES (:alias, :canonical, :method, :score, :day, :src, :superseded_by, :run_id)
"""


def alias_key(alias: str, day: str, run_id: str) -> str:
    """一行别名记录的 key（用于 ``superseded_by`` 反向指针）。"""
    return f"{alias}|{day}|{run_id}"


def _jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    match_dist = max(0, max(len1, len2) // 2 - 1)
    s1_hit = [False] * len1
    s2_hit = [False] * len2
    matches = 0
    for i in range(len1):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, len2)
        for j in range(lo, hi):
            if s2_hit[j] or s1[i] != s2[j]:
                continue
            s1_hit[i] = s2_hit[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i in range(len1):
        if not s1_hit[i]:
            continue
        while not s2_hit[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0


def jaro_winkler(s1: str, s2: str, scaling: float = 0.1) -> float:
    """Jaro-Winkler 相似度（前缀加成上限 4 字符）。纯本地实现，不引第三方依赖（R2）。"""
    j = _jaro(s1, s2)
    prefix = 0
    for c1, c2 in zip(s1, s2):
        if c1 != c2 or prefix == 4:
            break
        prefix += 1
    return j + prefix * scaling * (1.0 - j)


def _person_nodes(conn: sqlite3.Connection) -> dict[str, str]:
    """全部 Person 节点的 node_id → label（消解只针对人）。"""
    rows = conn.execute(
        "SELECT node_id, label FROM kg_nodes WHERE node_type = 'Person'"
    ).fetchall()
    return {str(node_id): str(label or "") for node_id, label in rows}


def _neighbors(conn: sqlite3.Connection, day: str) -> dict[str, set[str]]:
    """当日 kg_edges 的邻接表（语境重叠 / 共现的原料）。"""
    out: dict[str, set[str]] = {}
    for from_node, to_node in conn.execute(
        "SELECT from_node, to_node FROM kg_edges WHERE day = ?", (day,)
    ).fetchall():
        out.setdefault(str(from_node), set()).add(str(to_node))
        out.setdefault(str(to_node), set()).add(str(from_node))
    return out


def _score(label_a: str, label_b: str, nb_a: set[str], nb_b: set[str]) -> float:
    jw = jaro_winkler(label_a, label_b)
    union = nb_a | nb_b
    context = len(nb_a & nb_b) / len(union) if union else 0.0
    temporal = 1.0 if (nb_a & nb_b) else 0.0
    surname = 1.0 if (label_a[:1] and label_a[:1] == label_b[:1]) else 0.0
    return _WEIGHT_JW * jw + _WEIGHT_CONTEXT * context + _WEIGHT_TEMPORAL * temporal + _WEIGHT_SURNAME * surname


def _insert_alias(
    conn: sqlite3.Connection,
    *,
    alias: str,
    canonical: str,
    method: str,
    score: float,
    day: str,
    run_id: str,
    superseded_by: str | None = None,
) -> int:
    return conn.execute(
        _INSERT_ALIAS,
        {
            "alias": alias,
            "canonical": canonical,
            "method": method,
            "score": score,
            "day": day,
            "src": "resolve",
            "superseded_by": superseded_by,
            "run_id": run_id,
        },
    ).rowcount


def _active_rows(conn: sqlite3.Connection, alias: str) -> list[tuple[Any, ...]]:
    return conn.execute(_ACTIVE_SQL, (alias,)).fetchall()


def resolve_alias(conn: sqlite3.Connection, alias: str) -> str | None:
    """取 alias 当前生效的 canonical；候选（0.6≤score<0.85 的 fuzzy）不生效。无则 None。"""
    for canonical, method, score, _day, _run_id in _active_rows(conn, alias):
        if method == "fuzzy" and (score is None or float(score) < AUTO_MERGE):
            continue
        return str(canonical)
    return None


def revoke_alias(conn: sqlite3.Connection, alias: str, day: str, run_id: str) -> bool:
    """撤销 alias 当前生效的映射：追加一条反向指针记录，使旧映射失效（旧行不改）。

    撤销后 alias 生效映射指向自身（即"未合并"）。
    """
    rows = conn.execute(
        "SELECT day, run_id FROM kg_aliases a WHERE a.alias = ? "
        "AND NOT EXISTS (SELECT 1 FROM kg_aliases b "
        "WHERE b.superseded_by = a.alias || '|' || a.day || '|' || a.run_id) "
        "ORDER BY a.day DESC, a.run_id DESC LIMIT 1",
        (alias,),
    ).fetchone()
    if not rows:
        return False
    with conn:
        _insert_alias(
            conn,
            alias=alias,
            canonical=alias,
            method="manual",
            score=1.0,
            day=day,
            run_id=run_id,
            superseded_by=alias_key(alias, str(rows[0]), str(rows[1])),
        )
    return True


def resolve_entities(conn: sqlite3.Connection, day: str, run_id: str) -> None:
    """对全部 Person 节点跑三级消解，把结果**追加**进 ``kg_aliases``（幂等：INSERT OR IGNORE）。"""
    nodes = _person_nodes(conn)
    if len(nodes) < 2:
        return
    neighbors = _neighbors(conn, day)
    ordered = sorted(nodes)
    with conn:
        # ① exact：同名者合并到字典序最小的 node_id
        by_label: dict[str, list[str]] = {}
        for node_id in ordered:
            by_label.setdefault(nodes[node_id], []).append(node_id)
        for label, ids in by_label.items():
            if not label or len(ids) < 2:
                continue
            canonical = ids[0]
            for alias in ids[1:]:
                _insert_alias(
                    conn, alias=alias, canonical=canonical, method="exact",
                    score=1.0, day=day, run_id=run_id,
                )
        # ② fuzzy：两两打分，≥0.85 生效合并，0.6~0.85 写候选
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                score = _score(
                    nodes[a], nodes[b], neighbors.get(a, set()), neighbors.get(b, set())
                )
                if score < CANDIDATE_MIN:
                    continue
                _insert_alias(
                    conn, alias=b, canonical=a, method="fuzzy",
                    score=score, day=day, run_id=run_id,
                )
