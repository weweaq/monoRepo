# withlanggraph 路线图与执行记录

> 本文件记录 withlanggraph（gacore / langTrack）在 mono 仓库侧的迁移与集成进度。
> 格式约定（根规则 R5）：每次改动后追加一条记录，含「背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新」。
> 数据链路路书与技术文档统一用 `apps/withlanggraph/docs/`（原仓库已停更，见 AGENTS.md「唯一开发源头」）。

## 项目背景

langTrack 数据链路服务端：`/ingest` 接收 + ETL 加工 + 报告 + dashboard 展示，
含 QQ 机器人前端（gacore 包）。曾为独立 git 仓库，经 `git subtree` 迁入 mono。

## 与上游的同步约定（原仓库已冻结）

- 上游 HEAD `b127a2e`（2026-09-04 迁入），2026-09-09 `subtree pull` 同步至
  `834f8bb`（日报v2 全量合入，见下方执行记录）
- **2026-09-09 起原仓库停止更新**，后续开发一律在 mono 的 `apps/withlanggraph/`
  直接进行，不再等待/回写上游（决策见执行记录「上游冻结 + mono 单写决策」）
- 既知遗留（原路书登记的 root conftest `_KNOWN_UPSTREAM_FAILURES`：qq 角色卡
  切换 + checkSelf 4 个 schema 漂移用例）改为在 mono 侧直接修复后移除。

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
- [x] qq-botpy extra 未装入门禁环境（已修，2026-09-09）：`tools/scripts/check.ps1`
      的 `uv sync` 已补 `--extra qq`。否则门禁 sync 会卸载 qq-botpy 并损坏
      aiohttp（dist-info 缺 RECORD），导致 gacore QQ 前端启动失败
- [x] 带 `--extra qq` 后 test_qq 全量回归（2026-09-09）：**21 passed, 1 skipped**，
      skip 为既知上游失败（qq 角色卡切换 checkpointer adelete_thread），无新增暴露
      （见下方执行记录「门禁 --extra qq 生效验证 + 服务重启」）
- [x] 原仓库停止更新、mono 单写（决策定案，2026-09-09）：原仓库不再回写，
      路书/tech（`apps/withlanggraph/docs/`）与 ROADMAP/AGENTS.md 均以 mono 为准。
      见下方执行记录「上游冻结 + mono 单写决策」
- [x] 日报补跑 CLI：`python -m gacore.rerun --day <YYYY-MM-DD> [--no-email]`
      （2026-09-09，mono 单写首个功能，见下方执行记录）
- [x] 真实补发邮件的主题标记核看（2026-09-09 15:54 实发）：日志确认
      `subject='[gacore] daily-report · 2026-09-08（补跑）'`，收件人收到即为终验
- [x] memory 目录迁移遗漏修复（2026-09-09）：qq_chat_log/global_mem/daily
      notes/ocr_history 从旧仓合并迁入，9-08 QQ 摘录验证恢复
- [x] 9-08 完整版日报已重发（2026-09-09 16:16，含 7 条 QQ 摘录），
      归档 `daily-report_20260909_161635.md`
- [x] 记忆维护「阶段二」语义触发（2026-09-09 规划定案）：pgvector + 本地
      onnx embedding（bge-small-zh）。已探测确认本机 PG16+pgvector 扩展现成可用、
      deepface 已有同范式（psycopg+pgvector），故直接接 PG 不另装服务。见下方
      「记忆维护 阶段二」执行记录
- [x] 记忆维护「阶段二」实测闭环（2026-09-09 起分步实测，见「阶段二实测
      闭环」系列执行记录）：本地 bge-small 加载 + `embedding.encode` 冒烟 +
      `vector_store` 真实画像 sync + `VectorTrigger` 端到端 + `persist_entry`
      统一写入口 + Semantic/Episodic 双表并行召回均已落地

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

### 2026-09-09 — 同步上游日报v2（subtree pull 至 834f8bb）

**背景**：原仓库在迁入后又有 3 个提交 + 一批未提交 WIP（日报v2：每日信息包、
轨迹静态图、QQ/邮件/调度升级、报告事件流增强、email 附件 base64 修复）。
用户决定切到 mono 使用，需先把这些新改动同步进来。

**已完成**：
- 原仓库把 5 处 WIP 提交为 `834f8bb`（report.py 事件流 + email_tools 默认 base64
  修复 + test_langTrack_report_device + 路书/tech 同步）
- mono `git subtree pull --prefix=apps/withlanggraph` 干净合并（ort 策略，无冲突，
  merge commit `088a827`），27 文件 +4216/-111：daily_info_pack / trajectory_map /
  dashboard / scheduler / middleware / qq / geocode / report 等全部同步
- 新增 lint 豁免：上游日报v2 核心模块 `src/gacore/daily_info_pack.py` 的 5 处
  E402——tools `.func` import 刻意置中（懒加载避免循环依赖 + 模块级名字供测试
  monkeypatch），docstring 有明确说明，非误报，per-file-ignores 保留豁免

**实测验证**：
- `tools/scripts/check.ps1` 全绿：ruff 0 错误；pytest **1067 passed, 5 skipped**
  （5 个 skip 仍为根 conftest 既有登记；withlanggraph 侧新增日报v2 用例后
  约 1015+ 用例，全部通过，无新增失败）

**偏差说明**：
- 上游 `daily_info_pack.py` 等新文件的 import 风格与流程 lint 基线（E4/E7/E9/F）
  冲突仅 E402 一处，属刻意设计，豁免而非改上游
- 服务切换（原仓库 → mono）见 dev-console services.json 变更记录

**待办更新**：amap-sdk 提炼的上游 geocode.py WIP 已合入，可重启评估。

### 2026-09-09 — 服务切到 mono 运行 + 门禁 sync 修复

**背景**：决定正式切到 mono 运行。启动 langTrack/gacore 时两次踩坑——旧管理台
缓存旧配置导致服务跑回原仓库代码；门禁 sync 卸载 qq-botpy 并损坏 aiohttp 导致
gacore 启动失败。

**已完成**：
- 确认两边仓库同步：6 个「听歌/视频伴音分流」文件在 git blob 层一致（原仓库
  HEAD `7341dc5`、mono merge `61c3f10`），mono main 已 push 至远程
- 停旧管理台（旧独立目录 `D:\...\dev-console`，内存缓存旧 services.json），
  从 mono/apps/dev-console 重启，mono 配置生效
- 修 `tools/scripts/check.ps1`：`uv sync` 补 `--extra qq`
- 修 aiohttp 损坏：`uv sync --all-packages --extra qq --reinstall-package aiohttp`
- 启动 langTrack(0.0.0.0:8000) + gacore(QQ 机器人「韩立」+ 调度)

**实测验证**：
- 服务进程 cmdline 均指向 `mono\.venv\Scripts\python.exe` + mono 代码
- gacore checkpointer 路径 = `mono\apps\withlanggraph\data\gacore_chat.db`
- QQ bot ready: 韩立；langTrack uvicorn 0.0.0.0:8000 正常

**偏差说明**：
- dev-console 内存缓存 services.json，改配置后必须重启管理台才生效
- 门禁 `uv sync` 无 `--extra qq` 时卸载 qq-botpy；aiohttp dist-info 缺 RECORD
  致卸载不干净（`No module named 'aiohttp._cookie_helpers'`），需 --reinstall-package
- dev-console 启动器 start.bat/start-console.vbs 原本指向旧目录，已同步更新到 mono

**待办更新**：qq-botpy extra 待办标记已修；新增「带 --extra qq 后 test_qq 全量回归」待办。

### 2026-09-09 — 门禁 --extra qq 生效验证 + 服务重启

**背景**：上条记录新增的「带 --extra qq 后 test_qq 全量回归」待办，需确认
qq-botpy 入环境后是否引入新测试失败，并让 langTrack/gacore 以干净单实例运行。

**已完成**：
- test_qq 全量回归通过：**21 passed, 1 skipped**（skip 为既知上游失败——
  qq 角色卡切换 checkpointer MagicMock 无法 await adelete_thread，根 conftest
  登记），无新增暴露
- 确认 qq-botpy 已入环境且 import 名是 `botpy`（非 `qqbot`），aiohttp 3.14.3
  完整可用（含 `_cookie_helpers`）
- 清理三组重复服务进程（dev-console/langtrack/gacore 各双实例，双启动残留），
  并 `uv sync --all-packages --extra qq --reinstall-package aiohttp` 修复
  误卸载的 dev 组 22 包 + aiohttp RECORD 缺失
- 从 mono/apps/dev-console 干净重启 dev-console，经 API start 拉起
  langtrack(0.0.0.0:8000) + gacore，均为单实例

**实测验证**：
- `uv run --all-packages pytest apps/withlanggraph/tests/test_qq.py -q`
  → 21 passed, 1 skipped in ~5s
- `/api/status`：langtrack running pid=22544 ports_open=8000、
  gacore running pid=25576；gacore 日志显示 QQ bot「韩立」登录成功 + scheduler 启动

**偏差说明**：
- 排查时曾误 `import qqbot`（正确为 `botpy`）致短暂误判依赖缺失
- 曾误用 `uv sync --package gacore --extra qq`（--package 会排除其它成员与 dev 组），
  卸载 22 包并再次损坏 aiohttp；正确姿势始终是
  `uv sync --all-packages --extra qq [--reinstall-package aiohttp]`
- 重复进程根因是 dev-console 双启动（server.py 已设 allow_reuse_address=False
  做单实例互斥，修复后第二次启动会静默退出）

**待办更新**：「带 --extra qq 后 test_qq 全量回归」勾选完成。

### 2026-09-09 — 上游冻结 + mono 单写决策

**背景**：前一条待办「同步约定」仍假设原仓库继续演进（含「上游 WIP 合入后
subtree 同步再整改」等口吻）。用户拍板：**原仓库停止更新，所有改动都在
monorepo 里**。需把这一决策写进 AGENTS.md，让文档与代码落地唯一朝代。

**已完成**：
- `AGENTS.md` 新增「唯一开发源头（2026-09-09 起）」节：原仓库停更，
  所有改动在 `apps/withlanggraph/`，不再回写原仓库；路书/tech 路径明确指向
  mono 侧 `apps/withlanggraph/docs/`
- 路书「改完必更」补全 mono 侧完整路径，消除与旧版相对路径的歧义

**实测验证**：
- AGENTS.md / ROADMAP.md 均以 mono 为唯一事实来源；原仓库无新改动，
  后续 `git subtree pull` 不会产生文档冲突

**偏差说明**：
- 之前多处待办文案带「先在上游做 / 待上游修复后同步」口吻，现决策下这些
  都改为在 mono 直接做；后续专项整改都基于 mono，不再等上游

**待办更新**：勾选「原仓库停止更新、mono 单写」；lint 债务 / 混合换行 / ruff 评估
等专项，执行位改为当前 mono 仓库。

### 2026-09-09 — 日报补跑 CLI（mono 单写首个功能）

**背景**：用户反馈 9-08 早间收到的 9-07 日报内容有问题需重发。手工内联复跑
`run_job(for_day=...)` 验证数据无误后（9-08/9-07 两期均真实跑通），把"补跑某天
日报"固化为正式 CLI——这是上游冻结、mono 单写后的第一个功能提交。

**已完成**：
- 新增 `src/gacore/rerun.py`：`python -m gacore.rerun --day 2026-09-08
  [--job daily-report] [--no-email]`，日期/job 校验，`--no-email` 只归档不投递
- `scheduler.run_job` 加 `deliver: bool = True`：False 跳过 `_deliver`，
  output 归档 + daily note 照写
- `_deliver_email` 主题修正：历史补跑时挂数据日 + `（补跑）`标记，
  不再挂发送时刻的 today（9-08 重发时主题误标 09-09 的问题一并修掉）
- 测试 +3：补跑主题 2 例（for_day 标记 / 同日无标记）+ deliver=False
  路由 1 例；路书与 tech（§9.24）同步

**实测验证**：
- CLI 冒烟：非法日期 / 不存在 job → exit 2（附可用 job 列表）
- 真实补跑 9-07 `--no-email`：`delivery skipped (deliver=False)`、
  归档 `daily-report_20260909_135819.md` 落盘、CURRENT_TASK_DONE 162s
- `test_scheduler.py` 全量 **70 passed**；ruff 零告警
- 早前真实补跑 9-08（含邮件重发）已成功，正文数据与截图日报吻合
  （167 次解锁/3.3h 屏幕/5 段轨迹）

**偏差说明**：
- tech 文档记载的旧补跑入口 `rerun_daily.py` 从未入库（当时是临时脚本），
  本次是首个正式入口，tech §9.23 描述已修正
- 补跑发邮件路径的 `（补跑）`主题标记，单测已覆盖，真实邮件待下次补发人工核看

**待办更新**：新增待办「真实补发邮件的主题标记人工核看」。

### 2026-09-09 — gacore 重启载新码 + 9-08 补跑实发邮件

**背景**：rerun CLI 提交后 gacore 仍是旧代码进程；用户要求重启并真实补跑
9-08 日报发邮件，验证补跑链路 + 主题标记。

**已完成**：
- 经 dev-console API `/api/restart` 重启 gacore（停 2 进程、新 pid 29708）
- 补跑 9-08 并实发邮件：`python -m gacore.rerun --day 2026-09-08`

**实测验证**：
- gacore 重启后 QQ bot「韩立」登录成功 + Scheduler 后台线程启动（15:50）
- 补跑 CURRENT_TASK_DONE（223s），邮件发出至 1773465183@qq.com，
  **日志确认主题 `[gacore] daily-report · 2026-09-08（补跑）`**，
  归档 `daily-report_20260909_155437.md`

**偏差说明**：无（新代码进程即补跑进程；今晚 23:50 正常调度走同一份新码）。

**待办更新**：「真实补发邮件的主题标记人工核看」勾选完成。

### 2026-09-09 — memory 目录迁移遗漏修复（QQ 摘录缺失根因）

**背景**：用户发现补发的 9-08 日报缺 QQ 对话内容，问为什么。排查发现切换
mono 时只迁了 `data/`（9-09 11:42 整目录拷贝，手机/轨迹/聊天 checkpointer
数据完整），但 **`memory/` 目录从未迁移**——mono 的 QQ bot 12:54 接管后新开
空白记忆文件，9-08 当天的对话历史留在旧仓库 `memory/qq_chat_log.jsonl`。

