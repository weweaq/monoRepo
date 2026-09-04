# Monorepo 管理指导

> 版本：v1.0 | 日期：2026-09-04
> 适用范围：`d:\AAAmyPrj\github\myrepos` 下的自有项目集合
> 本文档基于对本地全部项目的实际调研生成，所有项目状态、重复模式、实践细节均来自真实仓库，非泛泛之谈。

---

## 1. 现状盘点：你的项目全景

### 1.1 自有项目（拟纳入 monorepo 的候选）

| 项目 | 语言/框架 | 依赖管理 | 测试 | 质量工具 | 说明 |
|---|---|---|---|---|---|
| WithLangGraph | Python 3.12+ (gacore) | setuptools + pyproject | pytest（305+ 用例） | ruff (line-length 120) | 旗舰项目：langTrack 数据管线，实践最成熟 |
| GenericAgent | Python 3.10-3.13 | pyproject + uv.lock | 无系统测试 | ruff | Agent 框架，前端矩阵最广 |
| checkSelf | Python + FastAPI | pyproject + uv.lock | pytest（10+ 文件） | 无 | 画像/自追踪，JSONL 脱敏日志 |
| claw1 | Python 3.11+ | pyproject + requirements | pytest (pytest.ini) | ruff + mypy | AGENTS.md 有任务类型/停止条件约定 |
| claw0 | Python | requirements.txt | 无 | 无 | 教学向 agent gateway |
| py-wei (WeiTracker) | Python + Win32 | requirements.txt | 无 | 无 | 桌面行为采集，AGENTS.md + ROADMAP.md |
| dev-console | Python 3.12 纯标准库 | 无依赖文件 | 无 | 无 | 单文件服务管理器，最轻量 |
| my_test_crew | Python + crewAI | pyproject + uv.lock | 无 | 无 | crewAI JSON 项目 |
| loot-transparent-curtain | - | 无 | 无 | 无 | 空壳 README |

### 1.2 保持独立仓库的项目（不纳入 monorepo）

| 项目 | 技术栈 | 理由 |
|---|---|---|
| weiCheckApp | Kotlin/Jetpack Compose (Android) | 异构技术栈，Gradle 生态独立；作为 langTrack 数据采集客户端，通过 HTTP 契约与服务端关联，无需同仓 |
| xiaozhi-esp32-server-java | Java 21 + Spring Boot + Vue | Maven 生态独立，且有 Docker 部署链路 |
| py-xiaozhi | Python + PyQt5 | 上游第三方项目的本地衍生，属于 fork，不应混入自有代码 |
| github/ 下 13 个克隆仓库 | 各异 | 纯学习/参考材料，属于"外部依赖"而非"自有资产" |

**核心判断**：monorepo 只收 Python 自有应用与共享包。异构技术栈（Java/Android）与 fork 项目保持独立仓库，在根 README 的"项目地图"中登记关联关系。

### 1.3 调研发现的主要问题

1. **依赖管理碎片化**：uv.lock（4 个）、requirements.txt（3 个）、裸 pyproject（2 个）、无依赖文件（2 个）并存。装环境时每个项目要回忆"这个是 pip 还是 uv"。
2. **质量基建不均**：仅 WithLangGraph 有成规模测试（305+）；大多数项目零测试、零 lint。ruff 配置只在一个项目里存在。
3. **重复造轮子**（跨项目重复实现，详见第 6 章）：
   - 日志：checkSelf 的 JSONL+脱敏、dev-console 的 JsonlLogger、claw1 的 loguru，三套各写一遍
   - 配置加载：dotenv / YAML / JSON / JSONC 四种方式散布在不同项目
   - LLM 客户端：claw1 的 pydantic-settings 版、checkSelf 的裸 OpenAI 兼容版、claw0 的 anthropic SDK 版
   - Amap API：WithLangGraph 的 geocode/routes/坐标转换，未来 py-wei 类项目还会需要
   - QQ/飞书集成：qq-botpy、lark-oapi 在 4 个项目中重复出现
4. **文档约定不统一**：AGENTS.md 在 6 个项目里各自为政，格式和严格程度不一（WithLangGraph 最完整，dev-console 最简）。
5. **myrepos 根目录本身不是 git 仓库**：无版本控制、无 README、有 .idea 残留。

---

