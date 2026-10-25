# 周报 + 人物闸（P4）实施计划

**Goal:** 周日 20:30 生成周报（数据窗 周一 00:00 – 周日 20:00）：**数字由程序填、叙事由模型写**，九节结构（§5.4），复用 `_audit_claims` 审计，邮件存档 `logs/delivered_weekly/{YYYY-Www}.md`，QQ 推摘要与评分提示；`5 4` 评分两问（rubric_version 版本化）与 `[候选-N]` 确认（决策 27）落库。

**Spec:** `docs/superpowers/specs/2026-10-24-daily-report-eval-design.md` §5.4（决策 16–22、26、27）

## Global Constraints

- **铁律：数字由程序填**（决策 18）：提交/睡眠/屏幕/审计分/提问回答全部由 `collect_week_numbers` 算好注入 prompt 只读区，模型只能引用。
- 周报正文 ≤1200 字、首屏（本周一句话）≤120 字、每条变化 ≤2 行；不重述日报（决策 21）。
- 周报想问你 ≤3；同样过 `cap_questions`（limit=3）与审计。
- 评分只问两维（像不像我/有用，决策 17），带 `rubric_version`；准确度不问人。
- 全部 best-effort：周报链路任何失败不影响既有日报。

## Review Focus

1. **weekday 调度**：`Sun 20:30` 形态与既有 `HH:MM`/`every` 兼容，daily job 零影响。
2. **week id 口径**：`YYYY-Www` 贯穿 delivered_weekly 文件名、审计 v1、评分存储、回看；周报审计的 last_seen 锚点=数据窗末日（周日）。
3. **捕获优先级**：评分（两数字）> 候选确认（数字/0/-N，仅 pending 期间）> QA；pending 评分/候选状态 3 天过期。
4. confirmed 候选写长期画像：追加 `global_mem_facts.txt`（`[date]` 行格式），向量同步靠既有记忆维护的全量 sync 兜底——不走 persist_entry（记录偏差）。

---

### Task 1: `gacore/weekly_stats.py` 周数字聚合

- `week_of(day) -> "YYYY-Www"`、`week_window(week) -> (周一, 周日)`、`collect_week_numbers(cfg, week) -> str`（只读数字区 markdown）。
- 数字：git 提交/日（`git log` 一次取 7 天分组）；屏幕小时/睡眠分钟（daily_stats `mode=ro`，缺表行标无数据）；审计分/日 + 周均（`report_audit/*.v1.json`）；QA 提问/回答数（qa_store）；环比=与上一周同口径。
- 测试：合成 daily_stats/git 桩/审计 v1，断言序列、周均、环比方向、缺数据行。

### Task 2: scheduler 周报接线

- `_TIME_RE` 扩展 weekday 前缀（`Sun 20:30`/`周日 20:30`）。
- `_is_weekly_job`（name 含 weekly）；run_job：weekly 分支注入数字区（`collect_week_numbers` 前置 prompt）；投递路径 `cap_questions(reply, limit=3)` → `stamp_report_bullets` → 存 `logs/delivered_weekly/{week}.md` → `_audit_delivered(week)`（audit_report 支持 week 路径）→ `_deliver(for_day=week)` → QQ 推评分提示（确定性文本，best-effort）。
- `audit.py`：`_report_path(cfg, report_id)`——`YYYY-Www` 走 delivered_weekly；`_anchor_day`：week → 周日（last_seen 锚点）；R5 对周报 fail-open（找不到上周六 v1 即跳过）。
- 测试：weekday due、weekly 注入与落盘、审计 v1 以 week 命名。

### Task 3: `gacore/weekly_eval.py` 评分与候选捕获

- 状态 `data/eval/human/pending.json`：`{week, candidates:[{key,question,answer}], created_at}`（投递时从 reply 的 `[候选-N]` 行与 candidates.jsonl pending 对齐写入）。
- `try_weekly_interaction(cfg, content) -> str | None`：`a b`（0-5 两数，可带第三数字与自由文本）→ 存 `{week, alive, useful, rubric_version:1, least_like_me, raw}` 至 `data/eval/human/{week}.json`；`1 3`（纯数字，仅 pending 有候选）→ confirmed；`-2` → rejected；`0` → 全部 rejected；未提及保持 pending。confirmed 追加 `memory/global_mem_facts.txt`（`[date] fact: …（周报候选确认）`）。
- `qq.py` 路由：try_answer 之后、feedback 之前插入；pending 过期 3 天。
- 测试：评分落盘与 rubric_version、候选确认/否掉/全否、confirmed 进 facts、纯数字不被 QA 吞（顺序）、过期。

### Task 4: schedule.json `weekly-report` job（替换 weekly-summary）

- schedule `Sun 20:30`、type job、deliver_to email；prompt = 九节结构 + 铁律声明 + 字数约束 + 预测要求（决策 26：三条变化 ≥1 条可回验预测）+ 回看指引（`file_read` 上期周报）+ `[候选-N]` 从数字区候选清单原样列出 + 想问你 ≤3。
- 旧 `weekly-summary`（every 7d 自由总结）删除——被本 job 取代（记偏差）。

### Task 5: 三处文档同步 + 门禁 + push

## 验收（对照 spec §9）

- [ ] 周日 20:30 触发（weekday 调度实测）；数字区由程序生成且模型不得改写（prompt 铁律）
- [ ] `logs/delivered_weekly/{YYYY-Www}.md` 存档 + `{week}.v1.json` 审计（周报同样跑 R1/R2）
- [ ] QQ 回 `5 4` 落 `data/eval/human/{week}.json`（含 rubric_version）；回 `1`/`-2`/`0` 操作候选，confirmed 进 global_mem_facts
- [ ] 周报想问你 ≤3（cap_questions limit）
