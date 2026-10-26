"""日报正文推 QQ（_split_for_qq / _push_daily_to_qq）的封闭测试。"""

from __future__ import annotations

from gacore.config import Config
from gacore.scheduler import _plain_for_qq, _push_daily_to_qq, _split_for_qq


def test_plain_for_qq_strips_markdown_noise():
    body = "- [关键变化-1] **连续两天**陪诊——`apps/<app>/docs/ROADMAP.md` 收尾"
    out = _plain_for_qq(body)
    assert "**" not in out and "`" not in out
    assert "连续两天" in out and "apps/<app>/docs/ROADMAP.md" in out


def test_split_short_body_single_segment():
    body = "# 今日状态\n- [今日状态-1] ok\n"
    assert _split_for_qq(body) == [body.rstrip("\n")]


def test_split_by_bytes_long_body_splits():
    line = "- " + "字" * 80  # 每行 ~242 字节
    body = "\n".join([line] * 30)  # ~7.2K 字节 → 超过 6000 字节上限，切 2 段
    segments = _split_for_qq(body)
    assert len(segments) == 2
    for seg in segments:
        assert len(seg.encode("utf-8")) <= 6000 + len("〔日报 9/9〕\n".encode("utf-8"))
        assert "字字字" in seg  # 行不被腰斩
    assert segments[0].startswith("〔日报 1/2〕")


def test_typical_report_fits_single_segment():
    """典型日报（~800 字 / ~1.7K 字节）单条送达，不再切两段。"""
    body = "# 今日状态\n- [今日状态-1] " + "好" * 900 + "\n\n# 想问你\n- [想问你-1] q（依据：a）\n"
    segments = _split_for_qq(body)
    assert len(segments) == 1
    assert "〔日报" not in segments[0]


def test_push_daily_carries_questions_and_records_touch(tmp_path, monkeypatch):
    """3.0 决策 D-A 随报问：夜间正文带想问你节，推送成功即 asks=1 + keys 快照（裸回答当晚生效）。"""
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
    assert len(calls) == 1  # 典型日报 ~2.9K 字节单条送达
    assert "你是在读还是上班" in calls[0]  # 问题随报走，不剥节
    assert "[想问你-1] 你的答案" in calls[0]  # 指引给正文锚点话术
    assert "明早" not in calls[0]  # 不再预告次日单独推
    assert open_questions(cfg)[0]["asks"] == 1  # 夜间即触达一次
    state = (tmp_path / "data" / "qa" / "open_state.json").read_text(encoding="utf-8")
    assert open_questions(cfg)[0]["question_key"] in __import__("json").loads(state)["keys"]


def test_push_daily_without_questions_records_nothing(tmp_path, monkeypatch):
    """正文无问题节：指引只留订正；不写 keys 快照、不记触达。"""
    cfg = Config.for_tests(tmp_path)
    calls: list[str] = []

    async def fake_send(content, targets, is_sandbox):
        calls.append(content)
        return {"ok": len(targets), "failures": [], "errors": {}, "ids": {}}

    monkeypatch.setattr("gacore.langTrack.qq_push.send_c2c", fake_send)
    monkeypatch.setattr("gacore.langTrack.qq_push.load_known_users", lambda: {"A" * 32: {}})
    _push_daily_to_qq(cfg, "2026-10-25", "# 今日状态\n- ok\n")
    assert "订正" in calls[-1]
    assert "想问你" not in calls[-1] and "[想问你-1]" not in calls[-1]
    state = (tmp_path / "data" / "qa" / "open_state.json")
    assert not state.exists() or __import__("json").loads(state.read_text(encoding="utf-8"))["keys"] == []


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
