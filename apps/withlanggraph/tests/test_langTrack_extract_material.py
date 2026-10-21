"""当日素材装配测试（封闭）：pack_detail 复用 / 回退现建 / raw_hash 稳定性 / 路由切片。

其中 round-trip 用例用真实 ``write_pack_detail`` 产出、再经 ``load_material`` 读回，
以此把"节头解析"与"落盘格式"的契约钉死——格式一变测试即红。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_extract_material.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.config import Config  # noqa: E402
from gacore.daily_info_pack import write_pack_detail  # noqa: E402
from gacore.langTrack.extract import material as mat  # noqa: E402

DAY = "2026-10-16"


def _cfg(tmp_path: Path) -> Config:
    return Config.for_tests(tmp_path)


def _write_detail(cfg: Config, key: str, detail: str) -> None:
    write_pack_detail(cfg, DAY, key, f"〔{key.strip('_')}〕", "ok", detail, "pack 文本", "进包文本")


def test_reads_pack_detail_written_by_real_writer(tmp_path):
    cfg = _cfg(tmp_path)
    _write_detail(cfg, "_CHAT", "- 12:30 与张三讨论相机选型\n- 21:00 记录设备调试")
    _write_detail(cfg, "_GIT", "- abc1234 feat(langTrack): 新增抽取骨架")

    material = mat.load_material(DAY, cfg)

    assert set(material.by_source) == {"_CHAT", "_GIT"}
    assert material.by_source["_CHAT"].startswith("- 12:30 与张三讨论相机选型")
    # 「渲染文本」「实际进包」两节不得混进 detail
    assert "进包文本" not in material.by_source["_CHAT"]
    assert "pack 文本" not in material.by_source["_CHAT"]


def test_fact_card_sidecar_is_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    _write_detail(cfg, "_CHAT", "- 有用的一行")
    day_dir = mat.pack_detail_dir(cfg, DAY)
    (day_dir / "_FACT_CARD.md").write_text("# _FACT_CARD · 2026-10-16\n\n## compact 全文\n内容\n", encoding="utf-8")

    assert set(mat.load_material(DAY, cfg).by_source) == {"_CHAT"}


def test_falls_back_to_build_report_when_no_pack_detail(tmp_path, monkeypatch):
    """pack_detail 缺失（当日抽取早于日报）→ 回退现建信息包的各源 detail_body。"""
    cfg = _cfg(tmp_path)
    calls: list[str] = []

    def fake_build(day: str, _cfg: Config):
        calls.append(day)
        return "pack", [
            {"key": "_CHAT", "detail_body": "chat 详情"},
            {"key": "_GIT", "detail_body": "git 详情"},
            {"key": "_BILI", "detail_body": ""},  # 空 detail 不入素材
        ]

    monkeypatch.setattr(mat, "_BUILD_REPORT_FN", fake_build)

    material = mat.load_material(DAY, cfg)

    assert calls == [DAY]
    assert material.by_source == {"_CHAT": "chat 详情", "_GIT": "git 详情"}


def test_prefers_pack_detail_over_rebuild(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_detail(cfg, "_CHAT", "落盘详情")

    def boom(*_args, **_kwargs):
        raise AssertionError("已有 pack_detail 时不应再触发 12 源取数")

    monkeypatch.setattr(mat, "_BUILD_REPORT_FN", boom)

    assert mat.load_material(DAY, cfg).by_source == {"_CHAT": "落盘详情"}


def test_raw_hash_is_stable_and_order_independent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def build(order: list[str]):
        def fake_build(_day: str, _cfg: Config):
            return "pack", [{"key": k, "detail_body": f"{k} 正文"} for k in order]

        monkeypatch.setattr(mat, "_BUILD_REPORT_FN", fake_build)
        return mat.load_material(DAY, cfg)

    first = build(["_CHAT", "_GIT"])
    second = build(["_GIT", "_CHAT"])

    assert first.raw_hash == second.raw_hash
    assert first.text == second.text
    # 素材变化 → 哈希变化（重跑会重新抽取，而不是误判幂等）
    third = build(["_CHAT", "_GIT", "_BILI"])
    assert third.raw_hash != first.raw_hash


def test_select_keeps_routing_order_and_skips_missing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def fake_build(_day: str, _cfg: Config):
        return "pack", [
            {"key": "_CHAT", "detail_body": "chat 正文"},
            {"key": "_LONG_TERM", "detail_body": "画像正文"},
        ]

    monkeypatch.setattr(mat, "_BUILD_REPORT_FN", fake_build)
    material = mat.load_material(DAY, cfg)

    picked = material.select(("_LONG_TERM", "_CHAT", "_BILI"))
    assert picked.index("_LONG_TERM") < picked.index("_CHAT")
    assert "_BILI" not in picked


def test_material_stats_reports_sources(tmp_path):
    cfg = _cfg(tmp_path)
    _write_detail(cfg, "_CHAT", "- 一行")

    stats = mat.material_stats(mat.load_material(DAY, cfg))

    assert stats["sources"] == ["_CHAT"]
    assert stats["chars"] > 0


def test_load_material_logs_origin_and_source_sizes(tmp_path, monkeypatch):
    """素材从哪来、各源多大要能进 app.jsonl——`_FILES` 25,806→424 的漂移靠这行定位。"""
    cfg = _cfg(tmp_path)
    _write_detail(cfg, "_CHAT", "- 一行")
    events: list[tuple[str, dict]] = []

    class Capture:
        def debug(self, event: str, **fields) -> None:
            events.append((event, fields))

        def info(self, event: str, **fields) -> None:
            events.append((event, fields))

        def warning(self, event: str, **fields) -> None:
            events.append((event, fields))

        def error(self, event: str, **fields) -> None:
            events.append((event, fields))

    monkeypatch.setattr(mat, "_LOG", Capture())

    mat.load_material(DAY, cfg)
    line = next(fields for event, fields in events if event == "kg_material_loaded")
    assert line["origin"] == "pack_detail"
    assert line["sources"] == ["_CHAT"]
    assert line["chars"] > 0
    assert line["by_source_chars"].keys() == {"_CHAT"}
    assert 0 < line["by_source_chars"]["_CHAT"] < line["chars"]

    import shutil

    shutil.rmtree(mat.pack_detail_dir(cfg, DAY))
    monkeypatch.setattr(
        mat, "_BUILD_REPORT_FN", lambda _day, _cfg: ("pack", [{"key": "_GIT", "detail_body": "git 详情"}])
    )
    events.clear()
    mat.load_material(DAY, cfg)
    line = next(fields for event, fields in events if event == "kg_material_loaded")
    assert line["origin"] == "fresh_build"
    assert line["sources"] == ["_GIT"]
