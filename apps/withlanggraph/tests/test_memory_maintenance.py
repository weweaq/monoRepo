"""Tests for gacore.memory_maintenance: event-driven long-term memory maintenance.

Covers the three-phase pipeline (trigger → judge → apply) plus the end-to-end
maintain_once entry and the graph node wiring. The LLM is never exercised: the judge
is injected as a deterministic fake, and Config.for_tests keeps all writes in tmp.
"""

from __future__ import annotations

import re
from pathlib import Path

from gacore.config import Config
from gacore.memory_maintenance import (
    VERDICT_MERGE,
    VERDICT_NEW,
    VERDICT_NOOP,
    Verdict,
    apply,
    build_llm_judge,
    judge_relation,
    maintain_once,
    should_evoke,
)
from gacore.graph import memory_cfg_context, memory_maintain
from langchain_core.messages import HumanMessage


# --------------------------------------------------------------------------- trigger


def test_should_evoke_true_on_persona_anchor() -> None:
    """Given a user text carrying a persona anchor, When checked, Then it triggers."""
    assert should_evoke("婚期改成9月12号了")
    assert should_evoke("我换工作了，新公司在科技园")
    assert should_evoke("这周去体检，血压有点高")


def test_should_evoke_false_on_routine_chitchat() -> None:
    """Given routine chat with no persona anchor, When checked, Then it stays silent."""
    assert not should_evoke("哈哈今天好热")
    assert not should_evoke("晚上吃啥")
    assert not should_evoke("")


def test_should_evoke_false_on_image_only_marker() -> None:
    """Given an empty/whitespace text, When checked, Then it does not trigger."""
    assert not should_evoke("   ")


# --------------------------------------------------------------------------- judge


def _fake_maker(action: str, revised: str = ""):
    def _judge(portrait: str, fact: str) -> Verdict:
        return Verdict(action=action, fact=revised or fact, reason=f"test:{action}")
    return _judge


def test_judge_relation_delegates_to_judge_and_returns_verdict() -> None:
    """Given a judge callable, When judge_relation runs, Then it returns the judge's Verdict."""
    judge = _fake_maker(VERDICT_MERGE, revised="[婚姻·登记日期] 改为 2026-09-12（原 2026-09-08）")
    v = judge_relation("portrait...", "婚期改成9月12", judge)
    assert v.action == VERDICT_MERGE
    assert "2026-09-12" in v.fact
    assert v.reason.startswith("test:")


def test_build_llm_judge_degrades_to_noop_on_non_verdict() -> None:
    """Given a structured judge returning a bad action, When judging, Then it falls back to NOOP."""
    class Stub:
        def with_structured_output(self, schema):
            class _Inner:
                def invoke(self, prompt):
                    return type("R", (), {"action": "BOGUS", "revised": "x"})()
            return _Inner()
    judge = build_llm_judge(Config.for_tests(Path("nonexistent")), llm=Stub())
    v = judge("portrait", "fact")
    assert v.action == VERDICT_NOOP


def test_build_llm_judge_catches_llm_exception_to_noop() -> None:
    """Given a judge whose LLM invocation raises, When judging, Then it degrades to NOOP."""
    class Stub:
        def with_structured_output(self, schema):
            class _Inner:
                def invoke(self, prompt):
                    raise RuntimeError("boom")
            return _Inner()
    judge = build_llm_judge(Config.for_tests(Path("nonexistent")), llm=Stub())
    v = judge("portrait", "fact")
    assert v.action == VERDICT_NOOP
    assert v.reason.startswith("judge_error:")


# --------------------------------------------------------------------------- apply


def test_apply_merge_writes_dated_revision_to_both_files(tmp_path: Path) -> None:
    """Given a MERGE verdict, When applied, Then a dated revision line lands in both files."""
    cfg = Config.for_tests(tmp_path)
    v = Verdict(
        action=VERDICT_MERGE,
        fact="[婚姻·登记日期] 改为 2026-09-12（原 2026-09-08）",
        field_hint="婚姻·登记日期",
    )
    res = apply(v, cfg)
    facts = (cfg.memory_dir / "global_mem.txt").read_text(encoding="utf-8")
    insights = (cfg.memory_dir / "global_mem_insight.txt").read_text(encoding="utf-8")
    assert res["updated"] is True
    assert res["action"] == VERDICT_MERGE
    assert "[婚姻·登记日期]" in facts
    assert "2026-09-12" in facts
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", facts)
    assert "[婚姻·登记日期]" in insights
    assert insights.startswith("[")


