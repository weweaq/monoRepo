# langTrack 结构化知识图谱（KG）抽取层 · 落地实现计划

> 状态：S1（骨架 + events 通道）、S2（relations/todos 通道 + 实体消解 + 区间闭合）、S3（emotions/patterns/worldview 候选通道）已落地（2026-10-18）；S4~S5 未实施 · 编写日期：2026-10-17
> 目标：把现有"笼统"的日报，升级为**按日结构化抽取 + 可溯源**的素材，且**不破坏现有日报功能**
> 依据：外部设计文档《日报知识库系统 · 实现技术文档 v2.0》+ 对 mono 现有代码的实测核实
> 上游设计缺口已在本方案中修正（见 §6 两个设计债）

---

## §1 目标与边界

### 1.1 要解决的病因（已定位）

现有日报"笼统"，根因是**信息在到达 LLM 前被压缩掉了**，三处机制：

1. `daily_info_pack.py` 的 `SOURCES` 给每个源设 `cap`，只有 top-N 字符进包（实测 10-16：`_LONG_TERM` 全量 28091 字符，进包仅 2946）；
2. 12 个 builder 产出的是**渲染后的散文块**，不是结构化条目；
3. LLM 拿到的是"摘要的摘要"，只能写笼统。

### 1.2 本方案做什么

在**现有取数层之上**加一层"结构化抽取"，把当日全量素材抽成 人物/关系/事件/情绪/待办/模式 等结构化数据，落进 langTrack.db，再作为**一个新的信息源**接进日报。

### 1.3 本方案不做什么（明确边界）

- ❌ 不新起 `journal/` 独立仓库（落进 mono，遵守 R1-R12）
- ❌ 不重写 `langTrack/report.py`（实测：它**不在日报链路上**，是独立 CLI，见 §2.3）
- ❌ 不改 `etl.py` 的 `run()` 流水线顺序（保持 ETL 纯本地、确定性、可重建）
- ❌ 不删/改任何现有信息源，不动现有 builder 的产出契约

---

## §2 现状事实（已核实，带文件:行号）

### 2.1 数据源与 ETL

| 事实 | 位置 |
|---|---|
| `events` 表（13.7 万条）字段：`id, device_id, ts, type, payload, received_at, created_at, updated_at` | `langTrack/storage.py` L8-L34 |
| ETL 是**硬编码流水线**，无 STAGES 注册表：`load_events → build_sessions → build_daily_stats → build_stays → _build_location_v1 → detect_anomalies → detect_route_changes → build_contract_coverage` | `langTrack/etl.py` `run()` |
| DDL 集中在 `etl.py` 的 `_SCHEMA`（L185-L551） | 同上 |
| 迁移模板：`PRAGMA table_info` 查列 → `ALTER TABLE ADD COLUMN`（在 `with conn:` 内） | `_migrate_daily_stats_pk()` L1975 |
| 增量水位：`etl_state(device_id, last_event_ts, ...)`，`_read_watermarks()` L2136、`_compute_affected_days(lookback_days=3)` L2142 | `etl.py` |
| 运行血缘：`etl_runs` 每跑一次记一条 | `etl.py` L516-L531 |
| **langTrack 全目录零 LLM 调用**（纯 SQLite 管道） | 实测确认 |
| `PRAGMA user_version = 2`，由 `activate_location_v2()` 设置 | `location_migration.py` |

### 2.2 日报链路

