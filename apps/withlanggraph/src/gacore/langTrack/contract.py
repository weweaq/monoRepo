"""langTrack 采集契约：期望事件类型（源自 weiCheckApp 设计 spec 2026-08-16）。

consumed 仅用于信息展示（ETL 是否消费该类型），不决定是否"期望到达"。
契约由人维护：客户端新增/废弃事件类型时，显式更新本文件。
"""

from __future__ import annotations

# type -> {desc, consumed}
# consumed: "true"=ETL 消费, "partial"=部分消费, "false"=仅采集不消费
EXPECTED_EVENT_TYPES: dict[str, dict] = {
    "usage":         {"desc": "前台App会话",    "consumed": "true"},
    "session":       {"desc": "屏幕/解锁/切换",  "consumed": "true"},
    "notification":  {"desc": "通知",           "consumed": "true"},
    "location":      {"desc": "GPS位置",        "consumed": "true"},
    "audio_env":     {"desc": "环境音频特征",    "consumed": "true"},
    "audio_clip":    {"desc": "环境音频片段",    "consumed": "true"},
    "accel":        {"desc": "加速度聚合",      "consumed": "false"},
    "snapshot":      {"desc": "状态快照",       "consumed": "partial"},
    "screen_content":{"desc": "屏幕内容",       "consumed": "false"},
    "clipboard":     {"desc": "剪贴板",         "consumed": "false"},
    "input":         {"desc": "输入法",         "consumed": "false"},
    "media":         {"desc": "媒体播放",       "consumed": "false"},
    "bt_device":     {"desc": "蓝牙设备",       "consumed": "false"},
    "battery":       {"desc": "电池",          "consumed": "false"},
    "network":       {"desc": "网络切换",       "consumed": "false"},
    "app_lifecycle": {"desc": "应用生命周期",   "consumed": "false"},
    "call":          {"desc": "通话",          "consumed": "false"},
    "sms":           {"desc": "短信",          "consumed": "false"},
}

# 覆盖判定的"近期"窗口（天）：last_seen 超过此值视为 stale
STALE_DAYS = 7
