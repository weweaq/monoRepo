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
    conn.commit()
