"""KG 抽取表结构（R6 合规）：kg_runs / kg_nodes / kg_edges。

与 etl.py 的 ``_SCHEMA`` 完全解耦：本模块只建 ``kg_`` 前缀表，只增不改，
既不进 ``_migrate_fact_tables`` 的 ``fact_tables`` 列表，也不参与位置 v1/v2 冻结逻辑。

S1 建三张表（runs / nodes / edges）；S2 追加 kg_aliases、kg_relations；S3 追加
kg_emotions / kg_patterns / kg_worldview——同文件同风格，全部 CREATE IF NOT EXISTS。
"""

from __future__ import annotations

import sqlite3
from typing import Final

EXTRACT_SCHEMA: Final = """
-- 一次抽取运行的血缘与幂等锚
CREATE TABLE IF NOT EXISTS kg_runs (
  run_id        TEXT PRIMARY KEY,
  day           TEXT NOT NULL,
  raw_hash      TEXT NOT NULL,
  prompt_ver    TEXT NOT NULL,
  status        TEXT NOT NULL,
  channel_stats TEXT NOT NULL DEFAULT '{}',
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  created_at    TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at    TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_kg_runs_day_hash_ver ON kg_runs(day, raw_hash, prompt_ver);

-- 实体节点
CREATE TABLE IF NOT EXISTS kg_nodes (
  node_id    TEXT PRIMARY KEY,
  node_type  TEXT NOT NULL,
  label      TEXT NOT NULL,
  canonical  TEXT,
  aliases    TEXT NOT NULL DEFAULT '[]',
  first_day  TEXT NOT NULL,
  last_day   TEXT NOT NULL,
  src        TEXT NOT NULL,
  confidence REAL,
  run_id     TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_type  ON kg_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_label ON kg_nodes(label);

-- 边（append-only；edge_id 由内容确定性派生 → 重跑天然幂等）
CREATE TABLE IF NOT EXISTS kg_edges (
  edge_id        TEXT PRIMARY KEY,
  from_node      TEXT NOT NULL,
  to_node        TEXT NOT NULL,
  relation       TEXT NOT NULL,
  edge_level     TEXT NOT NULL,
  attributor     TEXT,
  assertion_text TEXT,
  event_time     TEXT,
  ingest_time    TEXT,
  valid_from     TEXT,
  valid_to       TEXT,
  supersedes     TEXT,
  superseded_at  TEXT,
  invalidated    INTEGER NOT NULL DEFAULT 0,
  confidence     REAL,
  day            TEXT NOT NULL,
  src            TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  created_at     TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at     TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_edges_from ON kg_edges(from_node, relation, valid_from);
CREATE INDEX IF NOT EXISTS idx_kg_edges_to   ON kg_edges(to_node,   relation, valid_from);
CREATE INDEX IF NOT EXISTS idx_kg_edges_day  ON kg_edges(day);

-- 稳定关系（人际/实体间长期关联；与 kg_edges 的"当日事件边"区分，不参与 interval closure）
CREATE TABLE IF NOT EXISTS kg_relations (
  relation_id    TEXT PRIMARY KEY,            -- sha1(day|from|to|relation)[:16]
  from_node      TEXT NOT NULL,
  to_node        TEXT NOT NULL,
  relation       TEXT NOT NULL,               -- colleague_of / family_of / friend_of / follows ...
  edge_level     TEXT NOT NULL,               -- L1 事实 | L2 观点 | L3 推断
  attributor     TEXT,                        -- L2 必填
  assertion_text TEXT,
  evidence       TEXT,                        -- 素材内证据片段（<=40 字）
  strength       REAL,                        -- 关系强度/置信 0..1（L3 上限 0.6）
  valid_from     TEXT,
  valid_to       TEXT,
  day            TEXT NOT NULL,
  src            TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  created_at     TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at     TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_relations_from ON kg_relations(from_node, relation);
CREATE INDEX IF NOT EXISTS idx_kg_relations_to   ON kg_relations(to_node,   relation);
CREATE INDEX IF NOT EXISTS idx_kg_relations_day  ON kg_relations(day);

-- 别名（追加式，可撤销 —— 修正设计债②）
-- superseded_by 取**反向指针**语义：本行撤销的旧记录 key（alias|day|run_id），NULL=未撤销任何记录。
-- 之所以反向，是因为"旧行指向新行"必须 UPDATE 旧行，与"只追加、旧记录永不改"铁律冲突（见 docs/kg-extract-plan.md §6 ②）。
CREATE TABLE IF NOT EXISTS kg_aliases (
  alias         TEXT NOT NULL,
  canonical     TEXT NOT NULL,
  method        TEXT NOT NULL,                -- exact|fuzzy|manual
  score         REAL,
  day           TEXT NOT NULL,
  src           TEXT,
  superseded_by TEXT,
  run_id        TEXT NOT NULL,
  created_at    TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at    TEXT DEFAULT (datetime('now', '+8 hours')),
  PRIMARY KEY (alias, day, run_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_aliases_alias ON kg_aliases(alias);

-- 情绪信号（S3）：谁表现出什么情绪；日粒度事实，不参与 interval closure
CREATE TABLE IF NOT EXISTS kg_emotions (
  emotion_id   TEXT PRIMARY KEY,              -- sha1(day|subject|emotion|evidence)[:16]
  subject      TEXT NOT NULL,                 -- 情绪主体 node_id
  emotion      TEXT NOT NULL,                 -- 焦虑/愉悦/疲惫/平静/愤怒 ...
  valence      REAL,                          -- 效价 -1..1
  arousal      REAL,                          -- 唤醒 0..1
  intensity    REAL,                          -- 强度 0..1
  trigger_text TEXT,                          -- 触发事件描述
  evidence     TEXT,                          -- 素材内证据片段（<=40 字）
  confidence   REAL,                          -- 0..1
  day          TEXT NOT NULL,
  src          TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  created_at   TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at   TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_emotions_subject ON kg_emotions(subject, day);
CREATE INDEX IF NOT EXISTS idx_kg_emotions_day     ON kg_emotions(day);

-- 行为/偏好模式候选（S3）：只产候选，confidence 上限 0.7；status 恒 candidate
-- （候选→规律升格机制属后续独立阶段，见 docs/kg-extract-plan.md §12）
CREATE TABLE IF NOT EXISTS kg_patterns (
  pattern_id    TEXT PRIMARY KEY,             -- sha1(day|subject|pattern)[:16]
  subject       TEXT NOT NULL,
  pattern       TEXT NOT NULL,                -- 模式描述
  evidence      TEXT,
  support_count INTEGER,                      -- 支撑证据条数
  confidence    REAL NOT NULL,                -- 上限 0.7
  status        TEXT NOT NULL DEFAULT 'candidate',
  day           TEXT NOT NULL,
  src           TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  created_at    TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at    TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_patterns_subject ON kg_patterns(subject, day);
CREATE INDEX IF NOT EXISTS idx_kg_patterns_day     ON kg_patterns(day);

-- 世界观/信念候选（S3）：只产候选，confidence 上限 0.6；status 恒 candidate
CREATE TABLE IF NOT EXISTS kg_worldview (
  view_id       TEXT PRIMARY KEY,             -- sha1(day|subject|statement)[:16]
  subject       TEXT NOT NULL,
  statement     TEXT NOT NULL,                -- 世界观/信念陈述
  domain        TEXT,                         -- 领域：工作/技术/人际/价值 ...
  evidence      TEXT,
  confidence    REAL NOT NULL,                -- 上限 0.6
  status        TEXT NOT NULL DEFAULT 'candidate',
  day           TEXT NOT NULL,
  src           TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  created_at    TEXT DEFAULT (datetime('now', '+8 hours')),
  updated_at    TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_worldview_subject ON kg_worldview(subject, day);
CREATE INDEX IF NOT EXISTS idx_kg_worldview_day     ON kg_worldview(day);
"""


