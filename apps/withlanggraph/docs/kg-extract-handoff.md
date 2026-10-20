# KG 抽取层 · 交接文档（S1–S4 已落地，接手做 S5）

> 交接时间：2026-10-18 · 交接对象：下一个接手 agent（deepseek）
> 计划书（规格真源）：[`kg-extract-plan.md`](./kg-extract-plan.md)
> 技术事实真源：`langTrack-tech.md` §9.28 · 执行记录真源：`ROADMAP.md`
>
> **状态更新（2026-10-20 仓库体检）**：S5 已落地——`c12f2c5`（feat：scheduler `type="extract"` 通道 + `kg-extract` 23:30 job + 7 例封闭测试）、`b0cb053`（docs：S5 落地记录 + tech/架构图同步）。§2/§5 中"S5 待做"为起草时状态，现以实际代码与 `config/schedule.json` 为准；本文其余内容（红线 / 资产地图 / 数据层 / 风险）继续有效。测试基线已演进（全仓 pytest 现 1500+ passed，见 ROADMAP 最新执行记录）。

---

## 0. 一句话交接

把 langTrack 的结构化知识图谱（KG）抽取产物接进日报信息包。**S1–S5 已全部落地并提交**（抽取子系统六通道、实体消解、区间闭合、`_KG` 信息源注册、按日调度 `kg-extract` 23:30）；当前遗留待办为**灰度开启 `_KG` + 观测一周**。

全部工作都在 mono 仓库的 `apps/withlanggraph` 内。

---

## 1. 红线（先读，别踩）

1. **不碰 ETL**：不改 `etl.py` 的 `_SCHEMA`，不把抽取挂进 `etl.py` 流水线——抽取层是**独立 CLI**，保住 ETL「纯本地、确定性、可重建」属性（`kg-extract-plan.md` 决策 D2）。
2. **只增不改**：抽取层存储只用 SQLite 新增 `kg_*` 表；不进 `etl.py` 的 `fact_tables` 列表、不参与位置 v1/v2 冻结。
3. **不动现有 12 源**：`daily_info_pack.py` 的现有 12 个 `SourceSpec` 与 builder 一字不改；`_KG` 只**追加**一条。
4. **R6 迁移规范**：新表必须 `CREATE IF NOT EXISTS` + `created_at`/`updated_at`（默认 `datetime('now','+8 hours')`），语义变更走 `_migrate_*` + `PRAGMA user_version` 递增。
5. **R7 质量门禁**：提交前 `uv run ruff check apps packages tests` + `uv run --all-packages pytest` 必须全绿。
6. **R4 小步提交**：代码+测试一笔、文档一笔；`type(scope): 描述`，scope 用 `langTrack` 或 `withlanggraph`。
7. **R5 文档三处同步**：任何代码改动必须同步 `ROADMAP.md` / `langTrack-tech.md` / `architecture-flow.mmd`（外加 `add-info-source` 要求的 `data_catalog.py`）。
8. **R9 密钥**：`.env` 永不入库；CLI 边界必须显式 `load_dotenv()`（S1 曾因漏调导致首次实跑失败）。
9. **R11 运行时数据**：真实个人数据绝不入库；`data/` 整体 gitignore。
10. **测试封闭**：新测试一律 monkeypatch 打桩外部（LLM/网络/子进程），不触真实库、不触真实网络。

---

## 2. 阶段划分与提交历史

| 阶段 | 内容 | 状态 | 提交 |
|---|---|---|---|
| S1 | 抽取子系统骨架 + `events` 通道 | 已落地 | `ca90695` feat / `0fae469` docs |
| S2 | `relations`/`todos` 通道 + 实体消解 + 区间闭合 | 已落地 | `b52e324` feat / `440e356` docs |
| S3 | `emotions`/`patterns`/`worldview` 候选通道 | 已落地 | `c1ee694` feat / `72b0f9b` docs |
| S4 | `_KG` 源接入日报信息包（默认停用） | 已落地 | `9f61c53` feat / `8bbd304` docs |
| **S5** | **调度 + 灰度开启 + 观测一周** | **已落地**（调度部分；灰度/观测待办） | `c12f2c5` feat / `b0cb053` docs |

> 当前工作区唯一未跟踪项：`docs/sre/`（定时任务产出的运维检查报告，**与本案无关，别顺手提交**）。

---

## 3. 已完成资产地图

### 3.1 抽取子系统（独立 CLI）

入口：`python -m gacore.langTrack.extract --day YYYY-MM-DD [--force] [--only k1,k2] [--dry-run] [--dump] [--db PATH]`

