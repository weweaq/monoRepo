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
"""


def migrate_extract_tables(conn: sqlite3.Connection) -> None:
    """幂等建表：全部 ``CREATE ... IF NOT EXISTS``，连跑多次表结构不变。"""
    conn.executescript(EXTRACT_SCHEMA)
    conn.commit()