def test_apply_new_appends_like_start_long_term_update(tmp_path: Path) -> None:
    """Given a NEW verdict, When applied, Then it appends and returns the uniform result shape."""
    cfg = Config.for_tests(tmp_path)
    res = apply(Verdict(action=VERDICT_NEW, fact="用户现在每周二固定健身"), cfg)
    facts = (cfg.memory_dir / "global_mem.txt").read_text(encoding="utf-8")
    assert res["updated"] is True
    assert "每周二固定健身" in facts
    assert res["paths"] == [
        str(cfg.memory_dir / "global_mem.txt"),
        str(cfg.memory_dir / "global_mem_insight.txt"),
    ]


def test_apply_noop_writes_nothing(tmp_path: Path) -> None:
    """Given a NOOP verdict, When applied, Then memory files stay untouched."""
    cfg = Config.for_tests(tmp_path)
    res = apply(Verdict(action=VERDICT_NOOP, fact=""), cfg)
    assert res["action"] == VERDICT_NOOP
    assert res["updated"] is False
    assert not (cfg.memory_dir / "global_mem.txt").exists()


def test_apply_creates_memory_dir_when_missing(tmp_path: Path) -> None:
    """Given a cfg whose memory_dir does not exist, When applied, Then the dir is created."""
    cfg = Config.for_tests(tmp_path)
    res = apply(Verdict(action=VERDICT_NEW, fact="fact"), cfg)
    assert res["updated"] is True
    assert cfg.memory_dir.is_dir()


def test_apply_strips_std_prefix_from_fact(tmp_path: Path) -> None:
    """Given a judge leaving a 用户近况： prefix, When applied, Then the prefix is stripped."""
    cfg = Config.for_tests(tmp_path)
    apply(Verdict(action=VERDICT_NEW, fact="用户近况：搬到了朝阳区"), cfg)
    lines = (cfg.memory_dir / "global_mem.txt").read_text(encoding="utf-8").splitlines()
    assert "用户近况：" not in lines[-1]
    assert "搬到了朝阳区" in lines[-1]


# --------------------------------------------------------------------------- maintain_once


def test_maintain_once_skips_when_no_anchor(tmp_path: Path) -> None:
    """Given a text with no persona anchor, When maintained, Then it early-skips with no LLM."""
    judge = _fake_maker(VERDICT_NEW)  # would write if called
    res = maintain_once(Config.for_tests(tmp_path), "今天天气不错哈哈", judge)
    assert res["action"] == "SKIP"
    assert res["triggered"] is False


def test_maintain_once_merges_on_anchor(tmp_path: Path) -> None:
    """Given a triggered text + a MERGE judge, When maintained, Then a revision is written."""
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "global_mem_insight.txt").parent.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "global_mem_insight.txt").write_text("旧画像：婚期2026-09-08", encoding="utf-8")
    judge = _fake_maker(VERDICT_MERGE, revised="[婚姻·登记日期] 改为 2026-09-12（原 2026-09-08）")
    res = maintain_once(cfg, "婚期改成9月12号了", judge)
    assert res["triggered"] is True
    assert res["action"] == VERDICT_MERGE
    facts = (cfg.memory_dir / "global_mem.txt").read_text(encoding="utf-8")
    assert "2026-09-12" in facts


# --------------------------------------------------------------------------- graph node


def test_graph_node_skips_and_writes_nothing_via_context(tmp_path: Path) -> None:
    """Given routine chat, When the node runs with a tmp cfg, Then it returns {} and writes nothing."""
    cfg = Config.for_tests(tmp_path)
    state = {"messages": [HumanMessage(content="今晚吃啥呀")]}
    with memory_cfg_context(cfg):
        out = memory_maintain(state)  # type: ignore[arg-type]
    assert out == {}
    assert not (cfg.memory_dir / "global_mem.txt").exists()


