# mermaid .mmd 查看器

本地离线查看与评审 `.mmd`(mermaid 源码)架构图的小工具，纯浏览器 + 标准库，不联网。

## 文件说明

| 文件 | 作用 |
|------|------|
| `viewer.html` | 主查看器。工具栏：打开 .mmd / 拖拽导入 / 缩放平移 / Fit / 导出 PNG / 导出标记；图上**点节点或连线**可打标评论 |
| `viewer_server.py` | 本地服务。以仓库根为静态根托管 viewer，并把评论**持久化到 SQLite**(`data/reviews.db`)，断电/重启不丢 |
| `gen-mmd-viewer.mjs` | 把一个 `.mmd` 固化成独立离线 HTML(`<名>.viewer.html`)，适合提交或给别人看 |
| `scan-mmd-reviews.py` | 扫描 `.mmd` 里的 `%%[type:id]` 评审标记，汇总成意见清单供 LLM 消费 |
| `vendor/` | 本地化的 mermaid + svg-pan-zoom 库(离线渲染用) |
| `data/` | 运行时评论库，不入 git |

## 两种打开方式

**A. 只读看(双击 viewer.html 即可)** —— 用 `file://` 打开，评论只存浏览器 localStorage，换路径/清缓存可能丢。

**B. 评论要真正断电恢复 —— 用服务打开(推荐给你这种场景)**：

```powershell
python apps/mermaid-viewer/viewer_server.py
# 自动打开浏览器到 http://127.0.0.1:8123/apps/mermaid-viewer/viewer.html
# 可选: --port 8123 / --no-browser
# 或用 pyproject 入口(需 uv sync --all-packages): uv run --package mermaid-viewer mermaid-viewer
```

服务模式下评论写进 `data/reviews.db`，重启机器都还在。前端做了双后端：http 走 SQLite，file 自动回退 localStorage，互不干扰。

## 评论 → LLM 优化 的闭环

1. 服务/打开 viewer，加载某个 `.mmd`
2. 点图上节点(绑定符号)或连线(绑定边) → 在弹出框写意见 → 存库
3. 顶部「导出标记」→ 会把当前文件评论转成 `%%[type:id] 定位 + 意见` 文本 / 或让 LLM 直接读 `data/reviews.db`
4. `scan-mmd-reviews.py` 扫描 mmd 文件里的标记，生成结构化清单抛给 LLM 去改图

> 提示：`.mmd` 自身也可手工写 `%% 注释` 当评审标记，scan 脚本会一并收集。

## 生成独立 HTML(给别人/提交时)

```powershell
node apps/mermaid-viewer/gen-mmd-viewer.mjs apps/withlanggraph/docs/architecture-flow.mmd
```

（R10: 脚本与文档均为 ASCII/英文；数据 README 用中文说明数据来源）