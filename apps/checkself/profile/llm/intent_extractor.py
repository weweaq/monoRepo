import json

from profile.config import LLM_MODEL
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
        logger.info("意图提取开始", extra={
            "extra": {"source": source or "all", "batch_size": batch_size}
        })

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

        logger.info("记录分流完成", extra={
            "extra": {
                "total": total,
                "short_count": len(short_records),
                "long_count": len(long_records),
                "source": source,
                "short_threshold": SHORT_CONTENT_THRESHOLD,
            }
        })

        count = 0

        # 短文本：直接入库
        short_ok = 0
        short_fail = 0
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
                short_ok += 1
            except Exception as e:
                short_fail += 1
                log_error(logger, f"短文本入库失败 id={r.get('id')}", exc=e, context={
                    "raw_data_id": r.get("id"),
                    "content_preview": str(r.get("content", ""))[:100],
                })

        if short_records:
            logger.info("短文本入库完成", extra={
                "extra": {
                    "total": len(short_records),
                    "success": short_ok,
                    "failed": short_fail,
                }
            })
        count += short_ok

        # 长文本：批量 LLM 提取
        total_batches = (len(long_records) + batch_size - 1) // batch_size if long_records else 0
        batch_ok = 0
        batch_fail = 0
        for batch_idx, i in enumerate(range(0, len(long_records), batch_size), 1):
            batch = long_records[i : i + batch_size]
            batch_ids = [r.get("id") for r in batch]
            batch_contents = [str(r.get("content", ""))[:50] for r in batch]

            logger.info("批次开始", extra={
                "extra": {
                    "batch_idx": batch_idx,
                    "total_batches": total_batches,
                    "batch_size": len(batch),
                    "record_ids": batch_ids,
                    "content_previews": batch_contents,
                }
            })

            try:
                results = self._extract_batch(batch)
                inserted = 0
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
                    inserted += 1
                    count += 1
                batch_ok += 1
                logger.info("批次完成", extra={
                    "extra": {
                        "batch_idx": batch_idx,
                        "total_batches": total_batches,
                        "inserted": inserted,
                        "batch_size": len(batch),
                        "results_summary": [
                            {"id": r.get("id"), "category": res.get("category") if res else None, "summary": (res.get("summary") or "")[:60] if res else None}
                            for r, res in zip(batch, results)
                        ],
                    }
                })
            except Exception as e:
                batch_fail += 1
                log_error(logger, f"批次 LLM 处理失败 batch={batch_idx}/{total_batches}", exc=e, context={
                    "batch_idx": batch_idx,
                    "total_batches": total_batches,
                    "batch_size": len(batch),
                    "record_ids": batch_ids,
                })

        logger.info("意图提取完成", extra={
            "extra": {
                "extracted": count,
                "total": total,
                "short_inserted": short_ok,
                "short_failed": short_fail,
                "llm_batches_ok": batch_ok,
                "llm_batches_failed": batch_fail,
                "source": source,
            }
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
        logger.debug("构建批次 prompt", extra={
            "extra": {
                "batch_size": len(records),
                "prompt_length": len(user),
                "record_ids": [r["id"] for r in records],
            }
        })
        raw = self.client.chat(user, system_prompt=INTENT_SYSTEM, max_tokens=4096)
        logger.debug("批次 LLM 原始响应", extra={
            "extra": {
                "response_length": len(raw),
                "response_preview": raw[:200],
            }
        })
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
        data = self.client._extract_json(raw)
        if data is None:
            log_error(logger, "无法解析批量 LLM 响应", context={
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
            logger.warning("LLM 返回结果数不足", extra={
                "extra": {
                    "expected": expected_count,
                    "actual": len(results),
                    "padded_with_none": expected_count - len(results),
                }
            })
            results.extend([None] * (expected_count - len(results)))

        logger.info("批次响应解析完成", extra={
            "extra": {
                "expected_count": expected_count,
                "parsed_count": len(results),
                "valid_count": sum(1 for r in results if r is not None),
                "categories": [r.get("category") if r else None for r in results],
            }
        })
        return results[:expected_count]