**受影响范围（远不止 QQ 日志）**：
- `qq_chat_log.jsonl`：旧仓 101 行（9-05×57 + 9-08×44），mono 仅 21 行（9-09）
- `global_mem.txt`（19.4KB 长期画像）/`global_mem_insight.txt`（18.5KB）：
  mono 只有今天补跑新写的 2KB「首跑立锚」版，历史画像全丢
- `memory/daily/`：旧仓 31 天日报 note，mono 只有 4 个（且 9-07/9-08 是
  补跑产的缩水版——**旧版含婚期提前/证件遗失等 QQ 推导的家事要务**）
- `ocr_history.jsonl`（8.8KB）：完全缺失
- `schedule_state.json`：缺失（调度今晚 23:50 会自写新状态，补齐仅为连续性）

**已完成（合并策略：旧仓为基底 + mono 当日增量，替换前全量备份至
`data/backup/memory_pre_merge_20260909/`）**：
- qq_chat_log：101+21 按 (ts,direction,text) 去重排序合并 → 98 行（删的
  24 行全是 `/reboot` 命令被写 3 遍的机械重复，非对话内容，去重即提质）
- global_mem / insight：旧仓全文 + mono 今日 4 条增量（带边界标记注释）
- daily notes：补 27 个缺失日，9-07/9-08 用旧仓原版覆盖补跑缩水版，
  保留 mono 的 9-09
- ocr_history / schedule_state：直接拷贝

**实测验证**：
- `build_info_pack('2026-09-08')` 的〔对话·当日 QQ 摘录〕恢复 7 条用户原话
  （寻证/婚期改 9-12/记忆触发机制讨论），pack 5071 字符
- 9-08 note 恢复为 5622B 原版（含家事要务节）；global_mem 含婚事画像内容

**偏差说明**：
- mono 补跑期间产出的「首跑立锚」类记忆条目未删（与旧仓画像互补，重复
  度可接受，留待 LTM 机制自清理）
- 15:54 发出的那封 9-08 补跑邮件没有 QQ 内容（当时数据未迁移），已具备
  条件重发完整版——待用户确认是否再补发一次

**待办更新**：新增待办「9-08 完整版日报（含 QQ 摘录）是否重发待用户定」。

### 2026-09-09 — 记忆维护「阶段二」语义触发（pgvector 落库）

**背景**：阶段一（事件驱动 trigger→judge→apply）已落地后，用户指出静态关键词
触发有硬伤——"搬家到朝阳区"这类与画像行*无语义重叠*的消息会漏触发，而画像又
不会自动刷新词表。讨论后确立方向：既然终极目标是理解主人、画像要作检索，
不如**直接对接向量库**。经探测本机已具备全部底座，无需另装服务。

**底座探测结论**：
- **PostgreSQL 16.4**（`D:\workplace\postGreSql16`，服务运行中，`postgres/123789`
  @127.0.0.1:5432）**已启用 pgvector 扩展**（pg_available_extensions 可见 0.8.x），
  CREATE TABLE/INSERT/`<=>` 相似度排序实测通过
- **deepface 已有同范式**：`deepface/modules/database/pgvector.py` 用 psycopg +
  pgvector register_vector，`inventory.py` 把它注册为内置库类型——本仓库照搬
  该连接/建表范式即可，不必从零写
- 本机无现成中文 embedding 模型；mono venv 有 onnxruntime 但无 tokenizer

**技术选型（与用户两轮对齐）**：
- 落库式**上向量库**（非临时本地 cosine）：画像向量写 pgvector，触发层语义召回
- embedding 用 **sentence-transformers 生态（bge-small-zh）**：业界标准、中文
  效果好。代价：首次拉 ~2GB torch + ~95MB 模型，之后全离线（用户已接受）
- 独立 `vector` optional-dependency 组：厚重 onnx/embedding 栈不混进核心
  qq/langTrack 门禁环境

**已完成**：
- `pyproject.toml` 新增 `vector` extra：psycopg[binary]/pgvector/transformers/
  sentence-transformers/onnxruntime
- 新增 `src/gacore/embedding.py`：惰性加载 + 线程安全缓存 SentenceTransformer，
  encode/batch_encode/归一化；缺依赖时报 `EmbeddingUnavailable` 而非崩溃
- 新增 `src/gacore/vector_store.py`：psycopg+pgvector 连接（`GACORE_PG_DSN`
  可 override）、`ensure_schema` 幂等建表（UNIQUE(content,chunk_key)）、
  `upsert_line`/`nearby`(cosine 阈值)/`sync_portrait`（batch 全画像入库）
- `memory_maintenance.py`：新增 `VectorTrigger`（语义召回触发，缺后端优雅降级
  为 no-match）；`CombinedTrigger`（keyword OR vector，keyword 优先省 LLM）；
  `maintain_once` 把向量召回行作为 context 注入 judge 的 portrait
- `graph.py` `memory_maintain` 节点：改用 CombinedTrigger，画像**实际更新后**
  best-effort `sync_portrait` 写入向量库（失败绝不影响 turn）
- 新增 `tests/test_vector_trigger.py`（7 例，mock 后端）：VectorTrigger 语义
  命中 / 无匹配静默 / 后端故障降级 / Combined keyword 优先且不触发 vector /
  vector 补语义漏抓。**21 passed**（含 memory_maintenance 全量）
- ruff 零告警

**未完成（待依赖装毕）**：`uv sync --extra vector` 装完 torch 后，跑 embedding
冒烟 + vector_store 对真实画像 sync + VectorTrigger 端到端一轮，确认"搬家到
朝阳区"能召回"家住朝阳"。TODO 已登记待办表。

**偏差说明**：
- 测试中 VectorTrigger 的 `vector_store` patch 目标须为 `gacore.vector_store`
  （函数内是 `from gacore import vector_store`），非 `.memory_maintenance.vector_store`
- `_cursor` / `ensure_schema` 用 psycopg3 + pgvector `register_vector(conn)`
  才允许 python list→vector 参数绑定

**待办更新**：新增「阶段二实测闭环」待办（装完跑语义召回真命中断言）。

### 2026-09-09 — 阶段二实测闭环：本地模型 + 语义召回真命中

**背景**：上条记录的「未完成」实为**网络阻断**——`bge-small-zh` 首次下载需连
huggingface.co，而办公网络外网全不通（系统代理 127.0.0.1:7897 无进程、baidu/HF
均超时），`sentence-transformers` 加载挂起无输出。用户翻墙能力受限，改为**手动
从 hf-mirror 下载完整权重到本地目录**，彻底离线加载。

**已完成**：
- `embedding.py` `DEFAULT_MODEL_NAME` 改为本地路径 `D:\models\bge-small-zh-v1.5`
  （`r""` raw string 防反斜杠转义），加载本地目录免联网、免 HF hub 启动
- 用户下载 13/13 文件齐（`model.safetensors` 91.4MB + pytorch_model.bin 同
  权重 + 分词器 + 全套 sentence-transformers 配置），校验无空文件

**实测验证**（本地离线，真实模型，非 mock）：
- torch+sentence-transformers boot + 模型加载 **~0.0s**（本地 safetensors 秒载）
- `dim=512`，bge-small-zh 中文语义正确：
  | 对比 | 相似度 | 判断 |
  |---|---|---|
  |「搬去朝阳」↔「家住朝阳」 | **0.7006** | 同类居所 ✔ |
  |「搬去朝阳」↔「交房租」 | 0.4859 | 住房主题相关（阈值边缘） |
  |「搬去朝阳」↔「吃饺子」 | 0.4291 | 无关（基线） |
  | 同句自比 | 1.0 | 正确 |
- **结论**：向量触发阈值取 **0.50~0.55** 合适——放行「住朝阳」召回、挡掉「晚饭」
  类无关。阶段二语义召回端到端同网络下全通

**偏差说明**：
- 原计划走 huggingface 自动下载，改手动镜像下载——embedding.py 透明兼容本地目录
- `data/hf_cache`（HF_HOME）未用，无残留下载

**待办更新**：勾选「阶段二实测闭环」。补充待办：向量触发真实阈值调优（rating 标
注）与 `SyncPortrait` 全画像入库实测，留待真实画像数据攒一段后做。

### 2026-09-09 — 阶段二方案2：事实画像（vector 召回质量根治）

**背景**：端到端初测发现 `sync_portrait` 喂**事件日志**（`global_mem*.txt`，106 行带
`[2026-09-08T...]` 时间戳前缀）时向量召回质量差——`附近`查询「搬朝阳」误召回
「婚期定档」(dist 0.478)，且密码「体检/搬家」因画像里没有对应事实句全被阈值挡空。
诊断根因：**入驻行形态不利于语义检索**（事件日志 vs 事实陈述），非阈值能解。用户
选定方案2：**为向量召回调单维护一份"事实画像"**（简短可检索事实句），而非重写画像
生成。

**已完成**：
- 新增 `memory/global_mem_facts.txt`（从真实画像提炼 13 行简短事实：婚姻/居住/作息/
  生日/工作/环境/兴趣/音乐/消费，每行 `[类别] 事实`）
- `vector_store._portrait_lines`：**优先喂 `global_mem_facts.txt`**，不存在时回退
  `global_mem*.txt`（新旧兼融，未 seed 前优雅降级）
- `memory_maintenance.apply`：MERGE/NEW 写盘后 **best-effort 镜像一条简短事实到
  `global_mem_facts.txt`**（`_as_fact_statement`，不带时间戳），向量库近实时跟随
  画像演进；写失败仅告警不影响主 turn
- `nearby` 修 SQL `%s::vector`（psycopg 把 python list 误绑成 `double precision[]`）

**实测验证**（真实 pgvector，非 mock）：
- `sync_portrait` 只喂 13 行 facts（非 106 行日志）
- 召回质量大幅提升：
  | 查询 | 最佳召回 | dist |
  |---|---|---|
  | 婚期是什么时候 | `[婚姻] 婚期 2026-09-12 领证` | **0.344** |
  | 今晚又熬夜到三点 | `[作息] 深夜工作` | **0.320** |
  | 我搬到朝阳区了 | `[居住] 现居南京观云润府` | 0.544 |
  | 我老婆叫什么 | `[婚姻] 配偶：尚婧` | 0.547 |
- 初版事件日志时「搬朝阳」误召回「婚期定档」(0.478)——事实画像根除该噪声
- 21 passed（memory_maintenance + vector_trigger），ruff 零告警

**偏差说明**：
- 方案2 与方案1（重写画像生成）对比：改动小、见效快，且 facts 与事件日志并存互不
  干扰；日后画像生成升级为"可检索事实句"时，facts 文件即天然落点
- FTS/bge 对带 `[类别]` 前缀的事实句编码良好；「房贷」等未覆盖事实句的查询阈值边缘
  （0.54）待真实画像充实后再调

**待办更新**：勾选「阶段二实测闭环」中 sync_flags 入库部分。补充待办：真实阈值调优
与 `global_mem_facts.txt` 随画像持续充实（10 条核心事实锚点 vs 事件日志全量），
留待真实数据攒一段后做。

### 2026-09-09 — 统一记忆写入口：向量同步收口 persist_entry（修复主动写不漏同步）

**背景**：方案2 落地后发现架构裂缝——记忆有**两条写路径**：
1. 被动节点 `memory_maintain`（graph.py）
2. 主动工具 `start_long_term_update`（memory_tools.py）

向量同步只挂在被动节点的 `_sync_portrait_best_effort` 上，**主动工具写记忆根本不同步
向量库**（只写 txt，不镜像 facts、不写 pgvector）。用户指出：新增/修改长期记忆时
向量库会滞后。按用户确认的**方案2**（抽统一写函数）根治。

**已完成**：
- 在 `memory_maintenance` 抽 `persist_entry(cfg, *, fact_line, insight_line,
  facts_statement, sync=True)`：写 `global_mem.txt`+`insight`+`facts` 镜像，再
  best-effort `_sync_vector_store`（`ensure_schema`+`sync_portrait` 幂等全量重嵌）。
  写失败与同步失败皆吞掉，txt 仍是真相源，下次 sync 自愈。
- `apply()` 改为委托 `persist_entry`（返回结构不变）；`start_long_term_update` 也改
  委托它（获得 facts 镜像 + 向量同步能力），返回值由 `global_mem+insight` 变为
  `global_mem+insight+facts`。
- 删除 graph.py 节点层 `_sync_portrait_best_effort`（同步收口到 persist_entry，避免
  被动节点重复全量重嵌）。
- memory_tools 清理不再使用的 `_FACTS_FILE`/`_INSIGHTS_FILE`/`Final` 导入。

**实测验证**：
- 34 passed（memory_maintenance + vector_trigger + tools_memory + nodes_tools），
  ruff 零告警。
- 真实 e2e：调 `start_long_term_update` 写「八段锦」→ 返回 `global_mem+insight+facts`，
  向量库 `stored: 13` 立即刷新，查「我最近在干嘛/八段锦」能召回相关画像——
  **主动写路径不再漏同步**。

**偏差说明**：`persist_entry` 仍是 txt 为真、向量为索引的架构；全量重嵌 13 行很便宜，
暂不做增量同步（画像数据量级足够轻）。

**待办更新**：勾选「统一记忆写入口」为已完成。新增待办：确认 scheduler 日报沉淀路径
（批量 batch 写）是否也应经 `persist_entry` 收口，避免第三条写路径再漏同步。

### 2026-09-09 — 分层记忆：Semantic + Episodic 双表并行召回（日报要点入向量库）

**背景**：用户确认两级需求——①日报生成后，把 daily note 要点向量化存入**独立的 episodic 表**；
②对话时**并行召回**长期画像（semantic）+每日情景（episodic）两表，合并为 RAG 上下文注入。目标：
把日报沉淀为"当日人物画像集合"并作 RAG 材料，聊天时通过向量匹配召回"那天发生了什么"。

**已完成**：
- `vector_store.py`：
  - 新增 `gacore_episodic_vectors` 表（`UNIQUE(content, day)`）+ `ensure_episodic_schema` /
    `upsert_episodic` / `nearby_episodic` / `sync_episodic_daily`。
  - `nearby_episodic` 支持 `day_from/day_to` 日期窗口过滤（元数据过滤+向量检索的时间感知召回）。
  - `daily_notes_for(cfg, day)` 抽日报要点并**剔除调度审计噪声**（`[scheduled:...]` 行）与 Markdown 标题。
  - `recall_context()` 并行召回两表，合并为 `[长期画像·语义]` + `[某天发生·情景]` digest，异常吞空。
