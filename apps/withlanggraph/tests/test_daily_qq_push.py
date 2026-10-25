"""日报正文推 QQ（_split_for_qq / _push_daily_to_qq）的封闭测试。"""

from __future__ import annotations

from gacore.config import Config
from gacore.scheduler import _push_daily_to_qq, _split_for_qq


def test_split_short_body_single_segment():
    body = "# 今日状态\n- [今日状态-1] ok\n"
    assert _split_for_qq(body) == [body.rstrip("\n")]


def test_split_long_body_line_boundary_and_headers():
    line = "- " + "字" * 80
    body = "\n".join([line] * 30)  # ~2430 字 → 4 段左右
    segments = _split_for_qq(body, limit=600)
    assert len(segments) > 1
    for seg in segments:
        assert len(seg) <= 600 + len("〔日报 9/9〕\n")  # 标头开销内
        assert "字字字" in seg  # 行不被腰斩
    assert segments[0].startswith("〔日报 1/")
    assert f"〔日报 1/{len(segments)}〕" in segments[0]


def test_push_daily_segments_and_marks_touch(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    from gacore.qa_store import open_questions, seed_questions

    seed_questions(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])

    calls: list[str] = []

    async def fake_send(content, targets, is_sandbox):
        calls.append(content)
        return {"ok": len(targets), "failures": [], "errors": {}, "ids": {}}

    monkeypatch.setattr("gacore.langTrack.qq_push.send_c2c", fake_send)
    monkeypatch.setattr(
        "gacore.langTrack.qq_push.load_known_users", lambda: {"A" * 32: {}}
    )

    body = "# 今日状态\n- [今日状态-1] " + "好" * 900 + "\n\n# 想问你\n- [想问你-1] 你是在读还是上班？（依据：10-25）\n"
    sent = _push_daily_to_qq(cfg, "2026-10-25", body)
    assert sent == len(calls)  # 每段成功送达 1 人，累计口径
    assert len(calls) >= 2  # 正文分段
    assert any("[Q1]" in c for c in calls)  # 回复指引随行
    # 当日问题计一次触达 → 裸回答当晚可用
    assert open_questions(cfg)[0]["asks"] == 1


def test_push_daily_no_users_noop(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    monkeypatch.setattr("gacore.langTrack.qq_push.load_known_users", lambda: {})
    assert _push_daily_to_qq(cfg, "2026-10-25", "# 今日状态\n- ok\n") == 0


def test_push_daily_filters_placeholder_targets(tmp_path, monkeypatch):
    """known_users 里的占位残留（user-1/admin-1）不是合法 openid，推送只发真实格式目标。"""
    cfg = Config.for_tests(tmp_path)
    calls: list[list[str]] = []

    async def fake_send(content, targets, is_sandbox):
        calls.append(list(targets))
        return {"ok": len(targets), "failures": [], "errors": {}, "ids": {}}

    monkeypatch.setattr("gacore.langTrack.qq_push.send_c2c", fake_send)
    monkeypatch.setattr(
        "gacore.langTrack.qq_push.load_known_users",
        lambda: {
            "5270431CCFE7063EA0B30D0D396D91BF": {},
            "user-1": {},
            "admin-1": {},
        },
    )
    sent = _push_daily_to_qq(cfg, "2026-10-25", "# 今日状态\n- ok\n")
    assert sent == 1
    assert calls == [["5270431CCFE7063EA0B30D0D396D91BF"]]
