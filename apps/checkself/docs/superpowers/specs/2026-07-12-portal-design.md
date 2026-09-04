# checkSelf Portal 设计文档

> 2026-07-12 创建
> 状态：设计完成，待审阅

---

## 一、背景与目标

### 1.1 起因

checkSelf 个人画像系统 Phase 1 + Phase 2 已跑通全链路（入库 → 意图提取 → 画像生成 → diff → 变化报告），但整个项目的可观测性不足：

- 每次运行的全链路进展只能翻 JSONL 日志文件，没有"一眼看全"的视图
- 数据库内容（raw_data 1620 条 + llm_intents 3465 条）只能用 SQL 查，不能浏览
- LLM 交互（prompt/response/tokens/耗时）埋在日志里，不好翻
- 生成的画像散落在 Obsidian 目录，没有统一入口
- 没有跨运行的趋势对比

### 1.2 目标

构建一个本地 Portal 页面，提供：

1. **工作流进展可视化** — 展示每个任务的当前进展，点击进去看详情（步骤状态 + 实时日志流）
2. **数据库内容浏览** — 对 raw_data 和 llm_intents 的结构化展示
3. **LLM 交互展示** — 对 LLM 调用的 prompt/response/tokens/耗时展示
4. **生成内容展示** — 对画像产出的结构化渲染（5 维度卡片 + 变化报告 diff 表格）
5. **操作能力** — 支持触发整条流水线和分步触发

### 1.3 设计原则

- **零改动复用** — 不改动现有流水线逻辑，只新增展示层和任务调度层
- **轻量依赖** — 仅引入 FastAPI + uvicorn，前端单 HTML + Alpine.js（CDN），无构建步骤
- **进程内执行** — 任务在 portal 进程内通过线程池执行，日志通过 logging Handler 捕获
- **串行锁** — 同一时间只允许一个任务运行，避免 SQLite 并发写问题

---

## 二、决策记录

| 维度 | 决策 | 理由 |
|------|------|------|
| 定位 | 观察 + 操作 | 不仅展示，还要触发刷新和分步执行 |
| 操作范围 | 分步触发 + 整条流水线 | 支持单独跑某一步（入库/意图/画像）+ 一键全链路 |
| 技术栈 | FastAPI + 单 HTML + Alpine.js | 原生 async + BackgroundTasks；前端零构建；和现有"轻依赖"风格一致 |
| 任务追踪 | 步骤级 + 日志流 + 历史任务列表 | 持久化 task_runs 表，SSE 实时推送日志，首页展示历史任务 |
| LLM 交互存储 | 新增 llm_calls 表持久化 | 在 LLMClient.chat() 埋点，结构化存储所有 LLM 调用 |
| 画像展示 | 文件列表 + 结构化渲染 | 对 json 做卡片渲染，变化报告做 diff 表格 + 颜色标记 |
| 首页形态 | 混合式 Dashboard | 上半部分工作流进展，下半部分数据概览 |
| 任务执行方式 | 进程内线程池 | 日志捕获通过 logging Handler 一步到位；CLI 函数无需改动 |

---

## 三、整体架构

5 层架构，从上到下：浏览器 → FastAPI API → 任务引擎 → 现有 profile/ 包 → 存储层。

```
┌─────────────────────────────────────────────────────────┐
│  浏览器层 · 单 HTML + Alpine.js (CDN)                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │Dashboard │ │数据库浏览 │ │LLM 交互  │ │画像产出  │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP / SSE
┌──────────────────────┴──────────────────────────────────┐
│  API 层 · FastAPI + uvicorn                              │
│  ┌────────────┐ ┌──────────┐ ┌────────┐ ┌────────────┐  │
│  │/api/dashboard│ │/api/tasks│ │/api/data│ │/api/llm   │  │
│  │            │ │  + SSE   │ │        │ │/api/profiles│  │
│  └────────────┘ └──────────┘ └────────┘ └────────────┘  │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────────┐
│  任务引擎 · 进程内线程池执行                               │
│  ┌───────────┐ ┌────────┐ ┌───────────┐ ┌────────────┐  │
│  │TaskRunner │ │串行锁  │ │LogHandler │ │SSE 推送    │  │
│  └───────────┘ └────────┘ └───────────┘ └────────────┘  │
└──────────────────────┬──────────────────────────────────┘
                       │ 直接 import 调用
┌──────────────────────┴──────────────────────────────────┐
│  现有 profile/ 包 · 零改动复用 CLI 入口                    │
│  ┌────────┐ ┌──────────────┐ ┌────────────┐ ┌─────────┐  │
│  │ingest  │ │extract_intents│ │generate_   │ │refresh  │  │
│  │        │ │              │ │profiles    │ │_all     │  │
│  └────────┘ └──────────────┘ └────────────┘ └─────────┘  │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ llm/client.py (+ 埋点)                               │ │
│  └─────────────────────────────────────────────────────┘ │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────────┐
│  存储层                                                   │
│  ┌─────────┐ ┌────────────┐ ┌───────────┐ ┌───────────┐  │
│  │raw_data │ │llm_intents │ │task_runs  │ │llm_calls  │  │
│  │(现有)   │ │(现有)      │ │(新增)     │ │(新增)     │  │
│  └─────────┘ └────────────┘ └───────────┘ └───────────┘  │
│  logs/ (JSONL) · Obsidian 画像产出/ (md + json)          │
└─────────────────────────────────────────────────────────┘
```

