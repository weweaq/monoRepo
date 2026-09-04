# data/ 运行时数据目录

本目录存放各应用的运行时产物（SQLite 数据库、日志、导出文件等）。
除本 README 外，全部内容被 .gitignore 忽略，不入库。

约定：

- 每个应用使用独立子目录：`data/<app-name>/`，例如 `data/langTrack/`
- 数据来源与生成方式在各应用 ROADMAP.md 的执行记录中说明
- 严禁提交任何真实个人数据（R11）
- 应用自带的 `apps/<name>/data/` 同样整目录忽略；如需入库的配置模板（如 etl_config.json），在该应用目录下提供 `.example` 文件（R9）