## 2. 为什么用 monorepo，为什么选 uv workspace

### 2.1 monorepo 解决你的什么问题

- **一处 clone，全部可用**：`uv sync` 一次装齐所有项目依赖，不用逐项目回忆安装方式
- **共享代码有家可归**：日志、配置、Amap 封装等重复实现可以晋升为共享包，被所有 app 引用
- **规则统一下沉**：ruff/pytest/提交规范/AGENTS.md 在根配置一次，全部项目继承
- **原子化跨项目改动**：langTrack 的接口改动若涉及 weiCheckApp 契约或 dev-console 服务定义，一次提交完成

### 2.2 技术选型：uv workspace

理由基于你的实际情况：

1. 你已有 4 个项目在用 uv（GenericAgent、checkSelf、crewAI、my_test_crew），无学习成本
2. 你本地 crewAI 仓库本身就是 uv workspace 范例（`libs/` 多包结构），有现成参照
3. 主体语言是 Python，不需要 pnpm/nx/turborepo 这类 JS 生态工具
4. uv 的 workspace + `uv run --package` 机制对"单仓多应用"支持成熟，锁文件唯一（根 uv.lock）

不选 poetry/hatch workspace 的理由：你的存量项目里没有 poetry；hatch 更偏单包发布；uv 速度和 workspace 体验当前更优。

---

## 3. 目标架构与目录布局

### 3.1 目录结构

```
mono/                                # monorepo 根（建议新目录，勿直接改造 myrepos）
├── AGENTS.md                        # 全仓通用规则（AI 助手与人都必须读）
├── README.md                        # 项目地图：含仓外关联项目（weiCheckApp 等）
├── pyproject.toml                   # workspace 定义 + 统一工具配置（ruff/pytest）
├── uv.lock                          # 全仓唯一锁文件
├── .editorconfig
├── .gitignore
├── .pre-commit-config.yaml
├── .env.example                     # 全仓密钥模板（见规则 R9）
├── apps/                            # 应用：有入口、可独立运行
│   ├── withlanggraph/               # gacore 主项目（langTrack 服务端）
│   ├── generic-agent/               # Agent 框架
│   ├── checkself/                   # 画像/自追踪
│   ├── claw1/
│   ├── claw0/
│   ├── py-wei/                      # 桌面采集器
│   ├── dev-console/
│   └── my-test-crew/
├── packages/                        # 共享包：无入口，被 apps 依赖
│   ├── amap-sdk/                    # 高德 API 封装（首批提炼）
│   ├── ga-logging/                  # JSONL 日志 + 脱敏（首批提炼）
│   ├── ga-config/                   # 配置加载（第二批）
│   ├── llm-client/                  # 多后端 LLM 客户端（第二批）
│   ├── sqlite-kernel/               # SQLite 迁移/时间戳框架（第二批）
│   └── im-bridge/                   # QQ/飞书消息桥（第三批，按需）
├── tools/
│   └── scripts/                     # PowerShell 工具脚本（纯 ASCII）
├── docs/
│   ├── decisions/                   # ADR 架构决策记录
│   ├── playbooks/                   # 运维手册（承接 WithLangGraph 的 sre/）
│   └── template-package/            # 新包模板
└── data/                            # 运行时数据，整目录 gitignore，仅留 README
```

### 3.2 依赖方向（强制）

```
apps/* ──→ packages/*
apps/* ──→ 第三方库
packages/* ──→ packages/*（仅允许单向，禁止环）
packages/* ──✗──→ apps/*     （共享包禁止反向依赖应用）
```

判定口诀：**packages 不知道任何具体应用的存在**。如果 amap-sdk 需要知道 langTrack 的表结构，说明边界画错了。

### 3.3 workspace 根配置示例

```toml
# mono/pyproject.toml
[project]
name = "mono"
version = "0.1.0"
requires-python = ">=3.12"

[tool.uv.workspace]
members = ["apps/*", "packages/*"]

[tool.uv.sources]
amap-sdk = { workspace = true }
ga-logging = { workspace = true }

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy", "pre-commit"]

[tool.ruff]
line-length = 120          # 沿用 WithLangGraph 现行值，全仓统一
src = ["apps", "packages"]

[tool.pytest.ini_options]
asyncio_mode = "auto"       # 沿用 WithLangGraph 现行值
testpaths = ["apps", "packages"]
```

