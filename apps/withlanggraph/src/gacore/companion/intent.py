"""Unified intent cards (V3): one place for everything the character decides on its own.

Design note: a *promise* ("I said I'd check that"), an *action* ("go read the B站 history"),
and a *watch* ("the owner said they finish work at six — ask them at six") are the same
object with different fields:

    provenance   who asked for it   (character_send / owner_statement / reflection)
    trigger_at   when it becomes live
    authority    what it may do     (read-only tools, may it speak)
    status       where it is now    (planned → armed → running → done/failed/expired/cancelled)

The old ``companion_goal`` table is copied into this table by migration v9 and is kept
read-only for a while; new code writes intents only.
"""

from __future__ import annotations

from typing import Final

from gacore.companion.store import CompanionStore, Intent
from gacore.jsonl_logger import get_logger

logger = get_logger("companion.intent")

INTENT_VERSION: Final = "companion-intent@v1"
MAX_TITLE_CHARS: Final = 120
MAX_WATCH_PER_DAY: Final = 5


def authority(
    tools: tuple[str, ...] | list[str] = (),
    *,
    may_speak: bool = False,
    max_llm_calls: int = 1,
    deadline_ms: int | None = None,
) -> dict[str, object]:
    """The rights carried by one card; executors read this, never a global switch."""
    return {
        "tools": list(tools),
        "may_speak": bool(may_speak),
        "max_llm_calls": int(max_llm_calls),
        "deadline_ms": deadline_ms,
    }


def create_act(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    title: str,
    tools: tuple[str, ...] | list[str],
    provenance: str = "reflection",
    source_event_id: str | None = None,
    trigger_at_ms: int | None = None,
    expires_at_ms: int | None = None,
    success_criteria: str = "",
    commitment_id: str | None = None,
    status: str = "planned",
) -> Intent:
    """An intent that *does* something with read-only tools."""
    return store.add_intent(
        actor_id=actor_id,
        principal_id=principal_id,
        kind="act",
        title=title[:MAX_TITLE_CHARS],
        provenance=provenance,
        source_event_id=source_event_id,
        trigger_at_ms=trigger_at_ms,
        expires_at_ms=expires_at_ms,
        status=status,
        authority=authority(tools),
        payload={"success_criteria": success_criteria},
        commitment_id=commitment_id,
    )


def create_watch(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    title: str,
    trigger_at_ms: int,
    expires_at_ms: int | None = None,
    provenance: str = "owner_statement",
    source_event_id: str | None = None,
    question_hint: str = "",
    status: str = "armed",
) -> Intent:
    """An intent that *waits*: at ``trigger_at_ms`` it may become a question."""
    return store.add_intent(
        actor_id=actor_id,
        principal_id=principal_id,
        kind="watch",
        title=title[:MAX_TITLE_CHARS],
        provenance=provenance,
        source_event_id=source_event_id,
        trigger_at_ms=trigger_at_ms,
        expires_at_ms=expires_at_ms,
        status=status,
        authority=authority(may_speak=True),
        payload={"question_hint": question_hint},
    )


def due(store: CompanionStore, actor_id: str, principal_id: str, *, now_ms: int) -> list[Intent]:
    """Close overdue cards, then return the ones that are live right now."""
    expired = store.expire_intents(actor_id, principal_id, now_ms=now_ms)
    if expired:
        logger.info("companion intents expired", count=expired, actor_id=actor_id)
    return store.due_intents(actor_id, principal_id, now_ms=now_ms)


def arm(store: CompanionStore, intent_id: str) -> Intent:
    return store.set_intent_status(intent_id, status="armed")


def start(store: CompanionStore, intent_id: str) -> Intent:
    return store.set_intent_status(intent_id, status="running")


def finish(store: CompanionStore, intent_id: str, *, result_event_id: str | None = None) -> Intent:
    return store.set_intent_status(
        intent_id, status="done", result_event_id=result_event_id
    )


def fail(store: CompanionStore, intent_id: str, *, result_event_id: str | None = None) -> Intent:
    return store.set_intent_status(
        intent_id, status="failed", result_event_id=result_event_id
    )


def cancel(store: CompanionStore, intent_id: str) -> Intent:
    return store.set_intent_status(intent_id, status="cancelled")


def extend(store: CompanionStore, intent_id: str, *, trigger_at_ms: int) -> Intent:
    """Move a card's trigger later (e.g. "actually I work until nine")."""
    return store.set_intent_trigger(intent_id, trigger_at_ms=trigger_at_ms)


def view(store: CompanionStore, actor_id: str, principal_id: str) -> list[dict[str, object]]:
    """Human-readable card list for the CLI."""
    return [
        {
            "intent_id": item.intent_id,
            "kind": item.kind,
            "status": item.status,
            "title": item.title,
            "trigger_at_ms": item.trigger_at_ms,
            "expires_at_ms": item.expires_at_ms,
            "provenance": item.provenance,
            "source_event_id": item.source_event_id,
            "result_event_id": item.result_event_id,
        }
        for item in store.intents(actor_id, principal_id)
    ]
