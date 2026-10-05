"""daily_info_pack 空/满双向单测（三元组契约版）。

覆盖 build_info_pack_report 的每个确定性信息源（长期画像 / QQ 对话 / B站 / Edge / git /
文件活动 / ncm / 前日日报），各构造空（失败/无数据→降级标注）与满（大数据→被裁剪/计数
正确）两种形态，并钉住 C1 v0.7 的 builder 三元组契约：pack_body=挑选压缩后进包文本、
detail_body=当日取数全部结果。langTrack 源已按 C2 A′ 移除（细维度并入 fact_card compact）。
另验证整包 ≤8000 字硬控、超预算整块丢弃与"任何源抛异常也不中断整包"的兜底。

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
# 长期画像：空 / 满（detail=画像全文，pack=compact 40 行）                     #
# --------------------------------------------------------------------------- #
def test_long_term_empty(tmp_path):
    title, pack_body, detail_body = dip._build_long_term_picture("2026-09-02", _cfg(tmp_path))
    assert title == "〔长期画像·compact〕"
    assert "无长期画像" in pack_body or "memory/global_mem_insight" in pack_body
    assert detail_body == ""  # 无文件 → 无取数结果


def test_long_term_full(tmp_path):
    """尾部窗口语义：最新行保留、最旧行被丢（v3.2 修正，此前 head 截断丢新增）。"""
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    full_text = "\n".join(f"[2026-08-{i % 28 + 1:02d}] line{i} {'内容' * 20}" for i in range(60))
    (cfg.memory_dir / "global_mem_insight.txt").write_text(full_text, encoding="utf-8")
    title, pack_body, detail_body = dip._build_long_term_picture("2026-09-02", cfg)
    assert "line59" in pack_body      # 最新行在尾部窗口内
    assert "line0" not in pack_body   # 最旧行被窗口丢掉
    assert "动态层取编年史尾部" in pack_body  # 尾注标注
    assert "line59" in detail_body    # detail 含全文
    assert "〔编年史〕" in detail_body


def test_long_term_anchor_kept_and_single_truncation(tmp_path):
    """锚点全保留 + 单一截断：超预算时只从动态层最旧端丢行，整体 ≤ _LONG_TERM_CAP。

    锚点文件里的 "# " 维护注释只给人看，不注入 pack/detail。
    """
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    anchor = "# 维护约定：只由人工编辑\n- 锚点事实A：生日\n- 锚点事实B：婚期\n# 另一条注释"
    (cfg.memory_dir / "global_mem_anchor.txt").write_text(anchor, encoding="utf-8")
    # 60 行长行（每行 ~100 字）→ 尾部窗口远超 1600 预算，动态层须按预算再砍
    long_lines = [f"[2026-09-{i % 28 + 1:02d}] insight: {'细节' * 45}" for i in range(60)]
    (cfg.memory_dir / "global_mem_insight.txt").write_text("\n".join(long_lines), encoding="utf-8")
    title, pack_body, detail_body = dip._build_long_term_picture("2026-10-04", cfg)
    assert "〔固定锚〕" in pack_body
    assert "锚点事实A" in pack_body and "锚点事实B" in pack_body  # 锚点永不被挤掉
    assert "维护约定" not in pack_body and "另一条注释" not in pack_body  # 注释不注入
    assert len(pack_body) <= dip._LONG_TERM_CAP                    # builder 内单一截断到位
    assert "line0" not in pack_body
    assert "已覆盖" in pack_body and "完整见 memory/global_mem_insight.txt" in pack_body
    assert "锚点事实A" in detail_body and "〔编年史〕" in detail_body  # detail=锚+全文


def test_long_term_only_anchor(tmp_path):
    """只有锚点、编年史缺失：正常产出，不判无数据。"""
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "global_mem_anchor.txt").write_text("- 锚点事实A", encoding="utf-8")
    title, pack_body, detail_body = dip._build_long_term_picture("2026-10-04", cfg)
    assert "锚点事实A" in pack_body
    assert "〔固定锚〕" in detail_body
    assert dip.classify_body(pack_body)[0] == "ok"


def test_long_term_full_chars_within_budget_real_shape(tmp_path):
    """真实形态回归：84 行中文编年史（~24k 字符）进包后 ≤1600 且含最新日期行。"""
    cfg = _cfg(tmp_path)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"[2026-08-{d:02d}] insight: {'很长的一条画像描述，' * 20}" for d in range(1, 29)]
    lines += [f"[2026-10-{d:02d}] insight: {'十月新增的重要事实，' * 20}" for d in range(1, 5)]
    (cfg.memory_dir / "global_mem_insight.txt").write_text("\n".join(lines), encoding="utf-8")
    title, pack_body, _ = dip._build_long_term_picture("2026-10-04", cfg)
    assert len(pack_body) <= dip._LONG_TERM_CAP
    assert "2026-10-03" in pack_body   # 最新内容在窗口内
    assert "2026-08-01" not in pack_body


# --------------------------------------------------------------------------- #
# B站：空（失败/无当日）/ 满（>20 条截断；detail=全部当日条目）                #
# --------------------------------------------------------------------------- #
def test_bili_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dip, "_BILLI_FN", lambda **k: {"error": "not_authenticated", "message": "登录过期"}
    )
    title, pack_body, detail_body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    assert "失败" in pack_body
    assert detail_body == ""


def test_bili_no_today(tmp_path, monkeypatch):
    entries = [
        {"bvid": "BV1", "title": "旧视频", "author": "UP", "viewed_at": "2026-09-01T10:00:00"}
    ]
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": entries, "total": 1})
    title, pack_body, detail_body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    assert "今日无 B 站观看记录" in pack_body
    assert detail_body == ""


def test_bili_full_top20(tmp_path, monkeypatch):
    entries = [
        {"bvid": f"BV{i}", "title": f"视频{i}", "author": f"UP{i}", "viewed_at": f"2026-09-02T10:{i % 60:02d}:00"}
        for i in range(30)
    ]
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": entries, "total": 30})
    title, pack_body, detail_body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    lines = [ln for ln in pack_body.splitlines() if ln.startswith("- ")]
    assert len(lines) == dip._BILI_TOP  # pack 只列前 20
    assert "仅列前" in pack_body  # 超额标注
    detail_lines = [ln for ln in detail_body.splitlines() if ln.startswith("- ")]
    assert len(detail_lines) == 30  # detail 不做 top-N 挑选：全部 30 条


# --------------------------------------------------------------------------- #
# Edge：空（db_not_found）/ 满（域名归并 top10；detail=全部页面逐条）          #
# --------------------------------------------------------------------------- #
def test_edge_db_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"error": "db_not_found"})
    title, pack_body, detail_body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "不可用" in pack_body or "失败" in pack_body
    assert detail_body == ""


def test_edge_empty_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": []})
    title, pack_body, detail_body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "无 Edge 浏览记录" in pack_body
    assert detail_body == ""


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
    title, pack_body, detail_body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    lines = [ln for ln in pack_body.splitlines() if ln.startswith("- ")]
    assert len(lines) == dip._EDGE_TOP  # 归并后只列 top10
    assert "a.com：5 次" in pack_body  # 归并计数正确
    # detail=全部页面逐条（共 19 条），不做归并挑选
    assert len([ln for ln in detail_body.splitlines() if ln.startswith("- ")]) == sum(n for _, n in mapping)
    assert "https://a.com/p0" in detail_body


def test_edge_db_locked_degraded(tmp_path, monkeypatch):
    """Edge 库被占用（database is locked）→ 工具返回 error dict：该板块降级标注，不中断。"""
    monkeypatch.setattr(
        dip, "_BROWSER_FN",
        lambda **k: {"error": "db_open_failed", "message": "database is locked"},
    )
    header, pack_body, detail_body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "〔浏览·Edge 域名〕" in header
    assert "该源失败" in pack_body
    assert "database is locked" in pack_body
    assert detail_body == ""


def test_edge_db_locked_whole_pack_not_interrupted(tmp_path, monkeypatch):
    """Edge 取数抛 database is locked（真实锁定形态）→ 整包不中断，Edge 板块降级标注。"""
    import sqlite3

    def _locked(**k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dip, "_BROWSER_FN", _locked)
    # 其余真实外部源（bili/ncm=CLI 网络请求）一并 mock，
    # 本测试只验证"单源失败整包降级"的管线行为，不依赖真实机器状态。
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"error": "mock", "message": "mock"})
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"error": "mock"})
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda **k: {"error": "mock"})
    pack = dip.build_info_pack("2026-09-02", _cfg(tmp_path))
    assert isinstance(pack, str)
    # C3：header 带状态元信息（来自 classify_body 的 failed 判定 + 失败原因）
    assert "〔浏览·Edge 域名｜状态:失败:database is locked〕" in pack
    assert "该源失败" in pack
    assert len(pack) <= dip.PACK_BUDGET


# --------------------------------------------------------------------------- #
# git：空 / 满（detail=完整 log 行，pack=hash/subject 截断版）                 #
# --------------------------------------------------------------------------- #
def test_git_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (0, ""))
    title, pack_body, detail_body = dip._build_git("2026-09-02", _cfg(tmp_path))
    assert "今日无 git 提交" in pack_body
    assert detail_body == ""


def test_git_failed_rc(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (128, ""))
    title, pack_body, detail_body = dip._build_git("2026-09-02", _cfg(tmp_path))
    assert "失败" in pack_body


def test_git_full(tmp_path, monkeypatch):
    out = "abc1234567|feat: 重构日报信息包|aos\n"
    out += "def8901234|fix: 修 bug|aos\n"
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (0, out))
    title, pack_body, detail_body = dip._build_git("2026-09-02", _cfg(tmp_path))
    assert "abc12345" in pack_body  # hash 截断到 8 位
    assert "feat: 重构日报信息包" in pack_body
    assert detail_body.count("abc1234567") == 1  # detail 保留完整 hash（不截断）


def test_git_multirepo_groups(tmp_path, monkeypatch):
    main = tmp_path / "main"
    extra = tmp_path / "weiCheckApp"
    (main / ".git").mkdir(parents=True)
    (extra / ".git").mkdir(parents=True)
    cfg = Config(
        root=main,
        asset_dir=main / "a",
        memory_dir=main / "m",
        logs_dir=main / "l",
        temp_dir=main / "t",
        extra_git_repos=(extra,),
    )

    def fake_run(root, date):
        if root == main:
            return (0, "aaa1111111|commit main|aos\n")
        return (0, "bbb2222222|commit wei|aos\n")

    monkeypatch.setattr(dip, "_run_git", fake_run)
    title, pack_body, detail_body = dip._build_git("2026-09-02", cfg)
    assert "aaa11111" in pack_body and "commit main" in pack_body
    assert "bbb22222" in pack_body and "commit wei" in pack_body
    assert "main" in pack_body        # 分组标签 = 目录名（无 .git 时的 fallback）
    assert "weiCheckApp" in pack_body


# --------------------------------------------------------------------------- #
# 文件活动：空 / 满（目录聚合 top15；detail=全量目录）                         #
# --------------------------------------------------------------------------- #
def test_files_empty(tmp_path):
    title, pack_body, detail_body = dip._build_files("2026-09-02", _cfg(tmp_path))
    assert "无文件改动" in pack_body
    assert detail_body == ""


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
    title, pack_body, detail_body = dip._build_files("2026-09-02", cfg)
    assert "src" in pack_body
    assert "tests" in pack_body
    assert "3 个文件" in pack_body
    assert "docs：3 个文件" in detail_body  # detail 不做 top-N 挑选：全量目录


# --------------------------------------------------------------------------- #
# ncm：空（失败跳过）/ 满（pack=top10；detail=全部歌单）                       #
# --------------------------------------------------------------------------- #
def test_ncm_error_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"error": "login"})
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda *a, **k: {"error": "login"})
    title, pack_body, detail_body = dip._build_ncm("2026-09-02", _cfg(tmp_path))
    assert "失败" in pack_body
    assert detail_body == ""


def test_ncm_full(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"nickname": "某用户"})
    pls = [
        {"name": f"歌单{i}", "track_count": i + 10, "subscribed": i % 2 == 0}
        for i in range(12)
    ]
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda *a, **k: {"playlists": pls})
    header, pack_body, detail_body = dip._build_ncm("2026-09-02", _cfg(tmp_path))
    assert "某用户" in header  # nickname 进 header
    lines = [ln for ln in pack_body.splitlines() if ln.startswith("- ")]
    assert len(lines) == dip._NCM_TOP  # pack 只列前 10
    assert len([ln for ln in detail_body.splitlines() if ln.startswith("- ")]) == 12  # detail 全量


# --------------------------------------------------------------------------- #
# QQ 对话：空（无文件/无当日）/ 满（超 _CHAT_TOP 截断；detail=全部摘录）        #
# --------------------------------------------------------------------------- #
def test_chat_no_file(tmp_path):
    title, pack_body, detail_body = dip._build_chat("2026-09-02", _cfg(tmp_path))
    assert "qq_chat_log.jsonl 不存在" in pack_body
    assert detail_body == ""


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
    title, pack_body, detail_body = dip._build_chat("2026-09-02", cfg)
    assert "命令类消息" in pack_body or "无 QQ 对话记录" in pack_body
    assert detail_body == ""


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
    title, pack_body, detail_body = dip._build_chat("2026-09-02", cfg)
    bullet_lines = [ln for ln in pack_body.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) == dip._CHAT_TOP  # pack 只列最近 15
    assert "共 20 条" in pack_body
    assert "仅列最近 15" in pack_body
    assert "消息内容 19 号" in pack_body   # v3.2：取尾部=最新消息
    assert "消息内容 3 号" not in pack_body  # 最旧的被窗口丢掉
    # detail=当日全部摘录，不截条数也不截每条字数
    detail_lines = [ln for ln in detail_body.splitlines() if ln.startswith("- ")]
    assert len(detail_lines) == 20
    assert "消息内容 19 号" in detail_body


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
    title, pack_body, detail_body = dip._build_chat("2026-09-02", cfg)
    assert "今天的消息" in detail_body
    assert "昨天的消息" not in detail_body
    assert "明天的消息" not in detail_body


# --------------------------------------------------------------------------- #
# classify_body：状态行位置守卫（v3.2 修 _LONG_TERM 误报）                      #
# --------------------------------------------------------------------------- #
def test_classify_missing_status_lines():
    """各 builder 的状态行（"- " 开头、关键词在行首 8 字符内）→ missing_data。"""
    for body in (
        "- 今日无 B 站观看记录",
        "- 当日无 Edge 浏览记录",
        "- 今日无 git 提交",
        "- 无长期画像（memory/global_mem_anchor.txt 与 global_mem_insight.txt 均缺失），本日仅凭当日信号写作。",
        "- 无历史日报输出可作基准（首次运行）",
        "- 该账号无可列歌单",
        "- 今日仓库内无文件改动",
        "- 无 langTrack 音乐数据",
        "- 当日无 QQ 对话记录",
    ):
        assert dip.classify_body(body)[0] == "missing_data", body


def test_classify_quoted_content_not_missing():
    """正文引文里出现"无…数据"（位置超出状态行区）不再误报 missing_data（v3.2）。"""
    body = "- [2026-08-27] insight: langTrack 手机端 8/26 无采集数据，链路疑似中断，待恢复。"
    assert dip.classify_body(body)[0] == "ok"


def test_classify_failed_and_empty():
    assert dip.classify_body("")[0] == "empty"
    assert dip.classify_body("- 该源失败：database is locked")[0] == "failed"
    assert dip.classify_body("- 该源失败/未登录：登录过期")[0] == "failed"
    assert dip.classify_body("- 正常内容") [0] == "ok"


# --------------------------------------------------------------------------- #
# B站/Edge detail 拉取窗口诚实标注（v3.2）                                      #
# --------------------------------------------------------------------------- #
def test_bili_detail_window_note(tmp_path, monkeypatch):
    entries = [
        {"bvid": f"BV{i}", "title": f"视频{i}", "author": "UP", "viewed_at": f"2026-09-02T10:{i % 60:02d}:00"}
        for i in range(50)
    ]
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": entries, "total": 500})
    _, _, detail_body = dip._build_bili("2026-09-02", _cfg(tmp_path))
    assert "拉取上限 50 条" in detail_body


def test_edge_detail_window_note(tmp_path, monkeypatch):
    entries = [{"url": f"https://site{i}.com/p", "title": f"页面{i}"} for i in range(100)]
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": entries})
    _, _, detail_body = dip._build_edge("2026-09-02", _cfg(tmp_path))
    assert "拉取上限 100 条" in detail_body


# --------------------------------------------------------------------------- #
# v3.2.2 逐源预算配置：config/info_pack.json 覆盖 cap/enabled/priority/budget  #
# --------------------------------------------------------------------------- #
def _write_source_config(cfg: Config, data: dict) -> None:
    import json as _json

    p = cfg.root / "config" / "info_pack.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_source_config_missing_file_uses_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    assert dip.load_source_config(cfg) == {}
    assert dip.effective_source_caps(cfg)["_FILES"] == dip._FILES_CAP


def test_source_config_bad_json_falls_back(tmp_path):
    cfg = _cfg(tmp_path)
    p = cfg.root / "config" / "info_pack.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{broken", encoding="utf-8")
    assert dip.load_source_config(cfg) == {}
    assert dip.effective_source_caps(cfg)["_CHAT"] == dip._CHAT_CAP


def test_build_honors_cap_and_disabled_and_budget(tmp_path, monkeypatch):
    """cap 覆盖→提前截断；enabled=false→不进包记 disabled；pack_budget 覆盖→整包硬控。"""
    _flood_all_sources(monkeypatch)
    cfg = _cfg(tmp_path)
    _flood_cfg_files(cfg)
    _write_source_config(cfg, {
        "pack_budget": 3000,
        "sources": {
            "_BILI": {"cap": 300},
            "_NCM": {"enabled": False},
            "_GIT": {"priority": 5},   # 提到最前，熔断顺序变化可从 stats 顺序看出
        },
    })
    pack, stats = dip.build_info_pack_report("2026-09-02", cfg)
    by = {s["key"]: s for s in stats}
    assert len(pack) <= 3000                                  # 覆盖后的整包预算
    # 新 cap 截断生效（尾注"（已截断 N 行…）"约 30 字允许超出 cap）
    assert by["_BILI"]["chars"] < by["_BILI"]["full_chars"] and by["_BILI"]["chars"] <= 340
    assert by["_NCM"]["status"] == "disabled" and by["_NCM"]["chars"] == 0
    assert "〔基线·网易云歌单/收藏〕" not in pack               # 停用源不进包
    keys = [s["key"] for s in stats]
    assert keys.index("_GIT") < keys.index("_CHAT")           # priority=5 后排序提前（原 70）


def test_disabled_source_recorded_in_health_jsonl(tmp_path, monkeypatch):
    """disabled 状态随 stats 落 jsonl（write_pack_health 由 scheduler 调用，此处直调验证）。"""
    cfg = _cfg(tmp_path)
    _write_source_config(cfg, {"pack_budget": 8000, "sources": {"_CHAT": {"enabled": False}}})
    _flood_all_sources(monkeypatch)
    pack, stats = dip.build_info_pack_report("2026-09-02", cfg)
    dip.write_pack_health(cfg, "2026-09-02", "daily-report", "backfill", stats, len(pack))
    import json as _json
    rec = _json.loads(
        (cfg.root / "data" / "logs" / "info_pack_health.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    chat = [s for s in rec["sources"] if s["key"] == "_CHAT"][0]
    assert chat["status"] == "disabled"
    assert rec["budget"] == 8000


# --------------------------------------------------------------------------- #
# 前日日报：空 / 满（pack=260 字摘要；detail=1000 字更长摘要）                   #
# --------------------------------------------------------------------------- #
def test_memory_empty(tmp_path):
    title, pack_body, detail_body = dip._build_memory("2026-09-02", _cfg(tmp_path))
    assert "无历史日报" in pack_body
    assert detail_body == ""


def test_memory_full(tmp_path):
    cfg = _cfg(tmp_path)
    scheduled = cfg.logs_dir / "scheduled"
    scheduled.mkdir(parents=True, exist_ok=True)
    long_reply = "# 今日状态\n" + "\n".join(f"- 正文第{i}段落，内容足够长用于区分两级摘要" for i in range(60))
    (scheduled / "daily-report_20260901_235900.md").write_text(
        "# Scheduled Job: daily-report\n"
        "- time: 2026-09-01T23:59:00+08:00\n"
        "\n"
        "## Reply\n"
        "\n"
        f"{long_reply}\n",
        encoding="utf-8",
    )
    title, pack_body, detail_body = dip._build_memory("2026-09-02", cfg)
    assert "前一次日报" in pack_body
    assert "2026-09-01T23:59" in pack_body
    assert len(detail_body) > len(pack_body)  # detail 取更长摘要
    assert "正文第40段落" in detail_body        # detail 覆盖更靠后的正文
    assert "正文第59段落" not in detail_body    # detail 只到 _MEMORY_DETAIL_CHARS 为止


# --------------------------------------------------------------------------- #
# 整包：预算硬控 / 永不抛 / 熔断整块丢弃                                       #
# --------------------------------------------------------------------------- #
def test_pack_budget_contract():
    """预算契约：2026-09-04 用户拍板 2000 → 8000（8 路被动信号 + 第 9 路对话源的总量级）。"""
    assert dip.PACK_BUDGET == 8000


def _flood_all_sources(monkeypatch):
    """把所有确定性源注满，逼近预算上限（外部 CLI/子进程一律 mock，保持封闭）。"""
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
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"nickname": "某用户" * 3})
    monkeypatch.setattr(
        dip, "_NCM_PLAYLIST_FN",
        lambda *a, **k: {"playlists": [{"name": f"歌单{i}", "track_count": i} for i in range(12)]},
    )


def _flood_cfg_files(cfg: Config) -> None:
    """给洪泛场景补上文件型源：长期画像（撑爆预算）与前日日报。"""
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "global_mem_insight.txt").write_text(
        "\n".join(f"画像细节 line{i} 很长的描述内容来撑字数" for i in range(80)),
        encoding="utf-8",
    )
    scheduled = cfg.logs_dir / "scheduled"
    scheduled.mkdir(parents=True, exist_ok=True)
    (scheduled / "daily-report_20260901_235900.md").write_text(
        "# Scheduled Job: daily-report\n- time: 2026-09-01T23:59:00+08:00\n\n## Reply\n\n# 今日状态\n- 正文\n",
        encoding="utf-8",
    )


def test_build_info_pack_drops_tail_blocks_within_budget(tmp_path, monkeypatch):
    """预算耗尽 → 从装不下的块起整块丢弃（其后全弃），无半行断章（C3）。"""
    _flood_all_sources(monkeypatch)
    # 预算缩到 1500：头部(~430)+长期画像(尾窗 40 行≈1030)装得下，B站块(~550)装不下 → 整块丢弃
    monkeypatch.setattr(dip, "PACK_BUDGET", 1500)
    cfg = _cfg(tmp_path)
    _flood_cfg_files(cfg)
    pack = dip.build_info_pack("2026-09-02", cfg)
    assert isinstance(pack, str)
    assert 0 < len(pack) <= dip.PACK_BUDGET
    assert "〔当日信息包·2026-09-02〕" in pack
    assert "〔长期画像·compact" in pack          # 头部 + 长期画像装得下
    assert "〔浏览·B站观看 top" not in pack      # 装不下 → 整块丢弃
    assert "〔记忆·前日日报" not in pack          # 熔断后尾部板块全弃
    assert not pack.rstrip().endswith("…")        # 整块丢弃不产生半行


def test_build_info_pack_head_clip_when_first_block_exceeds_budget(tmp_path, monkeypatch):
    """首块（消费指令头）自身超预算 → 按字符裁剪兜底，整包不为空。"""
    _flood_all_sources(monkeypatch)
    monkeypatch.setattr(dip, "PACK_BUDGET", 100)
    pack = dip.build_info_pack("2026-09-02", _cfg(tmp_path))
    assert 0 < len(pack) <= 100
    assert pack.endswith("…")


def test_build_info_pack_never_raises(tmp_path, monkeypatch):
    """所有确定性源都抛异常：整包必须降级而不是抛给 run_job。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")
    for name in ("_BILLI_FN", "_BROWSER_FN", "_NCM_ME_FN", "_NCM_PLAYLIST_FN"):
        monkeypatch.setattr(dip, name, _boom)
    def _boom_run(root, date):
        raise RuntimeError("git boom")
    monkeypatch.setattr(dip, "_run_git", _boom_run)

    pack = dip.build_info_pack("2026-09-02", _cfg(tmp_path))
    assert isinstance(pack, str)
    assert "该源失败" in pack or "〔当日信息包" in pack
    assert len(pack) <= dip.PACK_BUDGET


