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
- [ ] 记忆维护「阶段二」实测闭环：等 `uv sync --extra vector` 装完（torch
      首次 ~2GB + 模型 ~95MB），跑 `embedding.encode` 冒烟 + `vector_store` 对真实
      画像 sync + `VectorTrigger` 端到端一轮，确认语义召回真能命中"搬家到朝阳区"

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