| 文件（`src/gacore/langTrack/extract/`） | 职责（真实符号） |
|---|---|
| `__init__.py` | 对外入口 `run_extract` / `export_day` |
| `__main__.py` | CLI；`main()` 内 `load_dotenv()`（R9）；`--day` 必填 |
| `types.py` | `ChannelSpec` / `ExtractContext` / `ChannelOutcome` / `infer_node_type` |
| `material.py` | `load_material(day,cfg) → Material(by_source/text/raw_hash)`；`Material.select` |
| `schema.py` | `EXTRACT_SCHEMA`（八表）+ `migrate_extract_tables(conn)` |
| `channels/__init__.py` | `CHANNELS` 注册表（6 通道，加通道只需追加一行） |
| `channels/common.py` | 候选类通道共享落库原语（S3 新增，**不改存量三通道**） |
| `channels/{events,relations,todos,emotions,patterns,worldview}.py` | 各通道 `SPEC` + `handle` |
| `resolve.py` | `resolve_entities(conn,day,run_id)`；`resolve_alias`/`revoke_alias`；纯本地 `jaro_winkler` |
| `intervals.py` | `reclose_intervals` / `reclose_day`——按 `valid_from` 全量重排重算 `valid_to` |
| `runner.py` | `run_extract(day, cfg=None, *, force, only, dry_run, db_path, llm)` → `RunResult`；`export_day`；`_postprocess`；`_DAY_SCOPED_SQL` |

**素材装配零新增采集**：优先读 `data/logs/pack_detail/{day}/{key}.md` 的「完整取数详情」节；缺失回退 `build_info_pack_report(day,cfg)` 各源 `detail_body`（**23:30 抽取早于 23:50 日报时，回退是常态路径**）。

### 3.2 `_KG` 信息源（S4）

全部在 `src/gacore/daily_info_pack.py`：

| 符号 | 行号 | 说明 |
|---|---|---|
| `_KG_CAP` | 883 | `= 1200`（计划书原定 600，实测上调，见 §7 风险） |
| `_KG_PERSON_TOP` / `_KG_EDGE_TOP` / `_KG_EMOTION_TOP` / `_KG_TODO_TOP` / `_KG_CLIP` | 886-890 | 6 / 6 / 4 / 6 / 40 |
| `_KG_MISSING` | 892 | `"- 当日无 KG 抽取结果（抽取未运行或无产出）"` |
| `_KG_PERSON_SQL` / `_KG_EDGE_SQL` / `_KG_TODO_SQL` / `_KG_EMOTION_SQL` / `_KG_RELATION_SQL` / `_KG_COUNT_SQL` | 894-963 | 各节取数 SQL |
| `_kg_rows` / `_kg_clip` | 966 / 973 | 只读查询、单条截断（唯一截断点） |
| `_kg_day_graph` | 979 | 只读读层（`mode=ro`），表未建吞 `sqlite3.OperationalError` → `None` |
| `_KG_FN` | 1023 | `= _kg_day_graph`，**测试注入点** |
| `_kg_detail_lines` | 1026 | detail 全量逐条渲染 |
| `_build_kg` | 1037 | builder，title `〔KG·当日图摘要〕` |
| `SOURCES` | 1228 | 注册表；`_KG` 条目在 **1244**：`SourceSpec("_KG", _KG_CAP, 105, _build_kg)` |
| `build_info_pack_report` | 1258 | 停用源误报 bug 已修（`st["status"] not in ("empty","disabled")`） |

**行为**：pack = 运行头 + 人物 Top6 + 未完成待办 Top6 + 关系变化 Top6 + 情绪基调 Top4；detail = 当日 `kg_*` 八表全量逐条（供 `/health/source` 归因）。只读、不触发抽取、不建表、不写文件。

### 3.3 测试（全封闭）

| 文件 | 覆盖 |
|---|---|
| `tests/test_langTrack_extract_schema.py` / `_material.py` / `_events.py` | S1（25 例） |
| `tests/test_langTrack_extract_relations.py` / `test_langTrack_resolve.py` / `test_langTrack_interval_closure.py` | S2 |
| `tests/test_langTrack_extract_candidates.py` | S3 |
| `tests/test_info_pack_kg.py` | S4（9 例：库缺失/表未建/当日无运行/零产出 → missing_data；满数据；TOP 收敛；停用；cap 截断） |
| `tests/test_source_registry.py` | 新源强制同步点：`_inject_failure` 必须登记 `_KG`（否则该测试主动 `pytest.fail`） |

