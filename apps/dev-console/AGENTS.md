# AGENTS.md — dev-console 包级约定

> 本文件是 dev-console 的特有约定，与根 AGENTS.md 配合使用。
> 冲突时以根 AGENTS.md 为准，并提 ADR 修订。

## 1. 项目定位

本地开发服务控制台：单文件 Python（stdlib only）HTTP 服务 + 原生 JS 仪表盘，用于在本机启停/检视 `services.json` 中定义的自家开发服务。仅绑 loopback，变更接口需 token。

## 2. 特有约定（本包独有，根规则未覆盖）

### 2.1 技术约束
- **stdlib only**：不引第三方库；新增依赖需提 ADR 确认（当前零依赖）
- **Windows 优先**：进程枚举/启停走 powershell；`CREATE_NO_WINDOW` / `DETACHED_PROCESS` 用于所有子进程，保持无窗口。非 Windows 上为 no-op
- **前端零构建**：`public/index.html` 自包含原生 JS，改完即生效，无打包/编译

### 2.2 配置双层制
- `services.json` 是模板（入库），含服务定义骨架与默认值
- `services.local.json` 是本地覆盖（gitignored），仅保留 `interpreter / args / cwd / env / ports` 五个可编辑字段
- "恢复默认" = 删除该服务在 local 中的覆盖条目

### 2.3 日志规范
- 所有运行日志走 `JsonlLogger`（`logs/<runstamp>/app.jsonl`），AI 可解析
- 禁止 `print` / 裸 `logging` 输出到控制台（pythonw 无控制台）
- 启动期异常写入 `logs/startup-error.log`，避免静默失败

### 2.4 安全红线
- **禁止绑非 loopback**：`_main` 会拒绝非回环 `bind_host` 并退出
- **禁止空 token 启动**：`token` 为空时拒绝启动，避免变更接口裸奔
- **禁止给 `do_POST` 新增路由却漏 `_token_ok` 校验**：变更接口必须先过 token
- **禁止子进程不带 `CREATE_NO_WINDOW`**：否则每次轮询/启停闪黑窗

## 3. 代码地图

| 符号 | 类型 | 位置 | 作用 |
|------|------|------|------|
| `main` / `_main` | fn | server.py | 入口：安全守卫 → 绑定 → serve_forever |
| `Handler` | class | server.py | 路由 + JSON 响应；`log_message` 被禁（日志走 JSONL） |
| `do_GET` | fn | server.py | `/`, `/api/status`, `/api/log`, `/api/config` |
| `do_POST` | fn | server.py | `/api/start`, `/api/stop`, `/api/config`（需 token） |
| `load_config` | fn | server.py | 读 services.json + 叠加 services.local.json |
| `save_service_override` | fn | server.py | 写 services.local.json（仅 EDITABLE_FIELDS） |
| `start_service` | fn | server.py | 单实例守卫 → 端口清理 → 启进程 → 1.2s 存活校验 |
| `stop_service` | fn | server.py | 杀 match 命中 PID + 声明端口占用者 |
| `detect_pids` | fn | server.py | 命令行子串 AND 匹配，存活判定核心 |
| `list_processes` | fn | server.py | 单次 powershell 枚举全量进程，带 10s 缓存 |
| `_kill_pids_force` | fn | server.py | 单次 powershell 强杀一批 PID 并同调用内校验存活 |
| `_free_service_ports` | fn | server.py | 启动前强杀所有占用声明端口的进程（最高权力策略） |
| `JsonlLogger` | class | server.py | 追加写 JSONL，带锁 |
| `now_cst` | fn | server.py | 东八区时间（zoneinfo 优先，fallback 固定偏移） |

## 4. 目录结构

```
dev-console/
├── server.py            # 后端全部逻辑（唯一源码模块）
├── public/
│   └── index.html       # 仪表盘前端（原生 HTML/JS）
├── services.json        # 受管服务模板（入库）
├── services.local.json  # 用户本地覆盖（gitignored）
├── start.bat            # 启动器（pythonw，无窗口）
├── start-console.vbs    # 双击启动器（wscript，零窗口）
├── logs/                # 运行产物（gitignored）
├── pyproject.toml       # workspace 成员声明
├── AGENTS.md            # 本文件
└── ROADMAP.md           # 执行记录与进度
```

## 5. 常用命令

```powershell
# 前台运行（调试用）
uv run --package dev-console dev-console

# 无窗口后台启动
start-console.vbs

# 访问
#   仪表盘: http://127.0.0.1:8787/
#   API:    GET /api/status  GET /api/config
#           POST /api/start|/api/stop|/api/restart|/api/config
#           Header: X-DevConsole-Token: <token>
```