每个 app/package 内保留自己的 `pyproject.toml`（声明名称、版本、各自依赖），但工具配置尽量只在根出现。

---

## 4. 优秀实践提炼（来自你的项目与参考仓库）

这些不是通用建议，是从你的仓库里实际验证过、值得全仓推广的做法。

### 4.1 来自 WithLangGraph（实践密度最高，作为范本）

**(1) roadmap 执行记录模式**（`docs/langTrack-roadmap.md`）
每次代码改动后强制追加执行记录，格式固定为：背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新。AGENTS.md 将其定为硬规则："任何代码变更后必须更新 roadmap"。
→ 全仓推广：每个 app 有自己的 `ROADMAP.md`，执行记录按日期追加，禁止删除历史记录。

**(2) tech.md 四层文档结构**（`docs/langTrack-tech.md`）
接口 / 实体 / 数据库表字典 / 数据流四层组织，且标注代码行号锚点（如 `etl.py:159`）。
→ 每个有数据层的 app 维护同级 tech 文档，代码行号会漂移，但"接口-实体-表-数据流"的四层骨架不变。

**(3) 人工维护的契约文件**（`src/gacore/langTrack/contract.py`）
`EXPECTED_EVENT_TYPES` 显式列出 19 种事件类型及消费策略，文件头写明"契约由人维护：客户端新增/废弃事件类型时，显式更新本文件"。配合 `STALE_DAYS = 7` 检测契约腐化。
→ 任何跨进程边界（weiCheckApp ↔ 服务端、QQ 客户端 ↔ bot）都建一个 contract 文件，跨项目改动先改契约再改实现。

**(4) shadow 表 + 灰度切换的迁移模式**
位置事实 v2 采用：shadow 全量重建 → v1/v2 双读对比 → 验收通过后正式激活。迁移期零停机，出问题可回退。配合 `PRAGMA user_version` 控制 schema 版本，`_migrate_*` 函数按表拆分（`_migrate_anomalies_unique`、`_migrate_trips_coord_columns` 等）。
→ sqlite-kernel 共享包的骨架直接照此实现。

**(5) 幂等写入**
`ingest_batch()` 用 batch 唯一约束 + 单事务，重复上报返回 False 而非报错，明确避免"批次登记了但事件只插了一半"的窗口。
→ 所有数据接入层默认幂等：唯一键 + upsert + 重复即忽略。

**(6) 质量可见性表**（`daily_location_quality`）
不是等数据烂了才发现，而是每日落一张质量表：总点数/有效点数/精度分桶/覆盖半时桶/中位间隔。
→ 任何数据管线 app 都要有对应的 daily_quality 表，ETL 跑完顺手产出。

**(7) review-loop 复审机制**
Task 5/6 的实际记录：subagent 一轮复审产出 P1/P2/P3 分级意见（索引丢失、语义对齐、代码去重、多设备支持），全部处置后二轮复审确认，才在 roadmap 记"终审 PASS"。
→ 大改动（>300 行或跨模块）沿用：一轮复审分级 → 全处置 → 二轮确认 → roadmap 记录结论。

**(8) 真实提交规范样本**（可直接当模板）
```
feat(langTrack): Task12c dashboard「立即转换(ETL)」按钮
fix(langTrack): Task12b off_schedule 进行中日守卫 + ingest 层别名归一
docs(langTrack): 路线缓存作废重编记录（坐标制生效后 9 条 polyline 偏移清除）
test: Task10 report/persona 证据边界验收用例
style(langTrack): ruff --fix 整理导入与格式
```

**(9) 测试基建模式**（`tests/conftest.py`）
用 langchain-core 的 FakeChatModel 体系（`BindableGenericFakeChatModel` 容忍 bind_tools、`scripted_llm` 工厂、`run_graph` 带 thread_id 与 recursion_limit）。44 个测试文件按 `test_<模块>_<场景>.py` 命名。
→ LLM 相关共享包（llm-client）的测试直接复用这套 fake 体系。

### 4.2 来自其他自有项目

