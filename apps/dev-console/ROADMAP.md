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