- `scheduler.py`：`run_job` 日报生成**成功**后调 `_sync_episodic(cfg, for_day)` 自动向量化日报要点，
  失败仅记日志不阻塞投递。
- `context.py`：`_rag_recall_block` 按最近用户文本以近 90 天窗召回，注入 `build_system_prompt` 增广 RAG 背景。

**修复**：`nearby_episodic` SQL 参数顺序错（threshold 被误转 vector）；`daily_notes_for` 噪声过滤不彻底。

**实测验证**：ruff 零告警；pytest 57 passed；真实 pgvector——episodic 建表 + 9-08 日报 18 条入库
（stored:18）；查「领证」并行命中 semantic（婚期定档）+ episodic（9-08 补证/寻证）；2099 空窗
episodic 零命中而 semantic 不受影响（两表隔离 + day 过滤正确）。

**偏差说明**：日报写路径仍走**独立** `_sync_episodic` 汇流点、未并入 `persist_entry`（批量 batch 写 +
日报更新频格，与单条 MERGE/NEW 不同），是否统一留待评估。scheduler 重启前未用真实日报 job 触发，仅直连
调用验证，真实调度触发日志待补。

**待办更新**：新增待办——日报路径是否收口 persist_entry（**决策：不并入**，各管各表、共享 vector_store
工具，见 docs/langTrack-roadmap.md「架构决策」）；episodic 90 天窗/阈值长期观测调优（调法见 tech.md
「调优参数一览」）；scheduler 重启后真实日报 job 验证 `_sync_episodic` 日志落盘。

### 2026-09-11 — 日报反馈闭环（QQ 订正 + LLM 主持澄清 + 分批重发）

**背景**：用户此前指出自动日报存在内容不准或缺漏，但无反馈渠道。需求几轮收敛为：
①QQ 直连回指任一分节条目订正/补充；②信息不全时由 LLM 主持式追问（不是机械填空）；
③多笔修改各自确认、**只写真相源不逐笔重发**，末笔「确认重发」统一重发一次。

**已完成**：
- 抽取新增 `feedback.py`（纯逻辑、依赖轻）：`parse_feedback` / `is_feedback_intent` +
  新增 `feedback_route`（edit/confirm/redeliver 三态路由）、`analyze_feedback`（五个维度
  确定性守卫）、`merge_context` / `draft_from_context`（跨轮澄清累积）、`confirm_feedback`、
  `redeliver_day` / `redeliver_latest`（幂等分批发，ledger 去重）。
- `apply_feedback` 改为**只写真相源、不内嵌重发**（原 `_redeliver` 删除）；删改后上报有
  `logs/delivered_report/{date}.md` 真相源（scheduler 日报成功即 `save_delivered` 落库）。
- QQ 前端 `qq.py`：`on_message` 在闲聊图前插入反馈路由（含「澄清会话中任何消息都回流」），
  新增 `_handle_feedback` 处理器，`get_llm([], bind_tools=False)` 直调 LLM；跨轮分片
  `_feedback_sessions`（RAM only）。
- LLM 主持式澄清 `clarify_feedback`：吸收开源 Rich-Elicitation 的提问纪律（≤3 题/轮、
  语境化推荐项并标单个(推荐)、按组、智能停止），失败回退确定性追问。

**实测验证**：
- `test_feedback.py` **33 passed**（新增 analyze/merge/draft/route/batch 幂等/clarify 成功与
  回退），ruff 三个改动文件零告警。
- 全仓 pytest **1076 passed, 1 skipped**（2 失败均在无关既有 langTrack 场所异常文件；
  5 处 lint 错误在无关既有 test_memory_maintenance.py，均非本次引入）。

**偏差说明**：
- 澄清会话期间用户任何普通消息都会并入反馈（避免答复无关键字被吞），以会话存在与否判定——
  轻微抢占闲聊，接受该取舍（`_handle_feedback` 只向澄清方向回流，不跑闲聊图）。
- 未移除 2 处既有 langTrack 失败与 test_memory_maintenance 的 F401 存量 lint（超出本次范围）。

**待办更新**：新增——QQ 真实环境联调一轮（澄清→确认→确认重发）；两段确认 if 中间态文案
（反馈 ID 短 hash）与「确认」即生效的取舍待用户实测反馈；`feedback.py` 后续可考虑并入
`redeliver_latest` 的按日批量聚合重发阈值观测。

### 2026-09-11 — 日志跨天轮转收尾 + langTrack 时间依赖测试修复

**背景**：补上日报反馈闭环落地当天的两件收尾——①前端订阅队列日志仍钉进程启动日，
需按实际记录日跨天轮转；②`detect_anomalies` 依赖系统当前时间导致测试不稳定
（随跑测时刻漂移）。

**已完成**：
- `jsonl_logger.py` 新增 `_DailyFileHandler`：按每条 `LogRecord.created` 推算发出日，
  `logs/{发出日}/app.jsonl` 落盘，跨午夜自动切新文件（线程安全），不再钉进程启动日。
- `etl.detect_anomalies` 增加 `now_ms: int | None` 注入参数，兼容原有行为；
  `test_langTrack_report_evidence.py` 两处探测用例改为注入固定时间戳。
- 根 `.gitignore` 追加 `.trae-html-share-packages/` 与 `**_shared/`（TRAE html-share
  运行时生成产物，非源码）。

**实测验证**：
- `test_jsonl_logger_daily.py` 新增 3 用例：跨天轮转 / 同日追加 / 记录带格式化 ts，全过。
- 全仓 pytest **1080 passed, 1 skipped**（1 skipped 为既知上游 qq 角色卡切换）。

**偏差说明**：
- 日志轮转仅覆盖审计日志流（app.jsonl 等 Handler 族）；若未来引入需跨天聚合的独立写入，
  沿用同一 Handler 即可，未另抽基类。

**待办更新**：无新增。

### 2026-09-11 — 历史日报正片补种 + 多份只留最新（覆盖式）

**背景**：日报反馈闭环的真相源 `logs/delivered_report/{date}.md` 是 2026-09-11 才接入
`save_delivered` 开始落库的；更早的日报只以完整运行归档存在 `logs/scheduled/` 下，
反馈循环对这些历史日没有可读可改的真相源。用户要求把历史日报统一补种出来，且**同一天
多次运行只留最新一份，用覆盖式**（补跑/多次生成以最新为准，不做融合）。

**已完成**：
- 新增 `tools/scripts/backfill_delivered.py`：遍历 `logs/scheduled/daily-report_*.md`
  归档，按文件名日标记分组 → 剔除失败/空跑（error!=none 或 Reply 为空）→ 每天**只取最新
  一份有效正文**（按 mtime），复用 `_sanitize_reply` 剥离 `<summary>` 等 DSL 残迹，
  经 `save_delivered` 打节锚点并**覆盖写入** `logs/delivered_report/{date}.md`。
- 跨天补跑矫正：正文含「补跑·YYYY-MM-DD」或「信息包：YYYY-MM-DD」标记时，改归入其实际
  覆盖日的真相源，避免并入文件名标记日。

**实测验证**：
- 实跑补种 **25 天**写入 delivered_report，2 天（09-03/09-04）无有效正文被跳过；
  输出与 `--dry-run` 完全一致，可重复执行（覆盖语义幂等）。
- `load_delivered` 读回最新一份（2026-09-10）正常，bullet 均已打 `[节-N]` 锚点
  （如 `[今日状态-1]`、`[工作日志-2]`），反馈可据此定位订正。
- `ruff check tools/scripts/backfill_delivered.py` 零告警；`test_feedback.py` 35 passed。

**偏差说明**：
- 覆盖范围为历史归档日（补种产物，可再生成）；2026-09-11 后由运行期 `save_delivered`
  实时产生的真相源不受影响（脚本只扫 scheduled 归档日的文件，且最新原则即运行期行为）。
- 09-03/09-04 无有效正文（两次运行均为失败/空跑），维持无真相源状态。

**待办更新**：无新增。

### 2026-09-11 — RAG 召回基线（只读打点，为 query 改写决策铺数据）

**背景**：评估在 `context.py::_rag_recall_block` 检索前加 LLM 改写（`rewrite_for_recall`）前，
需要先量化现状——「真实用户轮次里，向量召回到底注入了多少次、带回多少命中、相似度多高」，
避免往一个经常注空的管道里做无谓改动。要求只读、零依赖、完全不碰生产逻辑。

**已完成**：
- 新增 `src/gacore/recall_baseline.py`（`python -m gacore.recall_baseline --days N`）：纯 stdlib 回放
  `logs/*/llm_requests.jsonl`，识别生产系统提示词里的 `=== 向量召回记忆 ===` 头，统计
  逐条 invoke 的用户 query（兼容 LangChain 序列化的 `human` 角色）、是否注入、语义/情景命中数与
  `(sim x.xxx)` 相似度，输出 JSON 报告 + 人类可读摘要到 `data/`（gitignore，不入库）。
- 新增 `tests/test_recall_baseline.py`（6 用例）：解析去图片标记、命中计数、去重聚合、注入率均过。

**实测验证**（`--days 7`：2026-09-05~09-11，204 条 LLM 调用）：
- 去重后真实用户查询 15 个（gate 全过）；**注入 5 个 / 空 10 个 → 注入率 33.3%、空返回率 66.7%**。
- 注入时平均带回 3.0 条事实（语义 27 + 情景 25），**sim 仅 [0.502, 0.661]、均值 0.574**，贴近 0.5 阈值。
- 按天：09-08 注入 0/5（全空），09-09：1/5，09-10：3/4，09-11：1/1。空返回 query 中多为本条事件类
  提问（如「领结婚证的日子改了 九月十二」「尚婧身份证弄丢了…领证」），而注入样例里恰好含「婚姻·登记
  日期改为 2026-09-12」事实——提示不少空返回是「该召回却没召回」。
- `ruff` 零告警；`test_recall_baseline.py` 6 passed。

**偏差说明**：
- 「空返回」≠ 100% 命中失败：部分 query 与长期记忆本就无关（情绪化表达/工具类请求），空注入是正确结果；
  真实 miss 率需人工复核相关性，或下一步对空返回项做 DB 回查 + 改写 A/B 才能定。
- 时间窗仅 7 天、样本 15 个查询，统计显著性有限；个别空返回可能是「该时刻记忆尚未写入向量库」（写入时间
  晚于提问），而非召回失败。注入只出现在 09-08 之后，早于该日期的窗口（阶段三上线前）应排除在公平基线外。

**待办更新**：
- [ ] 抽取「空返回」示例 query 对实际 pgvector 库回查，区分「该召回未召回」vs「确实无需召回」。
- [ ] 以同为门的 query 做改写 A/B（raw vs LLM 改写），对比注入率 / 命中条数 / sim，验证改写收益后再决定是否落地。

### 2026-09-11 — 结构化召回/改写日志 + 改写 A/B 脚手架

**背景**：用户认可基线 report 的结构，希望「向量库召回/改写」的日志今后就用这种结构化格式
落盘，便于后续统计、维护、离线测试；并在基线之后做改写 A/B 判断 `rewrite_for_recall` 是否落地。

**已完成**：
- 新增 `src/gacore/recall_log.py`：JSONL 结构化日志层（`logs/<day>/recall.jsonl`），每条一个
  recall 事件，schema 含 `variant(raw/rewritten)`、`input_query`、`query_used`、`rewrite`、
  `semantic[]/episodic[]`（`sim=1-dist`）、`injected`；提供 `RecallLog` 写入/`iter_records`
  读取/`summarize` 聚合，全部纯 stdlib、可离线测试。
- 新增 `src/gacore/recall_ab.py`（`python -m gacore.recall_ab`）：从基线日志取真实 query，
  用 deepseek 改写后对**活 pgvector 库**分别做 raw/rewritten 召回对比，两条结果都写结构化日志。
- 新增 `tests/test_recall_log.py`（6 用例）+ `tests/test_recall_baseline.py`（7 用例），13 passed。
- 环境事实查明：现有 conda env /`.venv` 均未装 `vector` extra（psycopg/pgvector/sentence-transformers），
  起初 `recall_context` 属静默 no-op；postgres 服务本身在 5432 正常。轻件 `psycopg[binary]+pgvector`
  已装入 `py12`，验证 `vector_store._connect()` 可读 `gacore_memory_vectors`(132 行) /
  `gacore_episodic_vectors`。bge 模型在本地缓存 `D:\models\bge-small-zh-v1.5`，无需下载。

**实测验证**（`python -m gacore.recall_ab --subset all --max 8`，对活 pgvector 库重放 8 个真实 query）：
- 环境就位：复用 conda `base`（已有 torch 2.11-cpu）+ 补 `sentence-transformers`/`pgvector`；bge 512 维载入 OK，无重下 torch。
- 口径修正：**把旧 query 对「今天的库」重放，raw 8/8 全部注入、avg max-sim 0.764（0.59~0.83）**——一旦事实已入库，`recall_context` 的向量召回本身很强。
  基线里「66% 空返回」主因是**写入时机**：婚姻/身份证等事实在提问数小时后才同步进向量库，非检索失败。
- 改写对比：rewritten 仅 5/8 注入（3 次 deepseek 空响应失败），avg max-sim 0.714，**未超过 raw，且因改写失败反降了注入可靠性**。
- 唯一明确收益在 query「找不到了翻遍了也找不到」：raw 召回 langTrack 无关内容，改写后命中「尚婧身份证遗失当晚翻遍家中」。
- 结论：对本存储/本嵌入，query 改写在聚合口径上是**弱/负收益且脆弱**；更高杠杆是**记忆写入及时性**，而非改写。

**待办更新**：
- [x] sentence-transformers 就绪，recall_ab 真 A/B 已跑。
- [ ] 不建议仓促接入生产改写；优先核实并缩短「事件发生→向量库同步」的写入延迟。
- [ ] 保留 `recall_log` + `summarize` 作为长期召回/改写统计与回归测试底座（已落 `logs/<day>/recall.jsonl`）。

### 2026-09-11 — 清理迁移遗留的包级 `.venv`，统一到 mono 单一根环境

