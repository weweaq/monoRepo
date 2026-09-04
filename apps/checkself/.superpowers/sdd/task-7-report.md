# Task 7 Report: Tasks API + SSE

## Status: DONE

## Commits
- `6898fc9` feat(portal): tasks API with CRUD and SSE log streaming

## Test Summary
`tests/test_portal_routes.py` — 8 passed, 0 failed.
- 5 pre-existing dashboard tests still pass.
- 3 new tasks-API tests pass: `test_list_tasks`, `test_create_invalid_task_type`,
  `test_get_nonexistent_task`.

Command: `.venv\Scripts\python.exe -m pytest tests/test_portal_routes.py -v`

## What Was Implemented

Replaced the stub in `profile/portal/routes/tasks.py` with the full Tasks API per the task
brief. The router is mounted at `/api` in `app.py`, so the effective endpoints are:

1. **GET /api/tasks** — `list_tasks`: paginated list with optional `status` / `task_type`
   filters. Delegates to `db_store.query_task_runs`, returns `{items, total, page, page_size}`.
2. **GET /api/tasks/{task_id}** — `get_task`: single task detail via `db_store.get_task_run`;
   returns 404 when the task does not exist.
3. **POST /api/tasks** — `create_task`: validates `task_type` against `VALID_TASK_TYPES`
   (`refresh_all`, `ingest`, `ingest_single`, `extract_intents`, `generate_profiles`),
   then `task_engine.get_runner().start_task(...)`. Returns 400 on invalid type, 409 when a
   task is already running (`start_task` returns `None`), else `{id, status: "started"}`.
4. **DELETE /api/tasks/{task_id}** — `cancel_task`: soft-cancel via `runner.cancel(task_id)`.
   Returns 400 if the task is not the currently running one; else `{id, status: "cancelling"}`.
5. **GET /api/tasks/{task_id}/logs** — `stream_logs`: SSE log stream
   (`media_type="text/event-stream"`). Polls `runner.get_logs(task_id, after_idx=idx)` every
   0.5s, yields each log line as `data: {...}\n\n`. When the task reaches a terminal status
   (`done` / `failed` / `cancelled`), it flushes remaining logs, emits a `done` event, and
   closes the stream.

Request body model `TaskCreateRequest` (`task_type: str`, `params: dict = {}`) is a Pydantic
`BaseModel`.

## Pre-implementation Checks

- **`profile.portal.task_engine.get_runner`** — EXISTS; `TaskRunner.start_task` / `cancel` /
  `get_logs` all present and match the signatures used.
- **`profile.portal.db_store.query_task_runs`** — EXISTS; returns
  `{items, total, page, page_size}` — matches the `test_list_tasks` assertions.
- **`profile.portal.db_store.get_task_run`** — EXISTS; returns the row dict or `None`.
- **Router registration** — `app.py` already does `app.include_router(tasks.router, prefix="/api")`,
  so `/tasks` becomes `/api/tasks` as the tests expect. No app.py change needed.
- **`TaskRunner.start_task`** returns `None` when busy (non-blocking lock acquire fails), which
  the route maps to HTTP 409. Confirmed in `task_engine.py`.

## Deviations from the Brief

None. The implementation matches the brief code verbatim, including imports, status codes,
error messages, and the SSE polling/flush logic.

## Verification

- `pytest tests/test_portal_routes.py -v` → 8 passed, 2 warnings (both pre-existing
  `on_event` deprecation warnings from `app.py`, unrelated to this task).
- `test_list_tasks` → 200, response has `items` and `total`.
- `test_create_invalid_task_type` → 400 (validation rejects `"invalid"` before touching the
  runner, so no DB row is created and the global runner singleton is not polluted).
- `test_get_nonexistent_task` → 404 for id 99999.

## Self-review Notes

- **Route ordering**: `/tasks/{task_id}` is declared before `/tasks/{task_id}/logs`. These do
  not conflict because they differ in path-segment count; FastAPI correctly routes
  `/tasks/123/logs` to the SSE handler and `/tasks/123` to `get_task`.
- **Validation before side effects**: `create_task` validates `task_type` before calling
  `get_runner()` / `start_task()`, so invalid requests never create a DB row nor occupy the
  serial lock. This keeps the test for invalid types side-effect-free.
- **SSE format**: each frame is `data: <json>\n\n` with `ensure_ascii=False`, matching the
  Server-Sent Events spec for a `text/event-stream` response.
- **Sync vs async**: list/get/create/cancel are sync `def` (fast sqlite calls, single-user
  portal). Only `stream_logs` is `async def` because it uses `await asyncio.sleep` for polling.

## Concerns

- **SSE for a non-existent task never terminates.** If a client opens `/api/tasks/{id}/logs`
  for an id that does not exist, `get_task_run(task_id)` returns `None`, so the
  `if task and task["status"] in (...)` guard is never true and the generator polls forever
  (every 0.5s, yielding nothing). There is no 404 short-circuit on the SSE path. This is
  inherited from the brief; acceptable for now since the frontend only opens the log stream
  for tasks it just created, but a future hardening could 404 immediately when the task row
  is absent. (Not covered by a test, since the brief's tests do not exercise the SSE route.)
- **Type hints `status: str = None` / `task_type: str = None`** are slightly imprecise (should
  be `str | None = None`). FastAPI treats them as optional query params correctly regardless.
  Kept as-is to match the brief verbatim.
- **Mutable default `params: dict = {}`** on the Pydantic model is safe — Pydantic deep-copies
  model defaults per instance, so there is no shared-mutable-default hazard.
- **Test isolation**: `test_portal_routes.py` has no `setup_function` to reset the DB. The
  three new tests are robust to pre-existing data (structure-only assertions, validation
  rejected pre-DB, and id 99999 assumed absent). `test_get_nonexistent_task` would break only
  if a task with id 99999 were ever inserted, which is unrealistic for this single-user DB.
- The pre-existing `on_event("startup")` DeprecationWarning in `app.py` is out of scope.

## Files Changed
- `profile/portal/routes/tasks.py` (stub → full implementation, +83 / -2)
- `tests/test_portal_routes.py` (+21, 3 new tests appended)
