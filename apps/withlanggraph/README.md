# gacore

GenericAgent 核心的 LangGraph 重实现（学习项目）。

gacore 用 [LangGraph](https://langchain-ai.github.io/langgraph/) 重新实现了
[GenericAgent](https://github.com/lsdefine/GenericAgent)（下称 GA）的核心循环：
把 GA 里手写的 `while` 循环、增量消息、StepOutcome 机制，改写成官方
`langchain.agents.create_agent` 预置循环 + 自定义 AgentMiddleware + 显式状态通道 +
`interrupt()` 人类介入。代码刻意保持小、可读、带注释，目的是讲清楚"GA 的每个
设计决定，在 LangGraph 里对应什么、为什么这样对应"。

架构细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 功能特性 (Features)

- **官方 `create_agent` 循环 + 自写 middleware**：图拓扑（model 节点 + 预置
  `ToolNode` + 工具路由）由 `langchain.agents.create_agent` 提供；GA 的回合控制
  逻辑拆成两个 `AgentMiddleware`：`GAPromptMiddleware`（每轮动态系统提示词）与
  `GATurnLogicMiddleware`（exit_reason 短路、max_turns 守卫、空响应重试、
  done_hooks 续接、任务完成），通过官方 `hook_config`/`jump_to` 通道改变控制流，
  见 ARCHITECTURE.md 的拓扑图。

- **24 个原子工具**：
  - **代码与文件**：`code_run`、`file_read`、`file_patch`、`file_write`
  - **Web**：`web_scan`（httpx 静态抓取）、`web_execute_js`（stub，恒返回不支持）
  - **浏览器历史**：`browser_history`（Edge SQLite）、`bili_history`（B站观看历史，需 `bili` CLI 登录）
  - **记忆**：`update_working_checkpoint`（工作记忆）、`start_long_term_update`（长期记忆）
  - **每日笔记**：`read_daily`、`edit_daily`、`search_daily`（基于文件系统的每日笔记）
  - **邮件**：`send_email`（SMTP 发信，支持 HTML 正文与内联图片，配置走 SMTP_* 环境变量）
  - **网易云音乐**：`ncm_me`、`ncm_search_song`、`ncm_song`、`ncm_lyric`、`ncm_playlist_list`、`ncm_playlist_detail`、`ncm_login`（扫码登录）
  - **OCR**：`ocr_image`（本地图片 OCR）、`ocr_screen`（截图 OCR，基于 rapidocr-onnxruntime）
  - **人机交互**：`ask_user`（中断暂停，等待用户回复）
  其中 `browser_history` 读取 Edge 浏览历史（SQLite），支持关键词 / 域名 / 时间范围过滤；
  `bili_history` 封装 `bili` CLI，获取 B站认证用户的观看历史；
  `send_email` 移植自 py-wei 的 SMTP 发送器，其余与 GA 工具集一一对应。

- **三层记忆系统（L0 / L1 / L2）**：
  - **L0 工作记忆**：`update_working_checkpoint` 写入 RAM 中的 `state.working`，每轮注入系统提示词
  - **L1 每日笔记**：`read_daily` / `edit_daily` / `search_daily` 按日期组织，记忆重要决策、教训、用户偏好，跨会话持久化
  - **L2 长期记忆**：`start_long_term_update` 从每日笔记蒸馏精华到 `memory/global_mem.txt`（全局事实）与 `memory/global_mem_insight.txt`（洞察索引）

- **定时任务调度器**：`scheduler.py` 提供单进程轮询调度器，在指定时间触发 agent 执行自包含任务（如每日报告、周报），无需人工交互。支持 `"HH:MM"` 每日触发和 `"every N<m|h|d>"` 间隔触发；job 定义在 `config/schedule.json`，支持热更新。输出写入 `logs/scheduled/`。每个 job 的 `deliver_to` 决定结果投递通道：`"file"`（默认，写输出文件 + 每日笔记）或 `"email"`（额外通过 `send_email` 发送，收件人按 `email_to` → `SMTP_TO` → `SMTP_USER` 依次取）。

- **ask_user 人类介入中断**：`ask_user` 工具调用 `interrupt()` 暂停图执行，
  配合 MemorySaver 检查点和 `Command(resume=...)` 恢复。

- **max_turns 守卫**：middleware 的 `before_model` 钩子在调用 LLM 前检查轮数，
  超过上限直接以 `MAX_TURNS_EXCEEDED` 终止，不会再消耗一次模型调用。

- **done_hooks 续接**：收尾提示队列（GA 的 `_done_hooks`），任务主体完成后按序
  追加一轮 HumanMessage 继续执行（middleware `after_model` 经 `jump_to="model"`
  回环）。

- **LLM 异常兜底**：官方 `ModelRetryMiddleware`（max_retries=0）把 provider 异常
  转成 `[Agent error: ...]` 消息，图以 `AGENT_ERROR` 干净退出，不崩溃。

- **JSONL 结构化日志**：每天写入 `logs/YYYY-MM-DD/app.jsonl`，同一天的运行追加到同一文件，
   每条日志含 timestamp / level / module / message，错误日志附 error_type / stack_trace / context。

- **QQ Bot 前端**：`src/gacore/frontends/qq.py` 提供 QQ 官方机器人接入，支持私聊与群 @ 消息，
  会话按用户隔离，支持 `ask_user` 中断恢复。

---

## 环境要求 (Requirements)

- **Python 3.12**（pyproject.toml 声明 `>=3.12,<3.14`）。
- 本机路径示例：

  ```
  D:\softwares\miniconda\envs\py12\python.exe
  ```

- 运行依赖：`langchain`、`langgraph`、`langgraph-checkpoint`、`langchain-core`、
  `langchain-openai`、`langchain-anthropic`、`python-dotenv`、`httpx`、`prompt_toolkit`、
  `Pillow`、`rapidocr-onnxruntime`、`numpy`。
  本机锁定的关键版本：langchain 1.3.14、langgraph 1.2.10、langgraph-checkpoint 4.1.1、
  langchain-core 1.5.3。

---

## 安装 (Install)

在项目根目录（含 `pyproject.toml`）执行：

```powershell
pip install langchain langgraph langgraph-checkpoint langchain-core langchain-openai langchain-anthropic python-dotenv httpx prompt_toolkit Pillow rapidocr-onnxruntime numpy
```

开发 / 测试额外依赖：

```powershell
pip install pytest ruff
```

QQ Bot 前端额外依赖：

```powershell
pip install qq-botpy
```

项目使用 src 布局，不安装为包也可运行，见下文"运行"。

---

## 配置 (Configuration)

复制 `.env.example` 为 `.env` 并填写：

```powershell
Copy-Item .env.example .env
```

支持的变量：

| 变量 | 说明 | 示例 |
| :--- | :--- | :--- |
| `LLM_PROVIDER` | 模型提供方，`openai` / `anthropic` / `deepseek` | `deepseek` |
| `OPENAI_API_KEY` | OpenAI 系 API Key | `sk-xxx` |
| `OPENAI_BASE_URL` | OpenAI 兼容网关地址，留空用官方 | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | OpenAI 模型名 | `gpt-4o` |
| `ANTHROPIC_API_KEY` | Anthropic API Key | `sk-ant-xxx` |
| `ANTHROPIC_MODEL` | Anthropic 模型名 | `claude-sonnet-4-5` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（`platform.deepseek.com` 获取） | `sk-xxx` |
| `DEEPSEEK_BASE_URL` | DeepSeek 网关地址，留空用 `https://api.deepseek.com/v1` | `https://api.deepseek.com/v1` |
| `DEEPSEEK_MODEL` | DeepSeek 模型名 | `deepseek-v4-pro` |
| `DEFAULT_MAX_TURNS` | 单次任务默认最大轮数 | `40` |
| `SMTP_USER` | 发件账号（QQ/163 填 SMTP 授权码而非登录密码），`send_email` 工具用 | `xxx@qq.com` |
| `SMTP_PASSWORD` | 发件密码 / 授权码 | |
| `SMTP_TO` | 默认收件人，`send_email` 未传 `to` 时的兜底 | |
| `SMTP_HOST` | SMTP 服务器，留空按发件域名自动检测（qq/gmail/outlook/163） | `smtp.qq.com` |
| `SMTP_PORT` | SMTP 端口，留空自动推断（SSL 465 / STARTTLS 587） | `465` |
| `SMTP_SSL` | 是否启用 SSL（`1`/`true`/`yes`），留空按端口推断 | |
| `SMTP_TIMEOUT` | SMTP 连接超时（秒） | `10` |

`deepseek` 与 GA 的配置一致（`configure_mykey.py` 中 `type: native_oai`）：走 OpenAI 兼容
协议，由 `ChatOpenAI` 驱动，默认 base URL 为 `https://api.deepseek.com/v1`、默认模型为
`deepseek-v4-pro`（可选 `deepseek-v4-flash`）。

### QQ Bot 配置

| 变量 | 说明 |
| :--- | :--- |
| `QQ_APP_ID` | QQ 开放平台 App ID |
| `QQ_APP_SECRET` | QQ 开放平台 App Secret |
| `QQ_ALLOWED_USERS` | 白名单，`*` 或逗号分隔的 openid |
| `QQ_ADMIN_USERS` | 可触发 `/reboot` 的用户，`*` 或逗号分隔的 openid |
| `QQ_LOG_FILE` | QQ Bot 日志文件路径，默认 `logs/qq.log` |

另有环境变量可覆盖目录（不写进 .env 也行）：`GACORE_ASSET_DIR`、`GACORE_MEMORY_DIR`、
`GACORE_LOGS_DIR`、`GACORE_TEMP_DIR`，相对路径以项目根为基准。测试用
`GACORE_MEMORY_DIR` 指向临时目录，避免污染真实 `memory/`。

---

## 运行 (Run)

### 交互式 CLI（REPL）

```powershell
$env:PYTHONPATH = "src"
python -m gacore
```

要点：

- 启动后逐行输入任务，`/quit` 退出；一轮以 `EXITED` 结束时也会自动退出 REPL。
- **流式回放**：REPL 用 `graph.stream(stream_mode="updates")` 驱动，每个图节点的
  产出会实时打印——agent 调用工具时显示 `[agent] -> tool(args)`，工具结果回显为
  `[tools] <- ...`，最终回答直接输出，全程带节点标签，方便观察一轮任务里
  「agent 决定 → 工具执行 → 得出结论」的完整过程。
- 内置斜杠命令：

  | 命令 | 作用 |
  | :--- | :--- |
  | `/help` | 列出全部命令 |
  | `/working` | 显示当前工作记忆（working checkpoint） |
  | `/memory` | 显示长期记忆文件 `memory/global_mem*.txt` 的内容 |
  | `/reset` | 清空会话历史，开启新线程（换一个 thread_id） |
  | `/quit` | 退出 |

- 模型调用 `ask_user` 时，图会暂停并打印 `[ask_user] <问题>`，然后提示
  `Your answer: `（问题只打印一次，不会在输入提示里重复）。输入回答后通过
  `Command(resume=...)` 恢复执行；输入 `abort` / `exit` / `quit` / `stop` / `cancel`
  （或直接 Ctrl-D）视为中止，本轮以 `EXITED` 结束。
- 未配置 `.env`（缺 `LLM_PROVIDER` 或 API Key）时优雅退出，不崩溃：

  ```
  gacore: ConfigError: LLM_PROVIDER must be one of ('openai', 'anthropic', 'deepseek'), got ''
  ```

  退出码为 1。

### QQ Bot 前端

```powershell
$env:PYTHONPATH = "src"
python -m gacore.frontends.qq
```

每个 QQ 用户独立 `thread_id`，会话历史隔离；支持 `ask_user` 中断恢复——暂停时向用户发送问题，
下一条消息自动恢复执行。

### 定时任务调度器

```powershell
python -m gacore.scheduler
```

前台运行，`Ctrl-C` 停止。job 定义在 `config/schedule.json`，支持热更新——无需重启。

### 接收服务（langTrackApp 数据接收）

```powershell
$env:PYTHONPATH = "src"
python -m gacore.langTrack --host 0.0.0.0 --port 8000 --db langTrack.db
```

- `POST /ingest`：接收手机上报的事件（批量 JSON），幂等去重后落库。
- `GET /health`：健康检查。
- 手机连同一局域网 Wi-Fi，把上报地址指向本机 IP:8000。

---

## 测试 (Testing)

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests -q      # 当前 278 个用例全部通过
python -m ruff check src tests # lint 干净
```

278 个测试覆盖：状态初始化、middleware（before_model 短路 / max_turns 守卫、
after_model 空响应重试 / done_hooks 续接 / 完成 / AGENT_ERROR）单测与图集成、
LLM 工厂（openai / anthropic / deepseek 分支与缺失 Key 报错）、工具注册表、
code_run / file / memory / web / browser_history / bili_history / daily_notes / ocr / email / ncm 工具、
ask_user 中断与恢复（含 langgraph 两种中断表象）、端到端多轮循环与 done_hooks 续接、
流式 REPL 输出与斜杠命令、定时任务调度器、QQ Bot 前端。

---

## 与 GenericAgent 的对应关系 (Mapping to GA)

| GA 文件 | gacore 模块 | 说明 |
| :--- | :--- | :--- |
| `agent_loop.py`（`agent_runner_loop` :42-107） | `graph.py` + `middleware.py` | 主循环拆成官方 create_agent 拓扑 + 两个 AgentMiddleware（提示词 / 回合控制） |
| `agent_loop.py`（工具分发、`StepOutcome`） | 预置 `ToolNode` + `Command(update=...)` | 工具执行；控制信号（`exit_reason` / `working`）由工具直接经 Command 回写 |
| `agent_loop.py`（`no_tool` 分支、`_done_hooks`） | `middleware.py`（`GATurnLogicMiddleware`） | 最终答案校验 + done_hooks 续接（after_model 钩子） |
| `ga.py`（`turn_end_callback` :570、`_get_anchor_prompt`、`get_global_memory` :602） | `context.py` | 每轮提示词组装（系统提示词 + 折叠历史 + 周期提示） |
| `ga.py`（`do_code_run` / `do_file_*` / `do_ask_user` 等） | `tools/*.py` | 24 个原子工具（含 browser_history、bili_history、daily_notes、ocr、send_email、ncm_*，GA 无对应） |
| `llmcore.py`（`client.chat` / Session） | `llm.py` | LLM 工厂，openai / anthropic / deepseek 三选一 |
| `mykey.py` / `NativeToolClient` | `llm.py`（环境变量驱动） | 配置方式替换 |
| `frontends/tui_v3.py` | `cli.py` | 交互前端（REPL） |
| `frontends/qqapp.py` | `frontends/qq.py` | QQ Bot 前端 |
| `memory/global_mem*.txt` | `memory/` + `tools/memory_tools.py` + `tools/daily_notes.py` | L0/L1/L2 三层记忆 |
| （无 GA 对应） | `state.py` / `jsonl_logger.py` / `scheduler.py` | `GAState` 状态通道 + JSONL 日志 + 定时任务调度器 |

忠实移植的语义与刻意简化的差异，逐条记录在 ARCHITECTURE.md 第 4、5 节。

---

## 范围说明 (Scope)

以下 GA 能力**不在**本项目范围内（学习项目刻意裁剪）：

- **TMWebDriver 真实浏览器**：GA 用注入的 Chrome 会话保留登录态、跑真实网页。
  gacore 用 `httpx` 静态抓取作为 `web_scan` 的替代；`web_execute_js` 是显式 stub，
  恒返回"不支持"错误字典（见 `tools/web_tools.py`）。
- **reflect 自治模式**：`reflect/goal_mode.py` 等自驱、时间预算型执行未移植。
- **插件机制**：GA 的 `plugins/hooks.py` hook 链未移植。
- **IM 前端**：Telegram / Discord / 微信 / 飞书等 Bot 前端未移植，仅保留终端 REPL
  和 QQ Bot（`frontends/qq.py`）。
- **L4 会话归档**：长程任务归档记忆未移植（L0/L1/L2 三层已覆盖）。
- **Mixin 多模型故障切换**：GA `NativeToolClient` 的多模型兜底切换未移植，
  gacore 是单一 `LLM_PROVIDER` 的选择逻辑（`llm.py`）。

### gacore 特有（GA 无对应）

- **三层记忆系统**（ARCHITECTURE.md 第 6 节）：在 GA 的 L1/L2 基础上增加了基于文件系统的每日笔记层，填平工作记忆与长期记忆之间的断层。
- **定时任务调度器**（ARCHITECTURE.md 第 7 节）：单进程轮询调度，支持热更新，用于无人值守的周期性任务。
- **本地 OCR 工具**：`ocr_image` / `ocr_screen` 基于 rapidocr-onnxruntime，无需外部 API。
- **B站观看历史**：`bili_history` 工具封装 `bili` CLI。

---

## 项目结构 (Layout)

```
WithLangGraph/
├── pyproject.toml             # 依赖与工具配置（ruff line-length=120）
├── uv.lock                    # uv 依赖锁文件
├── .env.example               # 配置模板，复制为 .env 使用
├── start_all.bat              # 一键启动脚本（REPL + 调度器 + QQ Bot）
├── TODO.md                    # 用户交代的待办事项
├── config/
│   ├── schedule.json          # 定时任务定义
│   └── assets/
│       ├── sys_prompt.txt     # L0 系统规则
│       └── code_run_header.py # code_run 沙箱头
├── docs/superpowers/specs/    # 超能力规格文档
├── memory/
│   ├── global_mem.txt         # L2 全局事实
│   ├── global_mem_insight.txt # L2 洞察索引
│   ├── ocr_history.jsonl      # OCR 历史
│   └── daily/                 # L1 每日笔记（YYYY-MM-DD.md）
├── logs/
│   ├── YYYY-MM-DD/app.jsonl  # JSONL 结构化日志
│   └── scheduled/             # 定时任务输出
├── temp/                      # 运行时临时文件
├── src/gacore/
│   ├── __main__.py            # python -m gacore 入口
│   ├── cli.py                 # 交互 REPL（流式回放 + 斜杠命令 + ask_user 中断处理）
│   ├── graph.py               # create_agent 组装（middleware 链）、编译、run_once
│   ├── middleware.py          # GAPromptMiddleware / GATurnLogicMiddleware（GA 回合控制）
│   ├── state.py               # GAState（继承官方 AgentState）+ 初始化
│   ├── context.py             # 每轮提示词组装（纯函数）
│   ├── llm.py                 # LLM 工厂
│   ├── config.py              # 配置解析（目录 / max_turns）
│   ├── jsonl_logger.py        # JSONL 结构化日志
│   ├── scheduler.py           # 定时任务调度器
│   ├── frontends/
│   │   ├── qq.py              # QQ Bot 前端
│   │   └── qq.bat             # QQ Bot 启动脚本
│   ├── tools/                 # 24 个工具
│   │   ├── __init__.py        # 工具注册表（TOOL_NAMES + build_tool_list）
│   │   ├── ask_user.py        # 人类介入中断
│   │   ├── bili_history.py    # B站观看历史
│   │   ├── browser_history.py # Edge 浏览历史
│   │   ├── code_run.py        # 代码执行（沙箱）
│   │   ├── daily_notes.py     # 每日笔记（read_daily/edit_daily/search_daily）
│   │   ├── email_tools.py     # 邮件（send_email，SMTP + HTML 内联图片）
│   │   ├── file_tools.py      # 文件读写（file_read/file_patch/file_write）
│   │   ├── memory_tools.py    # 长期记忆（update_working_checkpoint/start_long_term_update）
│   │   ├── ncm_tools.py       # 网易云音乐（ncm_me/ncm_search_song/ncm_song/ncm_lyric/歌单/ncm_login）
│   │   ├── ocr_tools.py       # 本地 OCR（ocr_image/ocr_screen）
│   │   └── web_tools.py       # Web 工具（web_scan/web_execute_js）
│   └── memory/
│       └── __init__.py
└── tests/                     # 278 个测试
    ├── conftest.py
    ├── test_cli.py
    ├── test_context.py
    ├── test_e2e.py
    ├── test_graph_loop.py
    ├── test_interrupt.py
    ├── test_llm.py
    ├── test_middleware.py
    ├── test_nodes_tools.py
    ├── test_qq.py
    ├── test_registry.py
    ├── test_scheduler.py
    ├── test_state.py
    ├── test_tools_bili_history.py
    ├── test_tools_browser_history.py
    ├── test_tools_code_run.py
    ├── test_tools_daily_notes.py
    ├── test_tools_email.py
    ├── test_tools_file.py
    ├── test_tools_memory.py
    ├── test_tools_ncm.py
    └── test_tools_web.py
```
