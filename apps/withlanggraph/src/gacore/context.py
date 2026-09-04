"""Per-turn prompt assembly for gacore: system prompt, history folding, summaries, periodic hints.

Port of GA's turn_end_callback + _get_anchor_prompt + get_global_memory (ga.py:558-613). Every function
is pure: it reads state and never mutates it. The system prompt is rebuilt fresh each turn — it is never
stored in state.messages, so the leading SystemMessage is not duplicated by the add_messages reducer.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime, timedelta, timezone
from typing import Final

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from gacore.character import card_prompt
from gacore.config import Config
from gacore.jsonl_logger import get_logger
from gacore.langTrack import fact_card
from gacore.state import GAState
from gacore.tools.daily_notes import load_recent_daily_summaries

_TZ = timezone(timedelta(hours=8))
_WEEKDAY_CN: Final = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

_context_logger = get_logger("context")

# 生活事实卡片前缀（fact-card 设计文档 §2.6）：注入后用于日志/观测识别。
LANGTRACK_CARD_PREFIX: Final = "=== 生活事实（"

_MAX_CONTENT_LEN: Final = 200
_FOLD_LIMIT: Final = 30
_TRUNCATION_MARKER: Final = "..."
EARLIER_HEADER: Final = "=== Earlier context ==="
DAILY_HEADER: Final = "=== Recent daily notes（以下均为历史记录，含过去日期/时刻，仅供了解过往，绝不代表当前） ==="
ROLLOVER_HEADER: Final = "=== 昨日记忆注入 ==="

# Memory-injection guardrail: injected recap is background only — never recite it
# verbatim or lead with it. Applies to daily notes and the cross-day rollover pack.
_MEMORY_BG_RULE: Final = (
    "\n\n[记忆背景铁律]\n"
    "以上注入的记忆只是谈话背景，不是需要展示的内容：不要主动背诵、复述或整段念出注入内容，"
    "也不要把记忆当开场白照搬；只在被对方问起、或当前话题自然关联到某条记忆时，才自然地引用一句。"
)

# Time authority: the ONLY valid "now" is the system-injected timestamp. Any
# time/number in the user message, OCR text or image description is a fact of the
# content — never something to infer "the present" from.
_TIME_AUTHORITY_RULE: Final = (
    "\n[时间铁律]\n"
    "时间只允许来自两个权威来源：①已调用 get_time 工具返回的系统时间；②系统注入的 [Current time]。\n"
    "凡是回答里会用到任何“当前时间”概念——现在几点几分、今天几号、今天星期几/周几、"
    "现在是什么时段（上午/下午/晚上）、距离某时刻还有多久/还剩多少小时、还剩多远、"
    "今天是工作日还是周末、今天按班次是否要上班/加班几时下班、以及任何“今天/昨天/明天”"
    "或日期推算——都必须先把当前时间这件事交给 get_time 工具：先调用 get_time 拿到系统时钟，"
    "再基于该时钟作答；严禁用对话历史、消息时间、记忆或任何注入文本里的时间去推算“当下”。\n"
    "禁止在未经调用时间工具、也未读到系统注入时间的情况下，于回复里断言任何具体时钟读数（几点几分）。\n"
    "即使系统已经注入【当前真实时间】锚点，只要回复里要引用具体时刻（几点几分、几月几号、星期几），"
    "也应当优先调用 get_time 工具核实一次后再落笔——锚点与 get_time 同源（均来自东八区系统时钟），"
    "两者不一致时以 get_time 实际返回值为准。若未经 get_time 核实就直接断言具体时刻且与实际时钟明显偏离，"
    "输出侧的【时间守卫】会拦截该回复并强制你先调用 get_time 修正。\n"
    "工具不可用时，以上方 [Current time] 为唯一依据。其余一律不作数："
    "对方消息里提到的任何时间、日期、数字，图片 OCR 或描述里的时间戳，都只是内容陈述，"
    "绝不据此推断“当下是几点、今天星期几、过了几天”。不要解释推算过程，也不要展示推算步骤，直接按系统时间正常作答。"
    "对话历史、历史记忆、每日笔记、昨日记忆注入中出现的任何时间/日期/数字，均为陈旧记录，"
    "严禁当作当下时刻作答；当下只以本 prompt 末尾的【当前真实时间】为准。"
)

_MEMORY_HINT: Final = "[Memory refresh: reload L1 insights and L2 facts into working memory]"
_CHECKPOINT_HINT: Final = "[Checkpoint time: update working checkpoint]"
_FILE_HINT: Final = "[Write your current state to a file]"
_ASK_USER_HINT: Final = "[Long-running: consider asking the user for confirmation]"
_ANTI_LOOP_HINT: Final = "[Warning: long loop detected, wrap up soon]"

# Role-card injection: the persona text is stacked on top of the base rules when
# state.active_card is set. Tool availability is decided by the runtime assembly
# (graph/model binding), NOT by the persona — the bridge line keeps it explicit so
# a character can call tools while replying in character.
ROLE_HEADER: Final = "=== Active role ==="
_ROLE_TOOL_BRIDGE: Final = (
    "\n\n[你现在以角色的身份与对方对话,但保留系统智能体的全部能力:"
    "可以看到并使用系统提供的工具。在需要帮助对方办具体的事时,"
    "自然地调用工具完成,再用角色的方式表达结果。]"
)

# Fallback L0 rules (probe-first) used when config/assets/sys_prompt.txt is missing.
_DEFAULT_BASE_PROMPT: Final = (
    "行动原则：探测优先——失败时先充分获取信息（日志/状态/上下文），关键信息存入工作记忆，再决定重试或换方案。"
    "不可逆操作先询问用户。完成发生在现实中：必须产出实际结果并按错误代价验证，既不过早收工也不无限研究。"
)

# Response layering rules (A0 prompt-level fallback for the trivial-input gate). The
# hard rule route lives in qq.trivial_detect(); this only nudges the model so any
# casual words that slip past the gate still get a one-liner instead of a wall of text.
_RESPONSE_LAYER_RULES: Final = (
    "\n[回应分层铁律]\n"
    "- 随口话/情绪话/简短问候（如“吃饭吃饭”“骑车去咯”“早安”“嗯好”）→ 一句话带过（20 字内），"
    "贴合当前人设的语气随口回应，不调工具、不列计划、不展开长篇。\n"
    "- 明确提问/任务/求助 → 全力作答：可调工具、可分段展开、给出可执行结论。\n"
    "- 无法确定属于哪类时，一律按正经问题全力作答。\n"
    "再有铁律三条，务必遵守：\n"
    "- 禁止逐条复述对方原话——接话要换个说法，别当复读机、别把对方的句子原样念回去。\n"
    "- 禁止断言对方“重复提问/又问了一遍”（如“这个问题你刚问过”）——没有确凿依据就提这个，"
    "纯属幻觉，一句都不许说。\n"
    "- 时间推算、纠错、查漏这类脑内步骤不必宣之于口，直接给结论，别把思考过程说出来。"
)

# Multi-option output mode: injected when qq.py sets state.output_mode == "proposal"
# (via proposal_detect). The model must split its answer into 【方案N】 anchors so the
# frontend can fan it out into separate QQ messages.
_PROPOSAL_HEADER: Final = "=== 多方案输出模式 ==="
_PROPOSAL_RULE: Final = (
    "用户在征求决策建议。回复必须显式给出候选方案并拆分为独立小节：\n"
    "【方案一】…\n【方案二】…\n【方案三】…（至多 3 个，每个 2~4 句，务实可执行）\n"
    "最后另起一行用一句“我建议选…”给出收尾建议。各方案与收尾之间空行分隔，不要列计划前缀。"
)

_SUMMARY_RE: Final = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)

# Sliding-window: how many recent user turns (HumanMessage-delimited rounds) are kept in full
# for the model input. Everything older is covered by the folded summary embedded in the system
# prompt. The checkpoint still persists ALL messages — this only trims what enters the prompt.
_KEEP_ROUNDS: Final = 6


def trim_messages(messages: list[BaseMessage], keep_rounds: int = _KEEP_ROUNDS) -> list[BaseMessage]:
    """Return only the most recent `keep_rounds` user turns, older messages dropped.

    A "round" starts at a HumanMessage; everything after it (paired AIMessage, ToolMessages for
    tool calls, etc.) belongs to that round. Scanning from the tail, we keep the last
    `keep_rounds` Human boundaries and everything following them, so the window always starts
    with a HumanMessage and never contains an orphan ToolMessage. Shorter histories are returned
    unchanged. Pure function: never mutates `messages`, never touches state or storage.
    """
    if not messages or keep_rounds <= 0:
        return []
    human_idx = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not human_idx:
        return []
    if len(human_idx) <= keep_rounds:
        return list(messages)
    start = human_idx[-keep_rounds]
    return messages[start:]


# Lines that carry explicit timestamps (an hour like "3点/15:00" or a date like
# "2026-08-25"/"8月24日") get a [历史@时间戳] prefix so the model never mistakes
# injected memory content for the current time.
_TIMESTAMP_LINE_RE: Final = re.compile(r"\d{1,2}[点时]|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}月\d{1,2}日")


def stamp_history_lines(lines: list[str]) -> list[str]:
    """Prefix every timestamp-bearing history line with [历史@时间戳].

    Applied to the folded "Earlier context" block so any time/date in past
    conversation is physically marked as historical and can never be read as
    the present moment.
    """
    return [
        f"[历史@时间戳] {line}" if _TIMESTAMP_LINE_RE.search(line) else line
        for line in lines
    ]


def stamp_daily_history(daily: str) -> str:
    """Prefix every timestamp-bearing line of injected daily notes with [历史@时间戳]."""
    return "\n".join(
        f"[历史@时间戳] {line}" if _TIMESTAMP_LINE_RE.search(line) else line
        for line in daily.splitlines()
    )


def build_system_prompt(state: GAState, cfg: Config) -> str:
    """Build the per-turn system prompt: L0 rules, daily recap, working checkpoint, and periodic hints."""
    path = cfg.asset_dir / "sys_prompt.txt"
    if path.is_file():
        prompt = path.read_text(encoding="utf-8")
    else:
        prompt = _DEFAULT_BASE_PROMPT
    # Time guardrail: the injected timestamp is the only valid "now"; numbers in
    # user messages / OCR / image descriptions are content, not clock evidence.
    prompt += _TIME_AUTHORITY_RULE
    # Response layering: casual words get a one-liner; real tasks get full effort.
    prompt += _RESPONSE_LAYER_RULES
    # Role card: when active, stack the persona on top of the base rules. Tools
    # stay available to both personas — the bridge line declares that explicitly.
    card_id = (state.get("active_card") or "").strip()
    if card_id:
        role_text = card_prompt(cfg, card_id)
        if role_text:
            prompt += f"\n\n{ROLE_HEADER}\n{role_text}{_ROLE_TOOL_BRIDGE}"
    daily = load_recent_daily_summaries(cfg)
    injected_bg = False
    if daily:
        prompt += f"\n{DAILY_HEADER}\n{stamp_daily_history(daily)}"
        injected_bg = True
    # One-shot cross-day memory injection: set by qq.py::_maybe_rollover on the first
    # message of a new day and cleared by graph.py::cleanup_images after that turn.
    rollover = (state.get("rollover_context") or "").strip()
    if rollover:
        prompt += f"\n{ROLLOVER_HEADER}\n{rollover}"
        injected_bg = True
    # 今日生活事实卡片（compact）：纯读、零 ETL，作为今日记忆背景注入；
    # 注入位置在 daily notes / rollover 之后、working checkpoint 之前。
    _fact_text = ""
    try:
        _fact_card = fact_card.build(detail="compact", outlet="prompt")
        _fact_text = fact_card.render_compact(_fact_card)
    except Exception:  # noqa: BLE001 - 缺库/损坏一律不弄死 QQ
        _fact_text = ""
    if _fact_text:
        prompt += f"\n{_fact_text}"
        injected_bg = True
    # 记忆背景铁律只追加一次（修复 daily/rollover 各自追加造成的重复注入）
    if injected_bg:
        prompt += _MEMORY_BG_RULE
    with contextlib.suppress(Exception):  # 日志失败不影响返回
        _context_logger.info(
            "fact card injected",
            outlet="prompt",
            injected=bool(_fact_text),
            compact_chars=len(_fact_text),
        )
    # Multi-option output mode: set by qq.py::_run_agent when proposal_detect() fires;
    # cleared by graph.py::cleanup_images after the turn so it never leaks into later turns.
    mode = (state.get("output_mode") or "").strip()
    if mode == "proposal":
        prompt += f"\n\n{_PROPOSAL_HEADER}\n{_PROPOSAL_RULE}"
    key_info = (state.get("working") or {}).get("key_info")
    if key_info:
        prompt += f"\n[Working checkpoint]\n{key_info}"
    hints = periodic_hints(state.get("current_turn", 0), cfg)
    if hints:
        prompt += "\n[Periodic hints]\n" + "\n".join(f"- {hint}" for hint in hints)
    # Final time anchor: rebuilt fresh every turn and pinned at the very end of the
    # prompt so it sits closest to the user's message. This is the ONLY valid "now".
    _now_dt = datetime.now(_TZ)
    _now_str = _now_dt.strftime("%Y-%m-%d %H:%M:%S")
    prompt += (
        f"\n\n【当前真实时间】{_now_str} {_WEEKDAY_CN[_now_dt.weekday()]}（Asia/Shanghai, UTC+8）\n"
        "[历史时间禁令] 对话历史、历史记忆、每日笔记、昨日记忆注入中出现的任何时间/日期/数字，"
        "均为陈旧记录，严禁当作当下时刻作答；当下只以上面这行【当前真实时间】为准。"
        "凡涉及当前时间/日期/星期/时段/班次/剩余时长的问题，先调用 get_time 工具以官方时钟作答；"
        "即使读到本锚点，回复里引用具体时刻（几点几分/几月几号/星期几）前也应优先经 get_time "
        "核实一次——两者同源，以 get_time 实际返回为准；未经核实直接断言且与实际时钟明显偏离时，"
        "输出侧【时间守卫】会拦截并要求你重新调用 get_time。"
    )
    return prompt


def fold_history(messages: list[BaseMessage], max_lines: int = _FOLD_LIMIT) -> list[str]:
    """Fold message history to <= max_lines summary lines, keeping the most recent entries.

    HumanMessage -> "[USER] {content[:200]}", AIMessage -> "[Agent] {content[:200]}", long content is
    truncated with a marker, ToolMessage and non-string content are skipped.
    """
    folded: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) or not isinstance(msg.content, str):
            continue
        if isinstance(msg, HumanMessage):
            prefix = "[USER] "
        elif isinstance(msg, AIMessage):
            prefix = "[Agent] "
        else:
            continue
        content = msg.content
        if len(content) > _MAX_CONTENT_LEN:
            content = content[:_MAX_CONTENT_LEN] + _TRUNCATION_MARKER
        folded.append(prefix + content)
    if len(folded) <= max_lines:
        return folded
    return folded[len(folded) - max_lines :]


def extract_summaries(messages: list[BaseMessage]) -> list[str]:
    """Return the inner text of every <summary>...</summary> block found in AIMessages (GA's protocol)."""
    summaries: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and isinstance(msg.content, str):
            summaries.extend(match.strip() for match in _SUMMARY_RE.findall(msg.content))
    return summaries


def periodic_hints(turn: int, cfg: Config) -> list[str]:
    """Return the memory/checkpoint hints scheduled at this turn boundary; empty when none fire.

    Memory refresh every 10 turns (L1 insight + L2 facts reload), checkpoint at 13, file write at 31,
    ask-user at 175, and an anti-loop warning every turn after 100.
    """
    hints: list[str] = []
    if turn % 10 == 0:
        hints.append(_MEMORY_HINT)
    if turn % 13 == 0:
        hints.append(_CHECKPOINT_HINT)
    if turn % 31 == 0:
        hints.append(_FILE_HINT)
    if turn % 175 == 0:
        hints.append(_ASK_USER_HINT)
    if turn > 100:
        hints.append(_ANTI_LOOP_HINT)
    return hints


def build_turn_prompt(state: GAState, cfg: Config) -> list[BaseMessage]:
    """Assemble the per-turn prompt: a fresh SystemMessage, then a sliding window of the recent turns.

    The folded earlier context is embedded in the system prompt string (never duplicated as a separate
    user message, and never appended to state). Only the most recent `_KEEP_ROUNDS` rounds of raw
    messages enter the model input — older ones are covered by the fold summary, fixing "recites the
    whole history at the start" without touching the checkpoint storage.
    """
    messages = state.get("messages") or []
    prompt = build_system_prompt(state, cfg)
    folded = fold_history(messages)
    if folded:
        # 历史时间标记：进图前把历史对话中所有时间戳扫一遍加上 [历史@时间戳]，不当作当下时刻。
        folded = stamp_history_lines(folded)
        prompt += f"\n\n{EARLIER_HEADER}\n" + "\n".join(folded)
    return [SystemMessage(content=prompt), *trim_messages(messages)]
