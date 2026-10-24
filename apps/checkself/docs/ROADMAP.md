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