| 事实 | 位置 |
|---|---|
| 定时任务注册在 `config/schedule.json` 的 `jobs` 数组（`daily-report` = `23:50`）——**新增任务=改 JSON，零代码** | `config/schedule.json` |
| `run_job(job, cfg, graph_runner=None, for_day=None, deliver=True) -> ScheduleResult` | `scheduler.py` L422-L553 |
| `_build_job_prompt(job, cfg, for_day)`：L371 `info_pack = build_info_pack(today, cfg)`，L374 拼进 prompt | `scheduler.py` L318-L419 |
| `SourceSpec(key, cap, priority, builder)`；builder 签名 `(date, cfg) -> (title, pack_body, detail_body)` | `daily_info_pack.py` L998-L1006 |
| `SOURCES` 共 12 条，priority 升序（10…100） | `daily_info_pack.py` L1008-L1023 |
| `build_info_pack_report(date, cfg) -> (pack, stats)`；`stats[i]` 含 `key/status/chars/full_chars/detail_chars/note/pack_body/detail_body` | L1036-L1129 |
| `build_info_pack(date, cfg) -> str` = 薄包装（scheduler 测试 monkeypatch 此名） | L1132-L1135 |
| 装配按 priority 丢块：预算紧张时**最低优先级先丢** | `_assemble_blocks(blocks, budget)` L959 |
| 逐源体检自动落盘（新源自动纳入，无需改 scheduler） | `write_pack_health` L1141、`write_pack_detail` L1197，由 `scheduler.py` L389-L403 调 |
| 缺失态判定：`_MISSING_DATA_RE = 该日无\|今日无\|当日无\|未登录\|不可用\|…\|无.*数据`，行首 8 字符守卫 | `daily_info_pack.py` L876-L904 |

### 2.3 `report.py` 的真实地位（重要纠偏）

`langTrack/report.py` 的 `report(conn, day, ...) -> None` 是**独立 CLI**（`python -m gacore.langTrack.report`），产出 console 文本 + `data/profiles/langTrack_profile_{day}.json` 快照。
**日报不调用它**——日报的 langTrack 内容来自 `daily_info_pack` 的 `_build_phone_place/_build_phone_usage/_build_phone_notif`，它们内部调 `fact_card.build(day, detail="compact", outlet="info_pack")`（`daily_info_pack.py` L616）。
结论：优化日报**不需要碰 `report.py`**。

### 2.4 补跑链路（`for_day` 贯穿，已修复）

`rerun.py:66` → `run_job(for_day=args.day)` → `_build_job_prompt` L343 `today = for_day or now` → L371 `build_info_pack(today)` → L458 `_default_graph_runner` → L573 `run_once(target_day=for_day)` → `graph.py:340` → `context.py` L246-L249 `fact_card.build(day=state["target_day"])`。
`GAState.target_day`（`state.py` L48）；`new_state` L55-L72。
→ 补跑历史日时，KG 抽取也必须**按 `day` 取素材**，不能取"今天"。

### 2.5 预算余量（实测）

`config/info_pack.json`：`pack_budget = 10000`。
10-16 体检：`total_chars = 4670 / budget = 10000` → **余量 5330 字符**。
→ 新增一个 600 字符级源安全，且因其优先级最低，预算收紧时先被丢弃，**不会挤压现有 12 源**。

### 2.6 文档与测试约定

| 项 | 路径 |
|---|---|
| tech 文档 | `apps/withlanggraph/docs/langTrack-tech.md` |
| 路书 | `apps/withlanggraph/docs/langTrack-roadmap.md`（格式：`## YYYY-MM-DD：标题` + `### 已完成 / ### 实测数据 / ### 问题诊断`）|
| 架构图 | `apps/withlanggraph/docs/architecture-flow.mmd`（codemap 生成，节点=`真实符号名 <br/> 文件路径`）|
| 测试 | `apps/withlanggraph/tests/`，60 文件，`conftest.py` 提供 `tmp_cfg / scripted_llm / tool_list / run_graph` |
| **测试封闭性铁律** | 不得触碰真实 LLM API、外部网络、真实 DB、embedding 加载 |
| uv 包名 | `pyproject.toml` `[project].name = "gacore"` → `uv run --package gacore`（部分旧测试 docstring 写作 `withlanggraph`，以 pyproject 为准）|
| LLM 接口 | `get_llm(tool_list, env=None, *, bind_tools=True) -> BaseChatModel`；`get_provider(env) -> str`（openai/anthropic/deepseek/zhipu）；**无 JSON-mode 封装** |
| LLM 日志 | `log_llm_request(provider, model, run_kind, messages, tools, params, response, duration_ms)` → `logs/<date>/llm_requests.jsonl` |

