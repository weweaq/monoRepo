# withlanggraph 路线图与执行记录

> 本文件记录 withlanggraph（gacore / langTrack）在 mono 仓库侧的迁移与集成进度。
> 格式约定（根规则 R5）：每次改动后追加一条记录，含「背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新」。
> 上游（原仓库）的日常开发与业务路线图见其自带 `docs/langTrack-roadmap.md`，此处只记 mono 侧事项。

## 项目背景

langTrack 数据链路服务端：`/ingest` 接收 + ETL 加工 + 报告 + dashboard 展示，
含 QQ 机器人前端（gacore 包）。上游为独立 git 仓库，通过 `git subtree` 周期性同步。

## 与上游的同步约定

- 上游 HEAD `b127a2e`（2026-09-04 迁入），此后用 `git subtree pull` 同步
- 上游有未提交 WIP（qq.py / dashboard.py / geocode.py / spatial_profile.py /
  scheduler.py / middleware.py 等及其测试）——**这些文件 mono 侧不改**，
  避免同步冲突；mono 侧问题一律走配置豁免 + 待办登记
- 下次同步后需要复查：根 `conftest.py` 的 `_KNOWN_UPSTREAM_FAILURES`（qq 角色卡
  切换用例，上游修复合入后移除）与 lint 债务清单

## 待办

- [x] Phase 2 迁入：subtree add + 接入 workspace + 门禁全绿（2026-09-05）
- [ ] **lint 债务专项**：全量规则（根配置 select E/F/W/I/B/UP/RUF）下存量 362 处
      违规；当前应用配置用最小基线 E4/E7/E9/F + per-file-ignores 豁免
      （geocode/persona/spatial_profile/report 的 F401/F841/E702）。
      时机：上游 WIP 合入并 subtree 同步后，一次性整改（可考虑先在上游做）
- [ ] ruff 升级评估：0.16 起默认规则集扩大（本仓钉 0.12 系与 pre-commit 对齐），
      升级需评估全仓影响
- [ ] 混合换行存量：8 个 langTrack 文件（geocode/routes/weather/label_places/
      __main__ 及 3 个 test_langTrack_*）上游提交时即为混合 EOL（CRLF/CR/LF 混杂），
      mono 侧不单独修（会产生与上游的全文件 diff）；如上游规范化后再同步
- [ ] 提炼 `amap-sdk` 共享包（geocode/routes/坐标转换），全量回归必须保持绿
- [ ] qq-botpy extra 未装入门禁环境：`uv sync --all-packages --extra qq` 后
      test_qq 的既知失败用例之外是否有新增暴露，装后验证

---

## 执行记录

### 2026-09-05 — 迁入 mono 仓库 (Phase 2)

**背景**：按路线图 Phase 2 迁入主力项目 WithLangGraph（subtree 保留完整测试演进史）。

**已完成**：
- `git subtree add --prefix=apps/withlanggraph`（上游 HEAD b127a2e，历史完整保留）
- 依赖补 `pygraphviz`（原 py12 conda 环境手工装的，pyproject 从未声明）
- 应用级 `[tool.pytest.ini_options]` 移除：pytest 跑 `apps/withlanggraph` 时会优先
  用应用配置、绕开根配置的 `--basetemp`，收尾清理 %TEMP% 遗留 ACL 坏符号链接
  （`pytest-of-17734/pytest-current`，原仓库符号链接实验遗留，删不动）时
  PermissionError 崩溃；统一由根配置接管（asyncio_mode/basetemp 均已具备）
- 应用 ruff 配置显式最小基线：`select = ["E4","E7","E9","F"]` + 资产目录排除
  （config/assets/code_run_header.py 是注入前代码头部，单行紧凑+预置 import 是
  刻意设计）+ tests 豁免 E402/E731（langTrack 测试惯例：文件头先做路径准备、
  clock 用 lambda 简写）+ 4 个上游 WIP 文件的存量豁免
- 根 dev 组 ruff 钉 `>=0.12,<0.13`：uv sync 曾把 ruff 升到 0.16.6，其默认规则集
  扩大导致无 select 的应用配置爆出 94 处违规（与 pre-commit v0.12.5 也不一致）
- 测试封闭化：proactive e2e 补 `gacore.graph.get_llm` fake（原依赖环境 .env 的
  LLM_PROVIDER，mono 无 .env 即挂）；清 test_langTrack_spatial_profile 5 处
  未用变量
- 根 `conftest.py` 登记 1 个上游既有失败用例（qq 角色卡切换：
  checkpointer MagicMock 无法 await adelete_thread），skip 注明来源

**实测验证**：
- `tools/scripts/check.ps1` 全绿：ruff 0 错误；pytest **937 passed, 1 skipped**
  （skip 为上述上游既有失败；withlanggraph 侧 935+1，用例数不少于迁前 305+）
- `uv run --package gacore python -m gacore.langTrack --help` 冒烟通过
  （注意包名是 `gacore` 不是目录名 withlanggraph）
- 迁移忠实性：geocode.py 等抽查工作区与 blob 字节一致

**偏差说明**：
- 上游有未提交 WIP，mono 侧对涉及文件（qq.py、test_qq.py、geocode.py、
  spatial_profile.py 等）零改动，问题走配置豁免 + 待办
- 上游 8 个文件提交时即混合换行（392 CRLF + 392 lone CR + 192 lone LF 之类），
  曾导致 Read/ruff/PowerShell 行数口径不一（584/976 之差）；已确认 blob 即如此，
  mono 不修（见待办）
- 曾误判"工作区换行被 subtree 搞坏"——实为 PowerShell 重定向验证 blob 时的文本
  转码假象（PS 陷阱：`git cat-file > file` 会按文本重新编码），用 Python 读
  字节才看清真相

**待办更新**：见上表（lint 债务、ruff 升级评估、混合换行、amap-sdk、qq extra）。