**背景**：审查 monorepo 环境模型时发现 `apps/withlanggraph/.venv` 是从原独立仓库迁入时带进的遗留
产物——uv workspace 本就共用仓库根一份 `.venv` + 一份 `uv.lock`，包级 `.venv` 会造成环境分叉：
在里面手工 `pip install` 会绕过 `uv.lock`，踩中 R2「顺手 pip install 不落盘」。AGENTS.md 中
「双环境：langTrack 用 `.venv`、gacore 用 py12」的旧描述均为迁移前过期说法。

**已完成**：
- 删除 `apps/withlanggraph/.venv`：确认它为 uv 创建的空壳（17 文件 / 0.48MB，Python 3.13.3，
  `prompt=withlanggraph`），无任何进程在用，git 早已忽略（`git check-ignore` 命中），删除后无残留跟踪。
- 修正 `AGENTS.md` 三处过期描述到 mono 单一根环境：① 踩坑记录第 14 条「双环境/py12」→「mono 单一
  根环境，经 `uv run` 调用」；② 测试命令 `& miniconda\py12\python.exe` → `uv run pytest`；
  ③ ETL/逆编码/标签命令 `python -m` → `uv run python -m`。

**实测验证**：
- 清除后 `apps/`、`packages/` 下已无任何 `.venv`，仅剩仓库根 `.venv`。
- 根环境 `import gacore / langgraph / langchain` 全部 OK；旧 `.venv` 装不上的 langchain/langgraph/
  pygraphviz 问题在 mono 根环境（uv.lock 统一解析）不成立。

**待办更新**：
- [x] 删除遗留包级 `.venv`，统一走 mono 根环境。
- [ ] 历史文档（roadmap/tech 执行记录）里的 `.\\.venv` 命令字样为当时实测记录，留作历史，不改写。

### 2026-09-12 — 日报 git 提交：按仓库分组 + 纳入 weiCheckApp

**背景**：日报「当日 git 提交」此前只对 `cfg.root`（mono 所属仓库）跑 `git log`。用户主开发仓库有
两个——`mono` 与 `weiCheckApp`（均在 `D:\AAAmyPrj\github\myrepos\` 下），希望日报同时收录两个仓库的
当日提交。此前 git 仓库路径不可配置（硬编码 `_PROJECT_ROOT` 推算）。

**已完成**：
- `config.py` 新增字段 `extra_git_repos: tuple[Path, ...]`，从 `GACORE_EXTRA_GIT_REPOS` 解析
  （分号分隔路径，绝对路径原样、相对路径基于仓库根）；未设置时默认为空，行为与旧版完全一致。
- `daily_info_pack._build_git()` 改为遍历 `[cfg.root, *cfg.extra_git_repos]`，每仓库跑 `git log`，
  非空提交按 `【仓库名】` 分组输出；仓库名取最近含 `.git` 祖先的目录名（`_repo_label`）。
  单仓库无提交则跳过该组，全部为空返回「今日无 git 提交」；某仓库读取失败单独标注，不中断其它。
- `.env` 设置 `GACORE_EXTRA_GIT_REPOS=D:/AAAmyPrj/github/myrepos/weiCheckApp`；`.env.example` 补键说明。
- `_GIT_CAP` 700 → 1200（容纳两个仓库）。
- 新增单测 `test_git_multirepo_groups`（多仓库分组输出）；既有 3 条 git 测试因默认单仓库兼容不改。

**实测验证**：
- `uv run ruff check apps/withlanggraph/src apps/withlanggraph/tests` 通过。
- `pytest apps/withlanggraph/tests/test_daily_info_pack.py` 32 passed。
- 真实 `_build_git`：mono 当日 8 条按 `【mono】` 输出；9-08 两仓库提交分别出 `【mono】` / `【weiCheckApp】`。

**待办更新**：
- [x] 日报 git 提交支持多仓库并按仓库分组。
- [x] weiCheckApp 纳入日报 git 来源（经 `GACORE_EXTRA_GIT_REPOS`）。
- [ ] 后续新增主开发仓库，只需在 `.env` 的 `GACORE_EXTRA_GIT_REPOS` 增补路径即可。

### 2026-09-12 — 召回链路基线 + Query Rewrite A/B + vector 环境就绪

**背景**：探讨「用户不会提问 → Query Rewrite」时，先量化现状召回是否真是瓶颈。线上 09-08~09-09
期间 `_rag_recall_block` 大量「空返回」，需区分是「用户 query 太模糊」「向量召回弱」还是「事实未及时
入库」，避免凭空接入改写。

**已完成**：
- `recall_log.py`：结构化召回/改写日志层，`logs/<day>/recall.jsonl` 每条一事件，字段含
  `variant(raw/rewritten)`、`input_query/query_used`、`rewrite`、`semantic[]/${episodic[]}(sim)`、
  `injected`；提供 `summarize` 聚合。为后续统计/维护/回归的日志底座。
- `recall_baseline.py`：只读基线回放日志（`--days N`），零依赖，不写生产。
- `recall_ab.py`：A/B——取真实 query → LLM 改写 → 对活 pgvector 库分别按 raw/rewritten 召回，
  双份都落结构化日志。配套 `tests/`（13 用例全绿，ruff 通过）。
- 首轮基线（近 7 天）注入率 33.3%（5/15）；但逐条复核发现多数「空返回」是「事实当晚批量同步才入库、
  提问时库中尚无数条 或 与长期记忆本就无关」，非检索失败；真正问题反而是 5 条注入里 4 条跑题/弱相关。
- 重放 A/B（8 条真实 query，对已写满的库）：raw 召回 8/8 命中、avg max-sim 0.764；改写后 5/8、
  0.714。结论：`bge-small-zh` + `recall_context` 召回本身健康，Query Rewrite 在现存储下是弱收益且脆弱
  （3 次改写返回空反降注入率），不建议仓促接入生产。

**实测验证**：
- mono 根 uv 环境安装 `[vector]` extra（清华镜像）：psycopg 3.3.5 / pgvector / transformers 5.17 /
  sentence-transformers 6.0.1 / torch 2.14.0+cpu / onnxruntime 1.29.0 全部可 import。
- `recall_context('领证日期 结婚登记 婚期 最近定了哪天')` 在正式环境实测命中 sim 0.795，
  成功召回「婚期改到 09-12」——正是当初 09-08 口语 query 查不到的那条，复核 A/B 结论成立。
- 结构日志 `logs/2026-09-11/recall.jsonl` 16 条（raw+rewritten 各 8）已写入，可经 `summarize` 聚合。

**偏差说明**：
- `embedding.py` 的 `get_sentence_embedding_dimension` 在 ST 6.0 下触发 FutureWarning（重命名告警），
  仅提示性，不影响功能。

**待办更新**：
- [x] 量化现状召回（baseline），判断 Query Rewrite 是否值得接入。
- [x] 结构化召回/改写日志底座（`recall_log.py`），便于日后统计维护。
- [x] mono 正式环境补齐 `vector` extra，语义召回可跑通。
- [ ] 高杠杆待办：把 `_sync_vector_store` 由「全量重嵌入」改为「增量同步新增/变更行」，缩短
      「事件发生 → 向量库可见」从当天晚上批量缩到判定当刻（需走 R7 review-loop，动生产链路）。
- [ ] 消除 vector sync 静默失败：缺失 extra / 异常时应告警或统计（可接 `recall_log`）。
- [ ] 考虑让召回/改写日志作为长期统计与回归测试的数据底座（已具备 `summarize`，未接入 CI）。

### 2026-09-12 — 向量同步改两步式：`sync_line` 单行增量即时入召回（步骤1）

**背景**：改写 A/B 证明召回链路本身健康（`bge-small-zh` + `recall_context` 命中 raw 8/8、sim 0.59~0.83），
真正短板是把「事件发生 → 向量库可见」拉长的批量写时滞。按既定三步骤，先落地力度最小、不破坏现有
全量幂等的第 1 步：全量兜底 + 当次单行增量，让刚判定的事实当刻即可被语义召回，不必等下一次全量。

**已完成**：
- `vector_store.py` 新增 `sync_line(content, chunk_key=None)`：单行增量 batch 嵌入 + upsert，幂等去重
  （`ON CONFLICT DO NOTHING`），chunk_key 缺省按 `_source_of` 推断（insight/fact）。
- `memory_maintenance._sync_vector_store(cfg, extra_line=None)` 由「全量 `sync_portrait`」改为两步：
  `ensure_schema` + 全量 `sync_portrait`（兜底健康全表）+ 当次 `sync_line(facts_statement)`（刚写那条立即可召回）。
- `persist_entry` 在 `sync` 时为 `_sync_vector_store` 传 `extra_line=facts_statement or None`；无 statement 时仅全量，
  行为与旧版一致。向量同步失败仍被吞（best-effort，txt 为真相源），绝不带崩写动作。
- 单测 +3（`tests/test_memory_maintenance.py`）：增量下传、无 statement 跳过增量、后端异常不中断写路径；
  mock 采用 patch `gacore.vector_store` 包属性（`_sync_vector_store` 内 `from gacore import vector_store`）。

**实测验证**：
- `test_memory_maintenance.py` 18 passed；`test_recall_log.py` 24 passed（合计，含 embedding 冷启动）。
- `uv run ruff check src tests` 通过。
- 正式库真实验证：`sync_line('ZZ_TESTMARK_20260912_量子球…')` → `stored:1` → 立即 `nearby` 命中刚写行，
  耗时 0.69s ——「当刻写入 → 立即可召回」达成。
- 环境修正：此前 root venv 里 `sentence-transformers/transformers/torch` **实际未落 site-packages**（曾误判
  "六件套 OK"，实为 `find_spec` 在 resolver 层命中）。已用 `uv sync --package gacore --extra vector`（仓库根）
  正式落入 root venv，`st.__version__==6.0.1`、torch 2.14.0+cpu。

**偏差说明**：
- 未做情景（episodic）实时触发（既定步骤2，动调度热链路，未在本提交触碰）。
- 未做缺 extra 告警化（既定步骤3，接 recall_log，留待下步）。

**待办更新**：
- [x] 步骤1：语义画像全量 + 增量两步式，刚写事实当刻可召回。
- [ ] 步骤2：情景事件触发式实时补写（`sync_episodic_daily` 由纯调度改为判定当刻触发，需走 R7 review-loop）。
- [ ] 步骤3：消除 vector sync 静默失败（缺 extra/连不上 → recall_log 记 failed 或告警）。
- [ ] 观察「事件发生 → 向量库可见」延迟是否从"当天晚间批量"缩到"判定当刻"（对比 baseline）。

### 2026-09-12 — 消除 vector sync 静默失败：失败事件记入 recall 日志（步骤3）

**背景**：改写 A/B 期间发现根因之一：向量栈缺失 / pg 服务掉线时 `_sync_vector_store` 的异常被
`except Exception: logger.warning` 吞掉，只在 info 级留一行 warning，召回整天空返回却无从统计、
无告警、难以发现——「静默降级」是比改写更隐蔽的坑。步骤3 把这一处吞异常变为**可观测**。

**已完成**：
- `recall_log.py` 新增 `build_sync_failure(...)`：纯函数构造 vector-sync 失败事件，
  `event:"sync_failure"` 标签 + `ts/day/step/error_type/error/extra_line`，与召回事件共用同一
  每日 JSONL（`logs/<day>/recall.jsonl`），可 grep / `iter_records` 统计。
- `memory_maintenance._sync_vector_store` 失败分支追加 `_record_sync_failure(...)`：
  把 `type(exc).__name__` / str(exc) / 失败步（`sync_portrait`）/当次 extra_line 记入
  `cfg.logs_dir`。成功时零开销（try 内不产生记录），失败不再"silently off"。
- `_record_sync_failure` 自身 best-effort：连记录都失败（logs 不可写等）只降 warning，绝不二度
  干扰记忆写热路径（`persist_entry` 仍返回成功，txt 为真相源不变）。
- 单测 +4（`build_sync_failure` 形状/落盘 +2 收 recall_log；`_sync_vector_store` 失败落盘 +1、
  记录自身失败也不中断写 +1）。

**实测验证**：
- `test_memory_maintenance.py` 20 passed；`test_recall_log.py` 8 passed（合计 28）。
- `uv run ruff check src tests` 通过。
- 集成验证：patch `sync_portrait` 抛 `RuntimeError("pg down")` → `persist_entry` 仍 `updated:True`，
  且 `cfg.logs_dir` 下 `recall.jsonl` 出现 1 条 `event:"sync_failure"`（含 error_type/step/extra_line）。

**偏差说明**：
- 未接告警通道（仅落地结构日志；后续可基于 `sync_failure` 计数做阈值告警，本次不扩）。
- 情景（episodic）实时触发（步骤2）仍未动。

**待办更新**：
- [x] 步骤3：vector sync 失败记入 `recall.jsonl`（`event:"sync_failure"`），消除静默降级盲区。
- [ ] 基于 `sync_failure` 事件做阈值告警 / 统计（如"缺 vector extra 半天内 N 次"）= 步骤3 延展。
- [ ] 步骤2：情景事件触发式实时补写（`sync_episodic_daily` 由纯调度改为判定当刻触发，需 review-loop）。

## [2026-09-29] langTrack 历史日"无数据"假阴性 → 补建兜底

**背景**：2026-09-28 定时日报 + 09-29 凌晨补跑，两份日报的〔手机使用·langTrack〕均报"该日无 langTrack 手机数据"。排查发现手机数据**从未丢失**（`daily_stats` 9-28 行 `created_at=2026-09-28`、`total_screen_ms≈12.78M`≈3.55h；手动调 `langTrack_stats("2026-09-28")` 稳定返回 `available=true`、哔哩哔哩 1.4h/微信/抖音）。根因方向：ETL 全量/增量重建窗口 + 读取时序，使日报构建当时读到 `available=False`，把"该日未来得及汇总"误判成"无数据"。

**已完成**：
- `tools/langTrack_tools.py`：
  - `_ensure_etl` 捕获 `subprocess` 退出码，非 0 时记 warning（原静默吞返回 True）。
  - 新增 `_has_day_events(conn, day, device_id)`：按设备/日窗口（ts 毫秒，东八区当日边界）探测 events 表当日是否确有原始事件。
  - `langTrack_stats` 读取后若 `available=False` 且非多设备歧义，先探测当日是否有事件；有 → 触发 `_ensure_etl()` 重建并重读，避免假阴性；无 → 保持如实 `available=False`（真空日不被掩盖）。
- 文档：`docs/langTrack-tech.md` §5.1 补历史日兜底说明（R5 同步）。

**实测验证**：
- `langTrack_stats` 语义：9-28 `available=true`（屏幕3.55h/哔哩哔哩/微信）；9-29(今日) `available=true`（0.37h 实时）；2025-01-01 真空日 `available=false`（不误触发补建）。
- `_has_day_events`：9-28=device True、2025-01-01=False、设备限定匹配主设备。
- `uv run pytest apps/withlanggraph/tests -k "langtrack or langTrack or fact_card or daily"` → **527 passed**；`uv run ruff check` 通过。

**偏差说明**：
- 未复现日报当时的确切并发场景（现库已健康），兜底为防御性修复；若再现假阴性，需进一步确证 ETL subprocess 与读取的时序细节。
- `_ensure_etl` 仍为全量 ETL（幂等、几秒），未改 incremental 以避免历史日漏重建。

**待办更新**：
- [ ] 观察后续 2-3 天日报，确认手机数据不再出现假阴性。
- [ ] 关联：B 站历史源主线（`bili` CLI 未登录）待 `bili login` 后验证。

## [2026-09-30] 日报重复投递 + 格式不一致（模型自调 send_email）修复

**背景**：2026-09-30 凌晨补跑 9-29 日报，用户收到**两封**邮件：
1. 早封（约 00:24）：模型在 `send_email` 因 SMTP_TO 未配置失败后**自主排查并自行显式传 `to` 发信**——主题自拟「日报 09-29 | …」、正文**未打节锚点**；
2. 晚封（约 00:26）：系统 `_deliver_email` 标准投递——主题 `[gacore] daily-report · 2026-09-29（补跑）`、正文已 `stamp_report_bullets` 打锚点。

两封格式不一致（用户截图确认第一封格式有问题）且造成重复投递。根因有二：① `.env` 的 `SMTP_TO` 一直未开启，导致系统投递实体第一次发信缺失默认收件人；② 日报 prompt 未约束模型投递职责，模型在发信失败后自行补发。

**已完成**：
- `config/schedule.json` 日报 prompt 的【最终回复】节新增硬约束：「投递由系统负责，只产出正文，禁止自行调用 send_email 发送日报（系统会统一打锚点并以标准主题投递，你发信会造成重复投递与格式不一致）」。
- `.env` 开启 `SMTP_TO=1773465183@qq.com`（默认收件人；`.env.example` 键早已存在，R9 合规，未改）。

**实测验证**：
- `schedule.json` `ConvertFrom-Json` 校验通过（JSON 合法）。
- 意义：此后发信失败不再触发模型自主补发；收件人解析链路 `job.email_to → SMTP_TO → SMTP_USER` 首跳即命中。

**偏差说明**：
- 模型自主发信是 LLM 副产物，prompt 约束为软约束（不能 100% 保证模型遵守），真正兜底是 SMTP_TO 提前配置消除失败诱因；两者互补。
- dev-console 需重启加载新配置（services.json 改动），gacore 需重启令 `.env` 生效——均已在 dev-console 操作记录覆盖。

**待办更新**：
- [ ] 观察今晚 23:50 例行日报，确认只投递一封、主题与锚点格式正确。
- [ ] 若再出现模型自行发信，考虑在工具层面对日报/bypass 场景禁用 `send_email`（超出当前 prompt 层的马力）。

## [2026-09-30] dev-console langtrack 卡片「打开」按钮

**背景**：langtrack 已有 dashboard 前端（`GET /dashboard`，采集覆盖卡），但 `services.json` 未配 `frontend` 字段，dev-console 界面上 langtrack 卡片无「打开」入口。

**已完成**：
- `apps/dev-console/services.json` 的 `langtrack` 服务补 `"frontend": "http://127.0.0.1:8000/dashboard"`。
- 机制零改动（dev-console 早已支持 `frontend` 字段 + running 时显示「打开」按钮），仅补齐 langtrack 数据。

**实测验证**：
- `services.json` JSON 解析合法；重启 dev-console 后 `GET /api/status` 返回 `langtrack.frontend=http://127.0.0.1:8000/dashboard`、`running=True`，按钮显示且可点。
- gacore（无 HTTP 端口）与 py-wei（`--no-dashboard`）未填 `frontend`，避免出现不可点击的按钮。

