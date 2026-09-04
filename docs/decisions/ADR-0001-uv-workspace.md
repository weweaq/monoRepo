# ADR-0001：采用 uv workspace 管理本仓

- 日期：2026-09-04
- 状态：已接受

## 背景

myrepos 下 10+ 个自有 Python 项目的依赖管理碎片化：4 个 uv.lock、3 个 requirements.txt、2 个裸 pyproject、2 个无依赖文件。质量基建（ruff/pytest）只在个别项目存在，工具链配置无法统一下沉，共享代码（日志、配置加载、LLM 封装、Amap 封装）在多个项目中重复实现。

## 决策

以 uv workspace 建仓：根 pyproject.toml 定义 `members = ["apps/*", "packages/*"]`，全仓唯一 uv.lock，工具配置（ruff/pytest）集中在根 pyproject.toml。

## 理由

1. 已有 4 个项目（GenericAgent、checkSelf、crewAI、my_test_crew）使用 uv，无迁移学习成本
2. 本地 crewAI 仓库即为 uv workspace 多包范例，有现成参照
3. 主体为 Python 生态，无需 pnpm/nx/turborepo 等 JS 工具
4. uv 的 `uv run --package <name>` 对单仓多应用支持成熟

## 后果

- 根 requires-python = ">=3.12"：与旗舰项目 WithLangGraph 对齐；更低版本要求的项目（如 py-xiaozhi 3.9+）迁移时需单独评估
- 全仓共享一个锁文件，成员依赖变更影响全局解析，提交前跑全仓质量门禁（R7）
- Java/Android/fork 项目不进仓（R1），在 README 项目地图登记关联
