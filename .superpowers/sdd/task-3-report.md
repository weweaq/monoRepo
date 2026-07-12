# Task 3 Report: LLM Client 埋点 (contextvars)

## Status: DONE

## Summary

Created `profile/portal/task_engine.py` with the contextvars layer
(`set_task_context`, `clear_task_context`, `get_current_task_run_id`,
`get_current_step`) and instrumented `profile/llm/client.py` so every
`LLMClient.chat()` call — success or failure — is recorded into the `llm_calls`
table via a new module-level `_record_llm_call()` helper. Wrote 2 TDD tests in
`tests/test_llm_tracking.py`. Both new tests pass; Task 1's 3 schema tests and
Task 2's 6 CRUD tests still pass (11/11 portal tests green).

## What I Implemented (per brief step)

### Step 1: 创建 task_engine.py 的 contextvars 部分
Created `profile/portal/task_engine.py` verbatim from the brief. The file
declares two `contextvars.ContextVar` instances (`_current_task_run_id`,
`_current_step`, both default `None`) and the four produced functions:
- `set_task_context(task_run_id, step=None)` — sets both vars
- `clear_task_context()` — resets both vars to `None`
- `get_current_task_run_id()` — returns the current task_run_id (or `None`)
- `get_current_step()` — returns the current step (or `None`)

The module also keeps the imports (`threading`, `ThreadPoolExecutor`, `deque`,
`json`, `logging`, `time`, `datetime`, and the `db_store` CRUD functions) from
the brief's template; these are unused by the contextvars part but are required
by Task 4's TaskRunner, so they were kept verbatim per the brief.

### Step 2: 编写埋点测试
Created `tests/test_llm_tracking.py` with the exact 2 tests from the brief:
`test_llm_call_recorded_on_success` and `test_llm_call_recorded_on_failure`.
`setup_function()` calls `init_db()` then `DELETE`s all rows from `llm_calls`
and `task_runs` for per-test isolation. Each test sets a task context via
`set_task_context(task_run_id=None, step=...)`, mocks `urllib.request.urlopen`,
calls `client.chat(...)`, and asserts the recorded row's fields.

### Step 3: 运行测试验证失败
`pytest tests/test_llm_tracking.py -v` => **2 FAILED** (Red). Both failed with
`assert 0 == 1` on `calls["total"]` — `chat()` worked (returned `"hello"` /
raised `RuntimeError`) but recorded nothing, exactly as expected pre-impl.

### Step 4: 在 client.py 中加埋点
Modified `profile/llm/client.py`:
- Added top-level import:
  `from profile.portal.task_engine import get_current_task_run_id, get_current_step`
- Success branch: inserted a `_record_llm_call(...)` call after
  `logger.debug("LLM 响应全文"...)` and before `return content.strip()` —
  records `response=content`, `usage=usage`, `success=True`.
- `HTTPError` except block: inserted a `_record_llm_call(...)` call before
  `raise RuntimeError(...)` — records `response=None`, `usage={}`,
  `success=False`, `error_message=f"HTTP {e.code}: {error_body[:200]}"`.
- `URLError` except block: inserted a `_record_llm_call(...)` call before
  `raise RuntimeError(...)` — records `response=None`, `usage={}`,
  `success=False`, `error_message=f"网络错误: {e}"`.
- Added module-level `_record_llm_call()` function before `class JsonParseError`:
  does a deferred local `from profile.portal.db_store import insert_llm_call`
  inside a `try/except Exception: pass` so tracking failure never breaks the
  main call flow, and maps `success` bool -> `1`/`0` for the DB column.

### Step 5: 运行测试验证通过
`pytest tests/test_llm_tracking.py -v` => **2 passed** (Green).
`pytest tests/test_llm_tracking.py tests/test_portal_db_store.py tests/test_portal_db_schema.py -v`
=> **11 passed** (no Task 1/Task 2 regression).

### Step 6: 提交
Staged exactly the 3 specified files and committed (no unrelated changes
included).

## TDD Process

Followed Red-Green:
1. Wrote `tests/test_llm_tracking.py` first.
2. Ran tests => **Red**: 2 failed with `assert 0 == 1` (no `llm_calls` rows).
3. Implemented `task_engine.py` + instrumented `client.py`.
4. Ran tests => **Green**: 2 passed.

## Test / Verification Results

| Check | Result |
|-------|--------|
| `pytest tests/test_llm_tracking.py -v` (Red, pre-impl) | 2 failed — `assert 0 == 1` (no rows recorded) |
| `pytest tests/test_llm_tracking.py -v` (Green, post-impl) | 2 passed |
| `pytest tests/test_llm_tracking.py tests/test_portal_db_store.py tests/test_portal_db_schema.py -v` | 11 passed (no regression) |
| `python -c "import profile.llm.client, profile.portal.task_engine"` | OK (no circular import) |
| contextvars set/get/clear sanity check | `set(42,'s1')` -> `(42,'s1')`; `clear()` -> `(None,None)` |
| `_record_llm_call` present on client module | True |
| `HTTPError(fp=None).read()` behavior (Python 3.13.3) | returns `b''` -> `error_body=''` -> `error_message="HTTP 500: "` (not None, satisfies test) |

