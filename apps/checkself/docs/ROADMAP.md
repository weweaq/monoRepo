# checkself 路线图与执行记录

> 本文件记录 checkSelf（个人画像 / 自追踪）在 mono 仓库侧的迁移与集成进度。
> 格式约定（根规则 R5）：每次改动后追加一条记录，含「背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新」。
> 上游（原仓库 `../checkSelf`）的日常开发与业务路线图见其自带 `docs/个人画像-设计文档与路书.md`，此处只记 mono 侧事项。

## 项目背景

个人画像系统：多源数据（B 站 / 网易云等）采集入库 → 规则 + LLM 分析 → portal 门户展示。
FastAPI 应用，代码包名 `profile`。上游为独立 git 仓库，通过 `git subtree` 周期性同步。

## 与上游的同步约定

- 上游 HEAD `e8e7864`（2026-09-05 迁入），此后用 `git subtree pull` 同步
- mono 侧已改上游文件（lint 修复，见执行记录）——**建议把这批修复回传上游提交**，
  否则下次 subtree pull 会在这 11 个文件上产生小冲突；回传后 mono 保持零改动基线
- 下次同步后需要复查：根 `conftest.py` 的 `_KNOWN_UPSTREAM_FAILURES`（4 个 schema
  漂移用例，上游修好后移除）与 lint 债务清单

## 待办

- [x] Phase 2 迁入：subtree add + 接入 workspace + 门禁全绿（2026-09-05）
- [ ] **lint 债务专项**：全量规则（根配置 select E/F/W/I/B/UP/RUF）下存量 106 处
      违规（I001/RUF013/E501 等）；当前应用配置用最小基线 E4/E7/E9/F。
      时机：上游 refactor 收敛后一次性整改（优先在上游做，随 subtree 同步）
- [ ] ruff 升级评估：0.16 起默认规则集扩大（本仓钉 0.12 系与 pre-commit 对齐）
- [ ] **上游疑似遗漏**：`io/bilibili_reader.py` 的 `read()` 原有一行 `bvid = item.get(...)`
      取值后从未使用（mono 侧已按 F841 删除）——上游若本打算用 bvid 做去重 / 关联，
      需在上游补回真实逻辑
- [ ] dependency-groups 的 `httpx2` 已移除（测试从未 import；PyPI 上另有同名包，
      上游疑似笔误想要 httpx）——若后续要用 TestClient，工作区已有真 httpx
      （gacore 依赖），上游也建议改名
- [ ] 提炼 `ga-logging` 共享包（脱敏日志 + dev-console JsonlLogger，见指导第 6 章）

---

## 执行记录

### 2026-09-05 — 迁入 mono 仓库 (Phase 2)

**背景**：按路线图 Phase 2 迁入 checkSelf（subtree 保留历史）。

**已完成**：
- `git subtree add --prefix=apps/checkself`（上游 HEAD e8e7864）
- 接入 workspace 为**虚拟成员**（`[tool.uv] package = false`）：包名 `profile` 与
  Python 标准库 profile 模块同名，构建安装进 site-packages 会被标准库 shadow；
  运行语义保持"应用目录内运行"（如 `uv run --directory apps/checkself ...`），
  依赖（fastapi/uvicorn/pytest）仍由 workspace 统一解析
- 删除应用级 `uv.lock`（workspace 统一锁文件接管）
- 移除 dependency-groups 的 `httpx2`（见待办）
- lint 基线修复 23 处（最小规则集 E4/E7/E9/F 下）：20 个未用 import 名
  （10 个文件，`ruff check --fix` 自动 F401）+ 3 个未用赋值变量手删 F841
  （`bvid` / `instance_source` / `task_engine.py` 的 6 处 `result =`）——这些是
  改动上游文件，建议回传（见同步约定）
- 修一处删 F401 引入的回归：`task_engine.py` 从 db_store import 的
  `recover_stale_tasks` 本文件未用、但被 `app.py` 当 re-export 依赖，删除即断链；
  修法是 `app.py` 改从定义处 `profile.portal.db_store` 导入（与 routes 模块风格
  一致，顺带消除隐式 re-export——上游跑 ruff --fix 也会踩同样的坑）
- 根 `conftest.py` 登记 4 个上游既有失败用例（skip 注明来源）：近期 refactor
  （readers 内联、portal UI 重构）改了 analysis 输出键名，测试断言仍是旧键

**实测验证**：
- `tools/scripts/check.ps1` 全绿：ruff 0 错误；pytest **987 passed, 5 skipped**
  （全仓 992 collected；5 个 skip 均为上游 HEAD 既有失败——withlanggraph 1 +
  checkself 4，见根 conftest.py `_KNOWN_UPSTREAM_FAILURES`；checkself 侧
  50 passed + 4 skipped，用例数与迁前一致）
