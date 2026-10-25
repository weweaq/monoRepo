# 日报断言基线闸（P1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 离线审计已投递日报里的时间量级断言，用记忆库反查基线，输出带分数的审计 JSON 与历史基线报告；不改生成与投递逻辑。

**Architecture:** 新增纯函数模块 `gacore/audit.py`，不依赖 LLM、不依赖网络；所有输入通过 `Config`（`memory_dir` / `logs_dir`）注入，测试用 `Config.for_tests(tmp_path)`。断言抽取用“遮罩引文 + 距离约束”的正则，基线查询用显式主题词表扫记忆库。结果按版本落盘，v1 冻结。

**Tech Stack:** Python 3.12、pytest、现有 `gacore.config.Config`、`gacore.jsonl_logger`；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-24-daily-report-eval-design.md`

## Global Constraints

- 不改 `scheduler.py` 的生成/投递路径；P1 只做离线模块与 CLI。
- 不新增第三方依赖；只用标准库。
- 禁止用 `print` 做日志（其余日志一律 `gacore.jsonl_logger.get_logger("audit")`）；CLI 的结果输出（`python -m gacore.audit` 的 stdout）除外，且只输出结果本身。
- 测试一律 `Config.for_tests(tmp_path)`，禁止读真实 `memory/`、`logs/`、网络或 LLM。
- 所有公开函数带类型标注；行宽 ≤120。
- 中文只出现在 `.md` 与 Python 字符串/注释里；`.ps1/.bat` 纯 ASCII。
- 提交信息中文，格式 `type(scope): 描述`。

## Review Focus

1. **引文/标题里的量词**——`《…40 年》`、`23:50`、`第 3 期`、`“…”` 内的数字不得算作报告自己的断言（现有探针在这里误报过）。
2. **旧格式日报**——08 月的日报没有 `# 节` 标题，bullet 无锚点；审计要能跑，`section=""`、`anchor=None`，不得崩。
3. **同日多次审计**——v1 是首次投递快照，第二次运行必须写 v2 且不覆盖 v1。
4. **记忆库重复行**——同一主题在同一天出现在多个文件，`last_seen` 仍返回该日期，不得因重复而偏移。
5. **报告日期早于全部记忆**——`gap_days=None`，不得据此判冲突。

---

### Task 1: 持续时间解析与断言抽取

**Files:**
- Create: `apps/withlanggraph/src/gacore/audit.py`
- Create: `apps/withlanggraph/tests/test_audit.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class Claim`，字段：`section: str`、`anchor: str | None`、`text: str`、`claim_word: str`、`duration_text: str`、`claimed_days: int | None`、`anchored: bool`
  - `parse_duration_days(text: str) -> int | None`
  - `extract_claims(report_text: str) -> list[Claim]`

- [ ] **Step 1: 写失败测试**

```python
# apps/withlanggraph/tests/test_audit.py
import json

from gacore.audit import extract_claims, parse_duration_days
from gacore.config import Config

def test_parse_duration_days_basic():
    assert parse_duration_days("半年") == 180
    assert parse_duration_days("两个月") == 60
    assert parse_duration_days("三周") == 21
    assert parse_duration_days("4 日") == 4
    assert parse_duration_days("一年") == 365
    assert parse_duration_days("几周") is None

def test_extract_claims_ignores_quoted_and_clock_text():
    report = (
        "# 工作日志\n"
        "- [工作日志-1] 11:56 城事堂养老口号 40 年，话题首次探到社会民生\n"
        "- [工作日志-2] **十小时朱元璋创业史** 第 3 期收官\n"
        "- [工作日志-3] 《入手二十年前的索尼电视》看完\n"
    )
    claims = extract_claims(report)
    assert claims == []

def test_extract_claims_marks_anchor_and_bare():
    report = (
        "# 关键变化\n"
        "- [关键变化-1] 硬件线半年沉寂后重新点火\n"
        "# 明日计划\n"
        "- [明日计划-1] 代码连续第 3 日无提交（10-09 至 10-11）\n"
    )
    claims = extract_claims(report)
    assert len(claims) == 2
    assert claims[0].claimed_days == 180
    assert claims[0].claim_word == "沉寂"
    assert claims[0].anchored is False
    assert claims[1].anchored is True

def test_extract_claims_old_format_without_headings():
    report = "- 工学椅连续 2 天密集调研，仍未定型号\n"
    claims = extract_claims(report)
    assert len(claims) == 1
    assert claims[0].section == ""
    assert claims[0].anchor is None
    assert claims[0].anchored is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'gacore.audit'`