| 项目 | 实践 | 推广方式 |
|---|---|---|
| checkSelf | JSONL 结构化日志 + 敏感字段脱敏 + 按运行时间戳分目录 | ga-logging 包的核心需求来源 |
| dev-console | `services.json` 模板 + `services.local.json` 本地覆盖（后者 gitignore） | 所有含本地差异配置的 app 采用"模板入库 + local 不入库"二文件制 |
| claw1 | AGENTS.md 定义任务类型选择、停止条件、变更范围 | 根 AGENTS.md 的"AI 协作规则"一节照此结构写 |
| GenericAgent | 可选依赖组（`ui` / `all-frontends` extras） | 重前端依赖一律走 extras，核心安装保持轻量 |
| py-wei | AGENTS.md + ROADMAP.md 双文件分工（规则与进度分离） | 全仓 app 标配这两个文件 |

### 4.3 来自第三方参考仓库（本地已克隆，可随时查阅）

| 仓库 | 值得借鉴 | 落地方式 |
|---|---|---|
| deepseek-harness（pnpm monorepo） | lefthook 管 pre-commit；knip 查未使用依赖；jscpd 查重复代码；多套测试配置分层（单测/e2e/snapshot/性能） | knip 思路用 `uv tree` + 定期人工审查替代；测试分"单元/验收"两层即可，不必照搬六套 |
| headroom（Rust+Python） | commitlint 强制提交规范；Makefile 统一入口（make test/lint/typecheck）；pre-commit 全家桶 | pre-commit + 根 pyproject 脚本入口实现同等效果；commitlint 规则写进 pre-commit 的 commit-msg 钩子 |
| crewAI（uv workspace） | `libs/` 多包结构；pip-audit 安全审计；override-dependencies 钉传递依赖 | 结构参照；pip-audit 进 quarterly 例行检查 |
| MediaCrawler | uv.lock + pre-commit + mypy.ini 的简洁组合 | 证明轻量组合可行，起步阶段够用 |
| Agent-Reach | mypy strict + ruff lint/format 双用 | 新建共享包从第一天起开 strict |

---

## 5. Monorepo 必须遵守的规则

以下规则分两类：**R 系列（全仓强制）** 写入根 AGENTS.md；**P 系列（包级补充）** 写入各包自己的 AGENTS.md。

### R1 仓库边界
- 只有 Python 自有项目进仓；Java/Android/fork 项目留在独立仓库，在根 README 项目地图登记
- 克隆的第三方仓库永不进仓；学习结论提炼为 docs/decisions/ 下的 ADR，不搬运代码

### R2 依赖方向
- `apps → packages` 单向；packages 之间禁止循环依赖；packages 禁止 import 任何 app
- 新增依赖必须写进对应包的 pyproject.toml，禁止"顺手 pip install"后不落盘
- 共享包对外接口变更需在包的 CHANGELOG.md 记录（沿用 conventional commits 分类）

### R3 共享代码晋升（Rule of Three）
- 相同逻辑出现第 1、2 次：各自实现，不抽包
- 出现第 3 次：晋升进 packages/，迁移时以实现最完整的版本为底，其余项目改引用并补齐差异测试
- 禁止"预防性抽象"：为一个未来需求提前建包

### R4 提交规范
- 格式：`type(scope): 描述`，scope 为包/应用名（如 `feat(amap-sdk): ...`、`fix(langTrack): ...`）
- type 限定：feat / fix / docs / test / style / refactor / chore / perf
- 一个提交只做一件事；跨包联动改动允许一个提交覆盖所涉包，但 scope 写明（如 `feat(langTrack,amap-sdk): ...`）

### R5 文档同步
- 任何代码改动：对应 app 的 ROADMAP.md 追加执行记录（背景/已完成/实测验证/偏差/待办）
- 涉及接口、表结构、数据流：同步更新该 app 的 tech 文档
- 文档变更单独成提交（`docs(scope): ...`），不与代码混提

### R6 数据库约定
- 每表必有 `created_at` / `updated_at`，默认值 `datetime('now','+8 hours')`；`created_at` = 首写，`updated_at` = 最近更新
- schema 变更必须走 `_migrate_*` 函数 + `PRAGMA user_version` 递增，禁止手工 ALTER 旧库
- 破坏性迁移（语义变更）采用 shadow 表双写双读灰度，验收后再激活
- 数据接入层默认幂等：唯一约束 + 单事务 + 重复上报静默忽略