def test_build_info_pack_report_stash_and_real_headers(tmp_path, monkeypatch):
    """真实注册表跑通：build_info_pack 薄包装 + last_pack_stats 暂存（scheduler 落盘 seam）。"""
    _flood_all_sources(monkeypatch)
    cfg = _cfg(tmp_path)
    pack = dip.build_info_pack("2026-09-02", cfg)
    stats = dip.last_pack_stats()
    assert [s["key"] for s in stats] == [s.key for s in dip.SOURCES]
    assert all(isinstance(s.get("packed_body"), str) for s in stats)
    # langTrack 旧源 _LANGTRACK 不再出现（C2 A′）；v3.3 起手机事实源以 _PHONE_* 进包，
    # 测试环境无 langTrack.db → 降级为“无数据”行（可观测的空态，而非缺席）
    assert "_LANGTRACK" not in {s["key"] for s in stats}
    assert "无 langTrack 位置数据" in pack


# --------------------------------------------------------------------------- #
# 听歌与视频伴音：空库 / 网易云与 B站 分流（detail=全量榜单）                   #
# --------------------------------------------------------------------------- #
def _media_ts(dstr: str, hh: int, mm: int) -> int:
    import datetime as _dt
    d = _dt.datetime.fromisoformat(dstr)
    return int(_dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp() * 1000)


