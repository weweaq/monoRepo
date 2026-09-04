# 新成员包模板

复制本目录到 `apps/<name>`（应用）或 `packages/<name>`（共享包）后：

1. 目录名改为成员名（kebab-case，如 `amap-sdk`）
2. `pyproject.toml`：`name` 改为导入名（下划线，如 `amap_sdk`），补 `description` 与 `dependencies`
3. `src/template_package/` 改名为 `src/<pkg_name>/`，保留 `__init__.py`
4. 补包级 `AGENTS.md`（只写该包特有约定，不得与根冲突，R8）
5. 共享包必须补 `CHANGELOG.md`（R12）与 `README.md`（用途、接口速览、迁移自哪个项目）
6. 应用补 `ROADMAP.md`（执行记录模式，R5）
7. 纳入仓库：根目录 `uv sync` 确认 uv.lock 更新，测试放 `apps/<name>/tests/`，提交 `chore(<name>): 迁入/新建 <name>`

依赖 workspace 内共享包时，在成员 pyproject.toml 中声明：

```toml
[project]
dependencies = ["amap-sdk"]

[tool.uv.sources]
amap-sdk = { workspace = true }
```
