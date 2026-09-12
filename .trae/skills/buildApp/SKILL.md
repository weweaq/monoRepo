---
name: "buildApp"
description: "在 mono 仓库按规范新建一个 app(独立运行应用或本地工具),并把任意本地服务接入 dev-console 受管服务(services.json),使其出现在仪表盘可一键启停/重启/看日志/改配置。当用户要新建 app、把本地工具/HTTP 服务纳管进开发控制台时触发。产出新 app 骨架(按 mono 规范) + 一份可提交的 services.json 变更 + 接入核查清单。"
---

# 在 mono 新建 app 并接入管理台 (buildApp)

两部分：**A. 按 mono 规范新建一个 app**（依赖方向、共用环境、日志规范、公共模块复用、目录结构、文档同步）；**B. 把它接入 dev-console 成为受管服务**（可在仪表盘一键启停/看日志/改配置）。A、B 可分开用（只建 app 不纳管，或只把已有服务纳管）。

## 何时使用

- 要在 mono 仓新建一个独立运行的应用 / 本地工具 / HTTP 服务
- 新建后想把它接进 dev-console 统一启停
- 用户说"新建一个 app"、"把某服务加进管理台"、"接入 dev-console"

---

# A. 在 mono 新建 app 的规范

## 背景：仓库结构

- `apps/` = 可独立运行的应用（有入口，可被 `uv run --package <name>` 启动）
- `packages/` = 共享包（无入口，被 apps 依赖）
- workspace members = `apps/*` + `packages/*`（根 `pyproject.toml` 自动纳入）
- 根 dev 组提供 `pytest / ruff / mypy / pre-commit`；根 ruff(`line-length=120`)、根 pytest(`basetemp=tmp_pytest`) 全局生效，app 一般不重复配置

## 硬规则（来自根 AGENTS.md R 系列，冲突以此为准）

1. **依赖方向单向（R2）**：`apps → packages`；packages 之间单向无环；packages **禁止 import 任何 app**；app 之间不能互相 import（需协作只能经 packages 或约定接口）。
2. **公共模块复用（R3 Rule of Three）**：相同逻辑出现第 1、2 次**各自实现，不抽包**；第 3 次才晋升进 `packages/`（以实现最完整版本为底，其余改引用补齐测试）。**禁止"预防性抽象"**——不为未来需求提前建包。当前 `packages/` 仍为空是正常的。
3. **新增依赖写进 pyproject.toml（R2）**：禁止"顺手 pip install"不落盘。workspace 内互引永远走 `[tool.uv.sources] workspace = true`，不经 PyPI。
4. **测试必带放对位置（R7）**：业务测试放 `apps/<name>/tests/`（与被测模块同包）；根 `tests/` 只放仓库自身结构性测试。提交前全绿：`uv run ruff check apps packages tests` + `uv run --all-packages pytest`。
5. **运行时数据入 data/ 且 gitignore（R11）**：日志、数据库、图片等运行时产物放 `data/`（或 app 内 `data/` 子目录），整目录 gitignore；每个数据目录留 README.md 说明来源与生成方式；严禁提交真实个人数据。
6. **密钥不落地（R9）**：`.env` 永不入库，根 `.env.example` 列全量键名值留空；缺密钥显式报错退出，禁止静默降级。
7. **数据库约定（R6，若用 DB）**：每表必有 `created_at`/`updated_at` 默认东八；schema 变更走 `_migrate_*` + `PRAGMA user_version` 递增；数据接入默认幂等。

## 入口与打包

pyproject `name` 是 **workspace 成员名，不一定等于目录名，也不一定等于 Python import 名**（例：目录 `withlanggraph` → `name="gacore"`；目录 `dev-console` → `name="dev-console"` 但入口模块是 `server`）。真正决定 import 名的是构建配置与包目录结构。三种入口模式：

| 方式 | 用法 | 适用 |
|------|------|------|
| `[project.scripts]` | `dev-console = "server:main"` → `uv run --package <name> dev-console` | 有 CLI 入口，最直观 |
| `__main__.py` | `src/<pkg>/__main__.py` → `python -m <pkg> [sub]` | 有 python -m 子命令结构 |
| 脚本路径 | `args: ["path/start.py"]`（dev-console 纳管时用） | 单文件服务 |

多模块 app 用 `src/<pkg>/` 布局 + `[tool.setuptools.packages.find] where=["src"]` 或 hatchling；单文件 app 可直接 `[tool.setuptools] py-modules=["server"]`。`[tool.uv] package=false` 仅用于 name 与标准库同名需虚拟成员等特殊场景（如 checkself 的 `profile`）。

## 共用环境接线

