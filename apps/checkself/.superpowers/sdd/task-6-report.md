# Task 6 Report: Dashboard API

## Status: DONE

## Commits
- `fe4e604` feat(portal): dashboard API with aggregated overview data

## Test Summary
`tests/test_portal_routes.py` — 2 passed (including `test_dashboard_endpoint_exists`), 0 failed.

## What Was Implemented

Replaced the stub in `profile/portal/routes/dashboard.py` with the real `GET /api/dashboard`
implementation per the task brief. The endpoint aggregates six data sections:

1. **current_task** — `task_engine.get_runner().is_busy()` + `db_store.query_task_runs(status="running")`
2. **recent_tasks** — `db_store.query_task_runs(page_size=5)`
3. **data_overview** — raw SQL `GROUP BY source` over `raw_data` (count per source, desc)
4. **llm_overview** — `db_store.query_llm_calls` total + raw SQL `SUM(total_tokens)` and success rate
5. **latest_profile** — `output.writer.load_latest_json("个人画像-综合")` (date / summary / direction_alignment)
6. **latest_changes** — reads newest `个人画像-变化报告-*.md` from `OBSIDIAN_OUTPUT_DIR` and counts
   `[新增]` / `[上升]` / `[下降]` / `[消失]` markers

## Pre-implementation Checks

- **`profile.output.writer.load_latest_json`** — EXISTS (lines 106-118 of `profile/output/writer.py`).
  Used directly; no adaptation needed. It globs `{name_prefix}-*.json` sorted descending and
  returns the parsed JSON of the newest file (or `None`).
- **`profile.config.OBSIDIAN_OUTPUT_DIR`** — EXISTS, points to
  `d:/AAAmyPrj/gitee/obsidian/我的文档/AI使用/画像产出`.
- **`profile.portal.db_store.query_task_runs` / `query_llm_calls`** — EXIST and used.
- **`profile.portal.task_engine.get_runner`** — EXISTS; `TaskRunner.is_busy()` used.
- **`profile.db.init_db.get_connection`** — EXISTS; used for the two aggregate SQL queries.

## Deviations from the Brief

1. **Removed unused inline imports.** The brief's step-6 block contained `import json` and
   `from datetime import datetime` inline, but neither was actually referenced in that block.
   They were dropped for cleanliness. All top-level imports are organized at module top.
2. **Interfaces list vs. code.** The brief's "Interfaces" section lists
   `profile.db.store.query_raw_data` / `query_intents` as consumed, but the brief's actual
   code uses raw SQL via `get_connection()` for the aggregate counts instead. I followed the
   brief's code (raw SQL) since it is more efficient for `COUNT(*) ... GROUP BY` than pulling
   full rows through `query_raw_data`. No call to `query_intents` was needed for the dashboard.

## Verification

- `pytest tests/test_portal_routes.py -v` → 2 passed.
- Smoke test via `TestClient`: `GET /api/dashboard` returns 200 with all six keys populated
  from real data:
  - `recent_tasks` returned the latest task run (id 87, ingest_single, done).
  - `data_overview` returned 4 sources (bilibili 1207, trae 190, marvis 155, netease 100).
  - `llm_overview` returned `{total_calls: 0, total_tokens: 0, success_rate: 1.0}`.
  - `latest_profile` returned a populated object (date/summary/direction_alignment).
  - `current_task` is `null` when no task is running (expected).
- The existing `test_dashboard_endpoint_exists` still passes (it only asserts the four
  original keys; the two new keys are additive and do not break it).

## Self-review Notes

- The route is a plain `def` (sync) which is fine — the underlying sqlite calls are fast and
  the portal is single-user. No `async` needed.
- DB connections are opened/closed per query with `try/finally`, matching the existing
  `db_store.py` convention.
- File reads (`load_latest_json`, glob for change reports) are guarded by
  `OBSIDIAN_OUTPUT_DIR.exists()` and `if reports:` checks, so the endpoint degrades cleanly
  to `None` when output files are absent.
- The `on_event("startup")` DeprecationWarning in `app.py` is pre-existing and out of scope
  for this task.

## Concerns

- `latest_profile.summary` currently returns markdown-formatted text (the global profile JSON
  stores a markdown blob in its `summary` field). This is a data-shape artifact of the existing
  output writer, not a dashboard bug; the route faithfully forwards `global_json.get("summary")`.
  Frontend may want to render it as markdown rather than plain text.
- The change-report marker counting (`[新增]` etc.) is a literal substring count and assumes the
  diff report uses exactly those markers. If the diff report format changes, the counts will
  silently go to zero. Acceptable for now since `profile/refresh/diff.py` controls the format.

## Files Changed
- `profile/portal/routes/dashboard.py` (1 file, +108 / -15)
