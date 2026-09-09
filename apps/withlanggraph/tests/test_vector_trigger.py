"""Tests for the (阶段二) vector/semantic memory trigger.

Covers VectorTrigger semantics-only recall and CombinedTrigger's keyword→vector
fallback chain. The embedding model / pgvector backend are the *real* integration points
and are deliberately faked here so the trigger logic is unit-testable offline and
deterministically: we monkeypatch vector_store.nearby and assert the trigger's decisions
and audit context. The true model+PG round-trip is exercised separately (embedding smoke
/ PG smoke) and lightly here.

The key behaviour under test: a message that shares *no literal keyword* with a portrait
line still fires when the vector backend reports a close match — and degrades cleanly
when the backend is unavailable.
"""

from __future__ import annotations

from unittest.mock import patch

import gacore.vector_store  # import target for patching (VectorTrigger does `from gacore import vector_store`)
from gacore.memory_maintenance import (
    CombinedTrigger,
    KeywordTrigger,
    TriggerResult,
    VectorTrigger,
)


# --------------------------------------------------------------------------- VectorTrigger


def test_vector_trigger_no_text() -> None:
    """Empty message never triggers, even if a backend were present."""
    t = VectorTrigger()
    assert t.probe("").triggered is False
    assert t.probe("  ").triggered is False
    assert t.probe(None).triggered is False  # type: ignore[arg-type]


def test_vector_trigger_invokes_backend_and_fires() -> None:
    """A close semantic match fires the trigger and carries recalled lines."""
    hits = [
        {"content": "家住朝阳区金地国际花园", "chunk_key": "fact", "dist": 0.12},
        {"content": "养了一只叫阿黄的柯基", "chunk_key": "fact", "dist": 0.55},
    ]
    with patch.object(gacore.vector_store, "nearby", return_value=hits) as m:
        res: TriggerResult = VectorTrigger().probe("那边的新家离地铁远吗")
    m.assert_called_once()
    assert res.triggered is True
    assert res.reason == "vector_hit"
    # only the close hit clears the bar; the far/dist-less pair still shapes context
    assert any("家住朝阳区" in line for line in res.matched)
    assert "家住朝阳区" in res.context


def test_vector_trigger_no_match_stays_silent() -> None:
    """Backend returns nothing → no trigger, reason stays generic."""
    with patch.object(gacore.vector_store, "nearby", return_value=[]):
        res = VectorTrigger().probe("今天天气怎么样")
    assert res.triggered is False


def test_vector_trigger_degrades_on_backend_failure() -> None:
    """PG/embedding outage never raises; reports vector_unavailable and stays silent."""
    with patch.object(
        gacore.vector_store, "nearby", side_effect=RuntimeError("pg down")
    ):
        res = VectorTrigger().probe("我搬家到朝阳区了")
    assert res.triggered is False
    assert res.reason == "vector_unavailable"


# --------------------------------------------------------------------------- CombinedTrigger


def test_combined_keyword_first_without_vector() -> None:
    """Keyword hit decides immediately; vector backend need not be touched."""
    with patch.object(gacore.vector_store, "nearby") as m:
        res = CombinedTrigger().probe("婚期改成12号了")
    assert res.triggered is True
    assert res.reason == "keyword_hit"
    m.assert_not_called()  # keyword already fired → vector skipped (cheap path)


def test_combined_vector_rescues_semantic_fact() -> None:
    """No keyword, but semantic recall finds a portrait line → fires as vector_hit."""
    kw_hits_false = TriggerResult(triggered=False)
    vec_hits = [
        {"content": "住在朝阳区，通勤地铁半小时", "chunk_key": "fact", "dist": 0.18}
    ]
    t = CombinedTrigger(
        keyword=KeywordTrigger(frozenset()),  # no anchors → keyword never fires
        vector=VectorTrigger(),
    )
    with patch.object(
        KeywordTrigger, "probe", return_value=kw_hits_false
    ), patch.object(gacore.vector_store, "nearby", return_value=vec_hits):
        res = t.probe("新家那站地铁站有点远呢")
    assert res.triggered is True
    assert res.reason == "vector_hit"
    assert "朝阳区" in res.context