---

## §3 总体架构

```
【现有·不动】
12 个 SOURCES builder  ──┬──► pack_body（受 cap 截断）──► LLM 写日报
                          └──► detail_body（全量）──────► data/logs/pack_detail/{date}/{key}.md
                                                        │
【新增·本方案】                                          ▼
                                    material.py 组装"当日全量素材" + 算 raw_hash
                                                        │
                            六通道 LLM 抽取（schema 约束、并发、失败隔离）
                              events / relations / todos / emotions / patterns / worldview
                                                        │
                                    merge + resolve + interval_closure
                                                        ▼
                                    langTrack.db 新增 kg_* 表（R6 合规）
                                                        │
【新增·接日报】                                          ▼
                          _build_kg（新 SourceSpec `_KG`，priority 105=最低，cap 600）
                                                        │
                                              pack_body ──► 日报 LLM（背景素材）
                                              detail_body ──► health 归因
```

### 3.1 三个关键决策

| 决策 | 内容 | 理由 |
|---|---|---|
| **D1 抽取输入复用现有取数** | 素材 = `build_info_pack_report(date,cfg)[1]` 各源 `detail_body` 拼装（或读已落盘的 `pack_detail/{date}/*.md`）| 零新增采集，复用 12 源全部取数逻辑；`detail_body` 本就是"当日取数全部结果" |
| **D2 抽取不挂进 `etl.py`** | 独立 CLI `python -m gacore.langTrack.extract`，自己建表 | 保住 ETL"纯本地确定性可重建"的属性；避免把网络/LLM 引入 ETL；便于独立测试 |
| **D3 存储只用 SQLite（langTrack.db）** | 新表 `kg_*`，不引入 JSONL 作为第二真源；提供 `--dump` 导出 JSONL 供归档/对比 | R6 要求每表 `created_at/updated_at`；单一真源避免双份失真（同 R5 精神）|

### 3.2 素材路由表（每通道喂哪些源的 detail）

| 通道 | 素材来源（源 key）| 理由 |
|---|---|---|
| `events` | `_CHAT, _MEDIA, _BILI, _PHONE_NOTIF, _PHONE_PLACE, _EDGE, _FILES, _GIT, _LONG_TERM` | 事件散布在所有素材里 |
| `relations` | `_CHAT, _PHONE_NOTIF, _LONG_TERM` | 关系证据主要来自对话与人际上下文 |
| `todos` | `_CHAT, _GIT, _FILES, _LONG_TERM` | 待办/承诺来自对话与工作痕迹 |
| `emotions` | `_CHAT, _PHONE_NOTIF, _MEDIA` | 情绪信号来自表达与消费内容 |
| `patterns` | `_LONG_TERM, _CHAT, _GIT, _FILES` | 长期倾向需要长期画像支撑 |
| `worldview` | `_LONG_TERM, _CHAT` | 世界观察来自长期画像与对话 |

> 目的：避免"全部素材 × 6 通道"的浪费（实测 10-16 全量 detail ≈ 29803 字符，×6 = 179k 字符/天）。

---

## §4 新增数据表 DDL（R6 合规）

新建文件 `src/gacore/langTrack/extract/schema.py`，内含 DDL 常量 + `migrate_extract_tables(conn)`。
**全部 `CREATE TABLE IF NOT EXISTS`，只增不改，不动 `etl.py` 的 `_SCHEMA`、不加入 `_migrate_fact_tables` 的 `fact_tables` 列表**（避免与位置 v1/v2 冻结逻辑耦合）。