- [ ] **Step 3: 实现**

在 `audit.py` 定义模块级常量与两个函数：

- `_NUM`：`{"半": 0.5, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}` + 阿拉伯数字。
- `_UNIT_DAYS`：`{"天": 1, "日": 1, "周": 7, "星期": 7, "个月": 30, "月": 30, "年": 365}`。
- `parse_duration_days` 的 `半` 特殊处理：`半 + 年 = 180`，`半 + 个月/月 = 15`，其余按 `num × unit` 取整。
- `_DUR_RE = re.compile(r"(半|一|两|二|三|四|五|六|七|八|九|十|十一|十二|几|\d{1,3})\s*(个月|星期|年|月|周|天|日)")`
- `_MASK`：`《…》`、`「…」`、`“…”`、`` `…` ``、`**…**`、`\b\d{1,2}:\d{2}\b`、`第\s*\d+\s*[期集话章]`，逐个 `sub(" ")`。
- `_CLAIM_WORDS = ("沉寂","没再","没有再","未再","久未","许久","时隔","隔了","首次","第一次","重新","再次","连续","持续","半年","数月","多年","久违")`
- `_ANCHOR_RE = re.compile(r"^-\s*\[([^\]]+?)-(\d+)\]\s*")`
- `_INLINE_ANCHOR_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|上次|自\s*\d|第\s*[一二三四五六七八九十\d]+\s*天|至今|此前|前一日|前夜|昨日|去年|上周)")`

`extract_claims` 逻辑：按行扫 `# ` 维护 section；对 `- ` 开头行取锚点；遮罩后用 `_DUR_RE` 逐个匹配，取与 `_CLAIM_WORDS` 最近距离 ≤12 的匹配作为断言；`anchored = bool(_INLINE_ANCHOR_RE.search(masked))`。`Claim.text` 存**原文**行。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add apps/withlanggraph/src/gacore/audit.py apps/withlanggraph/tests/test_audit.py
git commit -m "feat(withlanggraph): 日报时间断言抽取与持续时长解析"
```

---

### Task 2: 主题识别与 `last_seen` 基线查询

**Files:**
- Modify: `apps/withlanggraph/src/gacore/audit.py`
- Modify: `apps/withlanggraph/tests/test_audit.py`

**Interfaces:**
- Consumes: `Config`（`config.py:98`，字段 `memory_dir`、`logs_dir`）
- Produces:
  - `TOPIC_SYNONYMS: dict[str, tuple[str, ...]]`
  - `find_topic(text: str) -> str | None`
  - `@dataclass(frozen=True, slots=True) class LastSeen`，字段：`found: bool`、`date: str | None`、`gap_days: int | None`、`source: str`、`snippet: str`
  - `last_seen(cfg: Config, topic: str, before_date: str) -> LastSeen`

- [ ] **Step 1: 写失败测试**

```python
def test_find_topic_maps_synonyms():
    assert find_topic("硬件线半年沉寂后重新点火") == "硬件"
    assert find_topic("工学椅调研时隔两个月重启") == "工学椅"
    assert find_topic("今天吃了碗面") is None

def test_last_seen_picks_latest_before_date(tmp_path):
    cfg = Config.for_tests(tmp_path)
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-03.md").write_text("晚上追番刷硬件，拆了台旧电视", encoding="utf-8")
    (daily / "2026-09-20.md").write_text("复古硬件首次入镜", encoding="utf-8")
    got = last_seen(cfg, "硬件", "2026-10-20")
    assert got.found is True
    assert got.date == "2026-10-03"
    assert got.gap_days == 17
    assert got.source.startswith("memory/daily/")

