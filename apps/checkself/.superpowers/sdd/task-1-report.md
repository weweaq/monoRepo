# Task 1 Report: 依赖与数据库 Schema (task_runs + llm_calls)

## Status: DONE_WITH_CONCERNS

## Summary

Added `fastapi`/`uvicorn` dependencies to `pyproject.toml`, appended the `task_runs`
and `llm_calls` table DDLs to `profile/db/init_db.py`'s `_SCHEMA`, and wrote 3 TDD
tests in `tests/test_portal_db_schema.py`. All 3 new tests pass; the two new tables
now exist in `data/profile.db` via `init_db()`.

## What I Implemented (per brief step)

### Step 1: 添加 fastapi, uvicorn 依赖到 pyproject.toml
Changed `dependencies = []` to `dependencies = ["fastapi>=0.100", "uvicorn>=0.20"]`
in `pyproject.toml`.

### Step 2: 安装新依赖
Installed `fastapi==0.139.0` and `uvicorn==0.51.0` (plus transitive deps: starlette,
pydantic, anyio, h11, etc.) into the project venv. Verified importable via
`python -c "import fastapi, uvicorn"`.

> Note: the brief specifies `.venv\Scripts\pip install ...`, but this venv is
> uv-managed (`uv venv` created, no pip bootstrapped, `uv.lock` present). I used
> `uv pip install fastapi uvicorn` instead, which installs into the same venv and
> achieves the identical result. See Concerns.

### Step 3: 在 init_db.py 的 _SCHEMA 中追加两张表 DDL
Appended the `task_runs` (12 columns) and `llm_calls` (15 columns, with FK to
`task_runs.id`) table DDLs to the `_SCHEMA` string in `profile/db/init_db.py`,
immediately after the `llm_intents` table's `);`. DDL text matches the brief
verbatim (comments, column names, types, FK).

### Step 4: 编写测试
Created `tests/test_portal_db_schema.py` with the exact 3 tests from the brief:
`test_task_runs_table_exists`, `test_llm_calls_table_exists`, `test_llm_calls_columns`.

### Step 5: 运行测试
`pytest tests/test_portal_db_schema.py -v` => 3 passed.

### Step 6: 提交
Committed only the 3 task files (no unrelated pre-existing changes included).

## TDD Process

Followed Red-Green:
1. Wrote `tests/test_portal_db_schema.py` first.
2. Ran tests => **3 failed** (Red): `task_runs`/`llm_calls` tables did not exist
   (`assert None is not None`, `assert 'task_run_id' in []`).
3. Implemented the DDLs in `init_db.py`.
4. Ran tests => **3 passed** (Green).

## Test / Verification Results

| Check | Result |
|-------|--------|
| `pytest tests/test_portal_db_schema.py -v` | 3 passed |
| `import fastapi, uvicorn` | fastapi 0.139.0, uvicorn 0.51.0 |
| DB tables in `data/profile.db` | `task_runs`, `llm_calls` present |
| `task_runs` columns | 12 cols, matches brief |
| `llm_calls` columns | 15 cols (incl. `task_run_id` FK), matches brief |
| Full suite `pytest -v` | 15 passed, 4 failed (pre-existing, see Concerns) |

## Files Changed

| File | Action | Commit |
|------|--------|--------|
| `pyproject.toml` | Modified (added deps) | c420732 |
| `profile/db/init_db.py` | Modified (appended 2 table DDLs to `_SCHEMA`) | c420732 |
| `tests/test_portal_db_schema.py` | Created (3 tests) | c420732 |

Commit: `c420732` — `feat(portal): add task_runs and llm_calls tables + fastapi/uvicorn deps`
(3 files changed, 95 insertions(+), 1 deletion(-))

## Self-Review Findings

- DDL text matches the brief byte-for-byte (column names, types, FK, comments).
- Only the 3 specified files were staged/committed; unrelated pre-existing
  modifications on the `feat/portal` branch (aggregator.py, log.py, new analysis
  modules, docs, etc.) were left unstaged.
- `init_db()` is idempotent (`CREATE TABLE IF NOT EXISTS`), so re-running on the
  existing `data/profile.db` is safe and does not drop/alter existing tables.
- The `init_db()` COUNT logging still only queries `raw_data` and `llm_intents`
  (unchanged), which is fine — the new tables have no rows yet.

## Concerns

1. **Dependency install method differs from brief**: The venv has no `pip`
   (it is uv-managed). Used `uv pip install fastapi uvicorn` instead of
   `.venv\Scripts\pip install ...`. Functionally equivalent; packages are
   installed and importable in the venv.

2. **`uv.lock` not regenerated**: `pyproject.toml` now declares fastapi/uvicorn,
   but `uv.lock` (currently untracked) was not refreshed. A future `uv lock` /
   `uv sync` is needed to keep the lockfile consistent for reproducible installs.

3. **Pre-existing test failures (unrelated)**: 4 tests fail on `feat/portal` —
   `test_activity.py::test_single_source_traces_through`,
   `test_direction.py::test_analyze_weekly_trends`,
   `test_topic.py::test_analyze_returns_top_words`,
   `test_topic.py::test_analyze_categorizes`. These test analysis modules
   (`profile/analysis/*`) modified/added on this branch and are independent of
   this task's schema changes. They were failing before this commit.

4. **Tests operate on the real DB**: The brief's tests call `init_db()` against
   the real `data/profile.db` (not an isolated temp DB). This is per-spec and
   safe (idempotent DDL), but the tests are not hermetic. Left as-is to match
   the brief.

5. **Unused imports in test file**: The brief's test code imports `tempfile`,
   `os`, and `pathlib.Path` but only uses `sqlite3`. Kept verbatim to match the
   brief exactly.
