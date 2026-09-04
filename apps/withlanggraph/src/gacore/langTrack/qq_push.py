"""Proactive QQ push: send a C2C message to known QQ users via botpy.

The QQ frontend (``gacore.frontends.qq``) records every user's openid into
``data/qq_known_users.json`` as soon as they message the bot. This script reads
that list and pushes an arbitrary message to them — e.g. a personalized report
hoisted from the langTrack pipeline.

Run with::

    python -m gacore.langTrack.qq_push "你好，船长！"
    python -m gacore.langTrack.qq_push --to <openid> "只推给指定用户"
    python -m gacore.langTrack.qq_push --show           # 只列出已知用户，不发消息

Environment (from the repo root .env)::

    QQ_APP_ID / QQ_APP_SECRET

Implementation note: this uses botpy's ``BotHttp``/``BotAPI`` (plain HTTP) instead
of ``Client``. ``Client.start()`` owns the long-lived websocket session and blocks
forever (``_pool_init`` awaits the ws loop), so a one-shot script that calls
``Client`` never reaches the send call — the process just sits there. ``BotHttp``
only fetches an access token and calls the REST API, which exits cleanly.

Policy notes (QQ Open Platform, 2025):
- C2C proactive pushes require the receiver's openid in the sandbox whitelist
  (沙箱名单) while the bot is in sandbox mode.
- Since 2025-04 Tencent officially discontinued "主动消息推送" (proactive push);
  whether the platform still delivers without a recent user message is not
  guaranteed. The most reliable path is a passive reply (with ``msg_id``) within
  5 minutes of a user message.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from gacore.config import load_dotenv

load_dotenv()

# 项目根：src/gacore/langTrack/qq_push.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
KNOWN_USERS_FILE = _PROJECT_ROOT / "data" / "qq_known_users.json"


def _fix_encoding() -> None:
    """Force stdout/stderr to UTF-8 so Chinese text renders correctly on Windows."""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if enc and enc.upper() != "UTF-8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def load_known_users() -> dict[str, dict]:
    """Load openid -> info map; missing/corrupt file degrades to {}."""
    try:
        raw = json.loads(KNOWN_USERS_FILE.read_text(encoding="utf-8"))
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _show_users(users: dict[str, dict]) -> None:
    if not users:
        print("暂无已知用户 —— 先给机器人发一条私聊消息，openid 会被自动记录。")
        return
    print(f"已知 QQ 用户（{len(users)}）：")
    for uid, info in users.items():
        first = (info.get("first_seen") or "-")[:19]
        last = (info.get("last_seen") or "-")[:19]
        print(f"  openid={uid}\n    first_seen={first}  last_seen={last}")


async def send_c2c(
    content: str, targets: list[str], is_sandbox: bool = False
) -> dict:
    """Send content to each target openid via botpy BotHttp/BotAPI; return a result dict.

    Shared by the CLI (``_push``) and the agent tool (``gacore.tools.qq_tools.qq_push``).

    Unlike ``Client`` (websocket bot loop), ``BotHttp`` is a plain REST client:
    fetch access token -> call POST /v2/users/{openid}/messages -> close. It
    cannot hang on a ws session and exits as soon as the sends are done.

    Returns ``{"ok": n, "failures": [openid...], "errors": {openid: str},
    "ids": {openid: message_id}}``; or ``{"error": tag, "message": str}`` when
    credentials are missing or botpy is not installed. Never raises.
    """
    if not targets:
        return {"ok": 0, "failures": [], "errors": {}, "ids": {}}

    app_id = os.environ.get("QQ_APP_ID", "").strip()
    app_secret = os.environ.get("QQ_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        return {
            "error": "qq_not_configured",
            "message": "缺少 QQ_APP_ID / QQ_APP_SECRET（请检查项目根 .env）",
        }

    try:
        from botpy import BotAPI  # noqa: PLC0415
        from botpy.http import BotHttp  # noqa: PLC0415
        from botpy.robot import Token  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"error": "botpy_missing", "message": f"qq-botpy not installed: {exc}"}

    http = BotHttp(timeout=15, is_sandbox=is_sandbox)
    ok = 0
    failures: list[str] = []
    errors: dict[str, str] = {}
    ids: dict[str, str] = {}
    try:
        await http.login(Token(app_id, app_secret))
        api = BotAPI(http=http)
        for openid in targets:
            try:
                result = await api.post_c2c_message(
                    openid=openid,
                    msg_type=0,
                    content=content,
                    msg_seq=int(datetime.now().timestamp() * 1000) % (2**31),
                )
                ok += 1
                mid = result.get("id") if isinstance(result, dict) else result
                ids[openid] = str(mid or "")
            except Exception as exc:  # noqa: BLE001
                failures.append(openid)
                errors[openid] = str(exc)
    finally:
        try:
            await http.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": ok, "failures": failures, "errors": errors, "ids": ids}


async def _push(content: str, targets: list[str], is_sandbox: bool) -> tuple[int, list[str]]:
    """CLI-facing wrapper around :func:`send_c2c`; prints per-recipient results."""
    if not targets:
        print("没有可推送的目标用户。")
        return 0, []

    result = await send_c2c(content, targets, is_sandbox)
    if result.get("error"):
        print(result.get("message", result["error"]))
        sys.exit(1)

    for openid in targets:
        if openid in result["ids"]:
            print(f"[ok] {openid} id={result['ids'][openid]}")
        else:
            print(f"[fail] {openid}: {result.get('errors', {}).get(openid, 'unknown')}")
    return int(result["ok"]), list(result["failures"])


def main() -> None:
    _fix_encoding()
    parser = argparse.ArgumentParser(description="向已知 QQ 用户主动推送私聊消息")
    parser.add_argument("message", nargs="?", default=None, help="要推送的消息内容")
    parser.add_argument("--to", default="", help="指定 openid（逗号分隔）；缺省推给全部已知用户")
    parser.add_argument("--show", action="store_true", help="只列出已知用户，不发消息")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱 API 域名（默认走正式域名）")
    args = parser.parse_args()

    users = load_known_users()

    if args.show:
        _show_users(users)
        return

    if not args.message:
        parser.print_help()
        return

    if args.to:
        targets = [x.strip() for x in args.to.split(",") if x.strip()]
    else:
        targets = list(users.keys())

    ok, failures = asyncio.run(_push(args.message, targets, args.sandbox))
    print(f"完成：成功 {ok}，失败 {len(failures)}")
    if failures:
        print("失败名单:", ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