def test_graph_node_merges_via_context_judge_patch(tmp_path: Path, monkeypatch) -> None:
    """Given a triggered text and a patched judge, When the node runs, Then a revision is written."""
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "global_mem_insight.txt").parent.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "global_mem_insight.txt").write_text("旧画像：婚期2026-09-08", encoding="utf-8")

    from gacore import graph as graph_mod

    def fake_build(_cfg):
        def _judge(portrait, fact):
            return Verdict(action=VERDICT_MERGE, fact="[婚姻·登记日期] 改为 2026-09-12（原 2026-09-08）")
        return _judge

    monkeypatch.setattr(graph_mod, "build_llm_judge", fake_build)
    state = {"messages": [HumanMessage(content="婚期改成9月12号了")]}
    with memory_cfg_context(cfg):
        out = memory_maintain(state)  # type: ignore[arg-type]
    assert out == {}
    facts = (cfg.memory_dir / "global_mem.txt").read_text(encoding="utf-8")
    assert "2026-09-12" in facts


# --------------------------------------------------------------------------- vector sync (两步式：全量 + 当次增量)


def _patch_vector_store(monkeypatch) -> "tuple[object, object, object]":
    """Stub gacore.vector_store inside memory_maintenance; return the callables to assert on."""
    import gacore
    import types

    fake = types.SimpleNamespace(
        ensure_schema_calls=0,
        sync_portrait_calls=0,
        sync_line_calls=[],
    )

    def _ensure_schema():
        fake.ensure_schema_calls += 1
    def _sync_portrait(cfg):
        fake.sync_portrait_calls += 1
    def _sync_line(line, chunk_key=None):
        fake.sync_line_calls.append(line)

    # `_sync_vector_store` imports `from gacore import vector_store` inside the try-block.
    # Patch the package attribute on the already-cached `gacore` module so that import
    # resolves to our stub regardless of whether package-level imports preloaded it.
    monkeypatch.setattr(
        gacore, "vector_store",
        types.SimpleNamespace(
            ensure_schema=_ensure_schema,
            sync_portrait=_sync_portrait,
            sync_line=_sync_line,
        ),
    )
    return fake, _sync_portrait, _sync_line


def test_sync_vector_store_fans_out_extra_line(monkeypatch, tmp_path: Path) -> None:
    """Given a facts_statement, When persisting with sync, Then the full pass + incremental line run."""
    import gacore.memory_maintenance as mm

    fake, _sp, _sl = _patch_vector_store(monkeypatch)
    cfg = Config.for_tests(tmp_path)
    mm.persist_entry(
        cfg,
        fact_line="[婚姻] 婚期 2026-09-12 领证",
        insight_line="[婚姻] 婚期提前定档 2026-09-12",
        facts_statement="[婚姻·登记日期] 领证日期调整为 2026-09-12",
    )
    assert fake.ensure_schema_calls == 1
    assert fake.sync_portrait_calls == 1
    assert fake.sync_line_calls == ["[婚姻·登记日期] 领证日期调整为 2026-09-12"]  # incremental fanned out


def test_sync_vector_store_skips_incremental_when_no_statement(monkeypatch, tmp_path: Path) -> None:
    """Given no facts_statement, When persisting with sync, Then only the full pass runs (no incremental)."""
    import gacore.memory_maintenance as mm

    fake, _sp, _sl = _patch_vector_store(monkeypatch)
    cfg = Config.for_tests(tmp_path)
    mm.persist_entry(cfg, fact_line="x", insight_line="y", facts_statement="")
    assert fake.sync_portrait_calls == 1
    assert fake.sync_line_calls == []


def test_sync_vector_store_never_raises_on_backend_failure(monkeypatch, tmp_path: Path) -> None:
    """Given a throwing vector_store, When persisting, Then the write path still succeeds (best-effort)."""
    import gacore
    import gacore.memory_maintenance as mm
    import types

    monkeypatch.setattr(
        gacore, "vector_store",
        types.SimpleNamespace(
            ensure_schema=lambda: None,
            sync_portrait=lambda _cfg: (_ for _ in ()).throw(RuntimeError("pg down")),
        ),
    )
    cfg = Config.for_tests(tmp_path)
    res = mm.persist_entry(cfg, fact_line="a", insight_line="b", facts_statement="c")
    assert res["updated"] is True