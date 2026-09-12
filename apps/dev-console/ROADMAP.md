# dev-console 路线图与执行记录

> 本文件记录 dev-console 的开发进度与每次改动的执行日志。
> 格式约定（根规则 R5）：每次代码改动后追加一条记录，包含「背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新」五段。

## 项目背景

本地开发服务控制台：单文件 Python（stdlib only）HTTP 服务 + 原生 JS 仪表盘，用于在本机启停/检视 `services.json` 中定义的自家开发服务。仅绑 loopback，变更接口需 token。

## 待办

- [x] Phase 0 → Phase 1 迁移：迁入 mono 仓库，接入 uv workspace
- [ ] 补基础测试（进程探测的纯函数部分）
- [ ] services.json 模板化（去掉本机绝对路径，用环境变量或相对路径）
- [ ] Windows 服务化（nssm 或 Task Scheduler 常驻）

---

## 执行记录

### 2026-09-04 — 迁入 mono 仓库 (Phase 1)

**背景**：mono 仓库 Phase 0 骨架搭建完成，按路线图 Phase 1 迁入最轻量的 dev-console 作为试点。

**已完成**：
- 通过 `git subtree add` 将 dev-console 完整历史（9 条提交）迁入 `apps/dev-console/`
- 新增 `pyproject.toml`，接入 uv workspace（零第三方依赖）
- 包级入口脚本 `dev-console` 指向 `server:main`

**实测验证**：
- `uv sync` 成功，workspace 正确识别 dev-console 成员（26 packages resolved）
- 单实例守卫：8787 被原实例占用时按设计退出，错误落盘 `logs/startup-error.log` + `app.jsonl`
- 临时改绑 8788 冒烟：`/api/status` 200（6 服务，运行检测与原实例一致）、`/` 200、无 token POST `/api/stop` 正确返回 401
- 迁移忠实性：新旧仓库 `public/index.html` git blob 哈希一致（3227aecc），工作区差异仅为 CRLF/LF 换行表示
- 冒烟后已还原端口为 8787 并清理测试日志

**偏差说明**：
- 原 AGENTS.md 为 PROJECT KNOWLEDGE BASE 格式，迁移后按 R8 规则调整为包级约定格式，代码地图与反模式条目保留
- `services.json` 中的绝对路径（cwd/interpreter 指向旧目录 WithLangGraph/py-wei 等）暂未模板化，待后续统一处理

**待办更新**：
- [x] 启动冒烟测试（2026-09-04 完成）
- [x] ruff + pytest 全绿（2026-09-04 完成）
- [ ] services.json 模板化（去本机绝对路径）
- [ ] 补基础测试（进程探测纯函数部分）
- [ ] Windows 服务化（nssm 或 Task Scheduler 常驻）

### 2026-09-09 — 启动器路径切到 mono + 管理台切换

**背景**：dev-console 迁入 mono 后，start.bat/start-console.vbs 仍指向旧独立目录
`D:\AAAmyPrj\github\myrepos\dev-console`；且实际运行中的管理台也是从旧目录启动、
内存缓存旧 services.json，导致通过它启动的 langTrack/gacore 跑回原仓库代码。

**已完成**：
- start.bat / start-console.vbs 的 cwd 与 pythonw 解释器路径改为
  `mono\apps\dev-console` + `mono\.venv\Scripts\pythonw.exe`
- 停旧管理台（旧目录），从 mono/apps/dev-console 重启，mono 的 services.json 生效
- langTrack/gacore 已通过 mono 管理台正常启动

**实测验证**：新管理台 `/api/config` 返回 langtrack/gacore 的 interpreter/cwd
均指向 mono；两个服务进程 cmdline 确认用 mono 解释器与代码。

**偏差说明**：
- 管理台的 CONFIG 在进程启动时加载一次，改 services.json 后必须重启管理台才生效
- 旧独立目录 `D:\AAAmyPrj\github\myrepos\dev-console` 仍在（独立 git 仓库），
  是否废弃待定；其启动器已不推荐使用

**待办更新**：上文「services.json 模板化」应一并覆盖 start.bat/start-console.vbs
的硬编码路径，或改用环境变量注入。

### 2026-09-12 — 服务卡片加「打开前端」直达按钮

**背景**：多个受管服务带浏览器页面（WeiTrack 看板、APK 分发页、mmd 查看器、opencode web），但卡片只展示端口，没有直达入口。用户希望每个有前端的服务能一键新开网页；buildApp skill 此前也未声明"前端接入"字段。

**已完成**：
- `server.py::_serve_status`：每个服务返回新增 `frontend` 字段（透传 `services.json` 的 `frontend`，无则空串），接口不影响其他元数据
- `public/index.html`：卡片 actions 区新增「打开」按钮（绿色、定宽），仅当服务声明 `frontend` 才显示；运行中且非 busy 时可点，点击 `window.open(s.frontend, "_blank")` 新开网页；未运行置灰
- `services.json`：给 4 个有浏览器界面的服务补 `frontend` 完整 loopback URL（py-wei-web→5555/、opencode→4096/、app-apk→8080/、mermaid-viewer→8123/apps/mermaid-viewer/viewer.html）；langtrack（纯上报 API）/gacore/py-wei 无页面不填
- `.trae/skills/buildApp/SKILL.md`：B 部分服务字段表新增 `frontend` 行；坑清单新增第 6 条（frontend 与端口手绑 + 后台服务防弹窗、建议 pythonw）

**实测验证**：
- `services.json` JSON 解析合法，`frontend` 共 4 条且值与端口对应
- `uv run ruff check apps/dev-console` 通过
- 重启 dev-console 后 `/api/status` 返回各服务 `frontend` 字段正确；（「打开」按钮的实际点选由仪表盘人工验证）

**偏差说明**：
- `frontend` 为完整绝对 URL，偏静态，刻意**不进配置面板可编辑集**（`EDITABLE_FIELDS` 不含它），只由 `services.json` 模板声明；改端口需手动同步 frontend，已写入 buildApp skill 坑清单
- dev-console 为单文件微服务，无独立 tech/architecture-flow 文档（由 AGENTS.md 代码地图承载），本次只同步 ROADMAP/AGENTS 心智模型未失真

**待办更新**：
- [x] 服务卡片「打开」按钮 + frontend 字段（2026-09-12）
- [ ] 补基础测试（进程探测纯函数部分）
- [ ] services.json 模板化（去掉本机绝对路径）
- [ ] Windows 服务化（nssm 或 Task Scheduler 常驻）
