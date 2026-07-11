import sqlite3
import tempfile
import os
from pathlib import Path
from profile.db.init_db import get_connection, init_db


def test_task_runs_table_exists():
    """init_db 后 task_runs 表应存在"""
    init_db()
    conn = get_connection()
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
        ).fetchone()
        assert result is not None
        assert result["name"] == "task_runs"
    finally:
        conn.close()


def test_llm_calls_table_exists():
    """init_db 后 llm_calls 表应存在"""
    init_db()
    conn = get_connection()
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_calls'"
        ).fetchone()
        assert result is not None
        assert result["name"] == "llm_calls"
    finally:
        conn.close()


def test_llm_calls_columns():
    """llm_calls 表应有正确的列"""
    init_db()
    conn = get_connection()
    try:
        cols = conn.execute("PRAGMA table_info(llm_calls)").fetchall()
        col_names = [c["name"] for c in cols]
        assert "task_run_id" in col_names
        assert "step" in col_names
        assert "model" in col_names
        assert "system_prompt" in col_names
        assert "user_prompt" in col_names
        assert "response" in col_names
        assert "prompt_tokens" in col_names
        assert "completion_tokens" in col_names
        assert "total_tokens" in col_names
        assert "elapsed_ms" in col_names
        assert "success" in col_names
        assert "error_message" in col_names
        assert "called_at" in col_names
        assert "finished_at" in col_names
    finally:
        conn.close()
