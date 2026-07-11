import json
import urllib.request
import urllib.error

from profile.config import (
    LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_TEMPERATURE
)
from profile.log import get_logger, log_error

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

        data = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or LLM_MAX_TOKENS,
            "temperature": temperature if temperature is not None else LLM_TEMPERATURE,
            "stream": False,
        }

        logger.debug("LLM request", extra={
            "extra": {
                "model": self.model,
                "api_url": self.api_url,
                "prompt_length": len(prompt),
                "system_prompt_length": len(system_prompt) if system_prompt else 0,
                "max_tokens": max_tokens or LLM_MAX_TOKENS,
                "temperature": temperature if temperature is not None else LLM_TEMPERATURE,
            }
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

        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if "choices" in body and len(body["choices"]) > 0:
                    content = body["choices"][0].get("message", {}).get("content", "")
                    usage = body.get("usage", {})
                    logger.info("LLM response ok", extra={
                        "extra": {
                            "model": self.model,
                            "content_length": len(content),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                        }
                    })
                    return content.strip()
                raise RuntimeError(f"LLM 响应格式异常: {body}")
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            log_error(logger, f"LLM API HTTP {e.code}", exc=e, context={
                "api_url": self.api_url,
                "model": self.model,
                "http_code": e.code,
                "response_body": error_body[:500],
            })
            raise RuntimeError(f"LLM API HTTP {e.code}: {error_body}") from e
        except urllib.error.URLError as e:
            log_error(logger, "LLM API 网络错误", exc=e, context={
                "api_url": self.api_url,
                "model": self.model,
            })
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
        return result

    def _extract_json(self, text: str) -> dict | None:
        """从 LLM 响应中提取 JSON，处理 markdown 包裹。失败返回 None。"""
        import re
        if not text or not text.strip():
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
        return None


class JsonParseError(RuntimeError):
    """LLM 响应无法解析为 JSON。"""
