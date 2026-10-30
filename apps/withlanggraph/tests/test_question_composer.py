"""出题人（question_composer）与 3.0 接线的封闭测试：T-C1~T-C6 + 拍板 2/3/4 接线。"""

from __future__ import annotations

import json
from typing import Any

from gacore.config import Config
from gacore.question_composer import LADDER_RULES, compose, composer_enabled
from gacore.question_surface import build_surface

_DAY = "2026-10-26"


def _surface(**overrides: Any) -> dict[str, Any]:
    """固定变化面：晚睡异常 + 提交序列（数字供 evidence 守卫核对）。"""
    surf = build_surface(Config.for_tests(_TMP), _DAY)
    if overrides.get("no_anomaly"):
        surf["anomalies"] = []
    else:
        surf["anomalies"] = [
            {
                "kind": "sleep_late",
                "days": ["2026-10-24", "2026-10-25", "2026-10-26"],
                "description": "近 3 天入睡 01:10，基线 23:20，平均推迟约 110 分钟",
                "evidence": {"baseline_mean_min": 1400},
            }
        ]
    surf["context"]["commits_by_day"] = {"2026-10-24": 12, "2026-10-25": 18, "2026-10-26": 45}
    surf["asked_recent"] = []
    return surf


from pathlib import Path

_TMP = None  # 由各测试的 cfg fixture 赋值（见下）


def _cfg(tmp_path: Path) -> Config:
    global _TMP
    _TMP = tmp_path
    return Config.for_tests(tmp_path)


def _llm_reply(payload: dict[str, Any]):
    return lambda prompt: json.dumps(payload, ensure_ascii=False)


