"""区间闭合（S2，修正设计债①）：按 ``valid_from`` 全量重排后重算 ``valid_to``。

上游设计把"新边 valid_from 早于旧边"当错误**拒绝写入**；但手机数据批量迟到是常态
（实测一次补发上万条）。本实现改为：取该 ``(from, to, relation)`` 的全部边**按 valid_from 排序**
后重算 ``valid_to``（旧边 valid_to = 下一条 valid_from，末条为 NULL），**接受乱序、不拒绝**。

只改 ``valid_to``（派生闭合列），不动边的其它字段；``invalidated=1`` 的边不参与闭合。
"""

from __future__ import annotations

import sqlite3
from typing import Final

_SELECT_EDGES: Final = """
SELECT edge_id, valid_from
FROM kg_edges
WHERE from_node = ? AND to_node = ? AND relation = ? AND invalidated = 0
ORDER BY (valid_from IS NULL), valid_from, edge_id
"""

_SET_VALID_TO: Final = """
UPDATE kg_edges
SET valid_to = ?, updated_at = datetime('now', '+8 hours')
WHERE edge_id = ?
"""


def reclose_intervals(conn: sqlite3.Connection, from_node: str, to_node: str, relation: str) -> int:
    """重算某 ``(from, to, relation)`` 组的 valid_to；返回处理的边数。"""
    rows = conn.execute(_SELECT_EDGES, (from_node, to_node, relation)).fetchall()
    if not rows:
        return 0
    with conn:
        for idx, (edge_id, _valid_from) in enumerate(rows):
            nxt = rows[idx + 1][1] if idx + 1 < len(rows) else None
            conn.execute(_SET_VALID_TO, (nxt, edge_id))
    return len(rows)


def reclose_day(conn: sqlite3.Connection, day: str) -> int:
    """对当日出现过的所有 ``(from, to, relation)`` 组逐一闭合（补跑乱序后重算）。"""
    triples = conn.execute(
        "SELECT DISTINCT from_node, to_node, relation FROM kg_edges WHERE day = ?", (day,)
    ).fetchall()
    total = 0
    for from_node, to_node, relation in triples:
        total += reclose_intervals(conn, str(from_node), str(to_node), str(relation))
    return total
