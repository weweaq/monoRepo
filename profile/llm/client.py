import json
import time
import urllib.request
import urllib.error

from profile.config import (
    LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE
)
from profile.log import get_logger, log_error
from profile.portal.task_engine import get_current_task_run_id, get_current_step

logger = get_logger("llm.client")


class LLMClient:
    """硅基流动 LLM 客户端，OpenAI 兼容接口"""

    def __init__(self, api_key=None, api_url=None, model=None):
        self.api_key = api_key or LLM_API_KEY
        self.api_url = api_url or LLM_API_URL
        self.model = model or LLM_MODEL

    def is_available(self) -> bool:
        """检查 API Key 是否已配置"""
        return bool(self.api_key and not self.api_key.startswith("YOUR_"))

    def chat(self, prompt: str, system_prompt: str = None, temperature: float = None, max_tokens: int = None) -> str:
        """
        发送聊天请求，返回文本响应。
        失败时抛出 Exception。
        """
        if not self.is_available():
            raise RuntimeError("LLM API Key 未配置，请设置环境变量 SILICONFLOW_API_KEY")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        effective_max_tokens = max_tokens or LLM_MAX_TOKENS
        effective_temperature = temperature if temperature is not None else LLM_TEMPERATURE

        data = {
            "model": self.model,
            "messages": messages,
            "max_tokens": effective_max_tokens,
            "temperature": effective_temperature,
            "stream": False,
        }

        logger.info("LLM 请求发出", extra={
            "extra": {
                "model": self.model,
                "api_url": self.api_url,
                "messages_count": len(messages),
                "prompt_length": len(prompt),
                "system_prompt_length": len(system_prompt) if system_prompt else 0,
                "max_tokens": effective_max_tokens,
                "temperature": effective_temperature,
            }
        })
        # DEBUG 级记录完整请求内容，仅落盘不刷控制台
        if system_prompt:
            logger.debug("LLM 请求 system_prompt 全文", extra={
                "extra": {"system_prompt": system_prompt}
            })
        logger.debug("LLM 请求 prompt 全文", extra={
            "extra": {"prompt": prompt}
        })

        req_data = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            self.api_url,
            data=req_data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                elapsed_ms = round((time.monotonic() - t0) * 1000)
                body = json.loads(resp.read().decode("utf-8"))
                if "choices" in body and len(body["choices"]) > 0:
                    content = body["choices"][0].get("message", {}).get("content", "")
                    usage = body.get("usage", {})
                    logger.info("LLM 响应成功", extra={
                        "extra": {
                            "model": self.model,
                            "elapsed_ms": elapsed_ms,
                            "response_length": len(content),
                            "response_preview": content[:100] if content else "",
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                            "finish_reason": body["choices"][0].get("finish_reason"),
                        }
                    })
                    # DEBUG 级记录完整响应内容，仅落盘不刷控制台
                    logger.debug("LLM 响应全文", extra={
                        "extra": {"response": content}
                    })
                    # 记录到 llm_calls 表
                    _record_llm_call(
                        model=self.model,
                        system_prompt=system_prompt,
                        user_prompt=prompt,
                        response=content,
                        usage=usage,
                        elapsed_ms=elapsed_ms,
                        success=True,
                    )
                    return content.strip()
                raise RuntimeError(f"LLM 响应格式异常: {body}")
        except urllib.error.HTTPError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000)
            error_body = e.read().decode("utf-8", errors="replace")
            log_error(logger, f"LLM API HTTP {e.code}", exc=e, context={
                "api_url": self.api_url,
                "model": self.model,
                "http_code": e.code,
                "elapsed_ms": elapsed_ms,
                "response_body": error_body[:500],
            })
            _record_llm_call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                response=None,
                usage={},
                elapsed_ms=elapsed_ms,
                success=False,
                error_message=f"HTTP {e.code}: {error_body[:200]}",
            )
            raise RuntimeError(f"LLM API HTTP {e.code}: {error_body}") from e
        except urllib.error.URLError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000)
            log_error(logger, "LLM API 网络错误", exc=e, context={
                "api_url": self.api_url,
                "model": self.model,
                "elapsed_ms": elapsed_ms,
            })
            _record_llm_call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                response=None,
                usage={},
                elapsed_ms=elapsed_ms,
                success=False,
                error_message=f"网络错误: {e}",
            )
            raise RuntimeError(f"LLM API 网络错误: {e}") from e

    def chat_json(self, prompt: str, system_prompt: str = None) -> dict:
        """
        发送聊天请求，期望返回 JSON。
        会尝试从响应中提取 JSON（处理 markdown code block 包裹的情况）。
        失败时抛出 JsonParseError。
        """
        raw = self.chat(prompt, system_prompt=system_prompt)
        result = self._extract_json(raw)
        if result is None:
            raise JsonParseError(f"无法从 LLM 响应中提取 JSON: {raw[:200]}")
        logger.info("JSON 解析成功", extra={
            "extra": {
                "result_type": type(result).__name__,
                "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                "raw_response_length": len(raw),
            }
        })
        return result

    def _extract_json(self, text: str) -> dict | None:
        """从 LLM 响应中提取 JSON，处理 markdown 包裹。失败返回 None。"""
        import re
        if not text or not text.strip():
            logger.warning("LLM 响应为空，无法提取 JSON")
            return None
        # 尝试直接解析
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        # 尝试从 ```json ... ``` 中提取
        match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except (json.JSONDecodeError, TypeError):
                pass
        # 尝试找到第一个 { 和最后一个 } 之间的内容
        first = text.find('{')
        last = text.rfind('}')
        if first != -1 and last != -1 and last > first:
            try:
                return json.loads(text[first:last+1])
            except (json.JSONDecodeError, TypeError):
                pass
        logger.warning("JSON 提取失败", extra={
            "extra": {"response_preview": text[:200], "response_length": len(text)}
        })
        return None


def _record_llm_call(model, system_prompt, user_prompt, response, usage,
                     elapsed_ms, success, error_message=None):
    """记录 LLM 调用到 llm_calls 表。"""
    try:
        from profile.portal.db_store import insert_llm_call
        insert_llm_call(
            task_run_id=get_current_task_run_id(),
            step=get_current_step(),
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=response,
            prompt_tokens=usage.get("prompt_tokens") if usage else None,
            completion_tokens=usage.get("completion_tokens") if usage else None,
            total_tokens=usage.get("total_tokens") if usage else None,
            elapsed_ms=elapsed_ms,
            success=1 if success else 0,
            error_message=error_message,
        )
    except Exception as e:
        # 埋点失败不影响主流程，但记录 warning 以便排查
        import logging
        logging.getLogger("profile.llm.client").warning(
            f"LLM 调用埋点失败: {e}"
        )


class JsonParseError(RuntimeError):
    """LLM 响应无法解析为 JSON。"""