def migrate_extract_tables(conn: sqlite3.Connection) -> None:
    """幂等建表：全部 ``CREATE ... IF NOT EXISTS``，连跑多次表结构不变。"""
    conn.executescript(EXTRACT_SCHEMA)
    _migrate_kg_alias_review_cols(conn)
    ensure_entity_views(conn)
    conn.commit()


def _migrate_kg_alias_review_cols(conn: sqlite3.Connection) -> None:
    """kg_aliases 补 reviewer / reason 人工审计列（设计 §8.1）。

    R6 存在性探测迁移：``PRAGMA table_info`` 探测缺列才 ALTER，幂等。
    不递增 ``PRAGMA user_version``——该版本号被 location v2 占用，KG schema 沿用存在性探测约定。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(kg_aliases)")}
    if "reviewer" not in cols:
        conn.execute("ALTER TABLE kg_aliases ADD COLUMN reviewer TEXT")
    if "reason" not in cols:
        conn.execute("ALTER TABLE kg_aliases ADD COLUMN reason TEXT")


# 实体身份投影视图（设计 §4.3）：raw 永不改写，kg_aliases 是唯一身份账本，
# 视图把 alias 链折到 terminal canonical 供所有消费方读取。
# 视图无数据，改定义 = DROP 后重建（ensure_entity_views 内处理）。
ENTITY_VIEWS: Final = """
-- 当前生效的 manual/self/linker 决策：每 alias 至多一条，manual 压过 self/linker，
-- 自指行（撤销标记）排除，被反向指针（superseded_by）引用的旧行排除。
CREATE VIEW IF NOT EXISTS kg_alias_active AS
WITH live AS (
  SELECT a.alias, a.canonical, a.method, a.score, a.day, a.run_id, a.reviewer, a.reason,
         ROW_NUMBER() OVER (
           PARTITION BY a.alias
           ORDER BY CASE a.method
                      WHEN 'manual' THEN 0
                      WHEN 'self'   THEN 1
                      ELSE 2
                    END,
                    a.day DESC, a.run_id DESC
         ) AS rn
  FROM kg_aliases a
  WHERE a.method IN ('manual', 'self', 'linker')
    AND a.alias <> a.canonical
    AND NOT EXISTS (
      SELECT 1 FROM kg_aliases b
      WHERE b.superseded_by = a.alias || '|' || a.day || '|' || a.run_id
    )
)
SELECT alias, canonical, method, score, day, run_id, reviewer, reason
FROM live WHERE rn = 1;