新增部分：task_runs 表、llm_calls 表、llm/client.py 埋点、portal/ 子包。其余为复用现有代码。

---

## 四、目录结构

在现有 `profile/` 包内新增 `portal/` 子包：

```
checkSelf/
├── profile/
│   ├── portal/                    # 新增：Portal 子包
│   │   ├── __init__.py
│   │   ├── app.py                 # FastAPI 应用 + 路由注册 + uvicorn 启动
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── dashboard.py       # GET /api/dashboard
│   │   │   ├── tasks.py           # 任务 CRUD + SSE 日志流
│   │   │   ├── data.py            # raw_data + llm_intents 浏览
│   │   │   ├── llm.py             # llm_calls 查询
│   │   │   └── profiles.py        # 画像文件列表 + 结构化渲染
│   │   ├── task_engine.py         # TaskRunner + 串行锁 + LogHandler
│   │   ├── db_store.py            # task_runs + llm_calls 的 CRUD
│   │   └── static/
│   │       └── index.html         # 单 HTML 文件（Alpine.js CDN）
│   ├── llm/
│   │   └── client.py              # 改动：chat() 内加 llm_calls 埋点
│   └── db/
│       └── init_db.py             # 改动：新增 task_runs + llm_calls 表 DDL
├── pyproject.toml                 # 改动：加 fastapi, uvicorn 依赖
```

**改动范围：** 新增 10 个文件，改动 3 个现有文件。现有流水线逻辑零改动。

---

## 五、数据模型

### 5.1 task_runs 表

记录每次任务执行的元信息、步骤状态和结果摘要：

```sql
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,          -- refresh_all | ingest | extract_intents | generate_profiles | ingest_single
    status TEXT NOT NULL,             -- pending | running | done | failed | cancelled
    steps_json TEXT,                  -- JSON数组：[{name, status, started_at, finished_at}, ...]
    current_step TEXT,                -- 当前执行中的步骤名
    params_json TEXT,                 -- 任务参数，如 {"source": "trae", "days": 7}
    result_json TEXT,                 -- 最终结果摘要，如 {"raw_data_new": 12, "intents_new": 47}
    log_dir TEXT,                     -- 对应的 logs/ 目录路径
    error_message TEXT,               -- 失败时的错误信息
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 5.2 llm_calls 表

记录每次 LLM 调用的完整上下文：

```sql
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_run_id INTEGER,              -- 关联的 task_run（手动触发时有值）
    step TEXT NOT NULL,               -- intent_extract | profile_trae | profile_marvis | profile_global
    model TEXT NOT NULL,              -- LongCat-2.0
    system_prompt TEXT,               -- 完整 system prompt
    user_prompt TEXT,                 -- 完整 user prompt
    response TEXT,                    -- 完整响应文本
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    elapsed_ms INTEGER,               -- 耗时
    success INTEGER NOT NULL DEFAULT 1,  -- 1成功 0失败
    error_message TEXT,
    called_at DATETIME NOT NULL,
    finished_at DATETIME,
    FOREIGN KEY (task_run_id) REFERENCES task_runs(id)
);
```

### 5.3 埋点改动（client.py）

`LLMClient.chat()` 方法内，在现有 `logger.info("LLM 响应成功")` 之后加一个 `_record_call()` 调用，通过 `contextvars` 获取当前 `task_run_id` 和 `step`：

- `_current_task_run_id`：`contextvars.ContextVar`，TaskRunner 启动任务时设置
- `_current_step`：`contextvars.ContextVar`，TaskRunner 在每步执行前设置
- LLM 调用时自动读取这些变量，不需要给 analysis 模块传参
- 直接调用 portal 外的 CLI（如命令行跑 `refresh_all`）时，这两个 ContextVar 为 None，`task_run_id` 和 `step` 写入 NULL

---

## 六、API 设计

5 个路由模块，共 14 个端点。

### 6.1 Dashboard

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/dashboard` | 首页聚合数据：当前运行任务、最近5次历史任务、各source数据条数、LLM统计、最新画像快照、最新变化报告摘要 |