**待办更新**：无。

## [2026-10-03] 测试封闭性修复：切断单测对真实外部资源的依赖（10 分钟 → 2.5 分钟）

**背景**：用户反馈全仓 pytest 慢（两次实测 615s / 318s，波动大），预期 3 分钟内。`--durations` + cProfile 定位：80% 耗时集中在 13 个测试，且根因不是测试冗余，而是单测偷偷触碰真实外部资源。

**根因（三处）**：
1. `test_proactive_p2.py::test_emotion_considered_logs_concern_due`（451 行）漏 mock `_headless_run`（同文件兄弟测试 319/332/347/363 行均有）→ 真实 build_graph + 27 工具发起真实 LLM 网络调用，单条 73s；416/429 两个测试只因 job guard 提前拦截才侥幸没踩。
2. `test_scheduler.py` TestRunJob/Retry/DeliverRouting 共 11 个用例的 job 名叫 `daily-report` → `run_job` → `_build_job_prompt`（scheduler.py:318，按名字嗅探 `"daily" in job.name`）→ 真实 `build_info_pack`：bili CLI 真实网络请求（11.5s，7 个子进程）+ 真实 Edge 历史库查询（7.5s），单条 23s。
3. `test_run_job_writes_daily_note_bullet` 成功路径 → `_sync_episodic` → 真实加载 100MB bge embedding 模型（pytest 下冷加载 47s）；`test_daily_info_pack.py::test_edge_db_locked` 只 mock 了 Edge，bili/langTrack（ETL 子进程）/ncm 全部真跑（16.5s）。

**已完成**：
- proactive 451 行测试补 `_headless_run` + `recall_topic` mock（照抄兄弟测试模式）。
- `test_scheduler.py` 新增 autouse fixture `_no_real_side_effects`：统一拦 `build_info_pack`（返回空串）与 `_sync_episodic`（no-op）；需要真实内容的用例自行再覆盖 seam。
- 新增接线测试 `test_daily_job_success_syncs_episodic` 钉住"日报成功后必调 episodic 同步"（此前 `_sync_episodic` 零测试覆盖）。
- `test_daily_info_pack.py::test_edge_db_locked` 补 mock `_BILLI_FN`/`_LANGTRACK_FN`/`_NCM_ME_FN`/`_NCM_PLAYLIST_FN`（只验证单源失败整包降级的管线行为）。
- 包级 AGENTS.md 新增「测试封闭性」节（规则/做法/信号）。

**实测验证**：
- 三个受影响文件 150 passed in 8.87s（修复前同范围 >150s）。
- withlanggraph 全套 1107 passed, 1 skipped in **145s**（修复前约 300s+）；全仓见体检报告。
- 慢测试 Top1 从 73s/47s 降到 12s（余下为 graph 级集成测试，fake LLM 但真实构图，属合理成本）。

**偏差说明**：
- `_sync_episodic` 的 wiring 由新增的接线测试覆盖，向量写入本体仍由 vector_store 侧保证；未复现"模型缓存命中后 8 个测试仍各 23s"的完整机理（与 pytest 冷/热加载顺序相关），但 mock 后该路径整体消失。
- test_e2e/test_cli/test_graph_loop 等 6-12s 的测试为 fake-LLM 真实构图集成测试，暂保留，不在本次范围内。

**待办更新**：
- [ ] 观察 CI/本地例行跑时总时长是否稳定在 3 分钟内（task：例行体检 skill 已建，每天 9:00 自动核查）。

## [2026-10-04] 日报链路重设计 v3 实施（S0~S6 全量落地）

**背景**：用户对日报三点不满——内容信息利用率低、LLM 输入双通路冗余、缺人工订正入口。设计稿 `docs/daily-report-redesign.md`（v0.7，Q1~Q6 定稿）分四层改造，本条为 S0~S6 全部步骤的实施记录。测试侧慢测试（真实 LLM/Edge/bili）已由用户另行修复。

**已完成**（提交序列）：
- S0（用户完成，已验证）：手机上报恢复（10-03 当日 1077 事件，最新 23:35）；bili CLI 已登录出真实数据。
- S3 `1476d35`：feedback.py 三级修复阶梯核心——`record_correction`（fact→`data/feedback/corrections/{date}.json` 同锚点 superseded；pref→preferences.json）、`list_active_corrections`、`revise_report_llm`（零工具单轮 `get_llm([], os.environ, bind_tools=False)` + diff 门禁非目标节逐字不变 + stricter 重试 1 次 + 降级节末追加）、`next/current_report_version`（versions.json）、`revise_from_pending`；测试 20 项。
- 接线① `0736e55`：`escalate_revise`（①→②升级：修订成功覆盖 delivered 存档并标记 applied）；qq.py edit 提交即落 corrections（best-effort），confirm 两路径补丁失败自动升级 ② 并 redeliver_day；测试 +3。
- S1+S2 `838ac4c`：daily_info_pack.py 注册表化——`SourceSpec/SOURCES`（9 源）替代平行清单，builder 契约三元组 `(title, pack_body, detail_body)`（detail=全部取数结果）；`classify_body` 四态、`_cap_lines` 行级截断、`_assemble_blocks` 整块丢弃熔断、header 带 `〔KEY｜状态:…〕`；落盘 `write_pack_health`（data/logs/info_pack_health.jsonl）+ `write_pack_detail`（pack_detail/{date}/{key}.md 三节，90 天保留）+ `write_fact_card_detail`（_FACT_CARD.md，context.py 注入段钩子）。A′：删 `_build_langtrack`，fact_card 新增 `_build_sleep_section`(p45)/`_build_time_app_section`(p55)，sections 7→9，compact 预算 600→900——langTrack 在 LLM 输入只剩 fact_card 单一渲染出口。scheduler `_build_job_prompt` 落盘钩子（last_pack_stats seam 保既有测试零副作用）。
- S4 `479e4b8`：`gacore/review_server.py`（:8010）——/review 锚点批注+diff 视图、/health 14 天×源矩阵、/health/source 三节详情+L0 外链、POST /api/corrections|revise|rerun（修订默认/重生成需确认/同日 409）、X-Review-Token 鉴权；dev-console services.json 注册 review 服务；**仓库根** `.env.example` 补 REVIEW_TOKEN（apps/withlanggraph/.env.example 无此键）。
- 接线② `fbc7344`：`_build_job_prompt` 包首注入〔人工订正〕(600)/〔用户偏好〕(400)（按 for_day 取，独立于 PACK_BUDGET，correction_chars 进 jsonl）；`_deliver_email` C5 版本号（历史天每次发送递增，主题 `{date}（重生成 vN）`，正文头部「本版为 vN 重生成，依据 M 条人工订正」）；schedule.json C7（素材清单按 A′ 修正、五条线挂来源、start_long_term_update 触发清单）+ `_instruction_head` 消费覆盖规则。
- 文档：langTrack-tech.md §5.4 补 A′ 说明 + 新增 §9.24（v3 技术事实）；architecture-flow.mmd 新增 ⑥ 日报链路子图（收敛：节点全部可回溯源码）。

**实测验证**：
- 新增测试：test_feedback_revise 20 + test_source_registry/test_info_pack_report/test_daily_info_pack/test_langTrack_fact_card 改写 219 + test_review_server 29 + scheduler 注入/版本 4 项。
- 全 app 回归 1184 passed；合流后关键套件 295 passed；ruff 全仓通过。
- design doc §0 目标态图与真源图已收敛（S6 校验：plan 虚线节点全部落地可回溯）。

**偏差说明**：
- `last_pack_stats`/`_LAST_PACK_STATS` 暂存 seam 为实施新增（scheduler 测试 monkeypatch build_info_pack 无法带出 stats）；seam 被替换时跳过落盘。
- `_deliver_email` 版本号集中单点：rerun 与 QQ「确认重发」都递增；当天例行投递不进版本序列（v1 无标记）。
- 上线前需在真实 `.env` 配 `REVIEW_TOKEN`（未配置时 review_server 所有 POST 401）。

**待办更新**：
- [ ] dev-console 重启加载 review 服务；真实 `.env` 配 REVIEW_TOKEN；浏览器实测批注→修订→vN 邮件闭环。
- [ ] 连续 3 天观察 info_pack_health.jsonl：ok 源消费覆盖率 100%（C7 验收）。
- [ ] 观察 langTrack 日报不再出现双份聚合数字（A′ 验收）。

## [2026-10-04] v3.1 统一订正入口：原样替换/LLM 改写双模式 + 邮件订正标记

**背景**：v3 部署真实验证（评审页修订 v2 邮件 + rerun 重生成 v3 邮件均真实送达）暴露两件事：③ 整版重生成对订正词是"消化转述"而非保真引用（"weiCheckApp"等上下文词被带进正文）；用户提出邮件应体现"哪些内容被人工修正过"，且两端（QQ/评审页）对外表现要一致、改动要内聚。

**已完成**（`ae3397d` + 收尾提交）：
- `feedback.py` 统一入口 `apply_correction`（QQ 确认与评审页共用）：mode=auto（默认，锚点能定位→① 原样替换；定位不到→自动升级 ② LLM 最小修订）/verbatim（强制原样，定位失败显式报错）/llm（强制改写）；pref 仅落偏好库。
- 邮件标记：① bullet 尾部 `（人工订正）`；② 文末 `✎ 人工订正：[锚点]` 脚注。
- 审计字段：corrections 记录新增 `mode`/`replaced_from`（被替换原句）/`note`。
- `apply_feedback` 改为 core 薄包装；`escalate_revise`/`revise_from_pending` 删除（被 core 取代）。
- corrections 语义收窄：QQ 提交阶段不再预落盘，确认生效时统一落（未确认订正不进 ③ 注入）；LLM 失败不落记录。
- review_server：`/api/revise` 走 core（pref 不再触发 LLM 修订与重发）；批注框加替换方式下拉 + 备注；`/review` 307 跳转；`/api/revise` 重发不再依赖 applied-pending 守卫（`_deliver_revised`）；状态 `finished_at/duration` 收尾取值。