- `uv run --directory apps/checkself python -c "import profile.portal.app"` 冒烟通过

**偏差说明**：
- 上游 HEAD 即有 4 个测试失败（schema 漂移），mono 不修业务断言（属上游职责），
  走 skip 登记 + 待办，等上游修复随 subtree 同步
- 包名 `profile` 与标准库同名是历史包袱：虚拟成员方案规避了安装 shadow 问题，
  但代价是不产出可安装包；若未来要提炼共享代码，从 `profile/` 内迁移到 `packages/`

**待办更新**：见上表（lint 债务 106 处、ruff 升级、bvid 疑似遗漏、httpx2、ga-logging）。

### 2026-10-31 — 日志改为按天归档 + 测试隔离

**背景**：用户发现 `apps/checkself/logs` 下一天出现几十个目录，预期是"每天一个文件夹"。
排查结论：164 个目录 **全部是 pytest 产物**（逐目录核对 `run.jsonl`，均含
`profile.test` 的「测试日志1/2」，真实运行目录为 0）。根因两条：
1. `profile/log.py` 的 `setup()` 原本每次进程启动建一个 `logs/YYYY-MM-DD-HHmmss/`；
2. `task_engine._run_task` 里 `setup(log_dir=LOG_DIR)` 用的是真实 `profile.config.LOG_DIR`，
   测试直接调 `TaskRunner.start_task()`，于是每跑一次 pytest 就新建一个目录。
   同一批测试还直接用真实 `data/profile.db` 并 `DELETE FROM llm_calls/task_runs`。

**已完成**：
- `profile/log.py`：改为按天归档 `logs/YYYY-MM-DD/run.jsonl`，同一天所有运行追加同一文件；
  新增 `_DailyFileHandler`，长驻进程跨 0 点自动切到新日期目录；每条日志注入 `run_id`
  （时间戳 + pid + uuid 片段，保证同秒同进程也不撞），每次运行开头写一条
  `message="运行开始"`（含 run_id/pid/cwd/argv）作为运行分界，且绕过文件级别过滤，
  设成 WARNING 也不丢；切天失败（磁盘满/权限）不向业务抛异常、状态不推进、下一条重试；
  新增 `get_log_file()`，`get_log_dir()` 语义改为"当天目录"
- `profile/portal/task_engine.py`：任务开始时把 `log_dir`（当天 run.jsonl 路径，未初始化
  时不写）记进 `task_runs.log_dir`，portal 可据此定位任务日志
- 新增 `tests/conftest.py`：autouse fixture 把 `LOG_DIR` / `DB_PATH` /
  `OBSIDIAN_OUTPUT_DIR` 指到 `tmp_path`；由于多个模块在**导入期**绑定这些常量，
  fixture 逐个改写绑定点（config / init_db / portal.app / output.writer /
  refresh.reporter / portal.routes.dashboard / portal.routes.profiles / 4 个 cli 模块），
  并在用例前后重置日志 handler 与 `_RUN_ID`
- 新增 `tests/test_log.py`：按天目录、同日追加（run_id 不同）、跨天切换、切天失败重试、
  WARNING 级别仍写运行分界、全部绑定点已隔离，共 6 个用例
- 清理历史垃圾：删除 `logs/` 下 **166** 个目录（原始 164 个测试产物 + 排查期间复跑新增 2 个；
  删除前逐个确认，无真实运行日志）

**实测验证**：
- `uv run --all-packages pytest apps/checkself/tests -q` → **56 passed, 4 skipped**；
  连续多轮运行后 `logs/` 目录数与 `data/profile.db` mtime 均不变（0 新增）
- 真实运行冒烟：两次 `setup(LOG_DIR)` 各写一条「运行开始」，落在同一个
  `logs/2026-10-31/run.jsonl`，`run_id` 不同（按天归档 + 当天追加生效）

**偏差说明**：
- 全局 `AGENTS.md` 日志规范写的是「每次运行独立目录 `logs/YYYY-MM-DD-HHmmss/`」，
  本次按用户明确要求改为按天归档，属有意偏差；如需回到按运行分目录，改
  `profile/log.py` 的 `setup()` / `_DailyFileHandler` 两处即可
- 「一天一个文件」的固有代价：多进程（常驻 portal + 手动 CLI）同时写当天文件时按行
  尽力追加，Windows 下超长单行理论上可能交错；已写进 `log.py` 的 `setup()` docstring
- `task_runs.log_dir` 现在存的是**文件路径**（`logs/YYYY-MM-DD/run.jsonl`）而非目录，
  当前无读取方；跨 0 点的任务可能后半段日志已落入新一天文件

**待办更新**：无新增；原 lint 债务等项不受影响。

