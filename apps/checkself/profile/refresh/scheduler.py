"""定时刷新入口。

被 Windows 计划任务或 Linux cron 调用，每周执行一次：
    python -m profile.refresh.scheduler
"""

from profile.cli.refresh_all import main


def run() -> int:
    return main([])


if __name__ == "__main__":
    raise SystemExit(run())