-- 递归解析 alias 链：每个 raw node → terminal canonical（深度上限 8 兜底环）。
CREATE VIEW IF NOT EXISTS kg_entity_map AS
WITH RECURSIVE walk(node_id, entity_id, depth) AS (
  SELECT n.node_id, COALESCE(a.canonical, n.node_id), 1
  FROM kg_nodes n
  LEFT JOIN kg_alias_active a ON a.alias = n.node_id
  UNION ALL
  SELECT w.node_id, a.canonical, w.depth + 1
  FROM walk w
  JOIN kg_alias_active a ON a.alias = w.entity_id
  WHERE w.depth < 8
)
SELECT node_id, entity_id
FROM walk w
WHERE NOT EXISTS (
  SELECT 1 FROM kg_alias_active a WHERE a.alias = w.entity_id
)
AND w.depth = (
  SELECT MAX(w2.depth) FROM walk w2 WHERE w2.node_id = w.node_id
);

-- resolved 投影：成员清单 / 实体聚合 / 边与稳定关系（自环标记）。
CREATE VIEW IF NOT EXISTS kg_resolved_node_members AS
SELECT n.node_id, m.entity_id, n.node_type, n.label, n.first_day, n.last_day, n.src
FROM kg_nodes n
JOIN kg_entity_map m ON m.node_id = n.node_id;

CREATE VIEW IF NOT EXISTS kg_resolved_nodes AS
SELECT m.entity_id, e.node_type, e.label,
       MIN(n.first_day) AS first_day,
       MAX(n.last_day)  AS last_day,
       COUNT(*)         AS member_count,
       group_concat(n.node_id, ',') AS node_ids
FROM kg_nodes n
JOIN kg_entity_map m ON m.node_id = n.node_id
JOIN kg_nodes e ON e.node_id = m.entity_id
GROUP BY m.entity_id, e.node_type, e.label;

CREATE VIEW IF NOT EXISTS kg_resolved_edges AS
SELECT e.edge_id,
       mf.entity_id AS from_entity,
       mt.entity_id AS to_entity,
       e.relation, e.edge_level,
       e.attributor, ma.entity_id AS attributor_entity,
       e.assertion_text, e.event_time, e.valid_from, e.valid_to,
       e.invalidated, e.confidence, e.day, e.src, e.run_id,
       (mf.entity_id = mt.entity_id) AS is_self_loop
FROM kg_edges e
JOIN kg_entity_map mf ON mf.node_id = e.from_node
JOIN kg_entity_map mt ON mt.node_id = e.to_node
LEFT JOIN kg_entity_map ma ON ma.node_id = e.attributor;

-- 聚合视图：同 (from,to,relation) 折一条，代表文本取最新一条（不是 MAX 字典序）。
CREATE VIEW IF NOT EXISTS kg_resolved_edges_agg AS
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY from_entity, to_entity, relation
           ORDER BY day DESC, edge_id DESC
         ) AS rn
  FROM kg_resolved_edges
  WHERE invalidated = 0 AND is_self_loop = 0
)
SELECT from_entity, to_entity, relation,
       MIN(day) AS first_day,
       MAX(day) AS last_day,
       COUNT(*) AS support_count,
       MAX(CASE WHEN rn = 1 THEN edge_id END)        AS latest_edge_id,
       MAX(CASE WHEN rn = 1 THEN assertion_text END) AS latest_assertion_text
FROM ranked
GROUP BY from_entity, to_entity, relation;

CREATE VIEW IF NOT EXISTS kg_resolved_relations AS
SELECT r.relation_id,
       mf.entity_id AS from_entity,
       mt.entity_id AS to_entity,
       r.relation, r.edge_level,
       r.attributor, ma.entity_id AS attributor_entity,
       r.assertion_text, r.evidence, r.strength,
       r.valid_from, r.valid_to, r.day, r.src, r.run_id,
       (mf.entity_id = mt.entity_id) AS is_self_loop
FROM kg_relations r
JOIN kg_entity_map mf ON mf.node_id = r.from_node
JOIN kg_entity_map mt ON mt.node_id = r.to_node
LEFT JOIN kg_entity_map ma ON ma.node_id = r.attributor;
"""

_VIEW_NAMES: Final = (
    "kg_resolved_relations",
    "kg_resolved_edges_agg",
    "kg_resolved_edges",
    "kg_resolved_nodes",
    "kg_resolved_node_members",
    "kg_entity_map",
    "kg_alias_active",
)


def ensure_entity_views(conn: sqlite3.Connection) -> None:
    """建/刷新实体投影视图（幂等）。

    ``CREATE VIEW IF NOT EXISTS`` 只建不更新；改定义时先 DROP 再建（视图无数据，安全，设计 §8.1）。
    """
    conn.executescript("DROP VIEW IF EXISTS " + ";\nDROP VIEW IF EXISTS ".join(_VIEW_NAMES) + ";")
    conn.executescript(ENTITY_VIEWS)