返回示例：
```json
{
  "current_task": {"id": 42, "task_type": "refresh_all", "status": "running", "current_step": "步骤3/5: 提取意图", "progress": "3/5"},
  "recent_tasks": [{"id": 41, "task_type": "refresh_all", "status": "done", "started_at": "...", "finished_at": "...", "result_json": {}}],
  "data_overview": [{"source": "trae", "count": 170}, {"source": "bilibili", "count": 1207}],
  "llm_overview": {"total_calls": 47, "total_tokens": 125000, "success_rate": 0.98},
  "latest_profile": {"date": "2026-07-11", "summary": "...", "direction_alignment": "中"},
  "latest_changes": {"date": "2026-07-11", "total_changes": 12, "new": 3, "increased": 5, "decreased": 4}
}
```

### 6.2 Tasks

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/tasks` | 历史任务列表（分页，支持 status/task_type 筛选） |
| GET | `/api/tasks/{id}` | 单个任务详情（含步骤状态、参数、结果） |
| POST | `/api/tasks` | 创建并启动任务（body: `{"task_type": "refresh_all", "params": {"days": 7}}`） |
| DELETE | `/api/tasks/{id}` | 取消运行中的任务（设置 cancelled 状态） |
| GET | `/api/tasks/{id}/logs` | SSE 流，实时推送该任务的日志条目 |

POST 支持的 task_type：
- `refresh_all` — 全链路（params: `{"days": 7}`）
- `ingest` — 全量入库（params: `{}`）
- `ingest_single` — 单源入库（params: `{"source": "trae", "refresh": false}`）
- `extract_intents` — 意图提取（params: `{}`）
- `generate_profiles` — 生成画像（params: `{"days": 7}`）

### 6.3 Data

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/data/raw` | raw_data 列表（分页，支持 source/日期范围筛选） |
| GET | `/api/data/raw/{id}` | 单条 raw_data 详情（含 raw_json） |
| GET | `/api/data/intents` | llm_intents 列表（分页，支持 source/category/model 筛选） |

### 6.4 LLM

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/llm/calls` | llm_calls 列表（分页，支持 step/model/success 筛选） |
| GET | `/api/llm/calls/{id}` | 单条 LLM 调用详情（含完整 prompt + response） |

### 6.5 Profiles

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/profiles` | 画像文件列表（扫描 Obsidian 目录，返回文件名+日期+类型） |
| GET | `/api/profiles/{filename}` | 单个画像内容（返回结构化 json 或 md） |

### 6.6 通用约定

- **分页：** 所有列表接口统一用 `?page=1&page_size=20`，返回 `{items: [...], total: N, page: 1, page_size: 20}`
- **SSE 日志流：** `GET /api/tasks/{id}/logs` 返回 `text/event-stream`，每条日志格式为 `data: {"timestamp": "...", "level": "INFO", "module": "...", "message": "..."}\n\n`。前端用 `EventSource` 接收。任务结束后流自动关闭

---

## 七、页面结构

单页应用，顶部导航栏切换 4 个视图。

### 7.1 Dashboard（首页）

上下两区：

**上半部分 — 工作流进展**
- 当前运行任务卡片：任务类型 + 进度条（步骤 3/5）+ 当前步骤名 + 实时日志流（SSE，最后 10 条，自动滚动）+ "取消"按钮
- 如果没有运行中的任务：显示"最近一次任务"摘要卡片 + "立即刷新"大按钮 + 分步触发按钮组（入库/意图/画像）

**下半部分 — 数据概览**
- 4 个数据源条数卡片（trae/marvis/bilibili/netease），显示总数 + 最近7天新增
- LLM 调用统计卡片（总调用数/总 tokens/成功率）
- 最新综合画像快照（一句话总结 + 方向真实度对齐度）
- 最新变化报告摘要（变化总数 + 新增/上升/下降分类计数）

### 7.2 数据库浏览

左右布局：
- 左侧：数据源筛选 tab（全部/trae/marvis/bilibili/netease）+ 分页表格
- 右侧（点击某行展开）：该条记录的完整详情（content、actions、outcome、learned、raw_json）
- 上方 tab 切换：raw_data / llm_intents（intents 表显示关联的 raw_data 内容）

### 7.3 LLM 交互

- 顶部筛选：step 下拉 + model 下拉 + success/error 切换
- 列表：每条调用显示时间、step、model、耗时、tokens、success 状态
- 点击展开：完整 system_prompt + user_prompt + response 并排展示（可折叠，默认折叠 prompt 只显示 response）

