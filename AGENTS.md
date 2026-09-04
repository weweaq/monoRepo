# AGENTS.md — mono 全仓规则

> 本文件是全仓最高规则，对人和 AI 助手同等生效。
> 包级 AGENTS.md（apps/*/AGENTS.md、packages/*/AGENTS.md）只能补充、不能冲突；冲突以本文件为准，修订需提 docs/decisions/ 下的 ADR。
>
> 当前状态：Phase 0 骨架刚初始化，apps/ 与 packages/ 为空。迁移路线图见 docs/monorepo-guide.md 第 7 章。

## 1. 仓库定位与结构

- `apps/`：可独立运行的应用（有入口，可被 `uv run --package <name>` 启动）
- `packages/`：共享包（无入口，被 apps 依赖）
- `tools/scripts/`：仓库级脚本（纯 ASCII，见 R10）
- `docs/`：decisions（ADR）、playbooks（运维手册）、template-package（新包模板）、monorepo-guide.md（建仓指导）
- `data/`：运行时数据，整体 gitignore，仅 README 入库
- `tests/`：仅放仓库自身的结构性测试（如成员 pyproject 规范检查）；业务测试一律放 `apps/<name>/tests/`

依赖方向（R2 强制）：`apps → packages`，packages 之间仅允许单向无环；packages 禁止 import 任何 app。

## 2. R 系列强制规则

### R1 仓库边界
- 只有 Python 自有项目进仓；Java/Android/fork 项目留在独立仓库，在 README.md 项目地图登记关联
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
- 任何代码改动：对应 app 的 ROADMAP.md 追加执行记录（背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新）
- 涉及接口、表结构、数据流：同步更新该 app 的 tech 文档
- 文档变更单独成提交（`docs(scope): ...`），不与代码混提

### R6 数据库约定
- 每表必有 `created_at` / `updated_at`，默认值 `datetime('now','+8 hours')`；`created_at` = 首写，`updated_at` = 最近更新
- schema 变更必须走 `_migrate_*` 函数 + `PRAGMA user_version` 递增，禁止手工 ALTER 旧库
- 破坏性迁移（语义变更）采用 shadow 表双写双读灰度，验收后再激活
- 数据接入层默认幂等：唯一约束 + 单事务 + 重复上报静默忽略

### R7 质量门禁
- 提交前必须全绿：`uv run ruff check apps packages tests` + `uv run pytest`（或一键 `tools/scripts/check.ps1`）
- 新功能必须带测试，测试文件放 `apps/<name>/tests/`，与被测模块同包
- 大改动（>300 行或跨模块）执行 review-loop：一轮复审 P1/P2/P3 分级 → 全处置 → 二轮确认

### R8 AGENTS.md 分层
- 根 AGENTS.md：全仓规则（即本文件）+ 项目地图（见 README.md）
- 包级 AGENTS.md：只写该包的特有约定（如 langTrack 的客户端/服务端分工、claw1 的任务类型选择）
- 包级规则不得与根规则冲突；冲突时以根为准并提 ADR 修订

### R9 密钥管理
- `.env` 永不入库；仓库根放 `.env.example` 列全量键名（AMAP_KEY、OPENAI_API_KEY 等），值留空
- 代码读密钥顺序：环境变量 → .env 文件，缺失时显式报错退出，禁止静默降级
- 随成员迁移新增密钥时，同步补进根 `.env.example`

### R10 Windows 兼容
- ps1/bat/sh 脚本内容纯 ASCII/英文
- `.bat` 文件 CRLF 换行 + GBK 编码
- PowerShell 脚本用 `;` 或换行分隔命令，不用 `&&`；不用 `2>/dev/null` 等 bash 专属语法

### R11 运行时数据
- 数据库、日志、图片等运行时产物统一放 `data/`（或 app 内 `data/` 子目录），整目录 gitignore
- 每个数据目录留一份 README.md 说明数据来源与生成方式（roadmap 执行记录可交叉引用）
- 严禁提交任何真实个人数据

### R12 版本策略
- 共享包版本从 0.1.0 起步，破坏性接口变更升次版本号
- workspace 内互引永远走 `[tool.uv.sources] workspace = true`，不经 PyPI

## 3. 常用命令

```powershell
uv sync                                          # 安装全部成员与 dev 依赖
uv run pytest                                    # 全仓测试
uv run ruff check apps packages tests            # 全仓 lint
uv run pytest apps/<name>/tests                  # 只跑某个应用的测试
uv run --package <name> <entry>                  # 运行某个应用入口
tools/scripts/check.ps1                          # 一键质量门禁（可加 -Fix 自动修 lint）
uv run pre-commit install                        # 启用 git 提交钩子（首次运行需联网拉取钩子环境）
```

## 4. 提交前检查清单

- [ ] ruff + pytest 全绿（R7）
- [ ] 提交信息符合 `type(scope): 描述`（R4）
- [ ] ROADMAP.md 追加了执行记录（R5）
- [ ] 涉及接口/表结构时 tech 文档已同步（R5）
- [ ] 无新硬编码密钥（R9）
- [ ] 新增依赖已写入对应 pyproject.toml（R2）
- [ ] 跨包改动依赖方向合规（R2）
- [ ] 大改动已走 review-loop 复审（R7）
