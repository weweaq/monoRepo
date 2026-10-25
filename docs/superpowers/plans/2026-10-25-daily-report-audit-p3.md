# 日报提问回路（P3）实施计划

**Goal:** 让 `# 想问你` 从"日报里的一节"变成闭环机制：投递后落 qa 库 → 次日 08:30 QQ 推送（决策 25）→ 用户在 QQ 回答被捕获（`[Q1]`/裸回答，决策：不走订正阶梯）→ 回答与未答 key 经 `_QA` 源注入下一期（cap 600 字，决策 24）→ 已答永不再问、未答最多重问 2 期。

**Spec:** `docs/superpowers/specs/2026-10-24-daily-report-eval-design.md` §5.3（决策 8/9/10/11/23/24/25）

## Global Constraints

- **问答绝不走 `parse_feedback`**（决策 8）：捕获在 `feedback_route` 之前，命中即出，不触发订正/重发。
- 回答只影响下一期（决策 9）；不自动写长期画像，候选进 `candidates`（决策 10）。
- qa 数据在 `data/qa/`（R11，不入库）；JSONL 带 `created_at/updated_at`（东八区）。
- 捕获与推送全部 best-effort：任何失败不影响 QQ 正常聊天与日报投递。
- 测试封闭：`Config.for_tests(tmp_path)`；不真实发 QQ、不碰真实 `data/qa/`。

## Review Focus

1. **裸回答误伤**：仅当「有且仅有 1 条未归档开放问题」且消息 ≤30 字且不含 feedback 关键词时归给该问题；`[Q N]`/`想问你-N` 显式引用优先。
2. **重问上限**：push 时 `asks+1`，`asks > 2` 仍未答 → `status=archived`（用户不关心），不再注入。
3. **同期语义去重**（P2 遗留）：seed 时同日 token 重合 >60% 的后续问题丢弃。
4. `_QA` 源按 add-info-source skill 的军规接入（cap 600、builder 只读、状态行、封闭测试）。

---

### Task 1: qa 落库层 `gacore/qa_store.py`

- `data/qa/{date}.jsonl` 行：`{question_key, question, anchor, day, status(open/answered/archived), answer, answer_day, asks, created_at, updated_at}`；`data/qa/candidates.jsonl`：`{question_key, question, answer, status(pending/confirmed/rejected), created_at, updated_at}`；`data/qa/open_state.json`：`{day, question_key}` 裸回答指针。
- `question_key = sha1(topic + 意图)`：topic 取 `audit_topics` 命中（无则空串），意图 = 问题文本去数字/日期/空白后的小写归一。
- 接口：`seed_questions(cfg, day, bullets)`（去重：已 answered 的 key 不再入；同期 token 重合 >60% 丢弃并计数）、`open_questions(cfg)`（未答未归档，按 day/anchor 排序）、`record_answer(cfg, question_key, answer)`（写 answer + status=answered + 更新指针 + 追加 candidates pending）、`archive_stale(cfg, max_asks=2)`、`stats(cfg)`（每期提问数/回答率/去重命中）。
- token 重合：中文按 2-gram 字符集 Jaccard > 0.6。

### Task 2: 投递后种入（scheduler）

- `_seed_qa(cfg, day, deliver_reply)`：从已 stamp 的投递正文提取 `# 想问你` bullets（带 `[想问你-N]` 锚），调 `seed_questions`；挂在 `save_delivered` 成功后（审计钩子旁），best-effort。
- 测试：stamp 后正文种入 qa 行、anchor 正确、同 key 重复种入不重复。

### Task 3: proactive `questions` 场景推送

- `run_proactive_job`：`scene == "questions"` 时从 qa_store 取 open_questions；空 → `skipped(no_open_questions)`；否则注入 `build_proactive_prompt(questions=...)` 硬块（逐条原样转述、保留 `[Q1]` 编号、不加不减不改写）；push 成功后 `asks+1` 并 `archive_stale`。
- `config/schedule.json` 新增 job：`daily-questions`，schedule `08:30`，type `proactive`，scene `questions`，window `08:00-09:30`，cooldown 720。
- 测试：prompt 构造含问题清单与硬约束；无开放问题的 skip 分支（run_proactive_job 的逐用户循环用桩）。

### Task 4: QQ 捕获 `try_proactive_qa`

- `gacore/qa_store.py` 提供 `try_answer(cfg, content) -> str | None`：显式 `[Q1]`/`Q 1`/`想问你-1` 解析序号 → 当日开放问题；裸回答（≤30 字、无 feedback 关键词、唯一开放问题、其 asks>0 即已推送过）→ 指针；命中返回确认文本（含将进下一期日报的说明），未命中返回 None。
- `qq.py on_message` 在 `feedback_route` 判定后、dispatch 前：`qa_reply = try_answer(cfg, content)` 非 None → `send_text` 回确认并 return（决策 8：不进订正、不进 agent 轮）。
- 测试：显式序号回答、裸回答唯一开放、多条开放时裸回答不吞（返回 None 走正常聊天）、订正关键词不被吞、回答后 open_state 指针清空。

### Task 5: `_QA` 信息源（走 add-info-source skill）

- `daily_info_pack` 注册 `_QA` 源：cap 600 字（决策 24），内容 =「最近 7 天已回答问答（问题→回答）」+「仍未回答的问题 key 清单」；超限优先保留未答 key + 最近 3 组问答。
- 按 skill 军规：SourceSpec 注册表、builder 三元组、封闭测试、状态行、真实验证。

### Task 6: 度量 + 文档三处同步 + 门禁

- `stats` 进 `qa_seed`/`qa_answer` 日志字段；ROADMAP 追加 P3 执行记录；tech §9.36；架构图（QA 节点 + QQ 捕获边 + proactive questions 场景）+ mermaid 断言同步；`docs/superpowers/specs` §14.1 状态更新。
- 门禁：ruff + 全量 pytest（--basetemp）全绿，分笔提交，push。

## 验收（对照 spec §9）

- [ ] 日报投递后 qa 行自动种入；08:30 有开放问题才推送，0 条不推
- [ ] `[Q1] 在读` 与唯一开放时的裸回答都能落 answer + candidates(pending)
- [ ] 已答 key 再 seed 不再开放；未答 push 2 期后归档
- [ ] `_QA` 进信息包（cap 600），次日日报可引用
- [ ] 问答不触发订正/重发（决策 8）
