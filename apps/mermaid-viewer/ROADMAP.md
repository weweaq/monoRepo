# ROADMAP — mermaid-viewer 执行记录与进度

## 待办清单

- [ ] 在 dev-console 仪表盘实测启动→运行→停止/重启三态（迁移后）
- [ ] 评论数据迁移（旧 tools/mermaid-viewer/data/reviews.db 若存在需搬入新路径）
- [ ] 补充 tests/（viewer_server 的 HTTP 行为测试：API 读写、路径逃逸防护）

## 执行记录

### 2026-09-12 · 升级为标准 app（tools → apps/mermaid-viewer）

**背景**：mmd 查看器原为 `tools/mermaid-viewer/` 下的零依赖工具（viewer.html + viewer_server.py + SQLite 评论），已被 dev-console 纳管。按 buildApp skill 升级为 mono 规范下的独立 app。

**已完成**：
- 迁移全部文件到 `apps/mermaid-viewer/`（git mv 保留历史）：`viewer_server.py` / `viewer.html` / `vendor/` / `gen-mmd-viewer.mjs` / `scan-mmd-reviews.py` / `data/` / `.gitignore` / `README.md`，删除空的 `tools/mermaid-viewer/`
- 新增 `pyproject.toml`：`name="mermaid-viewer"`，`[project.scripts] mermaid-viewer = "viewer_server:main"`，setuptools 单文件（`py-modules=["viewer_server"]`）
- 更新 viewer_server.py：docstring/URL 前缀 `tools/` → `apps/`；`ROOT` 三级上溯逻辑不变（docroot 仍为仓库根）
- README 更新命令路径
- 新增 `AGENTS.md`（包级约定）、`ROADMAP.md`（本文件）
- dev-console `services.json` 的 mermaid-viewer 条目 `cwd` 改为新路径，`match=viewer_server.py` 不变

**实测验证**：pending（迁移后需在 dev-console 重新启动验证，见待办）

**待办更新**：见上方待办清单

### 2026-09-12 · 初始迁移前（tools/mermaid-viewer）

（历史：SQLite 评论持久化 + 双后端存储，电力/重启不丢评论。见前一阶段提交 `feat(mermaid-viewer)`）