```sql
-- 一次抽取运行的血缘与幂等锚
CREATE TABLE IF NOT EXISTS kg_runs (
  run_id        TEXT PRIMARY KEY,             -- YYYYMMDDTHHMMSS-<8hex>
  day           TEXT NOT NULL,                -- 目标日 YYYY-MM-DD
  raw_hash      TEXT NOT NULL,                -- 当日全量素材 sha256
  prompt_ver    TEXT NOT NULL,                -- 如 kg@v1
  status        TEXT NOT NULL,                -- running|ok|partial|failed
  channel_stats TEXT NOT NULL DEFAULT '{}',   -- JSON: {channel:{rows,status,error}}
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  created_at    TEXT DEFAULT (datetime('now','+8 hours')),
  updated_at    TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_kg_runs_day_hash_ver ON kg_runs(day, raw_hash, prompt_ver);

-- 实体节点
CREATE TABLE IF NOT EXISTS kg_nodes (
  node_id    TEXT PRIMARY KEY,                -- person_zhangsan / event_20261016_001
  node_type  TEXT NOT NULL,                   -- Person|Event|Project|Theme|Observation|Axiom
  label      TEXT NOT NULL,
  canonical  TEXT,
  aliases    TEXT NOT NULL DEFAULT '[]',      -- JSON array
  first_day  TEXT NOT NULL,
  last_day   TEXT NOT NULL,
  src        TEXT NOT NULL,
  confidence REAL,
  run_id     TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_type  ON kg_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_label ON kg_nodes(label);

-- 边（append-only；edge_id 由内容确定性派生 → 重跑天然幂等）
CREATE TABLE IF NOT EXISTS kg_edges (
  edge_id        TEXT PRIMARY KEY,            -- sha1(day|from|to|relation|edge_level|assertion_text)[:16]
  from_node      TEXT NOT NULL,
  to_node        TEXT NOT NULL,
  relation       TEXT NOT NULL,
  edge_level     TEXT NOT NULL,               -- L1 事实 | L2 观点 | L3 推断
  attributor     TEXT,                        -- L2 必填
  assertion_text TEXT,
  event_time     TEXT,
  ingest_time    TEXT,
  valid_from     TEXT,
  valid_to       TEXT,
  supersedes     TEXT,
  superseded_at  TEXT,
  invalidated    INTEGER NOT NULL DEFAULT 0,
  confidence     REAL,
  day            TEXT NOT NULL,
  src            TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  created_at     TEXT DEFAULT (datetime('now','+8 hours')),
  updated_at     TEXT DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_kg_edges_from ON kg_edges(from_node, relation, valid_from);
CREATE INDEX IF NOT EXISTS idx_kg_edges_to   ON kg_edges(to_node,   relation, valid_from);
CREATE INDEX IF NOT EXISTS idx_kg_edges_day  ON kg_edges(day);

-- 别名（追加式，可撤销 —— 修正设计债②）
CREATE TABLE IF NOT EXISTS kg_aliases (
  alias         TEXT NOT NULL,
  canonical     TEXT NOT NULL,
  method        TEXT NOT NULL,                -- exact|fuzzy|manual
  score         REAL,
  day           TEXT NOT NULL,
  src           TEXT,
  superseded_by TEXT,                         -- 指向撤销/改写它的新记录（NULL=生效）
  run_id        TEXT NOT NULL,
  created_at    TEXT DEFAULT (datetime('now','+8 hours')),
  updated_at    TEXT DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY (alias, day, run_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_aliases_alias ON kg_aliases(alias);
```

后续阶段追加（同文件，同风格）：`kg_relations`、`kg_emotions`、`kg_patterns`、`kg_worldview`。

---

## §5 分阶段实现计划

> 每阶段独立可验收、可回滚。约 **7 个工作日**（少于设计文档的 10-11 天，因复用现有取数与落盘）。

### S0 前置（0.5 天，无代码）

- [ ] 备份 `apps/withlanggraph/data/langTrack.db` 到 `data/backup/langTrack_before_kg_<ts>.db`
- [ ] 确认预算余量（已测 4670/10000，余量 5330）
- [ ] 确认两个设计债的解法（见 §6）——本方案已定，无需再议
- [ ] 提交：无（仅本地备份，`data/` 已 gitignore）

### S1 抽取子系统骨架 + events 通道（1.5 天）