**实测验证**（全部真实 API）：
- 原样替换：`POST /api/revise mode=verbatim` → 2.3 秒完成（零 LLM），存档行与订正词一字不差 + `（人工订正）` 标记，`replaced_from` 捕获原句，真实邮件 v4 送达（主题 `2026-10-03（重生成 v4）`）。
- 此前已完成：② 最小修订真实链路（diff 门禁仅目标节变化，v2 邮件）、③ 整版重生成真实链路（14 分钟 agent，`〔人工订正〕`注入存档可见，v3 邮件）、401 鉴权、跳转路由。
- 测试：review_server 31 + feedback_revise 25 重写/新增，受影响套件 187 项全绿；ruff 全仓通过。

**偏差说明**：③ 重生成对订正仍为"消化转述"（LLM 行为特性），保真引用需在 schedule.json prompt 加硬约束——暂缓，观察实际使用后再定；`_LONG_TERM` 源 classify 误判 missing_data（正文含"无数据"字样触发启发式）——已知边界，待精化模式表。

**待办更新**：
- [ ] 收件箱确认 v2/v3/v4 三封邮件的标记与版本号展示效果。
- [ ] 观察 ③ 重生成转述问题是否实际困扰，决定是否加"订正词保真引用"prompt 约束。

### [2026-10-04] v3.1.1 追补：去掉 auto 模式（`bea4df1`）

用户决策：三模式收敛为显式二选一——`llm`（默认，输入是改写指令）/ `verbatim`（输入是成品，逐字生效），不再自动调度。apply_correction 默认 llm；评审页下拉仅剩两项；QQ 确认回复语同步。测试 33 项全绿；真实 API 复验默认路径（72s LLM 调用、文末 ✎ 脚注、邮件 v5 送达）。

### [2026-10-04] v3.1.2 追补：评审页点大标题新增子项 + 人工订正独立区块（`e813752`）

用户建议两条：①某大章节缺"新增子项"通路；②人工订正应独立成节并展示原始修改信息。
- 大标题渲染为可点（悬浮出现「＋ 新增子项」），侧栏进入新增模式；`feedback.add_section_item` 锚点自动取该节最大序号+1，逐字落档带标记，零 LLM；`/api/revise`、`/api/corrections` 支持 `op=add`。
- 原始修改信息独立成节：文末维护 `# 人工订正` 区块（`✎ [锚点]（方式）订正词原文；备注：…`），三种操作统一登记；llm 修订前剥离区块喂 LLM（不进 diff 门禁）、完成后带新条目重建。
- 实测：5 个大标题全部可点；真实 `op=add` 新增 `[工作日志-6]`、真实邮件 v7 送达；测试 191 项全绿。
- 另：收录用户本地实验的 docs/health-demos 原型（`000785b`）。

### [2026-10-04] v3.1.2 追补：源体检页 C+ 版式 + 总览日期窗口（`3fecebb`/`a18b7f2`）

用户评审三个 UI demo 后选定 C（双栏对照）并吸收 A/B：字符漏斗条（sqrt 比例 + 每层差值/无截断标注）、逐行归因（首 token 启发式 + 全部/进包/被剔除筛选 chips + 全中/全不中退化中性）、渲染==进包合并徽章/不一致双 Tab、日期 ‹› 导航；总览矩阵加 `?end=` 窗口参数与前后一周/日期选择器。修复 `_split_h2` 结果被二次 join 致正文逐字符拆开的 bug。demo 三版（docs/health-demos）评审后清理。

### [2026-10-04] v3.1.3 追补：体检历史补录 CLI（`079e3ad`）

用户问"10-02 为什么没有、能都补全吗"——体检 jsonl 10-04 才上线，此前日期无产物。新增 `gacore/backfill_health.py`（`python -m gacore.backfill_health --from --to`）：逐日重放 build_info_pack_report（取数源全部支持按天查询），trigger=backfill 区分真实运行；当日/未来跳过、单日失败不断链；事实卡支线一并补录。诚实边界：补的是"以当前数据回看该日"（_LONG_TERM/_MEMORY 为当前态；B站/Edge 受历史窗口限制，查不到如实 missing_data）。已执行 2026-08-05 ~ 2026-10-02：**59/59 天全部成功**，jsonl 60 天完整覆盖；早于 09-05 的日期 CHAT 如实 missing_data（QQ 日志 09-05 才开始）。测试 3 项。

## [2026-10-04] v3.2 信息源采样修正：_LONG_TERM 锚点+尾窗 / _CHAT 取最近 / classify 状态行守卫

**背景**：用户排查 `_LONG_TERM` 源发现两个问题——数据从哪来、为何"每次都取前几个"导致新增丢失。实测确认且比预想严重：`global_mem_insight.txt`（L1 编年史，append-only，修订也追加尾部，现 84 行/~24k 字符，跨 08-02~10-04）经 `_summarize_long_term`（前 40 行 → 08-02~09-07/7877 字符）再经 `_LONG_TERM_CAP=1600` 字符截断，**日报 LLM 每天只看到编年史开头 08-02~08-27**；结婚/领证/大帅离职/mono 迁移等最近数周全部不可见，且修订在尾部=专保留过时版本。60 天 health 数据佐证：full_chars 恒 7877、chars 恒 1604、status 恒 missing_data（假阳性）。

**全源排查**（9 源逐一走查 + 60 天 jsonl 实证）：
- 同类问题 2 处：`_LONG_TERM`（上述）；`_CHAT` 取当日**前** 15 条——日志按时间追加，丢晚间消息。
- 边界诚实性 2 处：`_BILI`（单次拉 50 条）/`_EDGE`（100 条）的 detail 声称"全部"但拉满窗口时静默截断。
- 无问题 5 源：`_BILI`/`_MEDIA`/`_FILES`（按计数聚合非时间线头部）、`_GIT`（date-order 最新在前，截断丢最旧合理，仅 8 天触顶）、`_NCM`/`_MEMORY`（静态基线/单份产物，无"新增丢失"形态）。

**已完成**：
- `scheduler.py`：新增 `_long_term_anchor`（读 `memory/global_mem_anchor.txt` 人工策展固定锚，`# ` 注释不注入）与 `_compact_long_term(anchor, text, max_chars)`（锚全保留 + 编年史尾部窗口，先按行数取尾再按字符预算从最旧端丢行，尾注按 `[YYYY-MM-DD]` 标注覆盖范围）；`_summarize_long_term` 语义改为尾部窗口；rollover `long_term_md` 同口径（预算 8000 保持原量级）。
- `daily_info_pack.py`：`_build_long_term_picture` 改用 `_compact_long_term(max_chars=_LONG_TERM_CAP)` 单一截断，detail=锚+编年史全文；`_build_chat` 取最近 15 条；`classify_body` 状态行守卫（`- ` 开头 + 关键词 ≤ 行首 8 字符，补 4 个缺失模式）修 60 天 missing_data 假阳性；`_BILI`/`_EDGE` detail 拉满窗口加"更早记录可能未覆盖"尾注。
- 锚点文件 `memory/global_mem_anchor.txt`（本地策展，gitignore 覆盖不入库）：生日/婚姻/居住/职业引路人/八段锦/长期兴趣主线 6 行，每行括注来源日期。
- 测试：test_daily_info_pack 新增/改写 8 项（尾窗语义、锚保留+单一截断、仅锚点、classify 守卫×3、窗口标注×2）；受影响断言同步。全 app 回归 1207 passed。

**实测验证**（真实数据 build_info_pack_report('2026-10-04')）：`_LONG_TERM` status=ok（60 天来首次）、full=chars=1543≤1600 单一截断生效、进包内容=锚点 6 行+编年史尾窗（覆盖 2026-10-03~10-04），最新条目可见。

**偏差说明**：锚点内容为本会话从编年史策展的初版，用户可自行增删（改文件即可，机制无感知）；行数上限 `_LONG_TERM_LINES=40` 先于字符预算生效，故字符预算 1600 少见触顶。

**待办更新**：
- [ ] 【已排期后续优化】LLM 定期蒸馏：把 ~24k 字符编年史定期蒸馏为"当前画像摘要"（接 8-6 memory proxy 课题），替代采样窗口成为画像 compact 的最终形态——用户确认必定要做，本期先以锚点+尾窗过渡。
- [ ] 观察 3 天日报：长期画像是否引用到 09 月以后的事实（尾窗生效的写作侧证据）。

## [2026-10-04] 日志 session 同源：llm_requests.jsonl 与 app.jsonl 精确关联

**背景**：LLM 运行可视化 demo（`docs/llm-view-demos/`）需要把系统侧动作（投递/episodic 同步等，记于 `logs/{date}/app.jsonl`）关联到对应的 LLM 调用序列（记于 `llm_requests.jsonl`），发现两套 sink 的进程级 session id 互不相通——`jsonl_logger.py` 与 `llm_request_log.py` 各自模块级 `uuid4().hex[:8]`，只能按 pid+时间窗启发式 join，而 pid 会被进程重启复用，存在误关联风险。

**已完成**：
- `jsonl_logger.py`：新增公开函数 `session_id()`（返回进程级 `_SESSION_ID`），成为 gacore 全部日志 sink 的唯一 session 源。
- `llm_request_log.py`：`_SESSION_ID` 改为 `session_id()` 复用（本来就 import 该模块取 `_SECRET_KEYS`，无新依赖）；模块 docstring 补同源语义。
- 测试：`tests/test_log_session_join.py` 2 项——两 sink `_SESSION_ID` 相等；`_JsonlFormatter` 产出的 app.jsonl 行 session 字段等于 `session_id()`。
- demo 消费侧（`docs/llm-view-demos/build_demos.py` `attach_system_events`）：进程匹配改为"session 命中或 pid 命中"双路——新日志精确 join，历史日志（两 id 不同期，session 不匹配不构成异进程证据）按 pid 兜底，2026-10-03 数据回归通过（run1/run6 投递归属、run2/3/4 批量重发判别均不变）。

**实测验证**：`uv run pytest tests/test_log_session_join.py tests/test_jsonl_logger_daily.py tests/test_llm.py` → 26 passed；ruff 通过；demo 重建后 10-03 关联结果与修复前一致（历史数据兜底路径生效）。

**偏差说明**：仅统一 id 来源，两侧日志的格式/落盘/脱敏均未改动；架构图 OUT 节点（jsonl_logger + llm_request_log）行为不变，无需改图。

**待办更新**：
- [ ] 观察下次重启后（2026-10-05 起）的新日志：同一天内多进程的 session 应与各自 app.jsonl 行一致（精确 join 生效的直接证据）。

### [2026-10-04] v3.2.1 追补：体检手动刷新按钮（`POST /api/health/refresh`）

用户问体检刷新怎么触发（当时仅 日报生成自动落盘 / backfill CLI 两路）并建议页面加按钮。`backfill_health` 抽出单日重放原语 `refresh_day(cfg, date)`（允许当日=以当前数据回看，拒未来；当日跳过策略保留在 CLI 的 backfill_range）；review_server 新增 `POST /api/health/refresh`（token 保护、同步秒级、零 LLM 零邮件），`/health` 矩阵导航与 `/health/source` 单源页（含空态）挂「↻ 重算体检」按钮（token 存 localStorage 与评审页共用，401 自动提示重输）。矩阵同日 jsonl 后行覆盖先行，刷新即时生效，服务无需重启。测试 +6（refresh_day 3 + 路由 5 内含按钮渲染断言，合计 test_backfill_health 6 / test_review_server 40 项全绿）；ruff 全仓通过。实测：8011 临时实例闭环验证（401/真实 token 重算 2026-10-02）后重启 8010 正式实例，按钮渲染与当日（10-04）刷新均真实验证通过。

### [2026-10-04] v3.2.2 追补：逐源预算网页配置（`/config` + `POST /api/config/sources`）

用户提出"每个源怎么截取、最大截取多少应做成配置项，网页上改完就生效"。实现：
- **配置层**（daily_info_pack）：`config/info_pack.json` 覆盖代码默认——每源 `cap`（最大截取字符，200~8000 钳制）/`priority`（熔断顺序，1~999，稳定排序）/`enabled`（停用源不取数不进包，stats 记 disabled 态）+ `pack_budget`（3000~20000）。`build_info_pack_report` 每次构建**现读现用**，改完下一次构建/重算体检即生效，无需重启；文件缺失/损坏全走代码默认。`effective_source_caps` 供 `_LONG_TERM` builder 内部单一截断取用；`write_pack_health` 的 budget 字段如实记录本次生效值。
- **采样方向不开放配置**（取最近/聚合 topN 是每源固有策略）——防把 v3.2 刚修的头部截断类 bug 做成可配项再踩。
- **评审服务**：`GET /config` 源预算页（每源显示最近一天 进包/截断前 用量辅助定预算；token 存 localStorage 与评审页共用）+ `POST /api/config/sources`（token 保护，未知源 key 400、预算越界 400、原子写 .tmp→replace）。矩阵状态样式新增 disabled（灰）。
- **实测闭环**（8011 临时实例 → 8010 正式实例重启后复核）：页面渲染 ✓；保存 `_FILES cap=600 + _NCM disabled` ✓；重算 2026-10-03 体检后 NCM=disabled、FILES chars=565/full=704（新 cap 生效）✓；验证后已删除测试配置文件恢复默认。
- 测试 +11（config 层 5 + 路由/页面 6），test_daily_info_pack 47 / test_review_server 45 项全绿；ruff 全仓通过。

## [2026-10-04] llm_requests.jsonl 补响应捕获：一行 = 请求+响应+耗时完整单元

**背景**：LLM 运行可视化 demo（docs/llm-view-demos）暴露运维盲区——llm_requests.jsonl 只记请求不记响应，"模型到底答了什么、调用了多久、失败原因"全部不可见；且 memory judge 的字符串 prompt 输入被记成空行（调用 #4"无消息列表输入"的根源）。从运维视角看，没有状态码和耗时的访问日志不合格。