def test_last_seen_returns_not_found_when_older_than_corpus(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-01.md").write_text("硬件", encoding="utf-8")
    got = last_seen(cfg, "硬件", "2026-09-30")
    assert got.found is False
    assert got.gap_days is None

def test_last_seen_handles_duplicate_rows(tmp_path):
    cfg = Config.for_tests(tmp_path)
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-03.md").write_text("硬件 A", encoding="utf-8")
    (cfg.memory_dir / "global_mem_insight.txt").write_text(
        "[2026-10-03] insight: 硬件 B\n", encoding="utf-8"
    )
    assert last_seen(cfg, "硬件", "2026-10-20").date == "2026-10-03"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: FAIL，`ImportError: cannot import name 'find_topic'`

- [ ] **Step 3: 实现**

`TOPIC_SYNONYMS` 初值（后续可加，不改接口）：

```python
TOPIC_SYNONYMS = {
    "硬件": ("硬件", "ESP32", "STM32", "单片机", "嵌入式", "开发板"),
    "工学椅": ("工学椅", "人体工学椅", "西昊", "永艺", "黑白调"),
    "显示器": ("显示器", "MateView", "屏"),
    "桌面升级": ("桌面升级", "扩展坞", "升降桌"),
}
```

`last_seen` 扫三类来源：`cfg.memory_dir/daily/*.md`（日期取文件名前 10 位）、`global_mem_insight.txt`、`global_mem_facts.txt`（日期取行首 `[YYYY-MM-DD`）。命中任一 synonym 即算命中；只保留 `date < before_date` 的最大日期。`snippet` 取命中位置前后各 40 字，`source` 为相对 `C:\...\apps\withlanggraph\` 的 `memory/...` 路径，全局文件带行号（`memory/global_mem_insight.txt:104`）。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add apps/withlanggraph/src/gacore/audit.py apps/withlanggraph/tests/test_audit.py
git commit -m "feat(withlanggraph): 主题识别与 last_seen 基线查询"
```

---

### Task 3: 冲突判定与单篇审计

**Files:**
- Modify: `apps/withlanggraph/src/gacore/audit.py`
- Modify: `apps/withlanggraph/tests/test_audit.py`

**Interfaces:**
- Consumes: `Claim`、`find_topic`、`last_seen`
- Produces:
  - `is_conflict(claimed_days: int, actual_gap_days: int) -> bool`
  - `@dataclass(frozen=True, slots=True) class Finding`，字段：`rule: str`、`severity: str`、`section: str`、`anchor: str | None`、`claim: str`、`claimed_days: int | None`、`actual_last_seen: str | None`、`actual_gap_days: int | None`、`evidence: str`
  - `@dataclass(frozen=True, slots=True) class AuditResult`，字段：`date: str`、`version: int`、`score: int`、`missing: bool`、`findings: tuple[Finding, ...]`
  - `audit_report(cfg: Config, date: str) -> AuditResult`

- [ ] **Step 1: 写失败测试**

```python
def test_is_conflict_tolerance_band():
    assert is_conflict(180, 17) is True
    assert is_conflict(60, 27) is True
    assert is_conflict(14, 13) is False
    assert is_conflict(20, 19) is False

def test_audit_report_flags_hard_conflict(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-03.md").write_text("硬件信号", encoding="utf-8")
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    (reports / "2026-10-20.md").write_text(
        "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n", encoding="utf-8"
    )
    got = audit_report(cfg, "2026-10-20")
    assert got.score == 75
    assert len(got.findings) == 1
    assert got.findings[0].rule == "R2"
    assert got.findings[0].severity == "error"
    assert got.findings[0].actual_last_seen == "2026-10-03"

def test_audit_report_missing_file(tmp_path):
    cfg = Config.for_tests(tmp_path)
    got = audit_report(cfg, "2026-01-01")
    assert got.missing is True
    assert got.findings == ()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: FAIL，`ImportError: cannot import name 'is_conflict'`

- [ ] **Step 3: 实现**

- `is_conflict`：`claimed_days >= 2 * actual_gap_days and claimed_days - actual_gap_days >= 14`。
- `audit_report` 读 `cfg.logs_dir / "delivered_report" / f"{date}.md"`；不存在返回 `AuditResult(date, 0, 100, True, ())`。
- 规则：
  - **R2**（error）：`find_topic(claim.text)` 命中且 `last_seen.found` 且 `is_conflict` → `Finding(rule="R2", ...)`。
  - **R1**（warn）：未被 R2 覆盖且 `claim.anchored is False` → `Finding(rule="R1", ...)`。
  - **R4**（warn）：`claim.claim_word in {"首次","第一次","久违"}` 且 `last_seen.found is False` → `Finding(rule="R4", ...)`。
- 分数：`max(0, 100 - 25 * errors - 5 * warns)`。
- 把规则循环抽成 `_audit_claims(cfg: Config, claims: list[Claim], before_date: str) -> tuple[Finding, ...]`，`audit_report` 只负责读文件并调用它——P4 的周报审计直接复用，不复制规则。
- `AuditResult.date` 对周报存 `2026-W43` 这种值，字段名不改，P4 复用同一个落盘函数。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add apps/withlanggraph/src/gacore/audit.py apps/withlanggraph/tests/test_audit.py
git commit -m "feat(withlanggraph): 断言冲突判定与单篇审计打分"
```

---

### Task 4: 版本化落盘与 backfill CLI

**Files:**
- Modify: `apps/withlanggraph/src/gacore/audit.py`
- Modify: `apps/withlanggraph/tests/test_audit.py`

**Interfaces:**
- Produces:
  - `write_audit(cfg: Config, result: AuditResult) -> Path`
  - `backfill(cfg: Config) -> dict[str, int]`
  - `python -m gacore.audit --backfill`

- [ ] **Step 1: 写失败测试**

```python
def test_write_audit_freezes_v1(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.logs_dir / "delivered_report").mkdir(parents=True)
    (cfg.logs_dir / "delivered_report" / "2026-10-20.md").write_text(
        "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n", encoding="utf-8"
    )
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-03.md").write_text("硬件", encoding="utf-8")
    p1 = write_audit(cfg, audit_report(cfg, "2026-10-20"))
    p2 = write_audit(cfg, audit_report(cfg, "2026-10-20"))
    assert p1.name == "2026-10-20.v1.json"
    assert p2.name == "2026-10-20.v2.json"
    v1 = json.loads(p1.read_text(encoding="utf-8"))
    assert v1["score"] == 75
    # 决策 23：v1 冻结投递原文，last_seen 读历史日报一律走 v1 口径
    assert v1["report_text"] == "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n"

def test_backfill_skips_existing_v1(tmp_path):
    cfg = Config.for_tests(tmp_path)
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    (reports / "2026-10-19.md").write_text("# 今日状态\n- [今日状态-1] 一切正常\n", encoding="utf-8")
    (reports / "2026-10-20.md").write_text("# 关键变化\n- [关键变化-1] 硬件线半年沉寂\n", encoding="utf-8")
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-03.md").write_text("硬件", encoding="utf-8")
    summary = backfill(cfg)
    assert summary["reports"] == 2
    assert summary["conflicts"] == 1
    assert (cfg.logs_dir / "report_audit" / "2026-10-20.v1.json").is_file()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: FAIL，`ImportError: cannot import name 'write_audit'`

- [ ] **Step 3: 实现**

- `write_audit`：目录 `cfg.logs_dir / "report_audit"`；找同日期现有 `*.v*.json`，新版本号 = 最大 + 1（无则 1）；写 `dataclasses.asdict` + `ensure_ascii=False, indent=2`；`AuditResult` 增加 `report_text: str` 字段（投递原文快照，决策 23：`last_seen` 读历史日报一律走 v1 的 `report_text`，不读被订正重写的当前文件）。
- `backfill`：遍历 `delivered_report/*.md` 按文件名排序；若 `{date}.v1.json` 已存在则跳过；否则 `audit_report` + `write_audit`；汇总 `{"reports", "claims", "bare", "conflicts"}`。
- CLI：`argparse`，`--backfill` 时 `load_dotenv(); cfg = Config.default(); summary = backfill(cfg)`，用 `json.dumps(..., ensure_ascii=False)` 打印到 stdout（这是 CLI 输出，不是日志，允许 `print`）。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: 13 passed

- [ ] **Step 5: 真实语料试跑**

Run: `cd apps/withlanggraph; $env:PYTHONPATH="src"; uv run python -X utf8 -m gacore.audit --backfill`
Expected: stdout 一行 JSON，`reports == 58`、`conflicts == 2`

- [ ] **Step 6: 提交**

```bash
git add apps/withlanggraph/src/gacore/audit.py apps/withlanggraph/tests/test_audit.py
git commit -m "feat(withlanggraph): 审计结果版本化落盘与历史 backfill"
```

---

### Task 5: 基线报告与文档同步

**Files:**
- Modify: `apps/withlanggraph/src/gacore/audit.py`（`baseline.md` 生成）
- Modify: `apps/withlanggraph/docs/ROADMAP.md`
- Modify: `apps/withlanggraph/docs/langTrack-tech.md`
- Modify: `apps/withlanggraph/docs/architecture-flow.mmd`（先用 codemap 走查，禁止手改）

**Interfaces:**
- Consumes: `backfill(cfg)`
- Produces: `render_baseline(cfg: Config) -> str`，写入 `logs/report_audit/baseline.md`

- [ ] **Step 1: 写失败测试**

```python
def test_render_baseline_lists_conflicts(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.logs_dir / "report_audit").mkdir(parents=True)
    (cfg.logs_dir / "report_audit" / "2026-10-20.v1.json").write_text(
        json.dumps({
            "date": "2026-10-20", "version": 1, "score": 75, "missing": False,
            "findings": [{
                "rule": "R2", "severity": "error", "section": "关键变化",
                "anchor": "关键变化-1", "claim": "硬件线半年沉寂",
                "claimed_days": 180, "actual_last_seen": "2026-10-03",
                "actual_gap_days": 17, "evidence": "memory/daily/2026-10-03.md",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    text = render_baseline(cfg)
    assert "2026-10-20" in text
    assert "硬件线半年沉寂" in text
    assert "R2" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: FAIL，`ImportError: cannot import name 'render_baseline'`

- [ ] **Step 3: 实现**

`render_baseline` 读 `report_audit/*.v1.json`，输出 Markdown：总览表（篇数 / 断言 / 无锚 / 冲突 / 均分）、硬冲突清单（按分数升序）、无锚清单（最多 50 条）。在 `--backfill` 结束时一并写入 `logs/report_audit/baseline.md`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: 14 passed

- [ ] **Step 5: 真实数据生成基线并核对**

Run: `cd apps/withlanggraph; $env:PYTHONPATH="src"; uv run python -X utf8 -m gacore.audit --backfill`
Expected: `logs/report_audit/baseline.md` 中能找到 10-20「半年」与 10-12「两个月」两条 R2。

- [ ] **Step 6: 同步三处文档**

- `docs/ROADMAP.md`：追加 `## 2026-10-24 · 日报断言基线闸 P1`，写清背景、实际数字、偏差。
- `docs/langTrack-tech.md`：新增 `## 日报断言基线闸` 小节，写接口签名、文件位置、v1 冻结语义，内嵌 `audit → last_seen → memory` 数据流图。
- `docs/architecture-flow.mmd`：用 codemap 走查后加 `audit` 节点，禁止手改。

- [ ] **Step 7: 质量门禁**

Run: `uv run ruff check apps packages tests; uv run pytest apps/withlanggraph/tests/test_audit.py -q`
Expected: ruff 无错误；14 passed

- [ ] **Step 8: 提交**

```bash
git add apps/withlanggraph/src/gacore/audit.py apps/withlanggraph/tests/test_audit.py apps/withlanggraph/docs/ROADMAP.md apps/withlanggraph/docs/langTrack-tech.md apps/withlanggraph/docs/architecture-flow.mmd
git commit -m "docs(withlanggraph): 日报断言基线闸 P1 基线报告与三处文档同步"
```

---

## 后续（不在本计划内）

- **P2**：`schedule.json` 加 `last_seen` 契约与 `# 想问你` 节；`scheduler.py` 在 `save_delivered` 后挂审计钩子（默认只标记）。
- **P3**：`proactive` job 推 QQ + `proactive_qa` 处理器 + `data/qa/` 落库 + `_QA` 信息源。
- **P4**：周报生成（程序填数字 + 模型写叙事）+ 评分两问 + 周趋势。周报也要过 `audit_report`。

每期单独写计划，不要合并。
