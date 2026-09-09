"""daily_info_pack 空/满双向单测。

覆盖 build_info_pack 的每个确定性信息源（长期画像 / QQ 对话 / langTrack / bili /
Edge / git / 文件活动 / ncm / 前日日报），各构造空（失败/无数据→降级标注）与满
（大数据→被裁剪/计数正确）两种形态；并验证整包 ≤8000 字硬控与"任何源抛异常也
不中断整包"的兜底。

运行：PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_daily_info_pack.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


import gacore.daily_info_pack as dip
from gacore.config import Config


# --------------------------------------------------------------------------- #
# 工具                                                                        #
# --------------------------------------------------------------------------- #
def _cfg(tmp_path: Path) -> Config:
    """tmp 隔离 cfg，绝不触碰真实仓库。"""
    return Config.for_tests(tmp_path)


# --------------------------------------------------------------------------- #
# 长期画像：空 / 满                                                           #
# --------------------------------------------------------------------------- #
def test_long_term_empty(tmp_path):
    _header, body = dip._build_long_term_picture("2026-09-02", _cfg(tmp_path))
    assert "无长期画像文件" in body or "memory/global_mem_insight" in body


def test_long_term_full(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "global_mem_insight.txt").write_text("\n".join(f"line{i}" for i in range(60)), encoding="utf-8")
    _header, body = dip._build_long_term_picture("2026-09-02", cfg)
    assert "line0" in body
    assert len(body.splitlines()) <= dip._LONG_TERM_LINES + 1  # 40 行上限（+摘要标记行）


# --------------------------------------------------------------------------- #
# langTrack：空（无数据/失败）/ 满                                            #
# --------------------------------------------------------------------------- #
def test_langtrack_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_LANGTRACK_FN", lambda **k: {"available": False})
    _header, body = dip._build_langtrack("2026-09-02", _cfg(tmp_path))
    assert "无 langTrack" in body or "失败" in body


def test_langtrack_error(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_LANGTRACK_FN", lambda **k: {"error": "boom"})
    _header, body = dip._build_langtrack("2026-09-02", _cfg(tmp_path))
    assert "失败" in body


def test_langtrack_full(tmp_path, monkeypatch):
    fake = {
        "available": True,
        "screen_hours": 5.5,
        "unlock_count": 42,
        "notification_clicked": 7,
        "sleep_signal": "restful",
        "sleep_start_hhmm": "00:30",
        "sleep_end_hhmm": "08:00",
        "sleep_duration_min": 450,
        "top_apps": [{"app": "bilibili", "value": "90m"}, {"app": "code-editor"}],
        "time_app": [{"segment": "morning", "app": "code-editor"}],
    }
    monkeypatch.setattr(dip, "_LANGTRACK_FN", lambda **k: fake)
    _header, body = dip._build_langtrack("2026-09-02", _cfg(tmp_path))
    assert "42" in body
    assert "00:30" in body
    assert "bilibili" in body


# --------------------------------------------------------------------------- #
# B站：空（失败/无当日）/ 满（>20 条截断）                                    #
# --------------------------------------------------------------------------- #
def test_bili_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dip, "_BILLI_FN", lambda **k: {"error": "not_authenticated", "message": "登录过期"}
    )
    _header, body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    assert "失败" in body


def test_bili_no_today(tmp_path, monkeypatch):
    entries = [
        {"bvid": "BV1", "title": "旧视频", "author": "UP", "viewed_at": "2026-09-01T10:00:00"}
    ]
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": entries, "total": 1})
    _header, body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    assert "今日无 B 站观看记录" in body


def test_bili_full_top20(tmp_path, monkeypatch):
    entries = [
        {"bvid": f"BV{i}", "title": f"视频{i}", "author": f"UP{i}", "viewed_at": f"2026-09-02T10:{i % 60:02d}:00"}
        for i in range(30)
    ]
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": entries, "total": 30})
    _header, body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(lines) == dip._BILI_TOP  # 只列前 20
    assert "仅列前" in body  # 超额标注


# --------------------------------------------------------------------------- #
# Edge：空（db_not_found）/ 满（域名归并 top10）                              #
# --------------------------------------------------------------------------- #
def test_edge_db_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"error": "db_not_found"})
    _header, body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "不可用" in body or "失败" in body


def test_edge_empty_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": []})
    _header, body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "无 Edge 浏览记录" in body


def test_edge_full_domain_aggregation(tmp_path, monkeypatch):
    # 3 个域名：a.com×5、b.com×3、c.com×2、d...×12 个域名共 20 条
    entries = []
    mapping = [("a.com", 5), ("b.com", 3), ("c.com", 2), ("d.com", 1), ("e.com", 1),
               ("f.com", 1), ("g.com", 1), ("h.com", 1), ("i.com", 1), ("j.com", 1),
               ("k.com", 1), ("l.com", 1)]
    for host, n in mapping:
        for i in range(n):
            entries.append({"url": f"https://{host}/p{i}", "title": f"{host}-{i}"})
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": entries})
    _header, body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(lines) == dip._EDGE_TOP  # 归并后只列 top10
    assert "a.com：5 次" in body  # 归并计数正确


def test_edge_db_locked_degraded(tmp_path, monkeypatch):
    """Edge 库被占用（database is locked）→ 工具返回 error dict：该板块降级标注，不中断。"""
    monkeypatch.setattr(
        dip, "_BROWSER_FN",
        lambda **k: {"error": "db_open_failed", "message": "database is locked"},
    )
    header, body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "〔浏览·Edge 域名〕" in header
    assert "该源失败" in body
    assert "database is locked" in body


def test_edge_db_locked_whole_pack_not_interrupted(tmp_path, monkeypatch):
    """Edge 取数抛 database is locked（真实锁定形态）→ 整包不中断，Edge 板块降级标注。"""
    import sqlite3

    def _locked(**k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dip, "_BROWSER_FN", _locked)
    pack = dip.build_info_pack("2026-09-02", _cfg(tmp_path))
    assert isinstance(pack, str)
    assert "〔浏览·Edge 域名〕" in pack
    assert "该源失败" in pack
    assert len(pack) <= dip.PACK_BUDGET


# --------------------------------------------------------------------------- #
# git：空 / 满                                                                #
# --------------------------------------------------------------------------- #
def test_git_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (0, ""))
    _header, body = dip._build_git("2026-09-02", _cfg(tmp_path))
    assert "今日无 git 提交" in body


def test_git_failed_rc(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (128, ""))
    _header, body = dip._build_git("2026-09-02", _cfg(tmp_path))
    assert "失败" in body


def test_git_full(tmp_path, monkeypatch):
    out = "abc1234567|feat: 重构日报信息包|aos\n"
    out += "def8901234|fix: 修 bug|aos\n"
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (0, out))
    _header, body = dip._build_git("2026-09-02", _cfg(tmp_path))
    assert "abc12345" in body  # hash 截断到 8 位
    assert "feat: 重构日报信息包" in body


# --------------------------------------------------------------------------- #
# 文件活动：空 / 满（目录聚合 top15）                                         #
# --------------------------------------------------------------------------- #
def test_files_empty(tmp_path):
    _header, body = dip._build_files("2026-09-02", _cfg(tmp_path))
    assert "无文件改动" in body


def test_files_full(tmp_path):
    import os
    import time

    cfg = _cfg(tmp_path)
    now = time.time()
    for sub in ("src/gacore", "tests", "docs"):
        d = cfg.root / sub
        d.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            p = d / f"f{i}.py"
            p.write_text("x", encoding="utf-8")
            os.utime(p, (now, now))
    _header, body = dip._build_files("2026-09-02", cfg)
    assert "src" in body
    assert "tests" in body
    assert "3 个文件" in body


# --------------------------------------------------------------------------- #
# ncm：空（失败跳过）/ 满                                                     #
# --------------------------------------------------------------------------- #
def test_ncm_error_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"error": "login"})
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda *a, **k: {"error": "login"})
    _header, body = dip._build_ncm("2026-09-02", _cfg(tmp_path))
    assert "失败" in body


def test_ncm_full(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"nickname": "某用户"})
    pls = [
        {"name": f"歌单{i}", "track_count": i + 10, "subscribed": i % 2 == 0}
        for i in range(12)
    ]
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda *a, **k: {"playlists": pls})
    header, body = dip._build_ncm("2026-09-02", _cfg(tmp_path))
    assert "某用户" in header  # nickname 进 header
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(lines) == dip._NCM_TOP  # 只列前 10


# --------------------------------------------------------------------------- #
# QQ 对话：空（无文件/无当日）/ 满（超 _CHAT_TOP 截断）                        #
# --------------------------------------------------------------------------- #
def test_chat_no_file(tmp_path):
    _header, body = dip._build_chat("2026-09-02", _cfg(tmp_path))
    assert "qq_chat_log.jsonl 不存在" in body


def test_chat_no_user_messages(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.memory_dir / "qq_chat_log.jsonl"
    # 只有 bot 侧消息，没有 user 侧
    log_path.write_text(
        '{"ts":"2026-09-02T10:00:00+08:00","direction":"bot","text":"你好"}\n'
        '{"ts":"2026-09-02T10:01:00+08:00","direction":"user","text":"/help"}\n',
        encoding="utf-8",
    )
    _header, body = dip._build_chat("2026-09-02", cfg)
    assert "命令类消息" in body or "无 QQ 对话记录" in body


def test_chat_full_user_messages(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.memory_dir / "qq_chat_log.jsonl"
    lines = []
    for i in range(20):
        lines.append(
            f'{{"ts":"2026-09-02T1{i % 10}:{i:02d}:00+08:00","direction":"user","text":"消息内容 {i} 号"}}'
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _header, body = dip._build_chat("2026-09-02", cfg)
    bullet_lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) == dip._CHAT_TOP  # 只列前 15
    assert "共 20 条" in body
    assert "仅列前 15" in body


def test_chat_date_filtering(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.memory_dir / "qq_chat_log.jsonl"
    log_path.write_text(
        '{"ts":"2026-09-01T10:00:00+08:00","direction":"user","text":"昨天的消息"}\n'
        '{"ts":"2026-09-02T10:00:00+08:00","direction":"user","text":"今天的消息"}\n'
        '{"ts":"2026-09-03T10:00:00+08:00","direction":"user","text":"明天的消息"}\n',
        encoding="utf-8",
    )
    _header, body = dip._build_chat("2026-09-02", cfg)
    assert "今天的消息" in body
    assert "昨天的消息" not in body
    assert "明天的消息" not in body


# --------------------------------------------------------------------------- #
# 前日日报：空 / 满                                                           #
# --------------------------------------------------------------------------- #
def test_memory_empty(tmp_path):
    _header, body = dip._build_memory("2026-09-02", _cfg(tmp_path))
    assert "无历史日报" in body


def test_memory_full(tmp_path):
    cfg = _cfg(tmp_path)
    scheduled = cfg.logs_dir / "scheduled"
    scheduled.mkdir(parents=True, exist_ok=True)
    (scheduled / "daily-report_20260901_235900.md").write_text(
        "# Scheduled Job: daily-report\n"
        "- time: 2026-09-01T23:59:00+08:00\n"
        "\n"
        "## Reply\n"
        "\n"
        "# 今日状态\n- 白天是代码手\n",
        encoding="utf-8",
    )
    _header, body = dip._build_memory("2026-09-02", cfg)
    assert "前一次日报" in body
    assert "2026-09-01T23:59" in body


# --------------------------------------------------------------------------- #
# 整包：预算硬控 / 永不抛                                                      #
# --------------------------------------------------------------------------- #
def test_pack_budget_contract():
    """预算契约：2026-09-04 用户拍板 2000 → 8000（8 路被动信号 + 第 9 路对话源的总量级）。"""
    assert dip.PACK_BUDGET == 8000


def _flood_all_sources(monkeypatch):
    """把所有确定性源注满，逼近预算上限。"""
    monkeypatch.setattr(
        dip, "_LANGTRACK_FN",
        lambda **k: {"available": True, "unlock_count": 10, "top_apps": [{"app": "x"}] * 5},
    )
    entries30 = [
        {"bvid": f"BV{i}", "title": f"视频{i}号", "author": "UP", "viewed_at": f"2026-09-02T0{i % 10}:0{i % 60:02d}:00"}
        for i in range(30)
    ]
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": entries30, "total": 30})
    edge_entries = []
    for i in range(30):
        edge_entries.append({"url": f"https://site{i}.com/a", "title": f"页面{i}号"})
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": edge_entries})
    monkeypatch.setattr(
        dip, "_run_git",
        lambda root, date: (0, "\n".join(f"aaaa{i:04d}x|commit 消息 {i} 号|aos" for i in range(20))),
    )
    # 真实文件树：建 40 个当天文件制造文件活动噪音
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"nickname": "某用户" * 3})
    monkeypatch.setattr(
        dip, "_NCM_PLAYLIST_FN",
        lambda *a, **k: {"playlists": [{"name": f"歌单{i}", "track_count": i} for i in range(12)]},
    )


def test_build_info_pack_within_budget(tmp_path, monkeypatch, caplog):
    _flood_all_sources(monkeypatch)
    # 预算缩回 2000 触发熔断路径：真实预算 8000 下洪泛数据（~3200 字）够不到熔断点，
    # 本测试验证的是"预算耗尽 → 截断 + 尾部板块挤出"机制本身，与常量取值解耦。
    monkeypatch.setattr(dip, "PACK_BUDGET", 2000)
    # 长期画像 80 行（保底撑爆预算，确保熔断/裁剪路径被真正走到）
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "global_mem_insight.txt").write_text(
        "\n".join(f"画像细节 line{i} 很长的描述内容来撑字数" for i in range(80)),
        encoding="utf-8",
    )
    # 前日日报
    scheduled = cfg.logs_dir / "scheduled"
    scheduled.mkdir(parents=True, exist_ok=True)
    (scheduled / "daily-report_20260901_235900.md").write_text(
        "# Scheduled Job: daily-report\n- time: 2026-09-01T23:59:00+08:00\n\n## Reply\n\n# 今日状态\n- 正文\n",
        encoding="utf-8",
    )
    pack = dip.build_info_pack("2026-09-02", cfg)
    assert isinstance(pack, str)
    assert 0 < len(pack) <= dip.PACK_BUDGET  # 硬控 ≤2000
    assert "〔当日信息包·2026-09-02〕" in pack
    # 切面断言：预算耗尽 → 熔断产生裁剪、尾部板块被挤出
    assert pack.rstrip().endswith("…")  # 存在 clipped 行，以省略号结尾
    assert "〔记忆·前日日报〕" not in pack  # 熔断后尾部板块缺失：前日日报（最后一块必被挤出）


def test_build_info_pack_never_raises(tmp_path, monkeypatch):
    """所有确定性源都抛异常：整包必须降级而不是抛给 run_job。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")
    for name in ("_LANGTRACK_FN", "_BILLI_FN", "_BROWSER_FN", "_NCM_ME_FN", "_NCM_PLAYLIST_FN"):
        monkeypatch.setattr(dip, name, _boom)
    def _boom_run(root, date):
        raise RuntimeError("git boom")
    monkeypatch.setattr(dip, "_run_git", _boom_run)

    pack = dip.build_info_pack("2026-09-02", _cfg(tmp_path))
    assert isinstance(pack, str)
    assert "该源失败" in pack or "〔当日信息包" in pack
    assert len(pack) <= dip.PACK_BUDGET