**已完成**：
- `llm_request_log.py` 重写拦截器：`invoke/ainvoke` 调用完成后写盘，一行含请求 + `response`（content/content_chars/tool_calls/usage tokens/finish_reason，截断与掩码同请求侧）+ `duration_ms`；失败记 `response.error` 并原样重抛。
- 流式 `stream/astream`：转发 chunk 的同时 `_StreamAgg` 聚合（文本 + tool_call_chunks 按 index 拼接 + usage 取末次），结束/异常/提前关闭（`interrupted: true`）均在 finally 落盘。
- 非消息输入：`_messages_to_log` 把字符串 prompt 包装为单条 user 消息，judge/structured 调用不再记空。
- 消费侧 demo（docs/llm-view-demos）：调用头增耗时与响应徽章（失败红色）、💬 模型响应折叠块（含 tokens）；系统泳道纳入 app.jsonl 的 ERROR 级事件（红色高亮，run 视图内自动浮现）。
- 测试：新增 `tests/test_llm_request_log.py` 5 项（invoke 响应+duration、失败 error+重抛、字符串输入包装、stream 聚合 4 chunks、提前关闭 interrupted）。

**实测验证**：新测试 5 passed；`test_llm.py`/`test_log_session_join.py` 回归通过（旧调用方零改动）；demo 重建后 10-03 数据（旧格式无响应字段）正常缺省渲染，run1 的真实 `memory_maintain gate failed` ERROR 自动红标浮现；注入自测数据确认响应徽章/耗时/tokens 渲染正确（DOM 注入，不污染真实日志）。

**偏差说明**：记录时机从"调用前"改为"调用完成后"——若进程在调用中途崩溃则该次调用无记录（app.jsonl 侧仍有进程级事件兜底）；响应内容使日志体积增约一倍，截断规则（单字符串 30k）与按日分文件继续兜底。旧记录无响应字段属预期，消费侧已容忍。

**待办更新**：
- [ ] 观察今晚 23:50 例行日报的新格式记录：response/usage/duration_ms 齐全、QQ 对话与 judge 调用的字符串输入可见。
- [ ] 后续可选：token 用量按日汇总（成本视图）、ERROR 事件阈值告警（与 ROADMAP 既有 sync_failure 告警项合并考虑）。

### [2026-10-04] v3.2.2 追补：顶部导航补「日报评审」回链

用户在 /health 发现回不去日报评审页——页面骨架 brand 链接误指向 /health。修正：brand 与导航均指向 /review（/review 无日期自动 307 跳最近一篇已投递日报），导航现为 日报评审｜源体检｜源预算 三入口。45 项测试全绿，8010 已重启验证。

### [2026-10-04] v3.2.2 追补：HTML 页统一 no-store 禁缓存

用户实测「↻ 重算体检」成功后页面不自动更新、需手动再刷——根因是浏览器对无 Cache-Control 的 GET 页面启发式缓存，location.reload() 命中旧页。review_server 加 `_html()` 统一出口（Cache-Control: no-store），/review、/health、/health/source、/config 四类页面全覆盖；新增 TestNoStore 断言 4 页响应头。46 项测试全绿，8010 已重启验证。

## [2026-10-04] /llm-requests 运行回放落地：demo 选型 → review_server 正式页

**背景**：LLM 运行可视化经 3 个 demo 评审选定 A（运行回放）并吸收 B/C 优点（搜索定位/NEW 增量/复制 JSON），用户确认不新建 app（数据引力在 gacore，跨 app 依赖不合 R2，R3 无第三消费者），落地为 review_server 的兄弟页（与 /review、/health 同源同鉴权模式）。

**已完成**：
- 新建 `src/gacore/llm_runview.py`：run 重建单一实现（自 build_demos.py 下沉）——run 边界（session+10min）、工具往返配对（tool_call_id）、类型分类启发式（日报/推送/快答/内部）、投递归属（session 精确+pid 兜底、多主题爆发=批量重发）、ERROR 泳道（module+message 聚合计数）、产出存档归属（Job finished 后 300s）；`run_view(cfg, date)` 单一入口，全部容错缺文件。
- `review_server.py`：`GET /llm-requests`（`?date=` 缺省最近有数日期，‹› 日期导航；DATA 服务端内嵌，CSS 作用域限定 #llmv）、`GET /api/llm-runs/{date}`（JSON）；导航栏加「运行回放」。
- `docs/llm-view-demos/build_demos.py` 改为复用 llm_runview（消除双份逻辑）；B/C 模板反提取为 .tmpl；demo_a 离线快照保留（A 已移植，脚本不再重建）；`template_a.html.tmpl` 改名防误开。
- 测试：`test_llm_runview.py` 8 项（分组/配对/分类/session精确+pid兜底/批量重发判别/ERROR窗口/存档归属/缺文件空态）+ `test_review_server.py` 新增 5 项路由测试。

**实测验证**：新测试 13 passed + 既有 59 review_server 测试回归通过；TestClient 真实数据（2026-10-03：6 runs/47 calls 关联正确）；临时实例（:8011）浏览器截图确认今日真实数据渲染——**页面首日即浮出真实运维问题**（send_email 535 auth failed ×2、bili_history 超时、Edge DB not found、ncm_login 无法启动、QQ on_message error 等红色 ERROR 聚合行）。

**偏差说明**：ERROR 聚合为 module+message 计数（开发日窗口内 74 条同类错误逐条渲染会淹没时间轴），首条时间展示、详情在 app.jsonl；run 边界与投递窗口为启发式（文档化于 llm_runview docstring）；demo A 的离线快照不再由脚本重建（正式页为准）。

**待办更新**：
- [ ] dev-console 重启 review 服务后从导航「运行回放」进正式页；观察今晚 23:50 例行日报的新格式记录（response/usage/duration_ms）在回放中的呈现。
- [ ] 可选：ERROR 计数阈值告警、token 成本按日汇总（接 ROADMAP 既有观测待办）。

## [2026-10-04] v3.2.3 /data 数据目录页：手机采集数据资产盘点（demo 选型 → review_server 正式页）

**背景**：用户提出"没有一个好用的页面看手机上报了哪些数据，不知道有哪些数据就没法分析接哪些源"——原料层可观测缺口。盘点确认 langTrack.db 219MB（16 种事件 10.7 万条）中仅 music_play 被 _MEDIA 消费，notification/sms/input/location 轨迹等大量未进日报；fact_card/daily notes 走 system prompt 旁路不受 /health·/config 管辖。经 3 个交互 demo（长卷/主从/行展开，真实数据烘焙）评审：**定交互为主从式**，吸收长卷密度与行展开钻取，融合为 demo4 定稿。

