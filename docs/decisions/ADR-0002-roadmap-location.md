# ADR-0002：路书位置固定为 `apps/<app>/docs/ROADMAP.md`

- 日期：2026-10-24
- 状态：已接受
- 关联：根 `AGENTS.md` R5（文档三处同步）

## 背景

R5 原文只写「ROADMAP.md 追加执行记录」，未规定位置，实际落成了两套：

1. 各 app 根目录 `apps/<app>/ROADMAP.md`（迁移初期按 buildApp skill 建）；
2. withlanggraph 同时存在 `apps/withlanggraph/docs/langTrack-roadmap.md`（详细路书）与根 `ROADMAP.md`（mono 侧摘要），两份各写各的，`AGENTS.md` 指其中一份、R5 指另一份。

后果：同一项目存在两条并行真相；新建 app 时按 skill 又会在根目录造出第三份；审计「改动是否记入路书」时没有唯一判据。

## 决策

路书位置固定为 `apps/<app>/docs/ROADMAP.md`：

- 每个 app 只有一份路书，禁止 `<app>-roadmap.md` 之类的平行文件；
- 与 tech 文档、架构图同放 `docs/`，三处产物同一目录，便于交叉链接与检视；
- 追加格式固定 `## YYYY-MM-DD · 标题`，正文含「背景 / 已完成 / 实测验证 / 偏差说明 / 待办更新」。

## 理由

1. `docs/` 已是 tech 文档与架构图的固定落点，路书并入后三处产物物理同源，减少「指针写错目录」类故障。
2. app 根目录只留配置与入口（`pyproject.toml`、`AGENTS.md`、启动脚本），职责更干净。
3. 合并后单一文件可被 `git grep` 一次性命中，历史记录不再需要跨文件拼接。

## 后果

- 存量迁移已完成：`withlanggraph`（两份合并为一份，154 条历史记录）、`checkself`、`dev-console`、`mermaid-viewer`。
- `checkself/.gitignore` 原本整体忽略 `/docs/`，改为 `/docs/*` + `!/docs/ROADMAP.md`，否则路书无法入库；该 app 新增 tech/架构图时需显式加白名单。
- 新建 app 的 `buildApp` skill、`docs/monorepo-guide.md`、根 `AGENTS.md` 提交前检查清单一并改为新路径。
- 迁移前的历史记录里出现的旧路径（`docs/langTrack-roadmap.md`、根 `ROADMAP.md`）保留原文，不做字符串替换，由合并后的文件头统一说明。