- 依赖 workspace 内共享包：`dependencies = ["<pkg>"]` + `[tool.uv.sources] <pkg> = { workspace = true }`
- 新 app 加进来后执行 `uv sync --all-packages`（uv 默认只装根项目，成员必须显式 `--all-packages`）
- 根 ruff/pytest 已覆盖 `apps`，一般无需在 app 里重复写；如需收紧可在 app pyproject 里加 `[tool.ruff]`（注意保留根 `line-length=120` 基调，见 checkself）

## 日志规范

仓库现有三种 JsonlLogger（字段名/路径/时区不完全统一），新 app 的**推荐约定**（不是既有事实，历史 app 有差异）：

- 封装自己的 JSONL Logger（追加写、带锁、可解析不覆盖），多模块 app 放 `src/<pkg>/logger.py`，单文件 app 直接放同级模块（如 `server.py`/`log.py`）
- 输出到 app 内 `logs/<YYYY-MM-DD>/app.jsonl` 或 `logs/<runstamp>/app.jsonl`，由 app `.gitignore` 管（根 `.gitignore` 只 ignore `data/`；`apps/*/logs/` 需 app 自己 gitignore）
- 时间字段**推荐**统一东八（Asia/Shanghai，ISO 带 +08:00，参考 withlanggraph/dev-console 的 `now_cst`；注意 checkself 当前用的是本地时间）
- 敏感字段脱敏：logger 层对 `api_key/password/token/secret/email/phone/authorization` 等掩码（参考 withlanggraph；dev-console 目前未脱敏，属历史差异）
- 服务端/后台运行类 app 推荐禁用 `print`，走 JsonlLogger（尤其被 dev-console / pythonw 托管的无控制台进程）；交互式 CLI 可保留 stdout

## R5 文档三处同步（新建 app 时初始化）

任何代码改动都要三处同步，新建 app 时先把三处骨架建好：

1. **`apps/<name>/ROADMAP.md`** — 空执行记录表 + 待办清单
2. **`apps/<name>/docs/<name>-tech.md`** — 接口/实体/表/数据流四节骨架
3. **`apps/<name>/docs/architecture-flow.mmd`** — 涉及模块/数据流/依赖时同步；## **架构图是单一真源，tech 只引用 `./architecture-flow.mmd` 相对链接，不得在 tech 里复制维护副本**（tech 自己的流程/时序图正常内嵌）

文档变更单独成提交（`docs(scope): ...`）。

## app 标准目录清单（新建时按需建）

```
apps/<name>/
├── pyproject.toml     必须（成员声明+依赖）
├── .gitignore         本地运行时产物
├── ROADMAP.md         推荐（执行记录，R5）
├── AGENTS.md          推荐（包级特有约定，R8，不得与根冲突）
├── docs/              推荐（<name>-tech.md、architecture-flow.mmd）
├── tests/             推荐（业务测试，R7）
├── src/<pkg>/         多模块（或顶层模块文件）
├── .env.example       有密钥时（R9）
├── data/              视运行时数据（R11，gitignore+README）
└── logs/              视需要
```

## 新建 app 最小执行流程

1. `uv init --package apps/<name>`（或手动起目录 + pyproject.toml）
2. 填 `pyproject.toml`：`name`（workspace 成员名）、`dependencies`、可选 `[project.scripts]`/构建
3. 建 `ROADMAP.md` + `AGENTS.md`（推荐）
4. 按需建 `docs/`、`tests/`、`src/`、`.env.example`、`data|logs/`
5. `uv sync --all-packages`
6. 在仓库根执行门禁：`uv run ruff check apps packages tests` + `uv run --all-packages pytest apps/<name>/tests`
7. 需要纳入统一启停再进 B 部分

---

# B. 接入 dev-console 受管服务

## 背景

`apps/dev-console/` = 本地开发服务控制台：stdlib-only 后端 + 原生 JS 前端。读 `services.json`（模板，入库）+ `services.local.json`（本地覆盖，gitignored）。仪表盘可启停/重启/看日志/改配置。绑 `127.0.0.1`，POST 变更接口需 `X-DevConsole-Token`。

## 服务条目字段

| 字段 | 必填 | 缺省 | 说明 |
|------|------|------|------|
| `id` | 是 | — | 唯一标识，用于 API / 配置覆盖 / 日志路径 |
| `name` | 是 | — | 仪表盘显示名 |
| `interpreter` | 是 | — | 可执行文件路径（`.venv\Scripts\python.exe` 等），与 args 拼成子进程命令 |
| `args` | 否 | `[]` | 命令行参数字符串数组，`cmd = [interpreter, *args]` |
| `cwd` | 否 | 继承父进程 | 工作目录 |
| `env` | 否 | `{}` | 环境变量，与系统环境 merge |
| `ports` | 否 | `[]` | 声明端口：启动前强杀占用者 + 状态展示 |
| `match` | 否 | `[]` | 进程存活判定子串（AND 语义、大小写不敏感） |
| `notes` | 否 | `""` | 注释 |

