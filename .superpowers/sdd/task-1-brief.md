### Task 1: 项目骨架

**Files:**
- Create: `pyproject.toml`
- Create: `profile/__init__.py`
- Create: `profile/io/__init__.py`
- Create: `profile/analysis/__init__.py`
- Create: `profile/output/__init__.py`
- Create: `profile/cli/__init__.py`
- Create: `tests/__init__.py`

**Produces:** 可 import 的空包结构，pytest 可运行。

- [ ] **Step 1: 创建 pyproject.toml**

```toml
[project]
name = "profile"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=7.0"]
```

- [ ] **Step 2: 创建所有 `__init__.py`**

```powershell
New-Item -ItemType File -Force -Path profile/__init__.py, profile/io/__init__.py, profile/analysis/__init__.py, profile/output/__init__.py, profile/cli/__init__.py, tests/__init__.py
```

- [ ] **Step 3: 验证包可导入**

```powershell
python -c "import profile; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: 安装 pytest 并验证**

```powershell
pip install pytest; if ($?) { python -m pytest --version }
```
Expected: 显示 pytest 版本号

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml profile/ tests/
git commit -m "feat: add project skeleton"
```