**新增文件**
```
src/gacore/langTrack/extract/__init__.py
src/gacore/langTrack/extract/__main__.py       # CLI: --day / --force / --only / --dry-run / --dump
src/gacore/langTrack/extract/schema.py         # §4 DDL + migrate_extract_tables(conn)
src/gacore/langTrack/extract/material.py       # load_material(date, cfg) -> (text, raw_hash)
src/gacore/langTrack/extract/runner.py         # run_extract(day, cfg, force=False) -> RunResult
src/gacore/langTrack/extract/channels/__init__.py
src/gacore/langTrack/extract/channels/events.py
config/kg_prompts/events@v1.md                 # prompt 外置，改 prompt 不动代码
```

**关键实现要点**
- `migrate_extract_tables(conn)`：照 `_migrate_daily_stats_pk` 风格，`PRAGMA table_info` 检查 + `CREATE TABLE IF NOT EXISTS`，在 `with conn:` 内执行；由 extract CLI 启动时调用（**etl.py 完全不改**）
- `load_material`：优先读 `data/logs/pack_detail/{date}/*.md`；缺失则调 `build_info_pack_report(date, cfg)` 拼 `stats[*]["detail_body"]`；返回 `(text, sha256(text))`
- `runner.run_extract`：
  1. 建表 → 2. 取素材算 `raw_hash` → 3. 查 `kg_runs(day, raw_hash, prompt_ver)` 命中且 `status='ok'` 则**跳过**（幂等）→ 4. 按 §3.2 路由分发通道 → 5. `asyncio.gather(..., return_exceptions=True)` 失败隔离 → 6. 逐通道解析写表（单事务）→ 7. 写 `kg_runs.channel_stats`
- LLM 调用：`get_llm([], cfg.env, bind_tools=False)`，`run_kind="kg_extract"`；prompt 强制 JSON，解析失败重试 1 次
- `edge_id` 用 `hashlib.sha1(f"{day}|{from}|{to}|{relation}|{edge_level}|{assertion_text}".encode()).hexdigest()[:16]` → 重跑同素材结果一致，PK 冲突即跳过

**新增测试**（封闭：`tmp_cfg` + `scripted_llm`，不触网不触真实库）
```
tests/test_langTrack_extract_schema.py    # 建表/幂等迁移：连跑两次表结构一致
tests/test_langTrack_extract_material.py  # pack_detail 优先 / 回退 report / raw_hash 稳定
tests/test_langTrack_extract_events.py    # 正常 JSON / 非法 JSON 重试 / 部分通道失败隔离 / 同素材重跑不重复写
```

**验证命令**
```powershell
uv run --package gacore ruff check apps/withlanggraph
uv run --package gacore pytest apps/withlanggraph/tests -q
uv run --package gacore python -m gacore.langTrack.extract --day 2026-10-16 --force
# 查库：kg_runs 1 条 ok；kg_edges 行数 > 0；kg_nodes 行数 > 0
```

**提交**：`feat(langTrack): 新增 KG 抽取子系统骨架与 events 通道`
**文档同步**：`langTrack-tech.md` 加"KG 抽取层"节；`architecture-flow.mmd` 加 extract 节点（走 codemap 复审）；`langTrack-roadmap.md` 追加执行记录

### S2 relations + todos 通道 + 实体消解（2 天）

**新增**
```
src/gacore/langTrack/extract/channels/relations.py
src/gacore/langTrack/extract/channels/todos.py
src/gacore/langTrack/extract/resolve.py       # resolve_entities(conn, day, run_id) -> None
src/gacore/langTrack/extract/intervals.py     # reclose_intervals(conn, from_node, to_node, relation)
config/kg_prompts/relations@v1.md
config/kg_prompts/todos@v1.md
```
**表**：`kg_relations`、`kg_aliases`（§4 已给 DDL）

