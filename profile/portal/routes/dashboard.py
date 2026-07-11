"""Dashboard 路由（Task 6 完善）。

当前为占位实现：返回 dashboard 概览所需的最小结构，
保证 Task 5 骨架测试通过。Task 6 将替换为真实数据查询。
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/dashboard")
def get_dashboard() -> dict:
    """返回 dashboard 概览数据。

    占位结构包含前端约定的四个键：
    - current_task: 当前运行中的任务（无则 None）
    - recent_tasks: 近期任务列表
    - data_overview: 原始数据与意图概览
    - llm_overview: LLM 调用概览
    """
    return {
        "current_task": None,
        "recent_tasks": [],
        "data_overview": {},
        "llm_overview": {},
    }
