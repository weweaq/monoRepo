"""当日素材装配（D1）：给抽取通道喂"当日全量取数结果"，并算 raw_hash 作幂等锚。

素材来源优先级：
1. 已落盘的 ``data/logs/pack_detail/{day}/{key}.md`` 的「完整取数详情」节——零成本复用，
   日报（23:50）落盘后补跑/重跑走这条；
2. 缺失时回退 ``build_info_pack_report(day, cfg)`` 的各源 ``detail_body``——当日抽取
   （23:30）早于当日日报，属常态路径。

节头解析与 ``daily_info_pack.write_pack_detail`` 的写法是一份契约，由
``tests/test_langTrack_extract_material.py`` 用真实 writer 的输出做往返校验钉住。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gacore.config import Config
from gacore.daily_info_pack import build_info_pack_report
from gacore.jsonl_logger import get_logger

_LOG = get_logger("langTrack.extract.material")

# 测试注入点：monkeypatch 本名即可让 load_material 不触碰真实 12 源取数。
_BUILD_REPORT_FN = build_info_pack_report

_DETAIL_HEADER: Final = "\n## 完整取数详情\n"
_RENDER_HEADER: Final = "\n## 渲染文本\n"
_FACT_CARD_STEM: Final = "_FACT_CARD"
# 长期画像的素材占比体检线：超过即说明当日事实被长时效画像稀释（2026-10-22 实测 82%）。
_LONG_TERM_KEY: Final = "_LONG_TERM"
_LONG_TERM_DOMINANCE_RATIO: Final = 0.6


@dataclass(frozen=True, slots=True)
class Material:
    """当日全量素材：按源切分 + 拼装文本 + 内容哈希。"""

    day: str
    by_source: dict[str, str]
    text: str
    raw_hash: str

    def select(self, keys: Sequence[str]) -> str:
        """按路由表取指定源的 detail 并拼装；缺失源跳过，顺序即 ``keys`` 顺序。"""
        parts = [f"## {k}\n{self.by_source[k]}" for k in keys if self.by_source.get(k)]
        return "\n\n".join(parts)


def pack_detail_dir(cfg: Config, day: str) -> Path:
    """单源详情目录，与 ``daily_info_pack.write_pack_detail`` 的落点一致。"""
    return cfg.root / "data" / "logs" / "pack_detail" / day


def _parse_detail_section(text: str) -> str | None:
    """从单源详情 md 中取「完整取数详情」节；格式不符返回 None。"""
    start = text.find(_DETAIL_HEADER)
    if start < 0:
        return None
    start += len(_DETAIL_HEADER)
    end = text.find(_RENDER_HEADER, start)
    body = (text[start:end] if end >= 0 else text[start:]).strip()
    if not body or body == "（无）":
        return None
    return body


def _read_pack_detail(cfg: Config, day: str) -> dict[str, str]:
    """读已落盘的单源详情；目录缺失/无可用文件 → 空 dict（触发回退）。"""
    day_dir = pack_detail_dir(cfg, day)
    if not day_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(day_dir.glob("*.md")):
        if path.stem == _FACT_CARD_STEM:
            continue
        try:
            body = _parse_detail_section(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if body:
            out[path.stem] = body
    return out


def _build_fresh(day: str, cfg: Config) -> dict[str, str]:
    """回退路径：现建信息包，取各源 detail_body（未挑选、未压缩的当日取数全量）。"""
    _pack, stats = _BUILD_REPORT_FN(day, cfg)
    out: dict[str, str] = {}
    for st in stats:
        key = str(st.get("key") or "")
        body = str(st.get("detail_body") or "").strip()
        if key and body:
            out[key] = body
    return out


def _share(by_source: dict[str, str], total_chars: int) -> float:
    """``_LONG_TERM`` 占素材字符的比例（0~1）；口径唯一，日志与 channel_stats 共用。"""
    if not total_chars:
        return 0.0
    return round(len(by_source.get(_LONG_TERM_KEY, "")) / total_chars, 3)


def long_term_share(material: Material) -> float:
    """长期画像占素材字符的比例（0~1）；无素材时为 0。"""
    return _share(material.by_source, len(material.text))


def load_material(day: str, cfg: Config) -> Material:
    """装配当日素材；raw_hash 对"按源名排序后的拼装文本"取 sha256，同素材恒等。"""
    detail = _read_pack_detail(cfg, day)
    by_source = detail or _build_fresh(day, cfg)
    text = "\n\n".join(f"## {k}\n{v}" for k, v in sorted(by_source.items()))
    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    long_term_chars = len(by_source.get(_LONG_TERM_KEY, ""))
    share = _share(by_source, len(text))
    _LOG.debug(
        "kg_material_loaded",
        day=day,
        origin="pack_detail" if detail else "fresh_build",
        sources=sorted(by_source),
        chars=len(text),
        by_source_chars={k: len(v) for k, v in sorted(by_source.items())},
        long_term_chars=long_term_chars,
        long_term_share=share,
    )
    if share >= _LONG_TERM_DOMINANCE_RATIO:
        # 只体检不动手：素材配比是否要改由人工判断，日志先留证据链。
        _LOG.warning(
            "kg_material_long_term_dominant",
            day=day,
            chars=len(text),
            long_term_chars=long_term_chars,
            long_term_share=share,
            threshold=_LONG_TERM_DOMINANCE_RATIO,
        )
    return Material(day=day, by_source=by_source, text=text, raw_hash=raw_hash)


def material_stats(material: Material) -> dict[str, Any]:
    """素材体检元信息（进 ``kg_runs.channel_stats``，便于回答"这天抽了多少料"）。

    ``by_source_chars`` 让运维日志能一眼看出当日哪个源把素材撑大（如 ``_LONG_TERM``
    独占大半），从而判断某通道耗时是不是素材侧问题；``long_term_share`` 把这个
    判断固化成可比指标，跨天趋势看它就够。
    """
    return {
        "chars": len(material.text),
        "sources": sorted(material.by_source),
        "by_source_chars": {k: len(v) for k, v in sorted(material.by_source.items())},
        "long_term_chars": len(material.by_source.get(_LONG_TERM_KEY, "")),
        "long_term_share": long_term_share(material),
    }
