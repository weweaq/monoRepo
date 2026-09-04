from __future__ import annotations

import pytest
from pydantic import ValidationError

from gacore.langTrack.schemas import IngestRequest


def test_valid_request():
    req = IngestRequest(
        device_id="dev1",
        batch_id="b1",
        client_ts=1000,
        events=[
            {"type": "usage", "ts": 1000, "data": {"pkg": "com.x", "foreground_ms": 5}},
            {"type": "session", "ts": 1001, "data": {"kind": "screen_on"}},
        ],
    )
    assert len(req.events) == 2


def test_arbitrary_type_accepted():
    """事件类型不限枚举：snapshot / notification / location 等采集扩展类型可直接上报。"""
    req = IngestRequest(
        device_id="dev1", batch_id="b1", client_ts=1000,
        events=[
            {"type": "snapshot", "ts": 1000, "data": {"fg_pkg": "com.x", "battery": 73}},
            {"type": "notification", "ts": 1001, "data": {"pkg": "com.x", "clicked": False}},
            {"type": "location", "ts": 1002, "data": {"lat": 31.2, "lon": 121.4}},
        ],
    )
    assert [e.type for e in req.events] == ["snapshot", "notification", "location"]


def test_missing_ts_rejected():
    with pytest.raises(ValidationError):
        IngestRequest(
            device_id="dev1", batch_id="b1", client_ts=1000,
            events=[{"type": "usage", "data": {"pkg": "com.x"}}],
        )
