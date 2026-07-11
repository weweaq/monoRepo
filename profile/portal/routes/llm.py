"""LLM API: llm_calls 查询。"""

from fastapi import APIRouter, HTTPException

from profile.portal.db_store import query_llm_calls, get_llm_call

router = APIRouter()


@router.get("/llm/calls")
def list_llm_calls(page: int = 1, page_size: int = 20,
                   step: str = None, model: str = None, success: int = None):
    return query_llm_calls(page=page, page_size=page_size, step=step, model=model, success=success)


@router.get("/llm/calls/{call_id}")
def get_llm_call_detail(call_id: int):
    row = get_llm_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="LLM 调用记录不存在")
    return row
