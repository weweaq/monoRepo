"""Minimal ``companion`` CLI (V2-Step 7).

Design: ``docs/companion-self-memory-v2-plan.md`` V2-Step 7 — everything the character
derives must be inspectable from the shell, and the destructive-looking commands are
preview-only unless the reader explicitly confirms.

Usage::

    python -m gacore.companion.cli status
    python -m gacore.companion.cli events --day 2026-10-30
    python -m gacore.companion.cli reflections --with-sources
    python -m gacore.companion.cli journals --day 2026-10-30
    python -m gacore.companion.cli commitments
    python -m gacore.companion.cli goals
    python -m gacore.companion.cli export --actor hanli --day 2026-10-30
    python -m gacore.companion.cli correct --source evt_x --dry-run
    python -m gacore.companion.cli reset --dry-run
    python -m gacore.companion.cli rebuild --projection relationship [--apply]

Safety rules baked in:

- read commands never write;
- ``reset`` only previews the blast radius (a real delete needs the documented procedure);
- ``rebuild`` re-runs projections from source events only — it never sends a message or
  replays an external action.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from gacore.companion.store import (
    CompanionStore,
    day_key,
)

DEFAULT_ACTOR: Final = "assistant-default"
PROJECTIONS: Final = ("relationship", "mood")


def _store(cfg_root: Path | None = None) -> CompanionStore:
    from gacore.config import Config

    cfg = Config.default() if cfg_root is None else Config.for_tests(cfg_root)
    return CompanionStore.from_config(cfg)


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def cmd_status(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    relationship = store.relationship(actor, "owner")
    return {
        "actor": actor,
        "schema_version": _schema_version(store),
        "events": len(store.events_for_actor(actor, "owner")),
        "reflections": len(store.latest_reflections(actor, "owner", limit=1000)),
        "journals": len(store.journals_for_day(actor, "owner", day_key(_now_ms()))),
        "commitments": len(store.commitments(actor, "owner")),
        "goals": len(store.goals(actor, "owner")),
        "activities": len(store.activities(actor, "owner")),
        "relationship": None
        if relationship is None
        else {
            "stage": relationship.stage,
            "status": relationship.status,
            "meaningful_interactions": relationship.meaningful_interactions,
            "active_days": relationship.active_days,
        },
        "projections": {
            name: store.projection_state(name) for name in PROJECTIONS
        },
    }


def cmd_events(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    day = args.day or day_key(_now_ms())
    events = store.events_for_day(actor, "owner", day)
    return {
        "actor": actor,
        "day": day,
        "count": len(events),
        "events": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "summary": event.summary,
                "occurred_at_ms": event.occurred_at_ms,
            }
            for event in events
        ],
    }


def cmd_reflections(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    rows = store.latest_reflections(actor, "owner", limit=200, status="active")
    if args.all_statuses:
        rows = store.reflections_for_events(
            [event.event_id for event in store.events_for_actor(actor, "owner")]
        ) or rows
    payload = []
    for row in rows:
        item: dict[str, object] = {
            "reflection_id": row.reflection_id,
            "statement": row.statement,
            "confidence": row.confidence,
            "status": row.status,
            "created_at": row.created_at,
        }
        if args.with_sources:
            item["source_event_ids"] = list(store.reflection_sources(row.reflection_id))
        payload.append(item)
    return {"actor": actor, "count": len(payload), "reflections": payload}


def cmd_journals(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    day = args.day or day_key(_now_ms())
    rows = store.journals_for_day(actor, "owner", day)
    return {
        "actor": actor,
        "day": day,
        "journals": [
            {
                "visibility": row.visibility,
                "status": row.status,
                "body": row.body,
                "source_event_ids": list(store.journal_sources(row.journal_id)[0]),
            }
            for row in rows
        ],
    }


def cmd_commitments(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    return {
        "actor": actor,
        "commitments": [
            {
                "commitment_id": item.commitment_id,
                "status": item.status,
                "quote": item.quote,
                "action": item.action,
                "due_at_ms": item.due_at_ms,
                "source_event_id": item.source_event_id,
            }
            for item in store.commitments(actor, "owner")
        ],
    }


def cmd_goals(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    return {
        "actor": actor,
        "goals": [
            {
                "goal_id": goal.goal_id,
                "status": goal.status,
                "text": goal.text,
                "allowed_tools": list(goal.allowed_tools),
                "deadline_ms": goal.deadline_ms,
                "result_event_id": goal.result_event_id,
            }
            for goal in store.goals(actor, "owner")
        ],
    }


def cmd_export(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    actor = args.actor or DEFAULT_ACTOR
    day = args.day or day_key(_now_ms())
    return {
        "actor": actor,
        "day": day,
        "events": cmd_events(store, args)["events"],
        "journals": cmd_journals(store, args)["journals"],
        "commitments": cmd_commitments(store, args)["commitments"],
        "goals": cmd_goals(store, args)["goals"],
        "projections": {name: store.projection_state(name) for name in PROJECTIONS},
    }


def cmd_correct(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    """Preview the blast radius of correcting one source event (never mutates)."""
    actor = args.actor or DEFAULT_ACTOR
    reflections = store.reflections_for_events([args.source])
    journals = [
        journal
        for journal in store.journals_for_day(actor, "owner", args.day or day_key(_now_ms()))
        if args.source in store.journal_sources(journal.journal_id)[0]
    ]
    return {
        "dry_run": args.dry_run,
        "source_event_id": args.source,
        "reflections_to_mark_stale": [row.reflection_id for row in reflections],
        "journals_to_mark_stale": [journal.journal_id for journal in journals],
        "note": "confirmation is manual: run `python -m gacore.companion.cli rebuild` after"
        " marking, or edit the row with mark_stale()/retract()",
    }


def cmd_reset(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    """Preview what a reset would drop; a real delete stays a documented manual step."""
    actor = args.actor or DEFAULT_ACTOR
    scope = {
        "events": len(store.events_for_actor(actor, "owner")),
        "reflections": len(
            store.latest_reflections(actor, "owner", limit=1000, status="active")
        ),
        "commitments": len(store.commitments(actor, "owner")),
        "goals": len(store.goals(actor, "owner")),
        "activities": len(store.activities(actor, "owner")),
        "preferences": len(store.preferences(actor, "owner")),
        "outreach": len(store.outreach_rows(actor, "owner")),
    }
    if not args.dry_run:
        raise SystemExit(
            "reset without --dry-run is intentionally unsupported; export first "
            "(`companion export`) then delete the companion.db rows manually"
        )
    return {"dry_run": True, "actor": actor, "would_delete": scope}


def cmd_rebuild(store: CompanionStore, args: argparse.Namespace) -> dict[str, object]:
    """Recompute one derived projection from source events (no message, no action)."""
    if args.projection not in PROJECTIONS:
        raise SystemExit(f"projection must be one of {PROJECTIONS}")
    actor = args.actor or DEFAULT_ACTOR
    before = store.projection_state(args.projection)
    if not args.apply:
        return {
            "dry_run": True,
            "projection": args.projection,
            "actor": actor,
            "current": before,
            "note": "add --apply to recompute from events (read-only path)",
        }
    if args.projection == "relationship":
        from gacore.companion.relationship import refresh_relationship

        refresh_relationship(store, actor, "owner")
    else:
        from gacore.companion.mood import refresh_mood

        refresh_mood(store, actor, "owner")
    return {
        "dry_run": False,
        "projection": args.projection,
        "actor": actor,
        "before": before,
        "after": store.projection_state(args.projection),
    }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _schema_version(store: CompanionStore) -> int:
    with store._connect() as conn:  # noqa: SLF001 — read-only diagnostics
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="companion", description="Companion self-memory CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_actor(p: argparse.ArgumentParser) -> None:
        p.add_argument("--actor", default=None)

    status = sub.add_parser("status", help="counts + relationship + projection versions")
    with_actor(status)
    status.set_defaults(func=cmd_status)

    events = sub.add_parser("events", help="events of one day")
    with_actor(events)
    events.add_argument("--day", default=None)
    events.set_defaults(func=cmd_events)

    reflections = sub.add_parser("reflections", help="injectable reflections")
    with_actor(reflections)
    reflections.add_argument("--with-sources", action="store_true")
    reflections.add_argument("--all-statuses", action="store_true")
    reflections.set_defaults(func=cmd_reflections)

    journals = sub.add_parser("journals", help="two journals of one day")
    with_actor(journals)
    journals.add_argument("--day", default=None)
    journals.set_defaults(func=cmd_journals)

    commitments = sub.add_parser("commitments", help="promises the character made")
    with_actor(commitments)
    commitments.set_defaults(func=cmd_commitments)

    goals = sub.add_parser("goals", help="goals and their outcomes")
    with_actor(goals)
    goals.set_defaults(func=cmd_goals)

    export = sub.add_parser("export", help="one actor/day export as JSON")
    with_actor(export)
    export.add_argument("--day", default=None)
    export.set_defaults(func=cmd_export)

    correct = sub.add_parser("correct", help="preview a source correction")
    with_actor(correct)
    correct.add_argument("--source", required=True)
    correct.add_argument("--day", default=None)
    correct.add_argument("--dry-run", action="store_true", default=False)
    correct.set_defaults(func=cmd_correct)

    reset = sub.add_parser("reset", help="preview a reset scope")
    with_actor(reset)
    reset.add_argument("--dry-run", action="store_true", default=False)
    reset.set_defaults(func=cmd_reset)

    rebuild = sub.add_parser("rebuild", help="recompute one projection from events")
    with_actor(rebuild)
    rebuild.add_argument("--projection", required=True)
    rebuild.add_argument("--apply", action="store_true")
    rebuild.set_defaults(func=cmd_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store()
    try:
        payload = args.func(store, args)
    except SystemExit as exc:
        print(str(exc))
        return 2
    print(_dump(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