**关键实现要点**
- `resolve_entities` 三级：① 精确匹配 `kg_aliases`（取 `superseded_by IS NULL` 的最新记录）② 模糊聚类 `weighted_score = 0.4*jaro_winkler + 0.3*context_overlap + 0.2*temporal_cooccurrence + 0.1*same_surname`，`≥0.85` 自动合并、`0.6~0.85` 写候选 ③ 人工确认（写 `kg_aliases(method='manual')`）
- **合并结果只 INSERT 到 `kg_aliases`**（追加式），撤销 = 写一条 `superseded_by` 指向的新记录，**绝不 UPDATE/DELETE 旧记录**（修正设计债②）
- `reclose_intervals`：取该 `(from,to,relation)` 全部边**按 `valid_from` 排序**后重算 `valid_to`（旧边 `valid_to = 下一条.valid_from`），**不因乱序而拒绝**（修正设计债①）

**新增测试**
```
tests/test_langTrack_extract_relations.py
tests/test_langTrack_resolve.py            # 精确/模糊/传递性/撤销合并（superseded_by 生效）
tests/test_langTrack_interval_closure.py   # 顺序 5 场景 + 乱序补录场景 + 撤销后重算
```

**提交**：`feat(langTrack): 新增 relations/todos 通道与追加式实体消解`

### S3 emotions + patterns + worldview 通道（1 天）

**新增**：`channels/emotions.py`、`channels/patterns.py`、`channels/worldview.py` + 3 个 prompt 文件
**表**：`kg_emotions`、`kg_patterns`、`kg_worldview`
**要点**：`patterns`/`worldview` 只产出**候选**（confidence 上限 0.7 / 0.6），不宣布规律；候选升格留给后续独立阶段（设计文档此处空白，本方案显式标注为**未完成设计**，不在本阶段实现）
**提交**：`feat(langTrack): 新增 emotions/patterns/worldview 通道`

### S4 接入日报信息包（1 天）

**改动现有文件（最小化，逐条列明）**
1. `src/gacore/daily_info_pack.py`
   - 加常量 `_KG_CAP = 600`
   - 加模块级 seam `_KG_FN`（供测试 monkeypatch，风格同 `_BILLI_FN`）
   - 加 builder `_build_kg(date, cfg) -> tuple[str, str, str]`：
     - 读 `kg_*` 汇总当日图摘要（人物 top-N + 关系变化 + 情绪基调 + 未完成待办）
     - **DB/表缺失时返回 `("- 当日无 KG 抽取结果（抽取未运行）", "", "")`** —— 命中现有 `_MISSING_DATA_RE` 的 `当日无`，**无需改 `classify_body`**
   - `SOURCES` **追加一条**：`SourceSpec("_KG", _KG_CAP, 105, _build_kg)`
     - priority=105：>100（排最后）、且满足 `% 5 == 0`（`test_registry_priority_strictly_increasing_in_pack_order` 的约束）
2. `config/info_pack.json`：加 `"_KG": {"enabled": false, "cap": 600, "priority": 105}`（**默认停用**，灰度时再开）
3. `tests/test_source_registry.py`：**必须**在 `_inject_failure` 的 `else` 前加分支
   ```python
   elif key == "_KG":
       monkeypatch.setattr(dip, "_KG_FN", _raise)
   ```
   （不加则 `test_every_builder_failure_follows_failed_sentinel` 会 `pytest.fail` 提醒补齐——这是设计好的强制同步点）
4. 可选：`tests/test_source_registry.py::test_classify_missing_data_variants` 补一行断言 `"- 当日无 KG 抽取结果（抽取未运行）"`

**新增测试**：`tests/test_info_pack_kg.py`（满数据/空数据/config 停用/cap 生效/失败 sentinel）
**可选新增**：`src/gacore/tools/kg_tools.py`（图查询工具：2 跳查询、关系演化链），供 agent 按需下钻