# --------------------------------------------------------------------------- #
# 听歌与视频伴音：空库 / 网易云与 B站 分流                                     #
# --------------------------------------------------------------------------- #
def _media_ts(dstr: str, hh: int, mm: int) -> int:
    import datetime as _dt
    d = _dt.datetime.fromisoformat(dstr)
    return int(_dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp() * 1000)


def test_media_empty_db(tmp_path):
    _header, body = dip._build_media("2026-09-02", _cfg(tmp_path))
    assert "无 langTrack 音乐数据" in body


def test_media_split_music_vs_video(tmp_path):
    import json
    import sqlite3

    from gacore.langTrack import storage

    cfg = _cfg(tmp_path)
    db = cfg.root / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(storage._SCHEMA)
    events = [
        (_media_ts("2026-09-02", 9, 0), "真歌A", "歌手A", "com.netease.cloudmusic"),
        (_media_ts("2026-09-02", 9, 3), "B站解说标题", "某UP", "tv.danmaku.bili"),
        (_media_ts("2026-09-02", 9, 6), "真歌B", "歌手B", "com.netease.cloudmusic"),
    ]
    for ts, title, singer, pkg in events:
        conn.execute(
            "INSERT INTO events(device_id, ts, type, payload, received_at) VALUES (?,?,?,?,?)",
            ("dev1", ts, "music_play",
             json.dumps({"title": title, "singer": singer, "pkg": pkg, "state": "playing"}), ts),
        )
    conn.commit()
    conn.close()

    _header, body = dip._build_media("2026-09-02", cfg)
    assert "听歌 Top" in body and "真歌A" in body and "真歌B" in body
    assert "视频伴音" in body and "B站解说标题" in body
    # 真实值核对：row_factory=Row 未设会导致 _listen_music 静默返回空 → 断言能兜住
    assert "今日无听歌记录" not in body
