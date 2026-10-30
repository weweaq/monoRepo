"""Relationship state and open threads: a deterministic projection of real events.

Design: ``docs/companion-self-memory-v2-plan.md`` V2-Step 2 / §4.2. Nothing here calls a
model. Stage and status are derived from V1 events by rules, so every claim can be traced
back to an event id:

- ``stage``        new → familiar → established, by verifiable counters only;
- ``status``       stable → strained → repairing → stable, driven by explicit boundaries
                   the owner stated in their own words (never by "no reply");
- ``open_threads`` owner questions the character never answered, each with its source.

Rulings recorded in the plan and here:

1. ``meaningful_interactions`` counts owner messages with at least two characters plus
   every confirmed character send (a one-letter "嗯" is not a meaningful exchange).
2. ``positive_feedback`` is a proxy: an owner message that arrives within
   ``POSITIVE_WINDOW_MS`` after a character send. Nothing else in V1 can evidence
   "the owner liked it", and inventing a sentiment model would break the evidence rule.
3. ``established`` needs a reliability signal. Commitments arrive in Step 4, so until
   then the equivalent signal is "the owner replied to a proactive outreach"
   (``positive_feedback >= 1`` after a ``proactive.message.sent``).
4. Actor boundaries come from policy (``DEFAULT_ACTOR_BOUNDARIES``) and are never
   written by this projector — the model cannot widen its own boundaries.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from gacore.companion.store import (
    RELATIONSHIP_STAGES,
    CompanionEvent,
    CompanionStore,
    Relationship,
)
from gacore.jsonl_logger import get_logger

logger = get_logger("companion.relationship")

RELATIONSHIP_PROJECTION_VERSION: Final = "companion-relationship@v1"

MEANINGFUL_MIN_CHARS: Final = 2
POSITIVE_WINDOW_MS: Final = 10 * 60 * 1000
THREAD_ANSWER_WINDOW_MS: Final = 30 * 60 * 1000
MAX_TOPIC_CHARS: Final = 60
MAX_USER_BOUNDARIES: Final = 20
MAX_EVENTS_SCANNED: Final = 500

# ── stage thresholds (plan §4.2) ───────────────────────────────────────────────
FAMILIAR_MIN_INTERACTIONS: Final = 5
FAMILIAR_MIN_DAYS: Final = 2
ESTABLISHED_MIN_INTERACTIONS: Final = 20
ESTABLISHED_MIN_DAYS: Final = 14

_SENT_KINDS: Final = frozenset({"agent.message.sent", "proactive.message.sent"})
_PROACTIVE_KIND: Final = "proactive.message.sent"

# Explicit boundaries, in the owner's own words. Broad enough to catch the real phrasing
# ("这个话题不想聊了" / "别问这个了"), narrow enough not to fire on ordinary negation.
_BOUNDARY_PHRASES: Final = (
    "不想聊这个",
    "不想说这个",
    "不想谈这个",
    "别问这个",
    "不要提这个",
    "别提这个",
    "别再提",
    "换个话题",
    "换个别的",
    "不提这个",
    "别问了",
)
_ACK_PHRASES: Final = ("没关系", "没事", "好的", "好", "可以", "行吧", "嗯", "谢谢")
_QUESTION_MARKERS: Final = ("？", "?", "吗", "呢", "怎么", "为什么", "什么", "哪", "多少", "几点")
_TOPIC_TRIM: Final = re.compile(r"[\s，。！？、,.!?~～…—]+")
# Generic tails that describe *that it is a topic*, not which topic: keeping them would
# make the violation check require the whole clause to reappear verbatim.
_TOPIC_FILLERS: Final = (
    "这个话题",
    "这件事",
    "这个事情",
    "这事儿",
    "的事情",
    "的事",
    "的事吧",
    "吧",
)

# Policy boundaries: stated by the deployment, never by the model.
DEFAULT_ACTOR_BOUNDARIES: Final = (
    "不冒充别人，也不自称是人类",
    "不编造没有发生过的经历",
    "不泄露凭据、完整路径或他人隐私",
)


@dataclass(frozen=True, slots=True)
class BoundaryHit:
    """One explicit boundary the owner stated, with the event that proves it."""

    topic: str
    text: str
    source_event_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class RelationshipProjection:
    """The derived numbers and states, before they are written to the store."""

    stage: str
    status: str
    meaningful_interactions: int
    active_days: int
    positive_feedback: int
    user_boundaries: tuple[BoundaryHit, ...]
    last_negative_event_id: str | None
    last_repair_event_id: str | None
    last_interaction_at_ms: int | None
    open_threads: tuple[tuple[str, str], ...]  # (topic, source_event_id)


def refresh_relationship(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int | None = None,
) -> Relationship:
    """Recompute one actor's relationship + open threads and persist both.

    Idempotent: calling it after every event is cheap (a handful of indexed reads) and
    produces the same row for the same event history. Raises only on store failures.
    """
    events = store.events_for_actor(actor_id, principal_id, limit=MAX_EVENTS_SCANNED)
    previous = store.relationship(actor_id, principal_id)
    projection = project(events, previous=previous, now_ms=now_ms)

    saved = store.save_relationship(
        actor_id=actor_id,
        principal_id=principal_id,
        stage=projection.stage,
        status=projection.status,
        meaningful_interactions=projection.meaningful_interactions,
        active_days=projection.active_days,
        positive_feedback=projection.positive_feedback,
        user_boundaries=[
            {
                "topic": hit.topic,
                "text": hit.text,
                "source_event_id": hit.source_event_id,
                "created_at_ms": hit.created_at_ms,
            }
            for hit in projection.user_boundaries
        ],
        actor_boundaries=list(DEFAULT_ACTOR_BOUNDARIES),
        last_negative_event_id=projection.last_negative_event_id,
        last_repair_event_id=projection.last_repair_event_id,
        last_interaction_at_ms=projection.last_interaction_at_ms,
    )
    _sync_open_threads(store, actor_id, principal_id, events, projection.open_threads)
    logger.info(
        "companion relationship refreshed",
        actor_id=actor_id,
        stage=saved.stage,
        status=saved.status,
        meaningful=saved.meaningful_interactions,
        active_days=saved.active_days,
        positive=saved.positive_feedback,
        boundaries=len(saved.user_boundaries),
        open_threads=len(projection.open_threads),
    )
    return saved


def project(
    events: Sequence[CompanionEvent],
    *,
    previous: Relationship | None = None,
    now_ms: int | None = None,
) -> RelationshipProjection:
    """Derive the projection from ``events`` (oldest first); pure, no I/O."""
    ordered = sorted(events, key=lambda event: event.occurred_at_ms)
    owners = [event for event in ordered if event.kind == "user.message.received"]
    sent = [event for event in ordered if event.kind in _SENT_KINDS]

    meaningful = sum(1 for event in owners if len(event.summary.strip()) >= MEANINGFUL_MIN_CHARS)
    meaningful += len(sent)
    days = len({_day(event.occurred_at_ms) for event in ordered})
    positive = _count_positive(owners, sent)
    boundaries = _detect_boundaries(owners)
    status, negative_id, repair_id = _derive_status(owners, sent, boundaries)
    stage = _derive_stage(
        meaningful,
        days,
        positive,
        status=status,
        previous_stage=previous.stage if previous is not None else None,
        previous_positive=previous.positive_feedback if previous is not None else 0,
    )
    open_threads = _detect_open_threads(owners, sent)

    return RelationshipProjection(
        stage=stage,
        status=status,
        meaningful_interactions=meaningful,
        active_days=days,
        positive_feedback=positive,
        user_boundaries=boundaries,
        last_negative_event_id=negative_id,
        last_repair_event_id=repair_id,
        last_interaction_at_ms=max((event.occurred_at_ms for event in ordered), default=None),
        open_threads=open_threads,
    )


# --------------------------------------------------------------------- derivation


def _count_positive(
    owners: Sequence[CompanionEvent], sent: Sequence[CompanionEvent]
) -> int:
    """Owner replies landing within the positive window after a character send."""
    count = 0
    for sent_event in sent:
        window_end = sent_event.occurred_at_ms + POSITIVE_WINDOW_MS
        for owner_event in owners:
            if sent_event.occurred_at_ms < owner_event.occurred_at_ms <= window_end:
                if not _boundary_phrase(owner_event.summary):
                    count += 1
    return count


def _detect_boundaries(owners: Sequence[CompanionEvent]) -> tuple[BoundaryHit, ...]:
    """One entry per explicit boundary message, newest last, deduped by source event."""
    hits: list[BoundaryHit] = []
    seen: set[str] = set()
    for event in owners:
        phrase = _boundary_phrase(event.summary)
        if phrase is None or event.event_id in seen:
            continue
        seen.add(event.event_id)
        hits.append(
            BoundaryHit(
                topic=_boundary_topic(event.summary, phrase),
                text=event.summary.strip()[:120],
                source_event_id=event.event_id,
                created_at_ms=event.occurred_at_ms,
            )
        )
    return tuple(hits[-MAX_USER_BOUNDARIES:])


def _derive_status(
    owners: Sequence[CompanionEvent],
    sent: Sequence[CompanionEvent],
    boundaries: Sequence[BoundaryHit],
) -> tuple[str, str | None, str | None]:
    """stable / strained / repairing, plus the events that justify the last transition.

    A boundary is only violated when a *later* character send actually revisits the
    boundary topic. Silence never counts as conflict (plan: "无回复不算冲突"), and a
    repair is only claimed after the owner acknowledges **and** follows up.
    """
    negative = _first_violation(sent, boundaries)
    if negative is None:
        return "stable", None, None

    after = [event for event in owners if event.occurred_at_ms > negative.occurred_at_ms]
    ack = next(
        (
            event
            for event in after
            if event.summary.strip()
            and not _boundary_phrase(event.summary)
            and any(word in event.summary for word in _ACK_PHRASES)
        ),
        None,
    )
    if ack is None:
        return "strained", negative.event_id, None

    follow_up = next(
        (
            event
            for event in after
            if event.occurred_at_ms > ack.occurred_at_ms
            and event.summary.strip()
            and not _boundary_phrase(event.summary)
        ),
        None,
    )
    if follow_up is None:
        return "repairing", negative.event_id, ack.event_id
    return "stable", negative.event_id, follow_up.event_id


def _first_violation(
    sent: Sequence[CompanionEvent], boundaries: Sequence[BoundaryHit]
) -> CompanionEvent | None:
    """Earliest character send that revisits a named boundary topic after it was stated."""
    for boundary in boundaries:
        if not boundary.topic or boundary.topic == "unspecified":
            continue
        for event in sent:
            if event.occurred_at_ms > boundary.created_at_ms and boundary.topic in event.summary:
                return event
    return None


def _derive_stage(
    meaningful: int,
    active_days: int,
    positive: int,
    *,
    status: str,
    previous_stage: str | None,
    previous_positive: int,
) -> str:
    """Rule-derived stage; frozen (never upgraded) while the relationship is not stable."""
    earned = "new"
    if meaningful >= FAMILIAR_MIN_INTERACTIONS and active_days >= FAMILIAR_MIN_DAYS:
        earned = "familiar"
    if (
        meaningful >= ESTABLISHED_MIN_INTERACTIONS
        and active_days >= ESTABLISHED_MIN_DAYS
        and positive >= 1
    ):
        earned = "established"

    if previous_stage in RELATIONSHIP_STAGES:
        # Strained history freezes the stage: a conflict must not be papered over by
        # counters that keep growing, and a stage never regresses silently.
        if status != "stable":
            return previous_stage
        if _stage_rank(previous_stage) > _stage_rank(earned):
            return previous_stage
    return earned


def _stage_rank(stage: str) -> int:
    return ("new", "familiar", "established").index(stage)


def _detect_open_threads(
    owners: Sequence[CompanionEvent], sent: Sequence[CompanionEvent]
) -> tuple[tuple[str, str], ...]:
    """Owner questions that no character send answered within the answer window."""
    threads: list[tuple[str, str]] = []
    for event in owners:
        text = event.summary.strip()
        if len(text) < 4 or not any(marker in text for marker in _QUESTION_MARKERS):
            continue
        if _boundary_phrase(text):
            continue
        answered = any(
            event.occurred_at_ms < sent_event.occurred_at_ms
            <= event.occurred_at_ms + THREAD_ANSWER_WINDOW_MS
            for sent_event in sent
        )
        if not answered:
            threads.append((_topic(text), event.event_id))
    return tuple(threads)


def _sync_open_threads(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    events: Sequence[CompanionEvent],
    wanted: Sequence[tuple[str, str]],
) -> None:
    """Create the threads the projection still sees, and resolve ones the character answered."""
    existing = {thread.topic: thread for thread in store.open_threads(actor_id, principal_id)}
    by_id = {event.event_id: event for event in events}

    # Resolve first: a thread is answered as soon as any character send lands after the
    # question — the 30-minute window only decides whether a thread is *opened* at all.
    for thread in existing.values():
        source = by_id.get(thread.source_event_id)
        if source is None:
            continue
        if any(
            event.kind in _SENT_KINDS and event.occurred_at_ms > source.occurred_at_ms
            for event in events
        ):
            store.set_open_thread_status(thread.open_thread_id, status="resolved")

    for topic, source_event_id in wanted:
        if topic in existing:
            continue
        try:
            store.add_open_thread(
                actor_id=actor_id,
                principal_id=principal_id,
                topic=topic,
                source_event_id=source_event_id,
            )
        except Exception as exc:  # noqa: BLE001 — an open thread must never break a turn
            logger.warning(
                "companion open thread rejected",
                actor_id=actor_id,
                topic=topic[:40],
                error_type=type(exc).__name__,
            )


# ------------------------------------------------------------------------- helpers


def _boundary_phrase(text: str) -> str | None:
    for phrase in _BOUNDARY_PHRASES:
        if phrase in text:
            return phrase
    return None


def _boundary_topic(text: str, phrase: str) -> str:
    """Topic keyword from the clause before the refusal, stripped of generic fillers.

    "加班这个话题不想聊这个了" → "加班", so a later send that merely mentions 加班 is
    recognised as revisiting the boundary (matching the whole clause would never fire).
    """
    head = text.split(phrase, 1)[0]
    topic = _TOPIC_TRIM.sub(" ", head).strip()
    for filler in _TOPIC_FILLERS:
        topic = topic.removesuffix(filler).strip()
    return topic[:MAX_TOPIC_CHARS] or "unspecified"


def _topic(text: str) -> str:
    return _TOPIC_TRIM.sub(" ", text).strip()[:MAX_TOPIC_CHARS]


def _day(ms: int) -> str:
    from gacore.companion.store import day_key

    return day_key(ms)
