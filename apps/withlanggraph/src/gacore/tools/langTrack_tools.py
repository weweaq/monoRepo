"""langTrack data tools for gacore: surface phone usage signals into daily-report.



外挂式设计（高内聚/低耦合）：

- 只读 langTrack 服务端的 `data/langTrack.db`（ETL 产出的事实表），不触碰其他模块

- 需要最新数据时自动触发 ETL（幂等，几秒），再读事实表

- 数据读取统一委托给 `fact_card.build`（纯读聚合，单一数据源），本模块只做

  结果映射与工具注册，不再重复 SQL

- 不依赖 scheduler / graph / 其他 tools；注册只需在 tools/__init__.py 加一行

"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final, TypedDict

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger
from gacore.langTrack.fact_card import (
    AnomalyBrief,
    CurrentKnown,
    PlaceBrief,
    StayBrief,
    TripBrief,
)
from gacore.langTrack.fact_card import (
    build as build_fact_card,
)

logger = get_logger("tools.langTrack_tools")



# langTrack 服务端仓库根（data/langTrack.db 所在）；通过环境变量可覆盖（测试用）

_LANGTRACK_ROOT_ENV: Final = "LANGTRACK_ROOT"

_DEFAULT_ROOT: Final = Path(__file__).resolve().parents[3]  # WithLangGraph 仓库根



_ETL_TIMEOUT_SECONDS: Final = 120





def _root() -> Path:

    override = __import__("os").environ.get(_LANGTRACK_ROOT_ENV)

    return Path(override) if override else _DEFAULT_ROOT





def _db_path() -> Path:

    return _root() / "data" / "langTrack.db"





def _ensure_etl() -> bool:

    """ETL 幂等重建事实表（新数据落库后调用，保证读的是最新）。失败不阻塞。"""

    try:

        subprocess.run(

            [sys.executable, "-m", "gacore.langTrack.etl"],

            cwd=str(_root()),

            capture_output=True,

            timeout=_ETL_TIMEOUT_SECONDS,

            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),

            check=False,

        )

        return True

    except Exception as e:  # noqa: BLE001 - 工具错误路径：返回失败不抛

        logger.warning("langTrack ETL failed", error_type=type(e).__name__, error=str(e))

        return False





class LangTrackDayStats(TypedDict):

    """某一天的结构化使用画像。



    与 ``fact_card.FactCard`` 同构：数据读取委托给 fact_card.build（纯读），

    本 TypedDict 为工具对外契约，字段只增不减，保持向后兼容。

    语义约定：``available`` 仅表示当日有 daily_stats；``has_facts`` 才表示

    是否有可用的当日事实（daily_stats / stays / trips 任一存在）。

    """



    day: str

    available: bool

    has_facts: bool

    ambiguous_device: bool

    candidate_device_ids: list[str]

    device_id: str

    generated_at: str

    screen_ms: int

    screen_hours: float

    top_apps: list[dict]

    notification_count: int

    notification_clicked: int

    top_notification_apps: list[dict]

    screen_on_count: int

    screen_off_count: int

    unlock_count: int

    switch_count: int

    location_count: int

    audio_clip_count: int

    places: list[PlaceBrief]

    stays: list[StayBrief]

    trips: list[TripBrief]

    stay_minutes: dict[str, int]

    anomalies: list[AnomalyBrief]

    midnight_audio_n: int | None

    sleep_signal: str

    # P0 拟人语义字段（v2 设计文档附录 A；旧库未跑派生时为 None/[]，下游需做空值防御）
    # 作息节律三标量来自 daily_stats P0 列（schema 为 TEXT，故注解 str|None）；
    # sleep_signal 为遗留兼容信号，保留但以本组为准
    sleep_start_hhmm: str | None
    sleep_end_hhmm: str | None
    sleep_duration_min: int | None
    time_app: list[dict]  # 时段×应用矩阵（daily_stats.time_app_json 解析后）

    # A① 契约覆盖：非 ok 的期望事件类型清单（缺失/停滞/未登记）
    coverage: list[dict]
    persona: dict

    # 数据水位 / 当前已知 / 日窗状态（fact_card 透传）
    etl_watermark: str
    etl_watermark_ms: int | None
    data_as_of: str
    data_as_of_ms: int | None
    data_as_of_source: str
    location_as_of: str
    location_as_of_ms: int | None
    data_age_min: int | None
    day_window_closed: bool
    current_known: CurrentKnown | None

    # 位置质量（Task 6 §3.2）：当日定位质量画像 + 人工 tag 冲突数（v1/缺表 → None/0）
    daily_location_quality: dict | None
    tag_conflict_count: int

    # 长期空间画像（Task 8 §2.9，full 卡透传；compact 不携带；无数据 → None）
    spatial_profile: dict | None

    # compact 事实卡片（注入用 / 审计用；空串/空表表示未生成）
    compact_sections: list
    compact: str
    compact_chars: int
    compact_lines: list[str]
    compact_omitted: dict[str, str]
    card_fp: str


def _unavailable(day: str, reason: str) -> LangTrackDayStats:
    """返回不可用画像（available=False）。replacement reason 供旧语义兼容。"""
    return LangTrackDayStats(
        day=day, available=False, has_facts=False,
        ambiguous_device=False, candidate_device_ids=[], device_id="",
        generated_at="",
        screen_ms=0, screen_hours=0.0, top_apps=[], notification_count=0,
        notification_clicked=0, top_notification_apps=[],
        screen_on_count=0, screen_off_count=0, unlock_count=0, switch_count=0,
        location_count=0, audio_clip_count=0,
        places=[], stays=[], trips=[], stay_minutes={}, anomalies=[],
        midnight_audio_n=None, sleep_signal=reason,
        sleep_start_hhmm=None, sleep_end_hhmm=None, sleep_duration_min=None,
        time_app=[], coverage=[], persona={},
        etl_watermark="", etl_watermark_ms=None,
        data_as_of="", data_as_of_ms=None, data_as_of_source="unknown",
        location_as_of="", location_as_of_ms=None, data_age_min=None,
        day_window_closed=False, current_known=None,
        daily_location_quality=None, tag_conflict_count=0,
        spatial_profile=None,
        compact_sections=[], compact="", compact_chars=0, compact_lines=[],
        compact_omitted={}, card_fp="",
    )


def _today() -> str:

    import datetime

    return datetime.datetime.now(datetime.UTC).astimezone().strftime("%Y-%m-%d")


def _map_card_to_stats(card: dict, day: str) -> LangTrackDayStats:
    """将 FactCard 映射为 LangTrackDayStats（字段全量透传，只增不减）。

    仅做语义兼容替换：fact_card 的「当日无 daily_stats」信号在工具层还原为
    旧的「当日无数据（可能未采集或未同步）」文案，保证下游与旧测试不破坏。
    """
    ss = card["sleep_signal"]
    if not card["available"] and ss == "当日无 daily_stats":
        ss = "当日无数据（可能未采集或未同步）"
    return LangTrackDayStats(
        day=day,
        available=card["available"],
        has_facts=card["has_facts"],
        ambiguous_device=card["ambiguous_device"],
        candidate_device_ids=list(card["candidate_device_ids"]),
        device_id=card["device_id"],
        generated_at=card["generated_at"],
        screen_ms=card["screen_ms"],
        screen_hours=card["screen_hours"],
        top_apps=card["top_apps"],
        notification_count=card["notification_count"],
        notification_clicked=card["notification_clicked"],
        top_notification_apps=card["top_notification_apps"],
        screen_on_count=card["screen_on_count"],
        screen_off_count=card["screen_off_count"],
        unlock_count=card["unlock_count"],
        switch_count=card["switch_count"],
        location_count=card["location_count"],
        audio_clip_count=card["audio_clip_count"],
        places=card["places"],
        stays=card["stays"],
        trips=card["trips"],
        stay_minutes=card["stay_minutes"],
        anomalies=card["anomalies"],
        midnight_audio_n=card["midnight_audio_n"],
        sleep_signal=ss,
        sleep_start_hhmm=card["sleep_start_hhmm"],
        sleep_end_hhmm=card["sleep_end_hhmm"],
        sleep_duration_min=card["sleep_duration_min"],
        time_app=card["time_app"],
        coverage=card["coverage"],
        persona=card["persona"],
        etl_watermark=card["etl_watermark"],
        etl_watermark_ms=card["etl_watermark_ms"],
        data_as_of=card["data_as_of"],
        data_as_of_ms=card["data_as_of_ms"],
        data_as_of_source=card["data_as_of_source"],
        location_as_of=card["location_as_of"],
        location_as_of_ms=card["location_as_of_ms"],
        data_age_min=card["data_age_min"],
        day_window_closed=card["day_window_closed"],
        current_known=card["current_known"],
        daily_location_quality=card.get("daily_location_quality"),
        tag_conflict_count=card.get("tag_conflict_count", 0),
        spatial_profile=card.get("spatial_profile"),
        compact_sections=card["compact_sections"],
        compact=card["compact"],
        compact_chars=card["compact_chars"],
        compact_lines=card["compact_lines"],
        compact_omitted=card["compact_omitted"],
        card_fp=card["card_fp"],
    )


@tool
def langTrack_stats(day: str = "") -> dict:
    """读取某天（默认今天）的手机使用数据画像：屏幕时长、App 排行、通知、睡眠信号、场景。



    数据来自 langTrack 采集链路（weiCheckApp 手机端 → /ingest → ETL 事实表）。

    返回结构化信号（含数据水位、当前已知地点、当日停留/移动/异常、compact 事实卡）

    供日报交叉分析；无数据时 available=False。

    注意：available 仅表示当日有 daily_stats；has_facts 才表示是否有可用当日事实。

    """

    day = day or _today()

    # 确保事实表最新（新数据落库后自动重建）

    _ensure_etl()



    db = _db_path()

    if not db.exists():
        return _unavailable(day, "langTrack 数据库不存在")


    conn = None
    try:
        conn = sqlite3.connect(db)
        # 健康探测：损坏库在此报错（避免把噪音交给 fact_card）
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        conn.row_factory = sqlite3.Row
        card = build_fact_card(conn=conn, day=day, detail="full", outlet="tool")
    except Exception as e:  # noqa: BLE001 - 工具错误路径：返回失败不抛
        return _unavailable(day, f"读取失败: {e}")
    finally:
        if conn is not None:
            conn.close()

    return _map_card_to_stats(card, day)