# T-C1：晚睡异常 → L3 归因式单问，种入 kind=composed
def test_t_c1_composes_l3_and_seeds(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    surf = _surface()
    monkeypatch.setattr("gacore.question_composer._call_llm", _llm_reply({
        "question": "连着几晚都折腾到挺晚——最近工作上是有点压着吗？",
        "depth": "L3",
        "topic": "工作压力",
        "evidence": ["异常1"],
    }))
    out = compose(cfg, _DAY, surface=surf)
    assert out is not None and out["depth"] == "L3" and out["topic"] == "工作压力"
    from gacore.qa_store import open_questions

    rows = open_questions(cfg)
    assert len(rows) == 1
    assert rows[0]["kind"] == "composed" and rows[0]["source"] == "composed"
    assert rows[0]["topic"] == "工作压力"
    assert rows[0]["question_key"] == out["key"]


# T-C2：无异常时 LLM 硬要 L3 → 降级 L1；无异常且 L1 闲聊 → 照常
def test_t_c2_l3_downgraded_without_anomaly(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    surf = _surface(no_anomaly=True)
    monkeypatch.setattr("gacore.question_composer._call_llm", _llm_reply({
        "question": "破门而入后来自己上手了吗？", "depth": "L3", "topic": "游戏", "evidence": [],
    }))
    out = compose(cfg, _DAY, surface=surf)
    assert out is not None and out["depth"] == "L1"


# T-C3：evidence 守卫——问题里的数字在素材中找不到 → 拒绝
def test_t_c3_evidence_guard_rejects_fabricated_numbers(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    surf = _surface()
    monkeypatch.setattr("gacore.question_composer._call_llm", _llm_reply({
        "question": "看了 99 小时的深空当铺，是不是该睡了？", "depth": "L1", "topic": "游戏", "evidence": [],
    }))
    assert compose(cfg, _DAY, surface=surf) is None
    from gacore.qa_store import open_questions

    assert open_questions(cfg) == []


# T-C4：同 topic 7 天冷却——第二次出题被拒
def test_t_c4_topic_cooldown(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    surf = _surface()
    reply = _llm_reply({
        "question": "连着几晚都折腾到挺晚——最近工作上是有点压着吗？",
        "depth": "L3", "topic": "工作压力", "evidence": ["异常1"],
    })
    monkeypatch.setattr("gacore.question_composer._call_llm", reply)
    assert compose(cfg, _DAY, surface=surf) is not None
    monkeypatch.setattr("gacore.question_composer._call_llm", _llm_reply({
        "question": "这几晚都熬到很晚，是不是活太多了？", "depth": "L3", "topic": "工作压力", "evidence": [],
    }))
    assert compose(cfg, _DAY, surface=surf) is None  # 同 topic 冷却


# T-C5/T-C6：开关关闭不出题；LLM 挂掉 fail-open 返回 None
def test_t_c5_disabled_and_t_c6_llm_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (cfg.root / "config").mkdir(parents=True, exist_ok=True)
    (cfg.root / "config" / "proactive.json").write_text(
        json.dumps({"composer": {"enabled": False}}), encoding="utf-8"
    )
    assert composer_enabled(cfg) is False
    assert compose(cfg, _DAY, surface=_surface()) is None

    (cfg.root / "config" / "proactive.json").write_text(
        json.dumps({"composer": {"enabled": True}}), encoding="utf-8"
    )

    def _boom(prompt):
        raise RuntimeError("api down")

    monkeypatch.setattr("gacore.question_composer._call_llm", _boom)
    assert compose(cfg, _DAY, surface=_surface()) is None


# 拍板 2：composed-only 推送走 verbatim_single（无编号、一字不改）
def test_verbatim_single_prompt_form(tmp_path):
    from gacore.scheduler import Job
    from gacore.proactive import build_proactive_prompt
    from datetime import datetime

    job = Job(name="daily-questions", schedule="08:30", prompt="主动询问")
    now = datetime(2026, 10, 27, 8, 30)
    q = "连着几晚都折腾到挺晚——最近工作上是有点压着吗？"
    verbatim = build_proactive_prompt(job, "u", now, questions=[q], verbatim_single=True)
    assert "一字不改" in verbatim and "不加编号" in verbatim and q in verbatim
    assert "[Q1]" not in verbatim
    numbered = build_proactive_prompt(job, "u", now, questions=["问题A", "问题B"])
    # 缺口 5：问题正文只进背景 JSON，prompt 只保留 [Qn] 编号规则。
    assert "open_questions" in numbered and "[Q1]" in numbered
    assert '"open_questions": ["问题A", "问题B"]' in numbered


# 拍板 4：静音豁免——mute_exemption 跳过 globally_muted 闸
def test_mute_exemption_skips_global_mute(tmp_path):
    from gacore.scheduler import Job
    from gacore.proactive import job_guard_allows

    job = Job(name="daily-questions", schedule="08:30", prompt="主动询问")
    state = {"u_u1": {"muted_until": "2026-11-01T00:00:00+08:00"}}
    from datetime import datetime, timezone, timedelta

    now = datetime(2026, 10, 27, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    allowed, reason = job_guard_allows("u1", job, state, now)
    assert not allowed and reason == "globally_muted"
    allowed2, reason2 = job_guard_allows("u1", job, state, now, mute_exemption=True)
    assert allowed2 and reason2 == ""


# 拍板 3：日报 job prompt 注入共享阶梯铁律（与 composer 同一文本源）
def test_daily_prompt_injects_ladder(tmp_path, monkeypatch):
    from gacore.scheduler import Job, _build_job_prompt

    cfg = Config.for_tests(tmp_path)
    job = Job(name="daily-report", schedule="23:50", prompt="日报指令")
    prompt = _build_job_prompt(job, cfg)
    assert "提问阶梯铁律" in prompt and "归因关心" in prompt
    # 单一文本源：与 composer 的 LADDER_RULES 同一常量
    assert LADDER_RULES in prompt
    # 非日报 job 不注入
    other = _build_job_prompt(Job(name="one-off", schedule="every 1d", prompt="别的指令"), cfg)
    assert "提问阶梯铁律" not in other