## Files Changed

| File | Action | Commit |
|------|--------|--------|
| `profile/portal/task_engine.py` | Created (44 lines: 2 ContextVars + 4 funcs + imports) | 615674d |
| `profile/llm/client.py` | Modified (+55 lines: 1 import, 3 `_record_llm_call` call sites, 1 helper func) | 615674d |
| `tests/test_llm_tracking.py` | Created (70 lines, 2 tests) | 615674d |

Commit: `615674d` — `feat(portal): add LLM call tracking via contextvars + client.py instrumentation`
(3 files changed, 169 insertions(+))

## Self-Review Findings

- **Implementation matches the brief verbatim.** The `task_engine.py`
  contextvars section, the `client.py` import line, all three `_record_llm_call`
  call sites (success + HTTPError + URLError), and the `_record_llm_call`
  helper signature/body all match the brief exactly. Placement confirmed:
  success call after `logger.debug("LLM 响应全文"...)` and before
  `return content.strip()`; failure calls before each `raise RuntimeError(...)`;
  helper defined outside the class before `class JsonParseError`.
- **TDD Red-Green followed.** Tests failed first with the expected
  `assert 0 == 1`, then passed after instrumentation.
- **Only the 3 specified files were staged/committed** (verified via
  `git show --stat 615674d`). The many unrelated pre-existing modifications on
  `feat/portal` (aggregator.py, log.py, analysis modules, docs, .superpowers
  files, etc.) were left untouched — consistent with Tasks 1 & 2.
- **No circular import.** `client.py` imports `task_engine` at module load;
  `task_engine` imports `db_store`; `db_store` imports `init_db.get_connection`.
  None of these import `client.py`, so the chain is acyclic. Confirmed by
  importing both modules in a fresh interpreter.
- **`_record_llm_call` uses a deferred local import** of `insert_llm_call`
  (inside the function body) while `get_current_task_run_id`/`get_current_step`
  are imported at the top of `client.py`. This asymmetry is per the brief; both
  work and there is no circular import. The deferred import also means a DB
  layer problem at import time could never break `client.py` loading.
- **`except Exception: pass` swallows tracking errors by design** ("埋点失败不
  影响主流程"). This is intentional per the brief: a recording failure must
  never abort the LLM call. The trade-off is that a bug in `insert_llm_call`
  would fail silently; the 2 passing tests confirm the happy/failure paths
  record correctly today.
- **`response=content` records the un-stripped content** while `chat()` returns
  `content.strip()`. In production the recorded response may retain leading/
  trailing whitespace that the returned value does not. This matches the brief
  exactly (records `content`, returns `content.strip()`); the test mock has no
  surrounding whitespace so `call["response"] == "hello"` holds.
- **Pre-existing unrelated test failures.** `pytest -q` (full suite) shows
  4 failures in `tests/test_activity.py`, `tests/test_direction.py`, and
  `tests/test_topic.py` — all `KeyError`/`AssertionError` on analysis result
  keys (`'source'`, `'每周趋势'`, `'主要诉求TOP10'`, `'诉求分类'`). These are
  caused by pre-existing uncommitted changes to `profile/analysis/aggregator.py`
  on this branch and are NOT related to Task 3: those test files do not import
  `profile.llm.client` or `profile.portal.*` (verified via grep). Task 3's
  scope (portal + llm.client) is fully green.
- **CRLF line-ending warnings.** Git warns `LF will be replaced by CRLF` for the
  new/modified files. This is cosmetic (Windows default Git behavior) and
  identical to Tasks 1 & 2; no functional impact.

## Concerns

1. **`except Exception: pass` in `_record_llm_call` is silent.** A regression in
   `insert_llm_call` or a schema drift would cause tracking to stop with no
   visible error. Intentional per the brief (main flow must not break), but
   worth noting for future debugging — if `llm_calls` rows stop appearing, this
   `try/except` is the first place to look. Could be loosened to log at
   `DEBUG`/`WARNING` in a future task without changing the swallow behavior.

2. **Unused imports in `task_engine.py`.** `json`, `logging`, `threading`,
   `time`, `deque`, `ThreadPoolExecutor`, `datetime`, and the five `db_store`
   CRUD functions are imported but unused by the contextvars-only portion. Kept
   verbatim per the brief because Task 4's TaskRunner will use them. A linter
   would flag these today; they will become used once Task 4 lands.

3. **`_record_llm_call` defined after the class that calls it.** The helper is
   defined at module level *after* `LLMClient` (before `JsonParseError`). This
   works because Python resolves the name at call time (the module is fully
   loaded before `chat()` is ever called). Verified by the passing tests. Kept
   per the brief's placement instruction ("在 `LLMClient` 类外部...追加").

4. **Tests run against the real DB** (`data/profile.db`), not an isolated temp
   DB. `setup_function()` mitigates by `DELETE`-ing all `llm_calls`/`task_runs`
   rows before each test. Same pattern as Tasks 1 & 2; left as-is to match the
   brief exactly.

5. **Pre-existing analysis-test failures on the branch** (4 failures) are out
   of scope for Task 3 but should be resolved in a separate task so the full
   suite can go green.
