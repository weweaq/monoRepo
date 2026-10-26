"""出题人（question composer，主动询问 3.0 P3c）：LLM 归因式「朋友式提问」。

设计：docs/proactive-inquiry-3.0-design.md §3.1/§3.3/§6。
输入 = 变化面（question_surface，程序算的「哪里不对劲+同期事件」）+ 长期画像摘编；
LLM 按提问阶梯（L1 闲聊 / L3 归因关心）出 **0-1 条无编号单问**——问题文本本身就是
朋友口吻的完整消息（一句铺垫 + 一句问句），直接原样推送。

硬约束：
- 只提名不命名身份：问题里出现的一切事实必须能在变化面素材中定位（evidence 守卫），
  素材没有的事不许提；
- L3 必须 ≥1 路真实异常支撑，否则降级 L1（或不出题）；
- 去重/冷却/已答不重问复用 qa_store.seed_candidates（kind=composed，同 topic 7 天冷却）；
- 失败/关闭一律 fail-open 降级候选池（proactive.py 编排），绝不阻塞 questions scene。

「别问了」（拍板 4）：本模块产出的出题受全局静音约束；日报未答提醒豁免（见 proactive.py）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("question_composer")

# 提问阶梯铁律（拍板 3：与日报「想问你」节共享同一份文本，scheduler 注入日报 prompt）
LADDER_RULES: Final = """【提问阶梯铁律】
- L0 数据复读（把观察到的事实念给用户）禁止作为问句；只能作铺垫句
- L1 兴趣/任务跟进：就近期话题自然接一句（默认档）
- L3 归因关心：≥2 路信号交汇形成因果假设，用「是不是 / 会不会 / 是……吗」的试探句式问原因或感受，给对方留台阶
- 禁止质问口径（你为什么 / 怎么又）；家庭与健康话题只允许 L1-L2，不做因果猜测
- L3 是例外不是默认：无显著异常时退 L1 闲聊或不出题；深挖全局限 2 次/周"""

_SYSTEM_PROMPT: Final = (
    "你是用户的长期个人助理，语气像熟识的朋友。用户授权你基于他的本地数据主动提问。\n"
    + LADDER_RULES
    + """

出题步骤（内心完成，不要写进消息）：
1. 读变化面：有没有值得注意的模式？没有 → 出 0 条或 1 条 L1 闲聊
2. 有 → 从同期事件里为该模式找 1-2 个可能的解释（不许编造素材外的事）
3. 选最可能/最重要的一个，把铺垫句（我注意到了什么，L2 语气）和试探问句合成一条朋友口吻的消息