## 配置双层制

- `services.json` = 模板（入库），`services.local.json` = 本地覆盖（gitignored，只存你改过的字段）
- 前端/API 可编辑字段限 `interpreter / args / cwd / env / ports / match`（`notes`、`id`、`name` 不在可编辑集，需改 services.json 本体）；"恢复默认"= 删该服务在 local 的覆盖条
- 变更 `services.json` 后需重启 dev-console（config 是启动时加载）

## 关键的坑（必须规避）

1. **`match` 是 AND 语义、大小写不敏感、子串匹配**：全部子串同时命中才判存活。写全项目/进程标识（如 `["gacore.langTrack"]`、`["opencode", "--port 4096"]`），避免误伤终端里相似进程。
2. **不写 `match` → 服务永远判未运行**：`detect_pids` 对空 match 直接返回空，单实例守卫失效，启动/停止/状态展示都不可靠。无 CLI 可匹配的裸服务别纳管。
3. **端口强杀是最高权力**：`_free_service_ports` 启动前会**无条件强杀所有占用声明端口的进程**（含外来进程），强杀失败则拒绝启动。所以端口号必须**选仓库内唯一的专属端口**。
4. **`interpreter` 是"可执行文件"，`args` 是参数**，`cmd = [interpreter, *args]`；`args` 可为空（服务本身就是可执行文件，如 opencode.exe）。
5. **绑定安全**：dev-console 自身强制只绑 loopback。它**不会**校验纳管服务绑什么 host，但安全上建议你的服务尽量只绑 `127.0.0.1`（langtrack/app-apk 绑 `0.0.0.0` 是为被手机/网络访问，属有意的例外）。
6. **POST 全部需要 token**：`/api/start`、`/api/stop`、`/api/restart`、`/api/config` 都在 `_token_ok()` 后；`X-DevConsole-Token` 缺失/错误/服务自身 token 为空都会拒绝。你的 app 若有变更接口也别裸奔。

## mono Python app 的典型写法

- `interpreter` 通常指向 `.venv\Scripts\python.exe`；非 Python 工具/外部环境按各自填写（miniconda `pythonw.exe`、node `opencode.exe`），dev-console 对 interpreter 不做限制。
- `args` 两种模式：
  - **A（推荐）**：`["-m", "<package>.<entry>", "--host", ...]`，需有可 `-m` 模块入口（如 `gacore.langTrack`）
  - **B**：脚本路径，如 `["D:\\...\\start.py"]` 或相对路径 `["web/app.py"]`（**相对路径相对 `cwd` 解析**），需脚本有 `if __name__=="__main__"` 或就是要执行的工具
- **若服务进程需 import workspace 包**，必须配 `cwd` + `env.PYTHONPATH`。原因：dev-console 用 `subprocess.Popen` 启子进程，只**继承父进程环境变量**，不会像 `uv run` 那样自动把 workspace `src/` 加入 import 路径。纯 stdlib 单文件 / 不依赖 workspace 包的服务可不配。

## 接入流程

1. **摸清启动方式**：`-m` 可用还是只有脚本？依赖什么 env/PYTHONPATH？需要什么端口？用哪个 cmdline 子串做 `match` 最稳（不撞别人）？
2. **起草条目**：填 `id/name/interpreter/args` + `cwd/env` + `ports/match`。对照坑清单核 match、端口、PYTHONPATH 是否必需。
3. **确认端口唯一 + match 唯一**：扫 `services.json` 现有端口与 match，不得冲突。
4. **改 services.json 并验证**：重启 dev-console，在 `http://127.0.0.1:8787/` 点启动，确认运行态、日志输出、能停止/重启、match 不误伤。
5. **提交**：R4（`feat(dev-console): ...` 或跨包 scope）。`services.local.json` 不入库。

## 验收

**A（新建 app）：**
- [ ] pyproject name=导入名、dependencies 落盘、入口可启动
- [ ] 依赖方向合规（apps→packages 单向，无 app 互 import）
- [ ] 公共逻辑按 R3（第 3 次才抽包，无预防性抽象）
- [ ] `uv sync --all-packages` + ruff + pytest 全绿
- [ ] 运行时数据在 data/ 且 gitignore；有密钥的话 .env.example 已补
- [ ] R5 三处（ROADMAP/tech/architecture-flow.mmd）已初始化且不失真

**B（接入 dev-console）：**
- [ ] services.json 条目字段齐全（id/name/interpreter 必填 + args/cwd/env + ports/match）
- [ ] match 子串全项目标识，AND 语义下不误伤
- [ ] 端口为仓库内唯一，不会被别的服务强杀互踩
- [ ] 若依赖 workspace 包：cwd + env.PYTHONPATH 正确，启动不报 ImportError
- [ ] 仪表盘实测：启动→运行→停止/重启三态正确，日志有输出