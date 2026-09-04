"""上报请求的 Pydantic 模型。只校验外层结构；data 内部存原文不深校验。
type 为自由字符串：客户端采集事件类型持续扩展（usage/session/snapshot/notification/...），
服务端不做枚举限制，全部按 JSON 落库，分析层自行区分。"""
from __future__ import annotations

from pydantic import BaseModel


class Event(BaseModel):
    type: str
    ts: int
    data: dict


class IngestRequest(BaseModel):
    device_id: str
    batch_id: str
    client_ts: int
    events: list[Event]
