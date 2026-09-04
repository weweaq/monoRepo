# Task 2 Report: Portal db_store (task_runs + llm_calls CRUD)

## Status: DONE

## Summary

Created the `profile/portal` package with an empty `__init__.py` and a
`db_store.py` data-access module implementing full CRUD for the `task_runs` and
`llm_calls` tables. Wrote 6 TDD tests in `tests/test_portal_db_store.py`. All 6
tests pass; Task 1's 3 schema tests still pass (9/9 portal tests green).

## What I Implemented (per brief step)

### Step 1: 创建 portal 包的 __init__.py
Created `profile/portal/__init__.py` (empty file, 0 bytes).

### Step 2: 编写 db_store 测试
Created `tests/test_portal_db_store.py` with the exact 6 tests from the brief:
`test_insert_and_get_task_run`, `test_update_task_run`,
`test_query_task_runs_pagination`, `test_insert_and_get_llm_call`,
`test_query_llm_calls_filter`, `test_recover_stale_tasks`. Each test is preceded
by `setup_function()` which calls `init_db()` then `DELETE`s all rows from
`llm_calls` and `task_runs` for isolation.

### Step 3: 运行测试验证失败
`pytest tests/test_portal_db_store.py -v` => **collection error** (Red):
`ModuleNotFoundError: No module named 'profile.portal.db_store'`. Confirmed the
module did not yet exist.

### Step 4: 实现 db_store.py
Created `profile/portal/db_store.py` (verbatim from the brief) implementing all
8 produced functions:
- `insert_task_run`, `update_task_run`, `get_task_run`, `query_task_runs`,
  `recover_stale_tasks` (task_runs)
- `insert_llm_call`, `get_llm_call`, `query_llm_calls` (llm_calls)
- helpers: `_row_to_dict`, `_parse_json`, `_to_json_str`
All consume `profile.db.init_db.get_connection` and use parameterized `?`
placeholders for values.

### Step 5: 运行测试验证通过
`pytest tests/test_portal_db_store.py -v` => **6 passed** (Green).

### Step 6: 提交
Staged exactly the 3 specified files and committed (no unrelated changes
included).

## TDD Process

Followed Red-Green:
1. Wrote `tests/test_portal_db_store.py` first (plus empty `__init__.py`).
2. Ran tests => **Red**: `ModuleNotFoundError: No module named
   'profile.portal.db_store'` (collection error).
3. Implemented `profile/portal/db_store.py`.
4. Ran tests => **Green**: 6 passed.

## Test / Verification Results

| Check | Result |
|-------|--------|
| `pytest tests/test_portal_db_store.py -v` (Red, pre-impl) | 1 collection error — `ModuleNotFoundError` |
| `pytest tests/test_portal_db_store.py -v` (Green, post-impl) | 6 passed |
| `pytest tests/test_portal_db_store.py tests/test_portal_db_schema.py -v` | 9 passed (no Task 1 regression) |
| Functions exported by `db_store` | 8 (matches brief "Produces" list) |

## Files Changed

| File | Action | Commit |
|------|--------|--------|
| `profile/portal/__init__.py` | Created (empty) | 69b1a22 |
| `profile/portal/db_store.py` | Created (192 lines, 8 funcs + 3 helpers) | 69b1a22 |
| `tests/test_portal_db_store.py` | Created (86 lines, 6 tests) | 69b1a22 |

Commit: `69b1a22` — `feat(portal): add db_store for task_runs and llm_calls CRUD`
(3 files changed, 278 insertions(+))

## Self-Review Findings

- Implementation matches the brief verbatim (function signatures, SQL, helper
  logic, JSON parse/serialize behavior). All 8 produced functions present.
- TDD Red-Green followed: tests failed first with the expected
  `ModuleNotFoundError`, then passed after implementation.
- Only the 3 specified files were staged/committed; the many unrelated
  pre-existing modifications on `feat/portal` (aggregator.py, log.py, analysis
  modules, docs, etc.) were left untouched — consistent with Task 1's approach.
- `setup_function()` clears both tables before every test, giving per-test
  isolation despite operating on the real DB.
- `query_task_runs`/`query_llm_calls` return `{items, total, page, page_size}`
  with `ORDER BY id DESC` + `LIMIT/OFFSET` pagination — matches the test
  assertions (page 1 size 3 -> 3 items; page 2 -> 2 items; total 5).
- `recover_stale_tasks()` flips `running`->`failed` with message
  `'portal 重启中断'` (contains `'重启'`), matching the test.
- Task 1's `test_portal_db_schema.py` still passes (3/3) — no regression.

## Concerns

1. **`_to_json_str` defined after first use**: The helper is defined at the
   bottom of `db_store.py` but called inside `insert_task_run`/`update_task_run`
   above. This works because Python resolves module-level names at call time
   (the whole module is loaded before any function is called), and the passing
   tests confirm it. Slightly unusual ordering but kept verbatim per the brief.

2. **`update_task_run` builds column names via f-string**: Column names are
   interpolated from `**kwargs` keys (`f"UPDATE task_runs SET {', '.join(sets)}
   WHERE id = ?"`). Values still use `?` placeholders so there is no value
   injection risk, but an invalid column name would raise
   `sqlite3.OperationalError` at runtime with no upfront validation. Per brief.

3. **Tests operate on the real DB**: As noted in Task 1, the brief's tests call
   `init_db()` against the real `data/profile.db` (not an isolated temp DB).
   `setup_function()` mitigates this by clearing rows before each test. Left
   as-is to match the brief exactly.

4. **CRLF line-ending warnings**: Git warns `LF will be replaced by CRLF` for
   the new files. This is cosmetic (Windows default Git behavior) and identical
   to Task 1; no functional impact.
