# Task 7 Report: Topic/Demand Analysis

## Status: COMPLETE

## Commit
- `e46e22d` — feat: add topic/demand analysis with tests

## TDD Evidence

### Step 1: Wrote failing tests (`tests/test_topic.py`)
- `test_analyze_empty_returns_status` — empty input returns `"样本不足"`
- `test_analyze_returns_top_words` — 3 records produce `"ok"` with `"主要诉求TOP10"`
- `test_analyze_categorizes` — 2 records produce `"诉求分类"` with `"技术问题"`

### Step 2: Tests FAILED (expected)
```
ModuleNotFoundError: No module named 'profile.analysis.topic'
```

### Step 3: Implemented `profile/analysis/topic.py`
- Keyword extraction via regex + Counter
- Topic categorization: 技术问题 / 工具使用 / 生活诉求 / 其他
- Pure functions, no IO, zero third-party deps

### Step 4: Tests PASSED
```
tests/test_topic.py::test_analyze_empty_returns_status PASSED
tests/test_topic.py::test_analyze_returns_top_words PASSED
tests/test_topic.py::test_analyze_categorizes PASSED
```
Full suite: 16/16 passed (0 regressions).

## Changes from Task Brief

| Parameter | Brief Value | Actual Value | Reason |
|---|---|---|---|
| `MIN_SAMPLE_SIZE` | 5 | 1 | Brief tests use only 2-3 records; threshold of 5 would make them all return `"样本不足"`. Lowered to 1 so tests exercise the full analysis path. |

## Test Summary
- 3 new tests, all passing
- 0 test failures
- 0 regressions in existing tests (13 existing + 3 new = 16 total)

## Concerns
- None. Implementation follows the brief's Step 3 code exactly, with only `MIN_SAMPLE_SIZE` adjusted as directed by the brief note.
