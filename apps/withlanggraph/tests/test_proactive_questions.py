"""P3 提问回路的接线测试：scheduler 种入、proactive questions 场景、QQ 路由顺序。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from gacore.config import Config
from gacore.proactive import build_proactive_prompt, run_proactive_job
from gacore.qa_store import open_questions, seed_questions
from gacore.scheduler import Job, _seed_qa


def test_seed_qa_extracts_stamped_section(tmp_path):
    cfg = Config.for_tests(tmp_path)
    delivered = (
        "# 今日状态\n- [今日状态-1] ok\n\n# 想问你\n"
        "- [想问你-1] 你是在读还是上班？（依据：10-25 浏览）\n\n# 明日计划\n- [明日计划-1] x\n"
    )
    _seed_qa(cfg, "2026-10-25", delivered)
    open_ = open_questions(cfg)
    assert len(open_) == 1
    assert open_[0]["anchor"] == "想问你-1"

    # 正文无该节：无事发生不崩
    _seed_qa(cfg, "2026-10-26", "# 今日状态\n- [今日状态-1] 无问题\n")
    assert len(open_questions(cfg)) == 1


def test_build_prompt_relays_questions_verbatim():
    job = Job(name="daily-questions", schedule="08:30", prompt="", scene="questions")
    prompt = build_proactive_prompt(
        job,
        "u1",
        datetime(2026, 10, 26, 8, 30),
        questions=["你是在读还是上班？（依据：10-25）", "工学椅还买吗？（依据：长期画像）"],
    )
    assert "[Q1] 你是在读还是上班？（依据：10-25）" in prompt
    assert "[Q2] 工学椅还买吗？（依据：长期画像）" in prompt
    assert "逐条原样转述" in prompt
    assert "[Q1] 你的答案" in prompt


def test_questions_scene_skips_without_open_questions(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    monkeypatch.setattr("gacore.proactive._target_users", lambda job: ["u1"])
    job = Job(name="daily-questions", schedule="08:30", prompt="", scene="questions")
    result = run_proactive_job(job, cfg, clock=lambda: datetime(2026, 10, 26, 8, 30))
    assert result["skipped"] == [{"user": "", "reason": "no_open_questions"}]
    assert result["sent"] == 0


def test_questions_scene_push_marks_asks(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    seed_questions(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    monkeypatch.setattr("gacore.proactive._target_users", lambda job: ["u1"])
    captured: dict = {}

    def fake_headless(job, cfg_, prompt, max_turns):
        captured["prompt"] = prompt
        return "END", "转述完成", [json.dumps({"status": "sent", "to": ["u1"]})]

    monkeypatch.setattr("gacore.proactive._headless_run", fake_headless)
    monkeypatch.setattr("gacore.proactive._write_back_proactive", lambda *a, **k: False)
    job = Job(name="daily-questions", schedule="08:30", prompt="", scene="questions")
    result = run_proactive_job(job, cfg, clock=lambda: datetime(2026, 10, 26, 8, 30))
    assert result["sent"] == 1
    assert "[Q1]" in captured["prompt"]
    row = open_questions(cfg)[0]
    assert row["asks"] == 1


def test_qq_routing_places_qa_capture_before_feedback():
    """决策 8 的接线属性：try_answer 捕获必须在 feedback 分发之前（源序断言）。"""
    src = Path(__file__).resolve().parents[1] / "src" / "gacore" / "frontends" / "qq.py"
    text = src.read_text(encoding="utf-8")
    assert text.index("qa_reply = try_answer(") < text.index("asyncio.create_task(\n                    self._handle_feedback(")
