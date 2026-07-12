# Task 8 Report: Data API

## Status: DONE

## Commits
- `2026ae7` feat(portal): data API for raw_data and llm_intents browsing

## Test Summary
`tests/test_portal_routes.py` — 12 passed, 0 failed.
- 8 pre-existing tests still pass (root, dashboard x4, tasks x3).
- 4 new data-API tests pass: `test_list_raw_data`,
  `test_list_raw_data_with_source_filter`, `test_get_raw_data_not_found`,
  `test_list_intents`.

Command: `.venv\Scripts\python.exe -m pytest tests/test_portal_routes.py -v`

## What Was Implemented

Replaced the stub in `profile/portal/routes/data.py` with the full Data API per the
task brief. The router is mounted at `/api` in `app.py`, so the effective endpoints
are:

1. **GET /api/data/raw** — `list_raw_data`: paginated list with optional `source` /
   `start_date` / `end_date` filters. Delegates to `profile.db.store.query_raw_data`,
   then slices in memory, returns `{items, total, page, page_size}`.
2. **GET /api/data/raw/{raw_id}** — `get_raw_data`: single record by id, fetched via
   `profile.db.init_db.get_connection()` directly (`SELECT * FROM raw_data WHERE id = ?`).
   Returns 404 with `"记录不存在"` when the row is absent.
3. **GET /api/data/intents** — `list_intents`: paginated list with optional `source`
   filter (passed to `query_intents`) and `category` filter (applied in Python on
   `intent_category`). Returns `{items, total, page, page_size}`.

## Pre-implementation Checks

The brief referenced `profile.db.store.query_raw_data` and `profile.db.store.query_intents`.
Both were verified in `profile/db/store.py` before implementation:

- **`query_raw_data(source=None, start_date=None, end_date=None) -> list[dict]`** — EXISTS;
  matches the brief's call `query_raw_data(source=source, start_date=start_date, end_date=end_date)`
  exactly. Already deserializes `actions` / `learned` / `raw_json` JSON columns to objects.
- **`query_intents(source=None, start_date=None, end_date=None) -> list[dict]`** — EXISTS;
  the brief calls it with `source=source` only (a subset of the supported kwargs), which is
  valid. Returns joined rows including `intent_category`, `source`, `raw_content`, etc.
- **`profile.db.init_db.get_connection()`** — EXISTS; returns a `sqlite3.Connection` with
  `row_factory = sqlite3.Row`, so `dict(row)` works as the brief expects.
- **Router registration** — `app.py` already does `app.include_router(data.router, prefix="/api")`,
  so `/data/raw` becomes `/api/data/raw` as the tests expect. No `app.py` change needed.

No signature adaptation was required — the brief's code matched the store layer verbatim.

## Deviations from the Brief

None. The implementation matches the brief code verbatim, including imports, endpoint
paths, status codes, error messages, and the in-memory pagination / category-filter logic.

## Verification

- `pytest tests/test_portal_routes.py -v` → 12 passed, 2 warnings (both pre-existing
  `on_event` deprecation warnings from `app.py`, unrelated to this task).
- `test_list_raw_data` → 200, response has `items` and `total`.
- `test_list_raw_data_with_source_filter` → 200; every returned item has `source == "trae"`
  (trivially satisfied when no rows match, since the loop body never executes).
- `test_get_raw_data_not_found` → 404 for id 99999.
- `test_list_intents` → 200, response has `items`.

## Self-review Notes

- **Route ordering**: `/data/raw` is declared before `/data/raw/{raw_id}`. These do not
  conflict because they differ in path-segment count; FastAPI correctly routes
  `/data/raw/123` to `get_raw_data` and `/data/raw` to `list_raw_data`.
- **`get_raw_data` uses raw SQL** rather than a store helper because `store.py` exposes no
  `get_raw_data(id)` single-row accessor. This is exactly what the brief prescribes, and the
  note in the brief explicitly permits falling back to `get_connection()` directly.
- **Pagination is in-memory**: `query_raw_data` / `query_intents` return the full filtered
  set and the route slices `all_rows[start:end]`. Fine for this single-user local portal
  where the raw_data / intents tables stay small. If the DB grows large, a future
  optimization would push `LIMIT`/`OFFSET` into the store layer.
- **`category` filter is post-query** (Python list comprehension) because `query_intents`
  has no `category` parameter. Consistent with the brief.
- **`raw_json` / `actions` / `learned`** are already JSON-decoded by `query_raw_data`, so
  the `/data/raw` list returns them as structured objects rather than strings. The
  `/data/raw/{id}` detail endpoint, however, returns the raw `dict(row)` from the direct
  SQL query, so those columns remain as JSON strings there. This asymmetry is inherent to
  the brief's two-path design (store helper vs. raw SQL) and is acceptable for a browse API.

## Concerns

- **No test isolation / DB seeding.** `test_portal_routes.py` has no `setup_function` or
  fixture to reset or seed the DB. The four new tests are structure-only assertions
  (presence of `items`/`total`, 404 for a missing id, and a filter that is vacuously true
  when no `trae` rows exist), so they pass regardless of DB contents. This means the
  source-filter test does not actually prove filtering works when the DB is empty — it only
  proves the endpoint does not error. A future hardening would seed known rows in a fixture
  and assert the filter narrows the result set. (Inherited brief behavior; not changed.)
- **`test_get_raw_data_not_found` assumes id 99999 is absent.** Realistic for this
  single-user local DB, but would break if that id ever existed.
- **Type hints `source: str = None` etc.** are slightly imprecise (should be
  `str | None = None`). FastAPI treats them as optional query params correctly regardless.
  Kept as-is to match the brief verbatim.
- **In-memory pagination / category filtering** scales linearly with the table size. Acceptable
  for the intended local single-user usage; noted above as a possible future optimization.
- The pre-existing `on_event("startup")` DeprecationWarning in `app.py` is out of scope.

## Files Changed
- `profile/portal/routes/data.py` (stub → full implementation, +44 / -2)
- `tests/test_portal_routes.py` (+32, 4 new tests appended)