def test_media_empty_db(tmp_path):
    title, pack_body, detail_body = dip._build_media("2026-09-02", _cfg(tmp_path))
    assert "无 langTrack 音乐数据" in pack_body
    assert detail_body == ""


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

    title, pack_body, detail_body = dip._build_media("2026-09-02", cfg)
    assert "听歌 Top" in pack_body and "真歌A" in pack_body and "真歌B" in pack_body
    assert "视频伴音" in pack_body and "B站解说标题" in pack_body
    # detail=全部榜单条目（不截 top5）
    assert "听歌全量" in detail_body and "视频伴音全量" in detail_body
    # 真实值核对：row_factory=Row 未设会导致 _listen_music 静默返回空 → 断言能兜住
    assert "今日无听歌记录" not in pack_body


# --------------------------------------------------------------------------- #
# 手机事实源（v3.3）：_PHONE_PLACE / _PHONE_USAGE / _PHONE_NOTIF               #
# 合成库 schema 对齐 tests/test_langTrack_fact_card._make_db（已验证与
# fact_card.build 兼容的最小 v2 表结构）；落盘文件库（builder 按 db_path 打开）。
# --------------------------------------------------------------------------- #
def _ts8(y: int, mo: int, d: int, hh: int, mi: int = 0) -> int:
    import datetime as _dt

    return int(_dt.datetime(y, mo, d, hh, mi, tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp() * 1000)


def _seed_langtrack_db(
    cfg: Config, day: str, *, with_stats: bool = True, with_stays: bool = True,
    notif_payloads: list[str] | None = None,
) -> Path:
    """在 cfg.root/data/langTrack.db 写合成 v2 事实库，绝不触碰真实库。"""
    import json
    import sqlite3

    db = cfg.root / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("PRAGMA user_version=2")
    cur.execute(
        "CREATE TABLE daily_stats ("
        "device_id TEXT, day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT,"
        "notification_count INTEGER, notification_clicked INTEGER, top_notification_apps_json TEXT,"
        "screen_on_count INTEGER, screen_off_count INTEGER, unlock_count INTEGER,"
        "switch_count INTEGER, location_count INTEGER, audio_clip_count INTEGER,"
        "sleep_start_hhmm TEXT, sleep_end_hhmm TEXT, sleep_duration_min INTEGER, time_app_json TEXT)"
    )
    cur.execute("CREATE TABLE etl_state (device_id TEXT PRIMARY KEY, last_event_ts INTEGER)")
    cur.execute(
        "CREATE TABLE places ("
        "id INTEGER, device_id TEXT, place_id TEXT, grid_key TEXT, label TEXT,"
        "visit_count INTEGER, point_count INTEGER, stay_ms INTEGER,"
        "poi TEXT, address TEXT, district TEXT, behavior TEXT,"
        "name_confidence REAL, name_evidence TEXT, parent_poi TEXT,"
        "township TEXT, business_area TEXT)"
    )
    cur.execute(
        "CREATE TABLE stays ("
        "id INTEGER, device_id TEXT, place_id TEXT, grid_key TEXT,"
        "start_ts INTEGER, end_ts INTEGER, day TEXT, avg_accuracy_m INTEGER)"
    )
    cur.execute(
        "CREATE TABLE trips ("
        "id INTEGER, device_id TEXT, start_ts INTEGER, end_ts INTEGER, dist_m INTEGER, day TEXT,"
        "from_place_id TEXT, to_place_id TEXT, route_dist_m INTEGER)"
    )
    cur.execute(
        "CREATE TABLE anomalies ("
        "id INTEGER, device_id TEXT, day TEXT, kind TEXT, poi TEXT, detail TEXT, ts INTEGER)"
    )
    cur.execute(
        "CREATE TABLE events ("
        "id INTEGER, device_id TEXT, ts INTEGER, type TEXT, payload TEXT, received_at INTEGER)"
    )
    cur.execute("CREATE TABLE place_cells (device_id TEXT, place_id TEXT, grid_key TEXT)")

    y, mo, d = (int(x) for x in day.split("-"))
    dev = "dev-test"
    cur.execute("INSERT INTO etl_state VALUES (?,?)", (dev, _ts8(y, mo, d, 22, 0)))
    if with_stats:
        # 10 个 App：卡内截 8、pack 取 Top5、detail 直查全量 → 三层截断互为对照
        ranking = json.dumps(
            [{"app": f"App{i}", "ms": 3_600_000 - i * 300_000} for i in range(10)]
        )
        cur.execute(
            "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dev, day, 21_600_000, ranking, 12, 3,
             json.dumps([{"app": "微信", "n": 8}, {"app": "飞书", "n": 4}]),
             5, 4, 30, 80, 40, 0, "23:40", "06:30", 400, "[]"),
        )
    if with_stays:
        cur.executemany(
            "INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, dev, "p_home", "g_home", "家", 10, 10, 3_000_000,
                 "家小区", "XX路1号", "玄武区", "home", 0.0, "", "", "", ""),
                (2, dev, "p_work", "g_work", "公司", 20, 20, 6_000_000,
                 "公司大厦", "YY路2号", "雨花台区", "work", 0.0, "", "", "", ""),
            ],
        )
        cur.executemany(
            "INSERT INTO stays VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, dev, "p_home", "g_home", _ts8(y, mo, d, 0, 0), _ts8(y, mo, d, 8, 0), day, 15),
                (2, dev, "p_work", "g_work", _ts8(y, mo, d, 9, 0), _ts8(y, mo, d, 12, 0), day, 20),
            ],
        )
        cur.execute(
            "INSERT INTO trips VALUES (?,?,?,?,?,?,?,?,?)",
            (1, dev, _ts8(y, mo, d, 8, 0), _ts8(y, mo, d, 9, 0), 3200, day, "p_home", "p_work", None),
        )
        cur.execute(
            "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?)",
            (1, dev, day, "new_place", "德基广场", "访问 1 次", _ts8(y, mo, d, 20, 0)),
        )
    for i, payload in enumerate(notif_payloads or []):
        cur.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (i + 1, dev, _ts8(y, mo, d, 8, i), "notification", payload, _ts8(y, mo, d, 8, i)),
        )
    conn.commit()
    conn.close()
    return db


