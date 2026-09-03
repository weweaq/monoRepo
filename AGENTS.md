# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-22
**Commit:** f8b09aa
**Branch:** master

## OVERVIEW
本地开发服务控制台：单文件 Python (stdlib only) HTTP 服务 + 原生 JS 仪表盘，用于在本机启停/检视 `services.json` 中定义的自家开发服务。仅绑 loopback，变更接口需 token。

## STRUCTURE
```
dev-console/
├── server.py            # 后端全部逻辑（640 行，唯一源码模块）
├── public/
│   └── index.html       # 仪表盘前端（原生 HTML/JS，fetch 调用 /api/*，无框架）
├── services.json        # 受管服务模板（提交入库，勿写绝对路径敏感信息）
├── services.local.json  # 用户本地覆盖（gitignored，仅 interpreter/args/cwd/env/ports）
├── start.bat            # 启动器（pythonw，无窗口）
├── start-console.vbs    # 双击启动器（wscript，零窗口，优于 .bat）
├── logs/                # 运行产物：logs/<runstamp>/{app.jsonl, services/<id>.log}
└── .omo/                # opencode 续跑上下文（运行时）
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| 改 API 路由/鉴权 | `server.py` `Handler` 类 `do_GET`/`do_POST` | 路由表是 if-chain，非装饰器 |
| 改服务启停逻辑 | `server.py` `start_service`/`stop_service` | Windows 进程管理，powershell 枚举 |
| 改配置加载/覆盖 | `server.py` `load_config`/`save_service_override` | 模板 + local 覆盖合并 |
| 改前端 UI | `public/index.html` | 原生 JS，直接 fetch，无构建步骤 |
| 加受管服务 | `services.json` 加条目 | 必需字段见下；勿手改 services.local.json |
| 看运行日志 | `logs/<最新 runstamp>/app.jsonl` + `services/<id>.log` | JSONL 格式，给 AI 看 |

## CODE MAP
| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `main` / `_main` | fn | server.py:561 / :583 | 入口；安全守卫 → 绑定 → serve_forever |
| `Handler` | class | server.py:389 | 路由 + JSON 响应；`log_message` 被禁（日志走 JSONL） |
| `do_GET` | fn | server.py:420 | `/`, `/api/status`, `/api/log`, `/api/config` |
| `do_POST` | fn | server.py:532 | `/api/start`, `/api/stop`, `/api/config`（需 token） |
| `load_config` | fn | server.py:130 | 读 services.json + 叠加 services.local.json |
| `save_service_override` | fn | server.py:155 | 写 services.local.json（仅 EDITABLE_FIELDS） |
| `sync_port_args` | fn | server.py:176 | 改端口时同步重写 args 里的 `--port` |
| `start_service` | fn | server.py:295 | 单实例守卫 → 启进程 → 1.2s 存活校验 |
| `stop_service` | fn | server.py:358 | 按 match 子串找 PID → Stop-Process |
| `detect_pids` | fn | server.py:223 | 命令行子串匹配（match 字段），存活判定核心 |
| `list_processes` | fn | server.py:187 | 单次 powershell 枚举全量进程，带缓存 |
| `_kill_pids_force` | fn | server.py:268 | 单次 powershell 强杀一批 PID 并在同一调用内校验存活 |
| `_free_service_ports` | fn | server.py:301 | 启动前强杀所有占用声明端口的进程（含外来占用者；杀不动才拒绝启动） |
| `JsonlLogger` | class | server.py:93 | 追加写 JSONL（AI 可解析），带锁 |
| `now_cst` | fn | server.py:84 | 东八区时间（zoneinfo 优先） |

## CONVENTIONS
- **stdlib only**：不引第三方库；新增依赖需先确认（当前零依赖）。
- **Windows 优先**：进程枚举/启停走 powershell；`CREATE_NO_WINDOW`/`DETACHED_PROCESS` 用于所有子进程，保持无窗口。非 Windows 上为 no-op。
- **配置双层**：`services.json` 是模板（入库），`services.local.json` 是本地覆盖（gitignored，仅 5 个可编辑字段）。"恢复默认"= 删该服务覆盖。
- **日志 JSONL**：所有日志走 `JsonlLogger`（`logs/<runstamp>/app.jsonl`），禁止 `print`/裸 `logging` 到控制台（pythonw 无控制台）。
- **前端零构建**：`public/index.html` 自包含原生 JS，改完即生效，无打包/编译。
- **bat 脚本不含中文**（全局军规；`start.bat` 当前合规）。

## ANTI-PATTERNS (THIS PROJECT)
- **禁止绑非 loopback**：`_main` 会拒绝 `bind_host` 非回环并退出（安全）。
- **禁止空 token 启动**：`token` 为空时拒绝启动，避免变更接口裸奔。
- **禁止子进程带窗口**：任何 `subprocess` 启子进程必须带 `CREATE_NO_WINDOW`（否则每次轮询/启停闪黑窗）。
- **禁止提交 `services.local.json`**：已 gitignore；本地路径/解释器差异只放 local。
- **禁止给 `do_POST` 增路由却漏 `_token_ok` 校验**：变更接口必须先过 token。
- **勿用 `as any` / `@ts-ignore` 等类型抑制**（全局军规，本文件是 Python，对应：勿吞异常后静默）。
- **勿在 `Handler.log_message` 恢复 stderr 日志**：运行日志必须进 JSONL。

## UNIQUE STYLES
- 单实例守卫靠 `SingleInstanceHTTPServer.allow_reuse_address = False` + 绑失败即退出（防 Windows 端口复用导致双实例抢服务）。
- 进程"存活"判定 = 命令行子串匹配 `match` 字段，**多条件为 AND 语义**（全部 needle 命中才算，如 opencode 的 `["opencode", "--port 4096"]`），故 `match` 必须唯一且稳定；单条件不受影响。
- 启动前端口清理 = **强杀所有**占用声明端口的进程（dev-console 最高权力策略）：命令行命中本服务 match 的是自家僵尸，其余按外来占用者处理，两类都杀且都留 PID+命令行审计日志；只有杀不动（如对方是管理员权限进程、本控制台非提权）才拒绝启动并提示 taskkill。分类用 `list_processes()` 实时快照，勿改回带缓存的 `get_processes()`。
- `/api/status` 对有 ports 的运行中服务做 TCP connect 探测返回 `ports_open`，前端据此区分绿点（健康）/黄点（进程在但端口未就绪）。
- 启动后 `time.sleep(1.2)` + `proc.poll()` 校验，捕获"立即退出"并把日志尾部回给前端。

## COMMANDS
```bash
# 启动（无窗口，后台常驻）
start-console.vbs        # 推荐：双击，零窗口
# 或 start.bat（会闪一次 cmd 窗）

# 手动前台运行（调试用，有控制台可看 print/FATAL）
D:\softwares\miniconda\envs\py12\python.exe server.py

# 访问
#   仪表盘: http://127.0.0.1:8787/
#   API:    GET /api/status  GET /api/config  POST /api/start|/api/stop (header X-DevConsole-Token)
```
无 build / 无 test / 无 lint（项目无测试框架，无 CI）。

## NOTES
- **codegraph 索引跨仓库**：本机 codegraph 返回的是其他 repo（crewAI/xiaozhi 等），不可信；改本项目管理靠直接读 `server.py`。
- `services.json` 里的 `cwd`/`interpreter` 是绝对路径（本机专属），换机需改或用 `services.local.json` 覆盖。
- `services.local.json` 当前已覆盖 `py-wei`（用 `pythonw.exe` + 端口 8000）。
- 改完配置需"重启服务"才生效（前端提示语已说明）；`/api/config` 保存只写 local 覆盖，不重启 server。
