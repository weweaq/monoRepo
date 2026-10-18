"""抽取通道注册表。

S1 只注册 events；S2 追加 relations/todos；S3 追加 emotions/patterns/worldview。
新增通道 = 在 ``channels/`` 加一个模块 + 在此加一行，runner 无需改动。
"""

from __future__ import annotations

from typing import Final

from gacore.langTrack.extract.channels.events import SPEC as _EVENTS_SPEC
from gacore.langTrack.extract.channels.relations import SPEC as _RELATIONS_SPEC
from gacore.langTrack.extract.channels.todos import SPEC as _TODOS_SPEC
from gacore.langTrack.extract.types import ChannelSpec

CHANNELS: Final[tuple[ChannelSpec, ...]] = (_EVENTS_SPEC, _RELATIONS_SPEC, _TODOS_SPEC)
