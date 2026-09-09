"""pgvector persistence for gacore long-term memory (阶段二).

Bridges the local PostgreSQL/pgvector instance (already provisioned — the ``vector``
extension is enabled on the ``postgres`` DB) with the memory-maintenance pipeline.
Portrait lines are embedded with gacore.embedding and stored as ``vector`` rows so a
later user message can recall semantically-related portrait facts for the judge.

Connection details follow the deepface convention already in this repo
(``gacore/modules/database/pgvector.py``): a postgres-style DSN or explicit kwargs,
overridable via env. Defaults target the local ``postgres`` DB with the documented
credentials.

Schema (created idempotently)::

    gacore_memory_vectors (
        id          bigserial PRIMARY KEY,
        content     text NOT NULL,
        chunk_key   text          -- which portrait file / source the line came from
        embedding   vector(512)   -- bge-small-zh-v1.5
        updated_at  timestamptz
    )

Conventions honoured: ``updated_at`` in UTC/east-8 like the rest of the data layer,
no destructive rebuilds (a new portrait line INSERTs, never replaces an old one —
history preserved, matching memory_maintenance's MERGE-append philosophy).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Final, Iterator

from gacore.config import Config
from gacore.embedding import batch_encode, encode
from gacore.jsonl_logger import get_logger

_logger = get_logger("vector_store")

# deepface-managed credentials; env overrides keep local defaults working without a .env.
_DEFAULT_DSN: Final = "postgresql://postgres:123789@127.0.0.1:5432/postgres"
_ENV_DSN: Final = "GACORE_PG_DSN"

_TABLE = "gacore_memory_vectors"
_DIM = 512
# Curated "fact portrait": short, retrieval-friendly statements (one fact per line) kept
# separately from the verbose, timestamped event log in global_mem*.txt. Feeding the
# vector store ONLY this file yields high-precision semantic recall — a log line like
# "[2026-09-08T23:53:15+08:00] 生活大事：婚期提前定档..." is noisy for embedding, while
# "婚期：2026-09-12 领证" matches queries like "搬家/婚期/住哪" far better (阶段二 方案2).
_FACTS_FILE: Final = "global_mem_facts.txt"


def _dsn() -> str:
    return os.environ.get(_ENV_DSN, _DEFAULT_DSN)


def _connect():
    try:
        import psycopg  # optional dependency (vector extra)
        from pgvector.psycopg import register_vector  # optional dependency (vector extra)
    except ImportError as exc:  # pragma: no cover - vector extra required to reach here
        raise RuntimeError("psycopg/pgvector is not installed; add the gacore 'vector' extra") from exc
    conn = psycopg.connect(_dsn())
    register_vector(conn)  # enables python list <-> vector param binding
    return conn


@contextmanager
def _cursor(*_args, commit: bool = False) -> Iterator:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def ensure_schema(dim: int = _DIM) -> None:
    """Create the vector table + pgvector extension if absent (idempotent)."""
    with _cursor(commit=True) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "id bigserial PRIMARY KEY,"
            "content text NOT NULL,"
            "chunk_key text,"
            f"embedding vector({dim}),"
            "updated_at timestamptz DEFAULT now(),"
            "UNIQUE (content, chunk_key)"
            ");"
        )
        _logger.info("vector schema ensured", table=_TABLE, dim=dim)


def upsert_line(content: str, chunk_key: str, embedding: list[float] | None = None) -> None:
    """Store one portrait line with its embedding (dedup by content+chunk_key)."""
    if embedding is None:
        embedding = encode(content)  # expensive path reserved for callers without a precomputed vec
    if not embedding:
        return
    with _cursor(commit=True) as cur:
        cur.execute(
            f"INSERT INTO {_TABLE} (content, chunk_key, embedding, updated_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT DO NOTHING;",
            (content, chunk_key, embedding),
        )


def nearby(query: str, k: int = 3, threshold: float = 0.5) -> list[dict]:
    """Return portrait lines semantically closest to ``query`` (cosine distance ascending).

    ``threshold`` filters weak matches (0.0 = exact same vector, 1.0+ = unrelated).
    Returns rows with keys ``content``, ``chunk_key``, ``dist``, or an empty list when no
    similar line clears the bar (callers treat that as "no anchor evocation").
    """
    qvec = encode(query)
    if not qvec:
        return []
    with _cursor() as cur:
        cur.execute(
            f"SELECT content, chunk_key, embedding <=> %s::vector AS dist "
            f"FROM {_TABLE} WHERE embedding <=> %s::vector < %s ORDER BY dist LIMIT %s;",
            (qvec, qvec, threshold, k),
        )
        rows = cur.fetchall()
    return [{"content": r[0], "chunk_key": r[1], "dist": float(r[2])} for r in rows]


def sync_portrait(cfg: Config) -> dict:
    """Embed the current long-term portrait and (re)store every distinct line.

    Runs the whole portrait through the batch encoder once and upserts each line. Cheap,
    idempotent, safe to call after a MERGE/NEW write so the vector store tracks the
    portrait in near-real-time.
    """
    ensure_schema()
    lines = _portrait_lines(cfg)
    vectors = batch_encode(lines) if lines else []
    stored = 0
    for content, vec in zip(lines, vectors, strict=False):
        if not vec:
            continue
        upsert_line(content, chunk_key=_source_of(content), embedding=vec)
        stored += 1
    _logger.info("portrait synced", lines=len(lines), stored=stored)
    return {"lines": len(lines), "stored": stored}


def _portrait_lines(cfg: Config, limit: int = 2000) -> list[str]:
    """Flatten the portrait into a deduped list of non-empty lines.

    Prefers the curated facts file (``global_mem_facts.txt``) when it exists — short,
    retrieval-friendly statements give the vector store clean semantic recall. Falls back
    to the verbose timestamped event log (``global_mem*.txt``) when no facts file exists
    yet, so the pipeline degrades gracefully before the facts portrait is seeded.
    """
    seen: set[str] = set()
    out: list[str] = []
    facts = cfg.memory_dir / _FACTS_FILE
    sources = [facts] if facts.is_file() else sorted(cfg.memory_dir.glob("global_mem*.txt"))
    for path in sources:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line and line not in seen:
                seen.add(line)
                out.append(line)
            if len(out) >= limit:
                break
    return out


def _source_of(line: str) -> str:
    """Bucket a portrait line by a light heuristic for chunk_key (insight header vs fact)."""
    if "insight" in line or "观察" in line:
        return "insight"
    return "fact"


__all__ = ("ensure_schema", "nearby", "sync_portrait", "upsert_line", "_cursor")