def test_phone_place_full(tmp_path):
    """轨迹时间线/停留累计/水位/异常进 pack；stays/trips 全量进 detail。"""
    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10")
    title, pack, detail = dip._build_phone_place("2026-09-10", cfg)
    assert title == "〔手机·位置轨迹〕"
    assert "今日轨迹" in pack and "00:00-08:00" in pack and "移动 1 段" in pack
    # 合成地点 name_confidence=0 → _friendly_label 渲染为 "地址〔label〕"，只断锚点
    assert "00:00-08:00 → " in pack and "09:00-12:00" in pack
    assert "停留累计：家 8.0h · 公司 3.0h" in pack
    assert "数据窗口未闭合（数据至 22:00）" in pack  # 水位 22:00 < 日末
    assert "异常：new_place 德基广场（访问 1 次）" in pack
    assert "stays 全量（2 段）" in detail and "trips 全量（1 段）" in detail
    assert "公司大厦" not in detail  # 地点显示用 label，不带 poi 地址


def test_phone_place_no_db(tmp_path):
    title, pack, detail = dip._build_phone_place("2026-09-10", _cfg(tmp_path))
    assert "无 langTrack 位置数据" in pack
    assert dip.classify_body(pack)[0] == "missing_data"
    assert detail == ""


def test_phone_place_empty_day(tmp_path):
    """有库有 stats 但当日无 stays/trips → 空态行（不冒充有轨迹）。"""
    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10", with_stays=False, notif_payloads=[])
    title, pack, detail = dip._build_phone_place("2026-09-10", cfg)
    assert "当日无手机位置数据" in pack
    assert dip.classify_body(pack)[0] == "missing_data"
    assert detail == ""