**当前基线**：`uv run --all-packages pytest` → **1462 passed / 5 skipped**；ruff 全绿。

### 3.4 文档（已同步，S5 需继续维护）

- `ROADMAP.md` — S1–S4 执行记录（背景/已完成/实测验证/偏差/待办）
- `docs/langTrack-tech.md` §9.28 — 含「日报接入（`_KG` 源，S4）」小节
- `docs/architecture-flow.mmd` — `SIP` 注册表 13 源、`_KG(105)`、`KG → SIP` 只读连线
- `docs/kg-extract-plan.md` — 状态行标 S1–S4 已落地；S4 落地偏差已记档

---

## 4. 数据层（八表 + 铁律）

`kg_runs` / `kg_nodes` / `kg_edges` / `kg_relations` / `kg_aliases` / `kg_emotions` / `kg_patterns` / `kg_worldview`（均在 `data/langTrack.db`）。

- **三层时间轴**（不得混淆）：`day`（抽取目标日）· `valid_from`/`valid_to`（事实业务有效区间）· `created_at`/`updated_at`（入库时间）。
- **幂等**：唯一索引 `ux_kg_runs_day_hash_ver(day, raw_hash, prompt_ver)`——同日同素材同 prompt 重跑自动 `skipped`；`edge_id` 内容派生主键。
- **只追加**：实体别名撤销 = 追加新行 + `superseded_by` 反向指针；边撤销 = `invalidated=1` + `superseded_at`。**旧行永不改**。
- **区间闭合**：`reclose_day` 按 `valid_from` 全量重排后重算 `valid_to`（`NULL` 排末位），**接受乱序、不拒绝**（手机批量迟到是常态）。
- **候选**：`kg_patterns` confidence 上限 0.7、`kg_worldview` 上限 0.6，两表 `status` 恒 `candidate`。**升格机制显式未做**（上游文档空白，见计划 §12）。

---

## 5. 你的任务：S5（核心交接内容）

### 5.1 目标（计划书 §5 S5）

1. 让抽取按日自动运行（计划设想 23:30，排在 `daily-report` 23:50 之前，保证当日图能进当日日报）。
2. 灰度开启 `_KG`（`config/info_pack.json` 的 `"_KG".enabled = true`）。
3. 按军规⑥**观测一周** `data/logs/info_pack_health.jsonl`，确认 `_KG` status 与 chars 稳定。
4. 提交：`chore(langTrack): 新增 kg-extract 定时任务并灰度开启 _KG 源` + `docs(langTrack): KG 抽取上线观测记录`。

### 5.2 ⚠️ 重要发现：计划书的「S5 只改 JSON、零代码」假设不成立

计划书 §5 S5 与 §「约束」第 4 条都写「新任务只改 `config/schedule.json`，不动 `scheduler.run_job`」。**实测核实该假设有误**：

- `scheduler.py` **L202-L210**：`load_jobs` 只认 `type` 为 `"proactive"` 或默认 `"job"`；**非 proactive 的 job 必须有 `prompt`**，否则 `KeyError` → 该 job 被静默跳过（L226-227）。
- `scheduler.py` **L1093**：`run_loop` 只按 `job.type == "proactive"` 分派到 `PROACTIVE_POOL`，其余一律走 `run_job`（= 跑 LLM 图，`_build_job_prompt` + `graph_runner`）。
- 全仓搜索确认：**没有任何 CLI/shell 型 job 通道**，也没有从 scheduler 或工具侧触发 `run_extract` 的入口（`run_extract` 的调用点只有 `extract/__main__.py`）。

结论：**要让抽取自动跑，必须动 `scheduler.py`**（或换一种触发方式）。请把这个偏差按 R5 记进 ROADMAP 的「偏差说明」。

### 5.3 推荐实现路径（Option A：给 scheduler 加 `type="extract"`）

理由：与项目既有「统一调度 + 统一日志 + 统一状态」一致；抽取是**按日一次性**任务，不需要 LLM 图（`run_extract` 内部自己调 LLM 跑六通道）。

具体改动点（建议）：

1. `scheduler.py::load_jobs`（L202 附近）：把 `type == "extract"` 与 `"proactive"` 并列，允许**无 `prompt`**（避免 `KeyError` 静默跳过）。
2. `scheduler.py::run_loop`（L1093 附近）：加分支，`type == "extract"` 时提交到一个 worker（可直接复用 `PROACTIVE_POOL` 或另开单 worker 池），调用新函数 `run_extract_job(job, cfg)`。
3. 新增 `run_extract_job(job, cfg)`：计算目标日 → 调 `gacore.langTrack.extract.run_extract(day, cfg)` → 把 `RunResult.status/rows/channels` 写日志。**不要走 `_deliver`**（不投递、不写日报文件）；如需留痕，写 `data/logs/` 下的专用 jsonl。
4. `Job` dataclass：`type` 字段已存在（L141），无需新增字段。