**验证（证明"不影响现有功能"）**
```powershell
uv run --package gacore pytest apps/withlanggraph/tests/test_source_registry.py apps/withlanggraph/tests/test_daily_info_pack.py apps/withlanggraph/tests/test_info_pack_report.py -q
uv run --package gacore python -m gacore.backfill_health --day 2026-10-16   # refresh_day()
# 对比 data/logs/info_pack_health.jsonl 前后：现有 12 源 chars 必须一字不变，仅多出 _KG
```
**提交**：`feat(langTrack): 将 KG 接入日报信息包（_KG 源，最低优先级，默认停用）`

### S5 调度 + 灰度验收（1.5 天）

- [ ] `config/schedule.json` 新增 job（纯 JSON）：`{"name": "kg-extract", "schedule": "23:30", "enabled": true, ...}`
      —— 排在 `daily-report`（23:50）之前，保证当日图能进当日日报
- [ ] 灰度开启 `_KG`：`config/info_pack.json` 的 `"_KG".enabled = true`
- [ ] 用现有补跑链验证：`python -m gacore.rerun --day 2026-09-14`（北京那几天），人工对比新旧日报
- [ ] 按军规⑥**观测一周** `info_pack_health.jsonl`，确认 `_KG` 状态与字符数稳定
- [ ] 提交：`chore(langTrack): 新增 kg-extract 定时任务并灰度开启 _KG 源` + `docs(langTrack): KG 抽取上线观测记录`

---

## §6 两个设计债的落地解法（对上游文档的修正）

| 编号 | 上游设计的问题 | 本方案解法 |
|---|---|---|
| ① 乱序摄入 | §5.4 场景3"新边 `valid_from` < 旧边 → 报错、拒绝写入"——但手机数据**批量迟到是常态**（实测：9-21 一次补发 21612 条；曾积压 2689 条）| `reclose_intervals` 改为**按 `valid_from` 全量排序后重算 `valid_to`**，接受乱序、不拒绝 |
| ② 误合并不可回滚 | 消解写 `aliases.json` 是**覆盖式**，与"只追加不覆盖"铁律自相矛盾；Union-Find 传递性合并是误差累积器 | `kg_aliases` **追加式**（PK `(alias, day, run_id)` + `superseded_by`），撤销=写新记录，旧记录永不改 |

> 另：上游 §2 Phase1 验收表把"人工判定抽取质量｜主观可接受"混进 100% 客观指标表；本方案 §9 验收标准全部为客观可测项，主观项单列。

---

## §7 不破坏现有功能的硬约束（实施时逐条对照）

1. **只增不改表**：`kg_*` 全走独立 `migrate_extract_tables()`，不碰 `etl.py` 的 `_SCHEMA`、不加入 `_migrate_fact_tables` 的 `fact_tables` 列表
2. **不动 ETL 流水线**：`etl.py` 的 `run()` 顺序、`_stamp_fact_tables` spec、`_read_watermarks` 一律不改
3. **不动现有 builder 与既有 SourceSpec**：只**追加** `_KG` 一条，priority=105 排在最后
4. **不动 `scheduler.run_job` / `_build_job_prompt` 逻辑**：新任务只改 `config/schedule.json`
5. **不动 `langTrack/report.py`**（它不在日报链路上）
6. **默认停用**：`_KG` 初始 `enabled: false`，出问题**改 JSON 即可秒关**，无需回滚代码
7. **不动 `for_day`/`target_day` 贯穿链**：抽取按 `day` 取素材，天然对齐补跑语义

**唯一必然改动的现有测试**：`tests/test_source_registry.py` 的 `_inject_failure` 需登记 `_KG` 分支（该测试本身就用 `pytest.fail` 强制要求新源登记，属预期内同步）。

---

## §8 隐私增量警示（已拍板：选 A）

**事实**：日报目前送进 LLM 的是 `pack_body`（实测 10-16 合计 4670 字符）；抽取要送的是 `detail_body`（合计约 29803 字符）。
→ **抽取会把约 6.4 倍的原始个人数据（含 QQ 对话、通知、浏览记录）发给 LLM 服务商**，这是相对现状的**真实隐私增量**。