def test_phone_usage_full(tmp_path):
    """聚合行/App Top5/通知计数/作息进 pack；detail=排行全量+通知来源分布。"""
    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10")
    title, pack, detail = dip._build_phone_usage("2026-09-10", cfg)
    assert title == "〔手机·使用统计〕"
    assert "屏幕 6.0h · 解锁 30 · 切换 80" in pack
    assert "App Top：App0 1.0h、App1 0.9h、App2 0.8h、App3 0.8h、App4 0.7h" in pack
    assert "通知 12 条 · 点击 3 条 · 来源 微信、飞书" in pack
    assert "作息 睡 23:40 起 06:30（400min）" in pack
    assert "App 排行全量（10 个）" in detail  # 卡内截 8，detail 直查 daily_stats 全量
    assert "通知来源分布：微信×8、飞书×4" in detail


def test_phone_usage_no_stats(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10", with_stats=False)
    title, pack, detail = dip._build_phone_usage("2026-09-10", cfg)
    assert "当日无手机使用统计" in pack
    assert dip.classify_body(pack)[0] == "missing_data"
    assert detail == ""


def test_phone_notif_tail_and_merge(tmp_path):
    """尾窗取最近 8（军规①）+ 连续同源合并 + 单条 50 字截断（唯一截断点）。"""
    import json

    payloads = [
        json.dumps({"app": "微信", "title": "群A", "text": f"消息{i}", "clicked": "False"})
        for i in range(5)
    ]
    payloads.append(json.dumps({"app": "微信", "title": "群B", "text": "长" * 80, "clicked": "False"}))
    payloads.append(json.dumps({"app": "电话", "text": "未接来电", "clicked": "True"}))
    payloads += [
        json.dumps({"app": "飞书", "title": "群C", "text": f"连发{i}", "clicked": "False"})
        for i in range(3)
    ]
    payloads.append(json.dumps({"pkg": "com.android.systemui", "removed": "True"}))  # 无文本标记
    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10", notif_payloads=payloads)
    title, pack, detail = dip._build_phone_notif("2026-09-10", cfg)
    assert title == "〔手机·通知摘要〕"
    assert "内容事件 4 条，取最近 8" in pack  # 群A×5、群C×3 各合并为 1，移除标记剔除
    assert "（连续 5 条）" in pack and "（连续 3 条）" in pack
    assert "长" * 50 in pack and "长" * 51 not in pack  # 截断在 50 字
    assert "[08:06] 电话：未接来电" in pack
    assert "全量（4 条，同源已合并）" in detail
    assert "removed" not in detail


def test_phone_notif_empty(tmp_path):
    import json

    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10", notif_payloads=[json.dumps({"app": "x", "removed": "True"})])
    title, pack, detail = dip._build_phone_notif("2026-09-10", cfg)
    assert "当日无通知事件" in pack
    assert dip.classify_body(pack)[0] == "missing_data"
    assert detail == ""


def test_phone_sources_registered_and_config_disabled(tmp_path, monkeypatch):
    """三源进注册表（priority 严格升序）；config enabled=false 停用生效。外部工具一律 stub 保持封闭。"""
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": [], "total": 0})
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": []})
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"nickname": "x"})
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda **k: {"playlists": []})
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (0, ""))
    cfg = _cfg(tmp_path)
    _seed_langtrack_db(cfg, "2026-09-10")
    priorities = {s.key: s.priority for s in dip.SOURCES}
    assert list(priorities.values()) == sorted(priorities.values())
    assert priorities["_PHONE_PLACE"] == 15 and priorities["_PHONE_USAGE"] == 45
    _write_source_config(cfg, {"pack_budget": 12000, "sources": {"_PHONE_NOTIF": {"enabled": False}}})
    pack, stats = dip.build_info_pack_report("2026-09-10", cfg)
    by = {s["key"]: s for s in stats}
    assert by["_PHONE_NOTIF"]["status"] == "disabled"
    assert "〔手机·通知摘要" not in pack
    assert "〔手机·位置轨迹" in pack and "〔手机·使用统计" in pack