**已完成**：
- 新建 `src/gacore/data_catalog.py`：纯读数据层——langTrack.db 只读 URI 连接（不触发 ETL 不写文件），盘点表资产（行数/日期覆盖/影子·备份·空表分类）、事件类型（总量/近 7 日逐日/最新 10 条 payload 样本截 220 字/非法 JSON 原样兜底）、库外文件（memory/*.jsonl、weather_cache、daily notes，带消费方映射）；db 缺失/损坏降级空态不抛异常。`CONSUMERS`/`CONSUMER_SHORT` 消费方映射为"未接自动浮出"的依据，模块 docstring 明确"新增源/工具/旁路后必须同步更新，否则误报未接"；sms（验证码）/input/clipboard 在映射中明确"永不进包"负清单。
- `review_server.py`：`GET /data`（导航加「数据目录」）——demo4 形态：左目录（内嵌 7 日迷你柱状+消费方短标+最新时间+过滤框+键盘 ↑↓+URL `#type=` 定位）、右详情（KV+近 7 日逐日柱+payload 样本下拉切换）、底部表资产（隐藏影子/备份/空表开关）+库外文件；DATA 服务端内嵌 JSON（`</` 转义防 script 逃逸），CSS 以 `dc-` 前缀作用域隔离，复用 `_html()` no-store。
- 测试：`test_review_server.py` 新增 TestDataSource 4 项（真实渲染+no-store+导航互通/db 缺失空态/样本截断与非法 JSON 兜底/影子表分类与行数）。

**实测验证**：全仓门禁 `ruff check apps packages tests` 全绿 + `pytest` 1249 passed 1 skipped；真实数据快照（16 事件类型、41 表、6 库外文件）在 demo 阶段已由用户浏览器逐页评审交互。

**偏差说明**：payload 样本未脱敏（含微信消息/验证码，本机自查页性质，页头标注）；demo 生成器与产物在 `data/demos/`（gitignore，一次性原型不入库）；表资产"覆盖"取 ts/day 列 min-max，无该列的表显示 "-"。

**待办更新**：
- [ ] 用户逛 /data 后拍板 `_PHONE_PLACE`（位置轨迹）/`_PHONE_USAGE`（手机使用）两源接入（cap/priority/builder 设计已议：复用 report.py 查询函数、措辞口径借 fact_card 保证不失真）；`_PHONE_NOTIF`（通知内容）涉私信进邮件，单独立项。
- [ ] fact_card/daily notes 的 system prompt 旁路收编评估（daily notes 摘要优先）。

### [2026-10-04] v3.2.3 追补：/data 右详情改"按天"——全量按日直方图 + payload 全量取数

用户反馈：payload 下拉框不够看、要能看全部；且按天组织更符合排查习惯。改造：`collect` 每事件类型内嵌**全量按日直方图** `hist`（单条 SQL 按 UTC+8 分日 GROUP BY，ts<2026-01-01 脏数据不入图），右详情渲染为日历 chips（最新在前），点日期经新路由 **`GET /api/data/events?type=&day=&limit=`** 拉当日全部 payload（`data_catalog.events_for_day`：day/type 正则校验→非法 400，limit 默认 500 上限 20000，超限页内"加载全部"显式拉全量，单条截 1000 字，按时间倒序）。实测真实数据：session 2026-10-03 全 266 条；audio_env 2026-08-19 突发日 13,577 条全量拉取正常；非法 day 400。测试 57 项全绿（TestDataSource 改/增：collect 形态、API 取数/校验/截断、历史日、非法 JSON 兜底），8010 已重启验证。

### [2026-10-04] 追补：运行回放页折叠展开修复（CSS 优先级）

用户实测 /llm-requests 点击「调用 #2」无反应。浏览器实测定位：点击本身生效（class 正确切到 `call fold open`），但 `#llmv .call>.bd{display:none}`（ID 选择器）压过无前缀的 `.call.open>.bd{display:block}`——.open 展开规则漏加 `#llmv` 前缀，**全页所有折叠（调用/工具卡片）自上线起均不可展开**。修复两条规则补 `#llmv` 前缀并加回归测试（断言带前缀规则存在、无前缀规则不存在）；同页其余状态规则（.run.on/.toolhit）核查无同类问题。58 项 review_server 测试全绿（全仓 1252 passed），8010 已重启并浏览器复验展开正常。

## [2026-10-05] v3.3 手机事实源接入：_PHONE_PLACE/_PHONE_USAGE/_PHONE_NOTIF 三源进包

**背景**：/data 数据目录页盘点显示 langTrack.db 采集了 16 类事件（10.7 万条），日报仅消费 music_play；daily_stats 聚合、stays/trips 轨迹、通知内容全部未进信息包。且 fact_card 与 daily notes 摘要走 system prompt 旁路——不在 /health 观测体系、不受 /config 预算管辖。用户拍板接入三个手机事实源（通知内容经隐私确认：仅本地日报+本人邮箱，不出网）。

**已完成**（daily_info_pack.py + data_catalog.py + 测试 + R5 同步）：
- `_PHONE_PLACE`（priority 15，cap 800）：复用 `fact_card.build(day, detail="full")` 纯读卡——地名解析/日界裁剪/降级全复用，措辞与事实卡不失真（复用 `_build_timeline_section`/`_build_stay_section` 文本）。pack=轨迹时间线+停留累计+数据窗口未闭合标注+异常 top5；detail=stays/trips/anomalies 全量。
- `_PHONE_USAGE`（45，600）：daily_stats 聚合（屏幕/解锁/切换/App Top5/通知计数/作息窗口）；疑似熬夜信号才进包，"未见熬夜信号"默认态不写；detail 直查全量排行（fact_card 卡内截 8）+通知来源分布。
- `_PHONE_NOTIF`（65，800）：通知内容事件尾窗（最近 8 条，连续同源合并、单条 50 字唯一截断）。**采样策略实证修正**：原方案"只取点击过的消息"经真实数据检验失效（近 10 天 1359 事件中 clicked=True 只出现在无文本移除标记上，含内容且 clicked=True 为 0），改尾窗取最近内容事件；点击计数由 _PHONE_USAGE 聚合行承载不重复。
- SOURCES 注册顺序改为 priority 升序（注册序=装配序不变量保持）；`_instruction_head` 降级说明改为"fact_card=当前时刻视角、当日全天以 _PHONE_* 为准"；`data_catalog.CONSUMERS` 同步（location/session/usage/notification 已接，sms/input/clipboard 标注永不进包）。
- AGENTS.md 第 15 节军规新增第 7 条**进包负清单**（sms/input/clipboard 永不进包，notification 为已拍板例外）。

**实测验证**：真实数据回算 2026-10-04——三源全 ok（PLACE 216/USAGE 117/NOTIF 394 字），整包 7769/10000 在用户配置预算内；/config 的 NCM 停用与预算覆盖照常生效。测试：test_daily_info_pack 新增 8 项（合成库 schema 对齐 test_langTrack_fact_card._make_db：内容/截断/空态/尾窗合并/负清单注册/config 停用），test_source_registry 失败注入登记 `_lang_track_card` 打桩（fact_card.build 内部吞异常降级，须打读卡入口）、priority 不变量放宽为 5 的倍数。全仓 1316 passed，ruff 全绿。

**偏差说明**：① 通知"点击过"采样不可行是实测结论而非设计缺陷，文档已记实证数据；② sleep_start/end 字段 ETL 未产出（全 null），作息行在真实数据暂缺、产出后自动出现；③ C2 A′"langTrack 不做独立源"决策局部修订——细维度（事件级流水）仍不做源，聚合/轨迹/通知内容三个当日视角进包，fact_card 的 chat 注入出口不变。

**待办更新**：新增源观测一周（军规⑥）：关注 _PHONE_NOTIF 的 full_chars 与 cap 关系（通知多日可能触顶）；LLM 蒸馏画像待办不变。

### [2026-10-05] v3.3 追补：军规第 7 条放宽（负清单→统一隐私边界）+ 新源接入经验固化为 add-info-source skill

用户反馈两点：① sms/输入法/剪贴板"永不进包"过严——只要与通知同边界（仅本地日报+本人邮箱，不出网）即可接受；② 后续还会接入很多源，要求把本次经验固化成 skill。调整：AGENTS.md 第 15 节军规第 7 条由"进包负清单"改为**"敏感事件进包边界"**——notification/sms/input/clipboard 均允许进包，统一边界=仅本地日报落盘+本人邮箱、绝不出网，新增涉敏类型先向用户确认边界再登记再写 builder；data_catalog.CONSUMERS 三处文案、tech §9.24 v3.3 条目、mmd SIP 节点、_build_phone_notif docstring 同步改写（历史 ROADMAP 记录不回改，以本追补为准）。**新增项目级 skill `.agents/skills/add-info-source/SKILL.md`**（根 AGENTS.md 技能指引已登记）：八步流程（盘点→拆分决策→隐私拍板→builder 契约→注册→封闭测试→同步面清单→真实验证与提交）+ 已踩坑（采样假设先实证/连发合并/heredoc 坏中文），供后续新源接入直接复用。

### [2026-10-05] _BILI 单次拉取上限 50→100

用户要求提高 B 站观看源取数覆盖。`_build_bili` 的 `_BILLI_FN(limit=50)` 改 `_BILI_FETCH_LIMIT=100`（工具 `_MAX_LIMIT` 本即 100，非扩工具）；detail 边界尾注同步常量化（`len(entries) >= _BILI_FETCH_LIMIT` 才标注「拉取上限 100 条」，不再硬编码）；tech §9.24 v3.2 采样修正④同步。测试 `test_bili_detail_window_note` 改 100 条断言 + 补"未触顶不加尾注"对照。51 项 daily_info_pack 测试全绿。8010 已重启，/health 页对该日「↻ 重算体检」即以新上限重拉。

### [2026-10-05] v3.2.4 拉取条数上 /config——逐源 fetch_limit 旋钮 + 保存改合并语义

用户要求把 _BILI 拉取上限做成网页配置项（"各个源有单独的配置项也正常"）。`_FETCH_LIMIT_SPECS`（key→默认/min/max，max 受工具硬上限约束：`_BILI`/`_EDGE` 均为 10~100，默认 100）+ `effective_fetch_limit(cfg,key)`（config `sources[key].fetch_limit` 覆盖+钳制，现读现用）；`_build_bili`/`_build_edge` 改读旋钮，窗口边界尾注随生效值。`/config` 页加「拉取条数」列（仅旋钮源有输入框，其余显示 —），POST 校验：无旋钮源带 fetch_limit 400、越界 400。**保存接口改合并语义**：实测踩坑——curl 单源 POST 曾把既有配置（NCM disabled 等）整体清掉；现 payload 只覆盖显式给出的源/字段，其余保留（页面全量发送行为不变）。测试 +2+1（daily_info_pack 旋钮覆盖/钳制/默认、review_server 列渲染/落盘回读/越界拒绝/合并保留），113 项全绿；真实 API 验证 150→400、30 落盘、页面回显 30；8010 已重启。

## [2026-10-05] v3.4.0 地点语义化：手工语义配置 + 异地标注 + 同点短出合并 + route_change 地名化

**背景**：用户指出 _PHONE_PLACE 轨迹输出"张垛 00:00-03:05 → 张垛 03:11-14:14 → 张垛 15:55-00:00"没有利用价值——①不知道是哪里的张垛；②"张垛=张威的老家"这类语义无处配置；③区县背景信息没用上；④同点往返被切成三段同名 stay 读起来是废话。探索确认：places 表已存完整行政区划（张垛=安徽省马鞍山市当涂县乌溪镇张垛），缺的是显示层与"人的语义"；高德行政区划/天气/静态地图 API 实测可用但人文故事类内容无 API（用户拍板语义手工配置，静态地图确认 dashboard 已有动态轨迹图不需要）。

**已完成**（新模块 place_semantics.py + location_facts/fact_card/etl/daily_info_pack + 测试 + R5 同步）：
- `place_semantics.py`：读 `data/place_semantics.json`（手工编辑，gitignore 内用户数据）——places 条目（place_id 命中优先、poi 名次之）的 `tag` 覆盖 DB label、`note` 追加展示；districts 条目区县 note。mtime 缓存改完即生效；坏文件/缺文件静默降级空配置。
- `location_facts.region_suffix(district, home_district)`：区县≠家所在区县 → "（区县）"，本地/未知空串。
- fact_card：`_load_places` 处语义 tag 一次叠加全链路受益（stay_minutes 分桶/format_place/PlaceRef）；卡片新增 `home_district`（家点区县，visit 降序首个）；StayBrief 新增 `region`/`note` 字段（shape 快照契约只增不删）；`_build_timeline_section` 相邻同点 stay 合并 + 括注期间短出（`（期间 03:06 短出 2.0km、15:24 短出 0.4km）`），trips 尾注口径全同点往返 → "短出 N 次合计 X.Xkm" 否则 "移动 N 段"（`_trips_summary`）；stay 末端命中日窗终点渲染 "24:00"（修 "00:00-00:00" 观感）。
- etl `detect_route_changes`：route_change 详情端点地名化——from/to place_id 直查（poi>address>district）→ 500m 内就近 place → 坐标兜底；v1 缺列自动落坐标（历史存量行不回改）。
- daily_info_pack `_build_phone_place` detail：stays 全量行带 region；追加 `地点背景：{label}——{note}` / `区县背景：{district}——{note}`（去重，不占 compact 预算）。
- 测试封闭性：tests/conftest.py 全局 autouse fixture 将 place_semantics.CONFIG_PATH 指向不存在的 tmp 文件，语义用例自行写 tmp 配置。

**实测验证**：真实库 2026-10-04 回算——`今日轨迹：张垛〔张威的老家〕（当涂县） 00:00-24:00（期间 03:06 短出 2.0km、15:24 短出 0.4km）；短出 2 次合计 2.4km`（原"张垛×3 段"完全消除）；`place_semantics.note_for('7afcc1e4…')` 真实命中。全仓 1333 passed + ruff 全绿。

**偏差说明**：① 现有 3 项 fact_card 断言因异地标注/合并行为更新（test_compact_timeline_example_format 精串、900 字折叠用例改交替地点构造超长、budget 用例阈值 120→200）——均为契约演进而非回归；② route_change 地名化只影响新检测事件，存量 anomalies detail 保持坐标；③ 高德天气 API 仅实况/预报无历史，历史日天气背景暂缓；④ 语义配置为 data/ 下 gitignore 用户数据，格式以 tech §5.6 为准。

**待办更新**：区县人文背景先手写 place_semantics.json districts note；LLM 蒸馏画像待办不变（可顺带预填区县/地点 note 后人工复核）。

### [2026-10-05] v3.4.1 异地标注补全省市区

用户反馈"补充全一点又没有坏处，为啥不把省市区都标上"。`region_suffix` 增 address 参数：regeo 的 address 恒以"省+市+区"开头，截取起点到 district 结尾即得全量区域（"张垛（安徽省马鞍山市当涂县）"、直辖市如"北京市海淀区"天然无重复）；address 不含 district（合成/旧数据）退化为仅 district。零 API 零建列（不依赖此前搁置的行政区划缓存表）。fact_card StayBrief 装配传 address；测试断言同步，全仓 1333 passed，tech §5.6 同步。

## [2026-10-05] v3.4 主 graph 接入新闻热榜 MCP（aigroup-news-mcp → news_hot_list/news_search/news_list_sources）

**背景**：用户给 ModelScope 收录的 `aigroup-news-mcp`（Node.js stdio MCP server，MIT 免 key，热榜来源知乎/微博/GitHub/百度/B站），要求接进 gacore 主 graph，聊天中"遇到有趣的事情可以搜一下"。评估结论：值得接（热榜是日报与聊天都缺的"外部世界"维度），但stdio MCP 是异步子进程协议、npx 冷启实测 47.8s，必须常驻会话而非每 run spawn。

**已完成**：
- 新增 `src/gacore/tools/news_mcp.py`：`_McpBridge` 后台 daemon 线程独占事件循环 + 持久 `ClientSession`，同步/异步两条执行路径（QQ astream / 日报 invoke）共用同一会话；三个同步 `@tool`（news_hot_list/news_search/news_list_sources）注册进 `tools/__init__.py`（27→30）。
- 两个真实 bug 修复（详见 tech §9.27）：① 该 server 往 stdout 打人读日志，mcp SDK 把解析失败的行作为 Exception 项送入读流致 ClientSession 挂死——在 stdio_client 与 ClientSession 之间插过滤流只放行 SessionMessage；② `_acall` 运行在桥接循环上却用 `threading.Event.wait()` 阻塞该循环等待就绪信号（信号正是靠该循环 set 的）→ 死锁 90s——改 `asyncio.to_thread` 让出循环。
- 依赖 `mcp>=2.3.0` 落 pyproject（R2）；测试 `tests/test_news_mcp.py` 5 条封闭单测（注册/降级/参数透传/content 格式化），绝不真启 npx。

**实测验证**（真实链路）：zhihu 47.8s（首次冷启，含拉起+初始化+抓取）返回真实热榜；github 2.8s、search 0.6s（热会话+LRU 缓存）；降级路径（桥接崩溃→"新闻工具暂不可用：npx 进程崩溃"）单测覆盖。全仓 pytest 1349 passed，ruff 全绿。

**偏差说明**：① `search_news` 实测语义为**热榜标题内关键词匹配**而非搜索引擎（"人工智能"0 命中属正常），工具描述已写明预期；② weibo 源上游适配器当前已坏（server 端 `reading 'cards'`），zhihu/github/baidu/bilibili 正常——上游问题不修，等 server 更新；③ 评审服务 8010 不绑工具无需重启，新工具随 QQ 前端/日报进程下次重启生效。

**待办更新**：观察聊天中工具实际调用频率与价值；若日报需要"当日热点"维度，可另做确定性 `_NEWS` 信息包源（复用同一桥接会话，进包可观测可预算）——尚未拍板。

## [2026-10-06] v3.5.0 语义地点编辑器：自定义标签 DB 即真源 + /places 页（D+地图定稿）

**背景**：v3.4.0 落地后用户连续三轮推进语义化体验——①要可视化编辑（起 3 个交互 demo：A 主从式/B 表格批量/C 地图点选，真实数据烘焙实拍评审）；②拍板"自定义 tag，计算逻辑跟我的 tag 走"（讨论出角色绑定方案）；③最终改向**"我可以自定义 DB 标签，手动给地点设置，计算逻辑继续算，后续还能按新标签多算有趣指标"**——即取消 tag 概念，名字唯一真源进数据库，note（背景故事）保留为独立字段；note/区县背景确认是两个粒度不是一回事。编辑器形态采纳 subagent 用户视角评审的 D 方案（待办驱动主从式）并按用户要求并入 C 的地图。

**已完成**：
- `places.note` 列迁移（R6）：`location_reader.ensure_note_column` 幂等 ALTER（判据=列存在性）。**⚠️ 踩坑已修**：初版递增 user_version→3，但该版本号被 location v2 占用为"位置事实 schema 版本"（>=2 走 v2 全量重建分支），v1 测试库被误标 v2 → TestDailyQuality 三测连挂（place_cells missing）。修正为**不递增 user_version**（R6 递增规则的既记录例外）。
- fact_card：place_semantics tag 叠加层删除；StayBrief.note 改读 places.note 列；**停留累计按自定义标签聚合**（家/公司固定桶在前，其余用户标签时长降序上限 4，其他/未知收尾）——第一颗"按标签多算的指标"落地（真实数据实证：`停留累计：张威的老家 22.2h`，原"其他 22.2h"）。
- `place_semantics.py` 收缩重构：只管区县背景 note（json 格式 `{"districts":[...]}`，旧版 places/roles 键兼容忽略）+ 编辑器数据层 `editor_data()`（places 全量+近 14 天出现线索+区县聚合+家基准）+ `save_place_labels()`（直写 DB，place_id 优先/poi 兜底，label≤24/note≤500 截断）。
- **/places 编辑页**（review_server :8010，模板在 `gacore/places_page.py`）：待办队列（新出现未配置"新"徽标 + 高频未配置 Top5 + 同名歧义提醒）｜全部｜区县｜地图（AMap JS 打点：绿=家/蓝=公司/橙=自定义/灰=未知，初始视野锚家点+全国视野按钮）四 tab + 右侧编辑面板（标签输入+「家」「公司」快捷按钮+防错别字提示+地点 note+区县 note+实时渲染预览），保存即写库自动出队。API：`POST /api/places/labels`（直写 DB）、`POST /api/places/districts`（json 整表替换），均 token 保护。
- **存量迁移（一次性）**：v3.4 json 里张垛 tag/note 搬进 places.label/note，json 重写为 districts-only；发现 places 表同 poi 两行（新旧网格残留），由待办"同名歧义"提醒承载。
- label_places 交互确认流与 place_labels.json 退役（模块保留，无消费者）；计算逻辑零改动（按精确值"家"/"公司"匹配，UI 防错别字提示）。
- 顺手修测试午夜地雷：test_events_for_day_api 种子锚定"2 小时前"，凌晨跑落到昨天 → 改为"5-10 分钟前 + 东八区当天取日"。

**实测验证**：浏览器全流程实拍——待办队列真实数据正确（新徽标/张垛歧义）、编辑面板回显迁移后的"张威的老家"、**保存闭环 UI→POST→DB 写入实证**（note 更新后 DB 读回一致）、区县 note 保存落 json、地图 tab 渲染+全国视野；真数据 10-04 回算轨迹行不变（`张垛〔张威的老家〕（安徽省马鞍山市当涂县）…`）+ 停留累计升级。全仓 1349 passed + ruff 全绿（并行会话 news_mcp.py 的 F841 不属本会话文件）。8010 已重启加载（后台任务方式，dev-console 下次 start.bat 接管）。

**偏差说明**：①设计三次演进（json tag 叠加 v3.4 → 角色绑定（未提交即废弃）→ DB 直写 v3.5），历史 ROADMAP 记录不回改；②home_district 基准仍=label=="家"，用户把别的点标成"家"会改变异地基准与深夜判定——UI 已提示；③地图/编辑器对 v1 无 place_id 的点按 poi 匹配兜底（同名点有误伤可能，待办歧义提醒兜住）；④8010 本次以 ZCode 后台任务拉起（python.exe），会话结束即停，日常由 dev-console 管理。

**待办更新**：按自定义标签的到访频率趋势/距离圈层（用户点名的"有趣指标"，未排期）；区县背景待用户手写；add-info-source skill 不受影响。