现有边界（`apps/withlanggraph/AGENTS.md` 军规⑦）：涉敏数据仅本地落盘 + 本人邮箱，绝不出网。抽取不改变"不出网到第三方存储"，但**扩大了送进模型的内容面**。

三个选项：
- **A. 接受**（与现有日报同性质，只是量更大）
- **B. 路由裁剪**（按 §3.2 路由表，只送该通道需要的源，减少跨源泄露）
- **C. 先加脱敏层**（对对话/通知做实体掩码后再送模型）——成本最高，S1 前需加一个前置阶段

> **拍板结果（2026-10-18）：选 A 接受。**
> 同时 S1 仍按 §3.2 路由表执行——路由裁剪本就是为省 token 成本而设计，与 A 不冲突；不升级到 C。

---

## §9 验收标准（全客观可测）

| 指标 | 目标 |
|---|---|
| 六通道 JSON 解析成功率（重试后） | 100% |
| 同素材重跑幂等（`kg_runs` 不重复、`kg_edges` 行数不变） | 100% |
| 边 `from/to` 均存在于 `kg_nodes`（无悬空边） | 0 悬空 |
| L2 边 `attributor` 非空率 | 100% |
| L3 边 `confidence ≤ 0.6` | 100% |
| 乱序补录后 interval closure 正确（单测覆盖顺序+乱序共 7 场景） | 100% |
| 合并撤销可生效（`superseded_by` 后旧合并失效） | 100% |
| 现有 12 源 `chars` 加 `_KG` 前后不变 | 一字不差 |
| 现有测试 + 新增测试全绿（ruff + pytest） | 全绿 |

---

## §10 风险与回滚

| 风险 | 应对 |
|---|---|
| 抽取质量差（通道抽错/漏抽） | 先 7 天试跑对照 gold set；prompt 外置在 `config/kg_prompts/`，改 prompt 不动代码 |
| 预算被挤压 | `_KG` priority=105 最低，预算紧张时先丢；实测余量 5330 充足 |
| LLM 成本上涨 | §3.2 路由裁剪；`kg_runs` 幂等跳过重复素材；成本可在 `logs/<date>/llm_requests.jsonl` 按 `run_kind="kg_extract"` 统计 |
| 隐私增量 | §8 三选项，默认按 B |
| 抽取表与 ETL 表冲突 | 独立 `migrate_extract_tables()` + `kg_` 前缀，零耦合 |
| 回滚 | ① 关 `_KG.enabled=false`（秒级，无代码回滚）② 删 `kg_*` 表（源数据在 `events` 与 `pack_detail`，可重建）③ 阶段 tag 逐个 `git revert` |

---

## §11 文档同步清单（R5 三处，逐阶段）

| 阶段 | `langTrack-roadmap.md` | `langTrack-tech.md` | `architecture-flow.mmd` |
|---|---|---|---|
| S1 | 追加执行记录 | 加"KG 抽取层"节（新表/CLI/数据流） | 加 `extract/` 节点（codemap 复审） |
| S2 | 追加 | 补消解与 interval closure 说明 | 补 resolve/intervals 节点 |
| S3 | 追加 | 补三通道与候选表 | 补节点 |
| S4 | 追加 | 补 `_KG` 源（SourceSpec 三元组契约） | 补 `_KG` → 日报连线 |
| S5 | 追加观测记录 | 补调度与灰度说明 | 复审 |

> 另：`data_catalog.py` 的 `CONSUMERS / CONSUMER_SHORT` 需在 S4 同步（`add-info-source` 流程第 7 步要求）。

---

## §12 未完成设计（显式标注，不在本计划内）

1. **候选→规律升格机制**：`patterns`/`worldview` 只产出候选，升格标准/去重/合并规则上游文档空白，需独立设计后再加阶段
2. **图查询层（`ask.py` 等价物）**：5 类问题模板 + 强制 `[src:]` 校验，属独立能力，本计划仅留 `kg_tools.py` 作为最小下钻入口
3. **周期聚合（周/月/季）**：上游 Phase 6，依赖前两项完成