铁律：
- 问题文本 = 一句铺垫 + 一句问句，≤80 字，无编号、无称呼客套、无 emoji
- 「近 14 天已问过」列出的话题一律不再问
- 最多 1 条；素材不足以支撑任何问题时输出 {"question": ""}
- 只输出一个 JSON 对象，不要输出其他文字：
{"question": "...", "depth": "L1" 或 "L3", "topic": "2-6 字话题词", "evidence": ["异常1", "异常2"]}"""
)

_JSON_RE: Final = re.compile(r"\{.*\}", re.DOTALL)
_MAX_QUESTION_CHARS: Final = 80
_PORTRAIT_TAIL: Final = 1200


def composer_enabled(cfg: Config) -> bool:
    """出题人总开关（config/proactive.json ``composer.enabled``，缺失/损坏默认开启）。"""
    path = cfg.root / "config" / "proactive.json"
    if not path.is_file():
        return True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    section = raw.get("composer") if isinstance(raw, dict) else None
    if isinstance(section, dict) and "enabled" in section:
        return bool(section.get("enabled"))
    return True


def _call_llm(prompt: str) -> str:
    """LLM 调用缝（测试 monkeypatch 这里）。"""
    from langchain_core.messages import HumanMessage

    from gacore.llm import get_llm

    llm = get_llm((), bind_tools=False)
    resp = llm.invoke([HumanMessage(content=prompt)])
    content = resp.content
    return content if isinstance(content, str) else str(content)


def _portrait_tail(cfg: Config) -> str:
    """长期画像摘编：global_mem_insight.txt 尾部（编年史最近条目）；缺失/失败留空。"""
    try:
        text = (cfg.memory_dir / "global_mem_insight.txt").read_text(encoding="utf-8", errors="replace")
        return text[-_PORTRAIT_TAIL:].strip()
    except OSError:
        return ""


def _parse_payload(raw: str) -> dict[str, Any] | None:
    match = _JSON_RE.search(raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _evidence_guard(question: str, material: str) -> bool:
    """事实守卫：问题中出现的所有 ≥2 位数字必须能在变化面素材中找到（T-C3，防编造）。"""
    for token in re.findall(r"\d{2,}", question):
        if token not in material:
            return False
    return True


def compose(cfg: Config, day: str, *, user_id: str = "", surface: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """出 0-1 条朋友式问题并种入 qa 账本（kind=composed）。

    返回 {"question","depth","topic","key"}；不出题/被冷却/校验失败/LLM 失败返回 None
    （调用方降级候选池）。``surface`` 供测试注入固定变化面。
    """
    if not composer_enabled(cfg):
        logger.info("composer disabled by config", day=day)
        return None
    from gacore import question_surface

    surf = surface if surface is not None else question_surface.build_surface(cfg, day)
    material = question_surface.render_surface(surf)
    parts = [_SYSTEM_PROMPT, "\n\n〔素材〕", material]
    portrait = _portrait_tail(cfg)
    if portrait:
        parts.append("\n\n〔长期画像摘编（尾部）〕\n" + portrait)
    prompt = "".join(parts)
    try:
        raw = _call_llm(prompt)
    except Exception as exc:  # noqa: BLE001 — fail-open：降级候选池
        logger.warning("composer llm failed", error_type=type(exc).__name__, error=str(exc), day=day)
        return None
    payload = _parse_payload(raw)
    if not payload:
        logger.warning("composer unparsable output", day=day, raw_chars=len(raw or ""))
        return None
    question = str(payload.get("question") or "").strip()
    if not question:
        logger.info("composer chose not to ask", day=day)
        return None
    if len(question) > _MAX_QUESTION_CHARS * 2:  # 明显跑飞：整段叙述而非单问
        logger.warning("composer question too long", day=day, chars=len(question))
        return None
    if not _evidence_guard(question, material):
        logger.warning("composer evidence guard rejected", day=day)
        return None
    depth = str(payload.get("depth") or "L1")
    if depth not in ("L1", "L3"):
        depth = "L1"
    anomalies = surf.get("anomalies") or []
    if depth == "L3" and not anomalies:
        # L3 必须有真实异常支撑（设计 §3.1），否则降级 L1
        depth = "L1"
        logger.info("composer downgraded L3 to L1 (no anomalies)", day=day)
    topic = str(payload.get("topic") or "").strip()
    if not topic:
        from gacore.qa_store import topic_of

        topic = topic_of(cfg, question)
    from gacore import qa_store

    row = {
        "source": "composed",
        "ref": f"surface:{day}",
        "topic": topic,
        "anchor": "",
        "question": question,
        "evidence": [str(a) for a in (payload.get("evidence") or [])][:4],
        "freshness": "",
        "gain": 0.0,
    }
    seeded = qa_store.seed_candidates(cfg, [row], day=day, kind="composed")
    if not seeded.get("seeded"):
        logger.info(
            "composer question dropped by dedup/cooldown",
            day=day,
            dup_answered=seeded.get("dup_answered", 0),
            dup_similar=seeded.get("dup_similar", 0),
            dup_topic=seeded.get("dup_topic", 0),
        )
        return None
    key = qa_store.question_key(cfg, question)
    logger.info("composer question seeded", day=day, depth=depth, topic=topic, user=user_id)
    return {"question": question, "depth": depth, "topic": topic, "key": key}
