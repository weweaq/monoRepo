"""The two log sinks must share one per-process session id so their lines join exactly.

app.jsonl (jsonl_logger) and llm_requests.jsonl (llm_request_log) used to generate
independent uuids, which forced cross-log correlation by pid + time window (pids get
reused across process restarts, so the join was a heuristic). Regression:
jsonl_logger.session_id() is the single source and llm_request_log reuses it.
"""

from __future__ import annotations

import json
import logging

from gacore.jsonl_logger import _JsonlFormatter, session_id
from gacore.llm_request_log import _SESSION_ID as LLM_SESSION_ID


def test_both_sinks_share_one_session_id() -> None:
    assert LLM_SESSION_ID == session_id()


def test_app_jsonl_lines_carry_the_shared_session() -> None:
    record = logging.LogRecord("gacore.test", logging.INFO, __file__, 1, "event", (), None)
    payload = json.loads(_JsonlFormatter().format(record))
    assert payload["session"] == session_id()
