# 日报断言基线闸（P2）实施计划：查表契约接入生成

**Goal:** 把 P1 的 `last_seen` 变成日报生成时**必须调用**的工具（数字查表替代语感），prompt 增加 `# 想问你` 节与确定性硬闸，投递路径挂审计钩子（只标记不阻断）。改生成逻辑，但不改信息包取数（非目标守住）。

**Spec:** `docs/superpowers/specs/2026-10-24-daily-report-eval-design.md`（§5.2 契约、§5.3 硬闸、决策 2/12.1/23/28）

**Tech Stack:** 现有 `gacore.audit`、`gacore.tools` 注册表、`gacore.scheduler`；不新增第三方依赖。

## Global Constraints

- 审计钩子 **best-effort**：任何审计失败绝不影响投递（与 `_sync_episodic` 同纪律）。
- 不动 `daily_info_pack` 的 SOURCES（那是 P3 `_QA` 源的事）。
- prompt 新增规则合计 ≤200 字，用工具契约替代说教（spec §10 风险表）。
- 测试封闭：`Config.for_tests(tmp_path)`，禁真实 `memory/`、`logs/`、网络、LLM。
- `scheduler.py` 的改动只允许：想问你闸（stamp 前）+ 审计钩子（save_delivered 后、_deliver 前，决策 2 位置）；生成与投递逻辑本身零改动。

## Review Focus

1. **last_seen 读历史日报只认 v1 `report_text`**（决策 23）——被订正重写的当前文件、无 `report_text` 字段的快照都不作为证据。
2. **`cap_questions` 只动 `# 想问你` 节**——其余正文逐字不变（`_diff_gate` 纪律）。
3. **连续计数 R5 是 warn**，只在 topic 词表能同时识别两天主题时才判（fail-open）。
4. schedule.json 用 json round-trip 修改，diff 只允许落在 daily-report 的 prompt 字段。

---

### Task 1: `last_seen` 数据源扩展——历史日报 v1 快照

**Files:** Modify `gacore/audit.py`、`tests/test_audit.py`

- Produces: `_scan_reports(cfg, synonyms, before_date)`，并入 `last_seen` 的命中集合；source 形如 `logs/report_audit/2026-10-11.v1.json`。
- 规则：只读 `logs/report_audit/*.v1.json` 的 `report_text` 字段（日期取文件名前 10 位，严格早于 `before_date`）；`report_text` 缺失/为空的快照跳过（决策 23：无快照不作为证据源）。
- 测试：①v1 快照正文命中 topic，`last_seen` 返回该日期与 report_audit source；②无 `report_text` 字段的快照不被使用；③报告日期 ≥ before_date 不算。

### Task 2: `last_seen` 注册为生成工具

**Files:** Create `gacore/tools/audit_tools.py`；Modify `gacore/tools/__init__.py`、`tests/test_audit.py`

- Produces: `@tool last_seen(topic: str, before_date: str = "") -> dict`（自配 `Config.default()`，`before_date` 缺省=今天东八区）；返回 `{found, date, gap_days, source, snippet}`，与 `gacore.audit.last_seen` 同构。docstring 写清「写时间量级断言前先查此表」的用途。
- 注册三处（import / TOOL_NAMES / _TOOLS，外挂式），测试断言 `"last_seen" in TOOL_NAMES` 且 `build_tool_list` 含它。

### Task 3: `schedule.json` prompt 接入

**Files:** Modify `apps/withlanggraph/config/schedule.json`

- 【素材】的「不要再调用取数工具」清单里为 `last_seen` 开例外，并加查表契约（~120 字）：任何「N 天/周/月 + 沉寂/时隔/连续/首次…」断言，先调 `last_seen(topic)`；三选一出口：带 `（上次：YYYY-MM-DD，间隔 N 天）` / 改成问句进想问你 / 删量级只留观察。没查就写量级 = 违规。
- 固定分节追加 `# 想问你`（~150 字）：0–5 条、0 条合法（整节连同标题省略）；每条 = 一句铺垫（我看到了什么）+ 一个问句 + `（依据：…）`；只问信号存在但语义缺失、画像自相矛盾两类；价值取舍类必须两选项都在近 14 天数据里观测到且允许"都不是"（决策 12.1）。
- json round-trip 修改；`git diff` 核对只有 prompt 字段变化。

### Task 4: R5 跨篇计数连续性规则

**Files:** Modify `gacore/audit.py`、`tests/test_audit.py`

- Produces: `_yesterday_claims(cfg, before_date) -> list[Claim]`（读 `{前一日}.v1.json` 的 `report_text`，无则空）；`_audit_claims` 内对 `claim_word in ("连续","持续")` 且 `claimed_days` 非空的断言，同 topic（词表识别）对照昨日计数 M：`N == M+1` 或 `N == 1`（重新起算）为连续，否则 `Finding(rule="R5", severity="warn")`，evidence 写 `昨日 M 天 vs 今日 N 天`。
- topic 任一侧识别不出 → 不判（fail-open）。测试：昨日 v1 写「硬件 连续 2 天」，今日「连续第 4 天」出 R5、「连续第 3 天」不出、「连续第 1 天」不出。

### Task 5: scheduler 审计钩子 + 想问你硬闸

**Files:** Modify `gacore/audit.py`（`cap_questions`）、`gacore/scheduler.py`、`tests/test_audit.py`

- Produces: `cap_questions(reply: str) -> tuple[str, bool]`——`# 想问你` 节 bullet >5 截断（记 bool）；每条须含 `依据` 标记，没有的删；其余正文逐字不动；无该节原样返回。
- `run_job` 投递路径两处：①`stamp_report_bullets` 前对 reply 过 `cap_questions`（截断时 log `report_questions_capped`）；②`save_delivered` 成功后、`_deliver` 前挂 `_audit_delivered(cfg, day)`（`audit_report` + `write_audit` + 计数 log），整体 try/except best-effort（决策 2：只标记，阻断开关不存在）。
- 测试：`cap_questions` 截断/删无依据条/正文不动/无节返回原样 四例；`_audit_delivered` 落 v1。run_job 的接线属下一步夜间真实运行验证（封闭测试不起 LLM 图）。

### Task 6: 三处文档同步 + 门禁

- ROADMAP 追加 `## 2026-10-25 · 日报断言基线闸 P2`；tech 在 §9.34 后补 P2 小节（工具注册 / prompt 契约 / R5 / 钩子位置 / 硬闸）；架构图：TOOLS 区加 `last_seen`、AUD 节点补 R5 与 v1 报告源、SC→AUD 边改「P2 已挂 save_delivered 审计钩子（只标记）」，mermaid 断言行号同步。
- 门禁：`uv run ruff check apps packages tests` + `uv run --all-packages pytest -q --basetemp=…` 全绿后按任务分笔提交并 push。

## 验收（对照 spec §9 P2 部分）

- [ ] `last_seen` 工具可被日报 agent 调用（registry 三处 + docstring 契约）
- [ ] prompt 含查表契约与 `# 想问你` 节，新增 ≤200 字量级
- [ ] 投递后自动落 `{date}.v1.json`（当晚 23:50 真实运行可验）
- [ ] 想问你超 5 条被截断、无依据条被删，其余正文不动
- [ ] R5 warn 生效且 fail-open
- [ ] 生成与投递主逻辑零改动（diff 只在闸与钩子两处）
