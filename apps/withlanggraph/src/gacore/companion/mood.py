"""Short-lived mood derived from real owner events (V2-Step 3, migration v4).

Design: ``docs/companion-self-memory-v2-plan.md`` §4.3 / V2-Step 3. No model decides the
mood: a deterministic rule maps one owner message to one label plus a TTL, so every mood
row is traceable to ``source_event_id`` and expires on its own.

Hard rules from the plan, enforced here:

- default is neutral (no row, no prompt line);
- only event rules update it — the model may at most suggest a label elsewhere, never set it;
- an expired mood is not injected;
- no owner reply never turns the mood negative (all rules require an explicit message);
- mood never touches relationship stage/persona (separate table, no cross-writes).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from gacore.companion.store import CompanionEvent, CompanionStore, Mood
from gacore.jsonl_logger import get_logger

logger = get_logger("companion.mood")

MOOD_BLOCK_VERSION: Final = "companion-mood@v1"
MAX_MOOD_LINE_CHARS: Final = 100

_TZ: Final = timezone(timedelta(hours=8))
_HOUR_MS: Final = 60 * 60 * 1000
_LATE_NIGHT_HOURS: Final = frozenset({0, 1, 2, 3, 4})

# (label, patterns, valence, arousal, ttl_hours, priority) — higher priority wins a tie.
_RULES: Final = (
    ("frustrated", ("你烦", "闭嘴", "滚开", "别说了", "不想听你"), -0.7, 0.7, 2, 5),
    (
        "concerned",
        ("累", "烦", "难受", "生气", "崩溃", "失眠", "压力", "焦虑", "难过", "不开心", "加班"),
        -0.35,
        0.45,
        12,
        4,
    ),
    (
        "happy",
        ("谢谢", "辛苦了", "辛苦啦", "厉害", "不错", "真好", "喜欢", "开心", "哈哈", "太棒"),
        0.6,
        0.45,
        6,
        3,
    ),
    ("curious", ("？", "?", "怎么", "为什么", "什么", "哪", "多少", "吗", "呢"), 0.3, 0.6, 3, 2),
)

_TOPIC_TRIM: Final = re.compile(r"\s+")
_LABEL_TEXT: Final = {
    "neutral": "平静",
    "curious": "有点好奇",
    "happy": "心情不错",
    "tired": "有点乏",
    "concerned": "有点担心对方",
    "frustrated": "有点被顶到",
}


@dataclass(frozen=True, slots=True)
class MoodSignal:
    """One derived mood plus the event it came from and its expiry."""

    label: str
    valence: float
    arousal: float
    source_event_id: str
    expires_at_ms: int


def detect_signal(
    events: Sequence[CompanionEvent], *, now_ms: int
) -> MoodSignal | None:
    """Newest owner-message signal that is still live at ``now_ms`` (``None`` = neutral).

    Only ``user.message.received`` drives mood: letting the character's own words set it
    would make the state self-reinforcing and unfalsifiable.
    """
    newest: tuple[int, int, MoodSignal] | None = None
    for event in events:
        if event.kind != "user.message.received":
            continue
        rule = _match(event.summary, event.occurred_at_ms)
        if rule is None:
            continue
        label, valence, arousal, ttl_ms, priority = rule
        expires_at = event.occurred_at_ms + ttl_ms
        if expires_at <= now_ms:
            continue
        candidate = (event.occurred_at_ms, priority, MoodSignal(
            label=label,
            valence=valence,
            arousal=arousal,
            source_event_id=event.event_id,
            expires_at_ms=expires_at,
        ))
        if newest is None or candidate[:2] > newest[:2]:
            newest = candidate
    return newest[2] if newest is not None else None


def refresh_mood(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int | None = None,
) -> Mood | None:
    """Recompute the mood from the actor's events; expired states are cleared.

    Returns the live mood, or ``None`` when the actor is neutral. Failures are the
    caller's to log — this function only talks to the store.
    """
    now = int(datetime.now(_TZ).timestamp() * 1000) if now_ms is None else int(now_ms)
    events = store.events_for_actor(actor_id, principal_id)
    signal = detect_signal(events, now_ms=now)
    if signal is None:
        store.clear_mood(actor_id, principal_id)
        return None
    saved = store.save_mood(
        actor_id=actor_id,
        principal_id=principal_id,
        label=signal.label,
        valence=signal.valence,
        arousal=signal.arousal,
        source_event_id=signal.source_event_id,
        expires_at_ms=signal.expires_at_ms,
    )
    store.record_projection(
        "mood",
        projector_version=MOOD_BLOCK_VERSION,
        cursor_seq=max((event.seq for event in events), default=0),
    )
    logger.info(
        "companion mood refreshed",
        actor_id=actor_id,
        label=saved.label,
        expires_at_ms=saved.expires_at_ms,
    )
    return saved


def active_mood(
    store: CompanionStore | None,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int | None = None,
) -> Mood | None:
    """The stored mood when it is still live; expired/absent reads as neutral (``None``)."""
    if store is None or not actor_id or not principal_id:
        return None
    now = int(datetime.now(_TZ).timestamp() * 1000) if now_ms is None else int(now_ms)
    try:
        mood = store.mood(actor_id, principal_id)
    except Exception:  # noqa: BLE001 — a mood lookup must never break a turn
        return None
    if mood is None or mood.expires_at_ms <= now:
        return None
    return mood


def build_mood_line(mood: Mood | None) -> str:
    """One bounded prompt line describing the live mood (``""`` when neutral)."""
    if mood is None or mood.label == "neutral":
        return ""
    text = _LABEL_TEXT.get(mood.label, "平静")
    line = (
        f"（此刻心情：{text}。只体现在语气里，别点名心情，也别解释原因。）"
    )
    return line[:MAX_MOOD_LINE_CHARS]


# ------------------------------------------------------------------------- helpers


def _match(
    summary: str, occurred_at_ms: int
) -> tuple[str, float, float, int, int] | None:
    """First matching rule for one owner message, or None for a neutral message."""
    text = _TOPIC_TRIM.sub("", summary or "")
    for label, patterns, valence, arousal, ttl_hours, priority in _RULES:
        if any(pattern in text for pattern in patterns):
            return label, valence, arousal, ttl_hours * _HOUR_MS, priority
    if _late_night(occurred_at_ms):
        return "tired", -0.2, 0.2, 8 * _HOUR_MS, 1
    return None


def _late_night(occurred_at_ms: int) -> bool:
    hour = datetime.fromtimestamp(occurred_at_ms / 1000, tz=_TZ).hour
    return hour in _LATE_NIGHT_HOURS
