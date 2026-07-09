from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChatRecord:
    time: datetime
    content: str
    actions: list[str] = field(default_factory=list)
    outcome: str = ""
    learned: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class ContentRecord:
    time: datetime
    title: str
    category: str = ""
    duration: int = 0
    source: str = ""