**目标日取值语义（需你拍板，建议）**：23:30 抽取当天，`day = datetime.now(UTC).astimezone().date().isoformat()`（与 `_build_job_prompt` L343 的 today 口径一致）。注意此口径下抽取只覆盖到 23:30，**最后 20 分钟数据不进当日图**——这是计划书已接受的取舍，但要写进文档。

**env**：`scheduler.py::main()` 已调 `load_dotenv()`（L1147），所以 scheduler 进程内 `run_extract` 能拿到 provider key，**无需在 `run_extract_job` 里重复调**。若你另起独立进程触发，则必须在边界补 `load_dotenv()`（S1 的坑）。

**测试（R7 必须带）**：新增 `tests/test_scheduler_extract_job.py`——monkeypatch `scheduler.run_extract`（或 `run_extract_job` 内部引用名）打桩，断言：①`type="extract"` 的 job 能被 `load_jobs` 装载（无 prompt 不跳过）；②到期时被分派；③不投递、不写日报；④失败只记账不炸 run_loop。**不触真实 LLM、不触真实库**。

**不建议**的方案：
- Option B：把抽取包成一个 agent 工具、job 用 LLM prompt 触发——浪费 LLM 轮次、非确定性、且与「抽取是确定性收尾」的设计冲突。
- Option C：Windows 任务计划/独立服务——脱离项目统一调度与状态，运维分叉。

### 5.4 灰度开启与观测

- **最快路径（无需改文件）**：review_server 的 `/config` 页可**逐源热改** `enabled`（`daily_info_pack.load_source_config` 现读现用，改完**下次构建即生效**，无需重启 8010）。用它在 `/config` 里把 `_KG` 打开即可灰度。
- **落盘路径**：`config/info_pack.json` 加 `"_KG": {"enabled": true, "cap": 1200, "priority": 105}`。⚠️ 该文件被 `apps/withlanggraph/.gitignore:43` 忽略，**改它不会进 git**（见 §7 风险 1）。
- **观测**：`data/logs/info_pack_health.jsonl` 末行看 `_KG` 的 `status` / `chars` / `full_chars`；页面 `http://127.0.0.1:8010/health`（矩阵）与 `/health/source`（单源三节归因）。
- **回算历史日**（零 LLM 零邮件、秒级）：`backfill_health.refresh_day(Config.default(), "<date>")`。
- **验收标准**：`_KG` status 长期 `ok`（或数据缺失时 `missing_data`）；`full_chars` 不长期大于 cap（军规⑥：恒大于 cap 或长期非 ok = 采样设计有问题）。
- **回滚**：`_KG.enabled=false`（秒级，无代码回滚）→ 必要时删 `kg_*` 表（源数据在 `events` 与 `pack_detail`，可重建）→ 阶段 tag 逐个 `git revert`。

### 5.5 S5 必须同步的文档

- `ROADMAP.md`：S5 执行记录（含 §5.2 偏差：scheduler 需改代码）
- `docs/langTrack-tech.md` §9.28：补调度与灰度说明（调度入口符号、目标日口径）
- `docs/architecture-flow.mmd`：`SC`（scheduler）节点补 `kg-extract` job 与 `SC → EX` 触发边（用 codemap 复审，节点须可回溯源码符号）
- `docs/kg-extract-plan.md`：状态行 + §5 S5 的落地偏差
- `config/schedule.json`（**已入库**，需提交）+ `config/info_pack.json`（gitignore，不入库）

---

## 6. 环境与常用命令（复制即用）

```powershell
# 依赖（成员需显式 --all-packages）
uv sync --all-packages

# lint / 测试
uv run ruff check apps packages tests
uv run --all-packages pytest
uv run pytest apps/withlanggraph/tests/test_info_pack_kg.py -q      # 单文件
tools/scripts/check.ps1                                            # 一键门禁（可 -Fix）

# 跑一次抽取（真实库、真实 LLM，需 .env 配好 provider key）
uv run --package gacore python -m gacore.langTrack.extract --day 2026-10-16
uv run --package gacore python -m gacore.langTrack.extract --day 2026-10-16 --force --dump

# 只读验证 _KG builder（不触发抽取、不改库）
uv run --package gacore python -c "import sys; sys.path.insert(0,'apps/withlanggraph/src'); from gacore.config import Config; import gacore.daily_info_pack as d; t,p,det=d._build_kg('2026-10-16', Config.default()); print(d.classify_body(p)[0], len(p), len(det)); print(p)"

# 回算某日体检（零 LLM 零邮件）
uv run --package gacore python -m gacore.backfill_health --day 2026-10-16

# 提交钩子（首次需联网）
uv run pre-commit install
```

