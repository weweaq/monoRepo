"""SourceSpec 注册表结构性测试（C1）：注册表完整性 + classify_body 全分支。

设计稿要求"新增一个源 = 在注册表加一条 SourceSpec，监控/熔断/状态元信息自动生效"。
本文件用注册表驱动的遍历断言守住该承诺：
- 注册表完整性：key 唯一、cap>0、priority 沿 SOURCES 顺序严格递增（=装配顺序=熔断顺序）；
- 每个 builder 的失败输出遵循 sentinel 约定（mock 失败路径后以 "- 该源失败：" 开头），
  防新增源破坏 failed 分类；
- classify_body 四态分支（empty/failed/missing_data/ok），钉住 bili/edge 的失败文案变体。

运行：uv run --package withlanggraph pytest apps/withlanggraph/tests/test_source_registry.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

import gacore.daily_info_pack as dip
from gacore.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config.for_tests(tmp_path)


def _build_like_report(spec: dip.SourceSpec, date: str, cfg: Config) -> tuple[str, str, str]:
    """按 build_info_pack_report 的外层契约调用 builder：builder 抛异常时兜底为失败 sentinel。"""
    try:
        return spec.builder(date, cfg)
    except Exception as exc:  # noqa: BLE001 - 与生产外层兜底一致
        return f"〔{spec.key.strip('_')}〕", f"- 该源失败：{exc}", ""


# --------------------------------------------------------------------------- #
# 注册表完整性                                                                 #
# --------------------------------------------------------------------------- #
def test_registry_keys_unique_and_caps_positive():
    keys = [s.key for s in dip.SOURCES]
    assert len(keys) == len(set(keys)), f"key 重复：{keys}"
    for s in dip.SOURCES:
        assert s.key.startswith("_")
        assert s.cap > 0
        assert callable(s.builder)


def test_registry_priority_strictly_increasing_in_pack_order():
    """SOURCES 顺序 = 装配顺序 = priority 升序（越小越优先保留）。"""
    priorities = [s.priority for s in dip.SOURCES]
    assert priorities == sorted(priorities)
    assert len(set(priorities)) == len(priorities)
    # _LANGTRACK（priority 30）移除后留空位不重排，保证历史 jsonl 可比
    assert all(p % 10 == 0 for p in priorities)


def test_langtrack_removed_from_registry():
    """C2 A′：langTrack 在信息包中不再有独立源（唯一渲染出口 = fact_card compact）。"""
    assert "_LANGTRACK" not in {s.key for s in dip.SOURCES}
    assert not hasattr(dip, "_build_langtrack")
    assert not hasattr(dip, "_LANGTRACK_CAP")


def test_every_builder_returns_triple_contract(tmp_path):
    """每个 builder 在封闭环境（tmp cfg + mock 外部 FN）下都返回三元组且元素为 str。"""
    cfg = _cfg(tmp_path)
    # 封闭外部源：CLI 网络请求 / git 子进程 一律 mock 成"无数据"，不触碰真实机器状态
    monkey_data = {
        "_BILLI_FN": lambda **k: {"entries": [], "total": 0},
        "_BROWSER_FN": lambda **k: {"entries": []},
        "_NCM_ME_FN": lambda: {"error": "mock"},
        "_NCM_PLAYLIST_FN": lambda *a, **k: {"error": "mock"},
    }
    originals = {name: getattr(dip, name) for name in monkey_data}
    for name, fn in monkey_data.items():
        setattr(dip, name, fn)
    try:
        for spec in dip.SOURCES:
            result = spec.builder("2026-09-02", cfg)
            assert isinstance(result, tuple) and len(result) == 3, spec.key
            title, pack_body, detail_body = result
            assert isinstance(title, str) and title.startswith("〔"), spec.key
            assert isinstance(pack_body, str), spec.key
            assert isinstance(detail_body, str), spec.key
    finally:
        for name, fn in originals.items():
            setattr(dip, name, fn)


# --------------------------------------------------------------------------- #
# 失败 sentinel：每个源在"取数失败"路径下都以 "- 该源失败：" 开头              #
# --------------------------------------------------------------------------- #
def _raise(*args, **kwargs):
    raise OSError("boom")


def _inject_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config, key: str) -> None:
    """按源注入失败路径（新增源必须在此登记，否则本测试失败提醒补齐）。"""
    if key == "_LONG_TERM":
        monkeypatch.setattr("gacore.scheduler._long_term_insight", _raise)
    elif key == "_CHAT":
        # 文件必须先存在（is_file 短路会绕过 open 失败路径），再让读文件的 open 抛 OSError
        cfg.memory_dir.mkdir(parents=True, exist_ok=True)
        (cfg.memory_dir / "qq_chat_log.jsonl").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(Path, "open", _raise)
    elif key == "_BILI":
        monkeypatch.setattr(dip, "_BILLI_FN", _raise)
    elif key == "_EDGE":
        monkeypatch.setattr(dip, "_BROWSER_FN", _raise)
    elif key == "_MEDIA":
        db = cfg.root / "data" / "langTrack.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a database")  # sqlite 查询抛 DatabaseError → 外层兜底
    elif key == "_GIT":
        monkeypatch.setattr(dip, "_run_git", _raise)  # builder 无内层 try：由外层兜底产出 sentinel
    elif key == "_FILES":
        monkeypatch.setattr(dip.os, "walk", _raise)
    elif key == "_NCM":
        monkeypatch.setattr(dip, "_NCM_ME_FN", _raise)
    elif key == "_MEMORY":
        monkeypatch.setattr(dip, "_latest_prev_report", _raise)
    else:
        pytest.fail(f"新增源 {key} 未在 test_source_registry 登记失败注入方式——"
                    "请补齐后确认其失败输出遵循 '- 该源失败：' sentinel 约定")


def test_every_builder_failure_follows_failed_sentinel(tmp_path, monkeypatch):
    """注册表驱动：mock 每个源的失败路径 → pack_body 以 "- 该源失败：" 开头且 classify 为 failed。"""
    cfg = _cfg(tmp_path)
    for spec in dip.SOURCES:
        _inject_failure(tmp_path, monkeypatch, cfg, spec.key)
        title, pack_body, detail_body = _build_like_report(spec, "2026-09-02", cfg)
        assert pack_body.startswith("- 该源失败"), f"{spec.key}: {pack_body[:60]}"
        status, note = dip.classify_body(pack_body)
        assert status == "failed", spec.key
        assert isinstance(note, str) and note, f"{spec.key}: note 应保留失败原因"
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# classify_body 四态分支                                                       #
# --------------------------------------------------------------------------- #
def test_classify_empty():
    assert dip.classify_body("") == ("empty", "")
    assert dip.classify_body("   \n  ") == ("empty", "")


def test_classify_failed_variants():
    """bili "- 该源失败或未登录：" 与 edge "- 该源失败/不可用：" 变体必须判 failed。"""
    assert dip.classify_body("- 该源失败：CLI 超时") == ("failed", "CLI 超时")
    assert dip.classify_body("- 该源失败或未登录：登录过期") == ("failed", "登录过期")
    assert dip.classify_body("- 该源失败/不可用：Edge 历史库不存在（今日无浏览器历史可用）")[0] == "failed"
    assert dip.classify_body("- 该源失败/跳过：boom")[0] == "failed"
    assert dip.classify_body("- 该源失败")[1] == ""  # 无冒号 → note 为空
    # failed 优先于 missing_data（失败行同时含"未登录"字样也不改判）
    assert dip.classify_body("- 该源失败或未登录：x")[0] == "failed"


def test_classify_missing_data_variants():
    for body in (
        "- 当日无 QQ 对话记录",
        "- 今日无 B 站观看记录",
        "- 当日无 Edge 浏览记录",
        "- 该日无 langTrack 手机数据（可能未采集/未同步）",
        "- 网易云未登录",
        "- 历史库不可用",
    ):
        status, note = dip.classify_body(body)
        assert status == "missing_data", body
        assert note == body, "note = 首个命中行"
    # 多行时取首个命中行
    status, note = dip.classify_body("- ok 行\n- 今日无听歌记录\n- 尾行")
    assert status == "missing_data"
    assert note == "- 今日无听歌记录"


def test_classify_ok():
    assert dip.classify_body("- 当日屏幕时长：3.5h\n- 解锁 6 次") == ("ok", "")
    assert dip.classify_body("【mono】\n- abc12345 feat: x（aos）") == ("ok", "")
