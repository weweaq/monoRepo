# AGENTS.md — mermaid-viewer 包级约定

> 本文件是 mermaid-viewer 的特有约定，与根 AGENTS.md 配合使用。
> 冲突时以根 AGENTS.md 为准，并提 ADR 修订。

## 1. 项目定位

本地离线 `mermaid .mmd` 查看 + 评审工具：服务端以仓库根为静态根托管 viewer，前端在图上点节点/连线打标评论，评论持久化到 SQLite。纯标准库 + 原生 JS + 本地 vendor 库，不联网。默认仅绑 loopback，需手机/局域网访问时可显式 `--host 0.0.0.0`。

## 2. 特有约定（本包独有，根规则未覆盖）

### 2.1 技术约束
- **stdlib only**：服务端仅用 Python 标准库（http.server / sqlite3），不引第三方；前端零构建，原生 JS 改完即生效
- **离线**：mermaid / svg-pan-zoom 用本地 `vendor/` 库，不 CDN
- **docroot 是仓库根**：`viewer_server.py` 静态服务于全仓根（这样可打开任意 `apps/*/docs/*.mmd`），`ROOT` 三级上溯算仓库根；若改 docroot 需谨慎评估暴露面
- **loopback 默认，LAN 显式开启**：`viewer_server.py` 默认绑 `127.0.0.1`（`--host` 默认值），仅显式 `--host 0.0.0.0` 才监听所有网卡（供手机/局域网访问）。默认不联网；改绑 LAN 属有意暴露，需确认同网信任环境。`_lan_ip()` 用 UDP connect 探测本机默认网卡 IP，不发真实流量

### 2.2 评论存储双后端
- HTTP 模式（经服务打开）：评论写 `data/reviews.db`（SQLite，断电不丢）
- file:// 模式（双击打开）：评论退回到浏览器 localStorage
- 前端按 `location.protocol` 自动切换，互不干扰

### 2.3 日志规范
- 服务本身无 JsonlLogger（单用户本地工具，`log_message` 写 stderr）；若后续扩展运行日志，参考根 buildApp skill 的推荐约定

## 3. 代码地图

| 符号 | 类型 | 位置 | 作用 |
|------|------|------|------|
| `Handler` | class | viewer_server.py | 路由 + JSON API + 静态文件 |
| `_parse_reviews_key` | fn | viewer_server.py | 解析 `/api/reviews/<key>` |
| `do_GET` / `do_POST` | fn | viewer_server.py | 查询 / upsert 评论 |
| `translate_path` | fn | viewer_server.py | 强制 docroot=仓库根，防路径逃逸 |
| `_browser_target` | fn | viewer_server.py | 浏览器导向 URL |
| `_lan_ip` | fn | viewer_server.py | 探测默认网卡 IP，`--host 0.0.0.0` 时打印手机访问地址 |
| `main` | fn | viewer_server.py | 入口：绑 `--host`（默认 loopback）→ serve_forever |
| `viewer.html` | — | （同目录） | 前端：打开 .mmd / 缩放 / 评论 / 导出 |
| `gen-mmd-viewer.mjs` | — | （同目录） | 把 .mmd 固化成独立离线 HTML |
| `scan-mmd-reviews.py` | — | （同目录） | 扫描 mmd 内 `%%[type:id]` 评审标记 |

## 4. 目录结构

```
mermaid-viewer/
├── pyproject.toml        # workspace 成员 + [project.scripts] mermaid-viewer
├── viewer_server.py      # 服务端（唯一源码模块）
├── viewer.html           # 前端（原生 HTML/JS）
├── gen-mmd-viewer.mjs    # .mmd -> 独立 HTML 生成脚本
├── scan-mmd-reviews.py   # 评审标记扫描脚本
├── vendor/               # 本地 mermaid + svg-pan-zoom 库
├── data/                 # 运行时评论库 reviews.db（gitignored，留 README）
├── docs/                 # tech 文档 + 架构图
├── tests/                # 业务测试
├── AGENTS.md             # 本文件
└── ROADMAP.md            # 执行记录与进度
```

## 5. 常用命令

```powershell
# 直接运行服务（无需 uv sync）
python apps/mermaid-viewer/viewer_server.py

# 经 workspace 入口（需 uv sync --all-packages）
uv run --package mermaid-viewer mermaid-viewer

# 生成独立 HTML / 扫描评审标记
node apps/mermaid-viewer/gen-mmd-viewer.mjs <input.mmd>
python apps/mermaid-viewer/scan-mmd-reviews.py <input.mmd>

# 访问
#   查看器(本机): http://127.0.0.1:8123/apps/mermaid-viewer/viewer.html
#   查看器(手机/局域网, 需 --host 0.0.0.0): http://<电脑局域网IP>:8123/apps/mermaid-viewer/viewer.html
#   API:     GET/POST /api/reviews/<file>
```