### R7 质量门禁
- 提交前必须全绿：`uv run ruff check apps packages` + `uv run pytest`
- 新功能必须带测试，测试文件与被测模块同包（`apps/xxx/tests/`）
- 大改动（>300 行或跨模块）执行 review-loop：一轮复审 P1/P2/P3 分级 → 全处置 → 二轮确认

### R8 AGENTS.md 分层
- 根 AGENTS.md：全仓规则（即本章 R 系列）+ 项目地图
- 包级 AGENTS.md：只写该包的特有约定（如 langTrack 的客户端/服务端分工、claw1 的任务类型选择）
- 包级规则不得与根规则冲突；冲突时以根为准并提 ADR 修订

### R9 密钥管理
- `.env` 永不入库；每仓根放 `.env.example` 列全量键名（AMAP_KEY、OPENAI_API_KEY 等），值留空
- 代码读密钥顺序：环境变量 → .env 文件（参考 geocode.py 的 `AMAP_KEY` 加载实现），缺失时显式报错退出，禁止静默降级

### R10 Windows 兼容
- ps1/bat/sh 脚本内容纯 ASCII/英文
- `.bat` 文件 CRLF 换行 + GBK 编码
- PowerShell 脚本用 `;` 或换行分隔命令，不用 `&&`；不用 `2>/dev/null` 等 bash 专属语法

### R11 运行时数据
- 数据库、日志、图片等运行时产物统一放 `data/`（或 app 内 `data/` 子目录），整目录 gitignore
- 每个数据目录留一份 `README.md` 说明数据来源与生成方式（roadmap 执行记录可交叉引用）

### R12 版本策略
- 共享包版本从 0.1.0 起步，破坏性接口变更升次版本号
- workspace 内互引永远走 `[tool.uv.sources] workspace = true`，不经 PyPI

---

## 6. 共享包提炼清单（按优先级）

| 批次 | 包名 | 来源实现 | 内容要点 | 触发条件 |
|---|---|---|---|---|
| 1 | `amap-sdk` | WithLangGraph 的 geocode.py / routes.py / to_amap_coord | 坐标系转换（wgs84/gcj02 边界处理）、regeo、周边搜索、步行路径、批量退避（BATCH_SIZES 20/10/5/1）、geocoded_at 增量标记、route_key 归一化哈希 | 迁移 WithLangGraph 时顺手抽出 |
| 1 | `ga-logging` | checkSelf 的脱敏日志 + dev-console 的 JsonlLogger | JSONL 结构化、敏感字段脱敏、按运行时间戳分目录、统一异常堆栈格式 | checkSelf 迁入时抽出 |
| 2 | `ga-config` | etl_config.py 的 `_deep_update` + dev-console 双文件制 | 默认值深合并、模板+local 覆盖、非法配置显式抛错（参照 CoordSystemConfigError 模式） | 第三个 app 需要配置层时 |
| 2 | `llm-client` | claw1（pydantic-settings 版）+ checkSelf（OpenAI 兼容版） | 多后端适配、异常 fallback、请求日志（承接 llm_request_log.py 思路）、FakeChatModel 测试体系 | 下次新写 LLM 调用时 |
| 2 | `sqlite-kernel` | WithLangGraph storage.py / etl.py 迁移函数群 | 连接管理、时间戳列自动补齐回填、user_version 迁移框架、幂等 upsert、shadow 表灰度工具 | WithLangGraph 迁移完成后 |
| 3 | `im-bridge` | qq-botpy / lark-oapi 散布使用 | 消息发送、去重、主动推送、多前端适配 | 第三个 IM 集成需求出现时 |

每包结构统一（新建 `docs/template-package/` 模板）：

```
packages/amap-sdk/
├── pyproject.toml        # name, version, deps
├── AGENTS.md             # 包级特有约定
├── CHANGELOG.md
├── src/amap_sdk/
│   ├── __init__.py
│   └── ...
├── tests/
└── README.md             # 用途、接口速览、迁移自哪个项目
```

---

## 7. 分阶段迁移路线图

原则：**先立规则，再迁轻项目，抽包随主项目迁移走，主力项目最后动**。每阶段有明确验收标准，不达标不进下一阶段。