### 7.4 画像产出

- 时间线列表：按日期倒序排列所有画像文件
- 每个条目：日期 + 类型标签（综合/trae/marvis/content_consumption/变化报告）
- 点击综合画像：5 个维度卡片（方向真实度/知识兴趣/作息精力/决策模式/情绪审美），每个卡片渲染该维度的 json 字段
- 点击变化报告：时间线 + diff 表格（新增绿色标签、上升绿色、下降红色、消失灰色）
- "查看原始 JSON"切换按钮：在结构化渲染和原始 JSON 之间切换

### 7.5 技术细节

- Alpine.js 从 CDN 引入，无构建步骤
- Markdown 渲染用 marked.js（CDN），仅画像 md 预览时用
- 轮询策略：Dashboard 无运行任务时每 10 秒轮询 `/api/dashboard`；有运行任务时通过 SSE 实时更新，停止轮询
- 任务列表页无限滚动加载

---

## 八、任务引擎

`task_engine.py` 是 portal 的核心，负责在进程内执行流水线任务并追踪状态。

### 8.1 TaskRunner 核心逻辑

1. **串行锁** — 用 `threading.Lock` 保证同一时间只有一个任务运行。第二个 POST 请求返回 409 Conflict
2. **线程池执行** — 提交到 `ThreadPoolExecutor(max_workers=1)`，在后台线程调用 CLI 的 `main()` 函数
3. **步骤追踪** — 通过包装 `profile.cli` 各模块的 `main()` 函数，在每步前后更新 `task_runs.steps_json` 和 `current_step`
4. **日志捕获** — 给 `profile` logger 挂一个自定义 `Handler`，把 `LogRecord` 转成 dict 存入内存队列（`collections.deque`，maxlen=500），SSE 端点从队列读取推给前端
5. **contextvars** — 任务启动时设置 `_current_task_run_id` 和 `_current_step`，`LLMClient.chat()` 的埋点函数读取这些变量自动关联 `llm_calls.task_run_id` 和 `step`
6. **取消机制** — `DELETE /api/tasks/{id}` 设置 `cancelled` 状态。由于 CLI 函数不支持中断，取消是"软取消"：标记状态后等当前步骤自然结束，不再执行下一步。日志流推送"任务已取消"
7. **任务完成** — 无论成功/失败/取消，都更新 `task_runs.status`、`finished_at`、`result_json`/`error_message`，释放串行锁

### 8.2 步骤定义

| task_type | steps |
|-----------|-------|
| refresh_all | 读旧画像 → 增量入库 → 提取意图 → 生成画像 → 生成变化报告 |
| ingest | 数据入库 |
| ingest_single | 入库({source}) |
| extract_intents | 意图提取 |
| generate_profiles | 生成画像 |

### 8.3 启动恢复

portal 启动时扫描 `task_runs` 中 status=running 的记录，标记为 failed + "portal 重启中断"。

---

## 九、错误处理与边界

| 场景 | 处理 |
|------|------|
| 任务执行中 portal 重启 | 启动时扫描 running 记录，标记为 failed + "portal 重启中断" |
| LLM 调用超时/失败 | client.py 已有 try/except，埋点记录 success=0 + error_message，不影响任务继续（LLM 降级到规则） |
| SQLite 并发写 | 单人本地使用 + 串行锁，并发风险极低；get_connection() 每次新建连接、用完即关 |
| SSE 连接断开 | 前端 EventSource 自动重连，重连时从最后收到的 timestamp 之后继续推送（队列保留最近 500 条） |
| Obsidian 目录不存在 | config.py 的 ensure_dirs() 在 portal 启动时调用 |
| 画像文件为空/格式错误 | API 层 try/except，返回空结构 + error 提示，不让页面崩溃 |
| 并发 POST 创建任务 | 串行锁保证只执行第一个，后续返回 409 |

---

## 十、启动方式

```bash
python -m profile.portal.app
# 或指定端口
python -m profile.portal.app --port 8080
```

启动时自动初始化数据库（新增两张表）、启动 uvicorn、打开浏览器。

---

## 十一、依赖变更

pyproject.toml 新增依赖：

| 包 | 版本 | 用途 |
|----|------|------|
| fastapi | >=0.100 | Web 框架 |
| uvicorn | >=0.20 | ASGI 服务器 |

前端依赖（CDN 引入，不进 pyproject.toml）：

| 库 | CDN | 用途 |
|----|-----|------|
| Alpine.js | cdn.jsdelivr.net | 前端响应式 |
| marked.js | cdn.jsdelivr.net | Markdown 渲染 |
