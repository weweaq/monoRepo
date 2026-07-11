import json

from profile.config import LLM_FALLBACK_TO_RULES, LLM_MODEL
from profile.db.store import get_unprocessed_raw_data, insert_intent
from profile.llm.client import LLMClient
from profile.llm.prompts import INTENT_SYSTEM
from profile.log import get_logger, log_error

logger = get_logger("llm.intent_extractor")

# 小于此长度的 content 直接作为意图，不调 LLM
SHORT_CONTENT_THRESHOLD = 100


class IntentExtractor:
    """从 raw_data 提取用户意图，写入 llm_intents 表。

    短文本（<100字符）直接用原文作为意图，跳过 LLM。
    长文本批量调用 LLM 提取结构化意图。
    """

    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()

    def extract_all(self, source: str | None = None, batch_size: int = 5) -> int:
        """提取所有未处理记录的意图，返回成功条数。"""
        if not self.client.is_available():
            logger.warning("LLM 不可用，跳过意图提取")
            return 0

        unprocessed = get_unprocessed_raw_data(source=source)
        total = len(unprocessed)
        if total == 0:
            logger.info("没有需要处理的记录")
            return 0

        # 分流：短文本直接入库，长文本走 LLM
        short_records = []
        long_records = []
        for r in unprocessed:
            content = r.get("content", "")
            if len(content) < SHORT_CONTENT_THRESHOLD:
                short_records.append(r)
            else:
                long_records.append(r)

        logger.info("待提取记录", extra={
            "extra": {
                "total": total,
                "short_count": len(short_records),
                "long_count": len(long_records),
                "source": source,
            }
        })

        count = 0

        # 短文本：直接入库
        for r in short_records:
            try:
                insert_intent(
                    raw_data_id=r["id"],
                    intent_category="短消息",
                    intent_summary=r["content"],
                    keywords=[],
                    sentiment="neutral",
                    priority="low",
                    project_name=None,
                    llm_model="rule_short",
                )
                count += 1
            except Exception as e:
                log_error(logger, f"短文本入库失败 id={r.get('id')}", exc=e, context={
                    "raw_data_id": r.get("id"),
                    "content_preview": str(r.get("content", ""))[:100],
                })

        if short_records:
            logger.info("短文本直接入库完成", extra={
                "extra": {"count": len(short_records)}
            })

        # 长文本：批量 LLM 提取
        for i in range(0, len(long_records), batch_size):
            batch = long_records[i : i + batch_size]
            try:
                results = self._extract_batch(batch)
                for record, result in zip(batch, results):
                    if result is None:
                        continue
                    insert_intent(
                        raw_data_id=record["id"],
                        intent_category=result.get("category", "其他"),
                        intent_summary=result.get("summary", ""),
                        keywords=result.get("keywords", []),
                        sentiment=result.get("sentiment", "neutral"),
                        priority=result.get("priority", "medium"),
                        project_name=result.get("project"),
                        llm_model=LLM_MODEL,
                    )
                    count += 1
            except Exception as e:
                log_error(logger, f"批量 LLM 处理失败 batch={i}-{i+len(batch)}", exc=e, context={
                    "batch_start": i,
                    "batch_end": i + len(batch),
                    "batch_size": len(batch),
                })

            done = min(i + len(batch), len(long_records))
            if done % 20 == 0 or done >= len(long_records):
                logger.info("LLM 处理进度", extra={
                    "extra": {"done": done, "total": len(long_records)}
                })

        logger.info("意图提取完成", extra={
            "extra": {"extracted": count, "total": total}
        })
        return count

    def _extract_batch(self, records: list[dict]) -> list[dict | None]:
        """把多条记录打包成一次 LLM 调用。"""
        inputs = []
        for r in records:
            actions = r.get("actions")
            if isinstance(actions, str):
                try:
                    actions = json.loads(actions)
                except json.JSONDecodeError:
                    actions = []
            inputs.append({
                "id": r["id"],
                "content": r["content"],
                "actions": actions or [],
            })

        user = self._build_batch_prompt(inputs)
        logger.debug("LLM batch request", extra={
            "extra": {"batch_size": len(records), "prompt_length": len(user)}
        })
        raw = self.client.chat(user, system_prompt=INTENT_SYSTEM, max_tokens=4096)
        return self._parse_batch_response(raw, len(records))

    def _build_batch_prompt(self, inputs: list[dict]) -> str:
        lines = ["分析以下多条用户与AI助手的对话记录，为每条记录提取意图。"]
        for idx, item in enumerate(inputs, 1):
            lines.append(f"\n--- 记录 {idx} ---")
            lines.append(f"内容：{item['content']}")
            lines.append(f"动作：{json.dumps(item['actions'], ensure_ascii=False)}")

        lines.append("\n请返回 JSON 数组，每个元素对应一条记录：")
        lines.append(json.dumps([{
            "category": "技术探索|项目实施|问题排查|学习了解|生活诉求|其他",
            "summary": "一句话概括",
            "keywords": ["关键词"],
            "sentiment": "positive|neutral|frustrated|curious|excited",
            "priority": "high|medium|low",
            "project": "项目名或 null"
        }], ensure_ascii=False, indent=2))
        return "\n".join(lines)

    def _parse_batch_response(self, raw: str, expected_count: int) -> list[dict | None]:
        try:
            data = self.client._extract_json(raw)
        except Exception as e:
            log_error(logger, "无法解析批量 LLM 响应", exc=e, context={
                "response_preview": raw[:300],
                "expected_count": expected_count,
            })
            return [None] * expected_count

        if isinstance(data, list):
            results = data
        elif isinstance(data, dict) and "results" in data:
            results = data["results"]
        else:
            log_error(logger, "批量 LLM 响应格式异常", context={
                "response_type": type(data).__name__,
                "response_preview": str(data)[:300],
                "expected_count": expected_count,
            })
            return [None] * expected_count

        if len(results) < expected_count:
            results.extend([None] * (expected_count - len(results)))
        return results[:expected_count]