**运行环境认知**：uv venv 的 `python/pythonw.exe` 是 launcher 桩，每启动一个 Python 程序系统里会出现**父子两个进程**（桩 + 真解释器），端口由子进程监听——**正常现象，不是重复启动**，别把其中一个当孤立重复进程杀掉。

**本地服务（dev-console，`apps/dev-console/services.json`）**：
- `langTrack` → 8000（`gacore.langTrack --host 0.0.0.0`），前端 `http://127.0.0.1:8000/dashboard`
- `review_server` → 8010（`gacore.review_server`），前端 `http://127.0.0.1:8010/review`
- scheduler → `apps/withlanggraph/start.py`
- dev-console 自身 `http://127.0.0.1:8787/`（改 `services.json` 需重启 dev-console 才生效）

---

## 7. 已知风险 / 待决问题（接手前请读）

1. **「默认停用」不持久（重要）**：`config/info_pack.json` 被 gitignore，代码侧默认 `enabled`（即新克隆的环境里 `_KG` 是**开启**的）。当前本机靠该文件关着。若要求「默认停用」在仓库层可复现，需要你决策：要么把默认停用写进代码（如 `SourceSpec` 增加 `default_enabled=False`），要么提交一份 `config/info_pack.example.json`。**别想当然认为代码默认就是停用。**
2. **cap 600 → 1200 偏差**：实测 2026-10-16 当日图摘要 1071 字符，600 会截掉情绪段。已在常量注释、tech、plan、ROADMAP 记档。观测期若 `full_chars` 持续逼近/超过 1200，需回看 TOP 收敛参数。
3. **候选升格未做**：`patterns`/`worldview` 只产 `status='candidate'`，升格逻辑显式未实现（计划 §12）。**不要顺手实现**，属独立阶段。
4. **抽取时点覆盖不全**：23:30 跑，当日最后 20 分钟数据不进当日图。若不可接受，需改为次日凌晨抽取前一日（但那样就赶不上 23:50 的日报，与 S5 目标冲突——取舍需你拍板并记录）。
5. **LLM 非确定性**：六通道行数在重跑间会抖动（S2→S3 实跑 events 21→17 等），**属正常抖动，不影响不变量**；测试断言不要钉死行数，要钉不变量。
6. **消解阈值观察项**：`我（用户）`/`用户（本人）` fuzzy 得分 0.55，恰低于 0.6 候选线，故当日无别名行——机制正常，阈值调优留待候选复核阶段。
7. **`docs/sre/` 未跟踪目录**：定时任务产出，与本案无关，**不要误提交**。
8. **架构图维护方式**：`architecture-flow.mmd` 是全应用架构图**单一真源**，tech 里只放相对链接、**不得复制**。改动前用 `codemap` skill 走查，节点须能回溯真实源码符号（未经核实不得手改）。

---

## 8. 快速上手 checklist（按顺序）

1. [ ] 读 `docs/kg-extract-plan.md`（尤其 §5 S4/S5、§6 两个设计债、§12 未纳入）+ 本文件 §1 红线、§5.2 偏差
2. [ ] `uv sync --all-packages`；`uv run --all-packages pytest` 跑基线，确认 1462 passed / 5 skipped
3. [ ] 只读跑 §6 里的 `_build_kg` 单行命令，确认 `status=ok`、`pack≈1071`
4. [ ] 拍板 S5 触发方式（推荐 Option A）与目标日口径（建议 today），记入 ROADMAP 偏差
5. [ ] 实现 `scheduler.py` 的 `type="extract"` 分支 + `run_extract_job` + 封闭测试
6. [ ] `config/schedule.json` 加 `kg-extract` job（23:30），提交（`chore(langTrack): ...`）
7. [ ] `/config` 页或 JSON 灰度开启 `_KG`，回算一日验证现有 12 源 chars **一字不变**
8. [ ] 观测一周 `info_pack_health.jsonl`，写 `docs(langTrack): KG 抽取上线观测记录`
9. [ ] 每步：ruff + pytest 全绿 → R5 三处同步 → 小步提交