### Phase 0：骨架搭建（半天）
- [ ] 新建 `mono/` 目录，git init，写入根 pyproject.toml（workspace 定义 + ruff/pytest 统一配置）
- [ ] 写根 AGENTS.md（第 5 章 R 系列全文）+ README.md（项目地图，含仓外项目）
- [ ] `uv init` 建两个空成员（apps/placeholder、packages/placeholder 验证 workspace 解析）后删除
- [ ] 配 .gitignore（data/、.env、__pycache__、.idea 等）、.editorconfig、.pre-commit-config.yaml（ruff + 基础钩子）
- [ ] 推送远端（github/gitee 自选，建议双推）

验收：`uv sync` 与 `uv run pytest`（空跑）成功；pre-commit install 生效。

### Phase 1：轻量试点迁入（半天）
- [ ] 迁 `dev-console`（零依赖、单文件，最理想试点）：`git subtree add --prefix=apps/dev-console <repo> main` 保留历史，或直接拷贝（历史价值低的项目可省）
- [ ] 补包级 pyproject.toml，接入 workspace
- [ ] 按模板补 AGENTS.md / ROADMAP.md / 测试目录（先跑通结构，测试可后补）

验收：`uv run --package dev-console python server.py` 行为与迁前一致。

### Phase 2：首批共享包 + 主力项目迁入（1-2 个工作日）
- [ ] 迁 `WithLangGraph`（subtree add 保留完整历史——305 个测试的演进史值得保留）
- [ ] 迁移过程中提炼 `amap-sdk`（geocode/routes/坐标转换整体搬出，WithLangGraph 改为依赖 workspace 包，全量回归 305 用例必须全绿）
- [ ] 迁 `checkSelf`，提炼 `ga-logging`
- [ ] 存量迁移注意：WithLangGraph 的 `data/`（langTrack.db、etl_config.json）保持 gitignore 状态，配置文件按 R9 出 `.example` 模板

验收：全仓 `uv run pytest` 通过；WithLangGraph 用例数不少于迁前（305+）；`uv run python -m gacore.langTrack --db ...` 冒烟可用。

### Phase 3：其余项目逐个迁入（每项目约 1-2 小时）
- [ ] 迁 `GenericAgent`（uv 项目，改造成本最低；注意保留其 extras 结构）
- [ ] 迁 `claw1`（ruff+mypy 配置并入根，包内只留差异项）
- [ ] 迁 `claw0`、`py-wei`（requirements.txt 转 pyproject，依赖照抄即可）
- [ ] 迁 `my_test_crew`
- [ ] 每迁一个：跑通原有启动命令 + 在 ROADMAP.md 记录迁移执行记录

验收：所有迁入项目可从根 `uv run --package <name> <entry>` 启动。

### Phase 4：质量基建固化（持续）
- [ ] pre-commit 增加 commit-msg 规范校验
- [ ] 建 GitHub Actions：push 时 ruff + pytest（参考 deepseek-harness 的 13 套 workflow 精简为 1 套够用）
- [ ] 季度例行：`uv tree` 查未用依赖、`uv lock --upgrade` 升级、pip-audit 安全审计
- [ ] docs/decisions/ 开始记 ADR（第一条：为什么选 uv workspace——引用本指导第 2.2 节）

---

## 8. 常用命令速查

```powershell
# 全仓同步依赖（装齐所有成员）
uv sync

# 全仓质量门禁（提交前必跑）
uv run ruff check apps packages
uv run pytest

# 只跑某个应用的测试
uv run pytest apps/withlanggraph/tests
uv run --package withlanggraph pytest

# 运行某个应用的入口
uv run --package withlanggraph python -m gacore.langTrack --db data/langTrack.db
uv run --package dev-console python server.py

# 带历史迁入一个已有 git 仓库
git subtree add --prefix=apps/<name> <repo-url> main

# 新增一个成员包
uv init --package apps/<name>      # 或 packages/<name>

# 升级依赖 / 审计
uv lock --upgrade
uv run pip-audit
```

---

## 附：规则检查清单（合并提交前过一遍）

- [ ] ruff + pytest 全绿
- [ ] 提交信息符合 `type(scope): 描述`
- [ ] ROADMAP.md 追加了执行记录
- [ ] 涉及接口/表结构时 tech 文档已同步
- [ ] 无新硬编码密钥（密钥走环境变量/.env）
- [ ] 新增依赖已写入对应 pyproject.toml
- [ ] 跨包改动依赖方向合规（apps→packages 单向）
- [ ] 大改动已走 review-loop 复审
