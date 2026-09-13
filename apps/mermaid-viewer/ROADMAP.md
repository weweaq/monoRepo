# ROADMAP — mermaid-viewer 执行记录与进度

## 待办清单

- [ ] 在 dev-console 仪表盘实测启动→运行→停止/重启三态（迁移后）
- [ ] 评论数据迁移（旧 tools/mermaid-viewer/data/reviews.db 若存在需搬入新路径）
- [ ] 补充 tests/（viewer_server 的 HTTP 行为测试：API 读写、路径逃逸防护）

## 执行记录

### 2026-09-13 · 评审交互升级：建议中心 + 抽屉批注 + 节点角标 + LLM 闭环 + 记住上次文件

**背景**：评审打标交互存在可用性问题：全屏大弹窗太重、关闭后无法回看历史评论、点击节点看不到对应历史记录。按用户诉求，改成「点节点→该处批注+建议中心围观历史」的更轻工作流，并打通 LLM 修复闭环。

**已完成**（`viewer.html`，纯前端改，改完即生效）：
- **建议中心**：原「评论」面板改名「建议中心」，新增搜索框（节点/关键词）、状态筛选 chips（全部/待处理/已处理）、「标注于 <符号>」过滤条与「显示全部」；每条建议含类型 + 符号 + 状态（待处理/已处理，点击切换）+ 删除
- **抽屉批注**：全屏弹窗 → 右下角 340px 抽屉；类型改为胶囊单选（问题/修复/想法，英文 `review/fix/idea` 兼容旧数据）；元字符数实时统计（0/300）；提交后该处建议数随角标即时刷新
- **节点角标**：在图上每个还有待处理建议的节点右上角叠加红色数字角标，建议处理后被标记已处理即消失；`applyBadges()` 用 `getBBox()` 定位、`id="<符号>"` 绑定节点
- **LLM 闭环**：工具栏新增「LLM 修复提示」按钮，复制生成含全部待处理建议（不含已处理）的结构化 prompt 到剪贴板，并 toast 提示「已复制…（含 N 条待处理）」；空时提示无待处理
- **记住上次文件**：启动时先恢复内容缓存副本（`localStorage`，兼容 file://），若记得 File System Access handle 再取磁盘实时版；「打开」优先走 `showOpenFilePicker`（拿真实 handle），失败回退原生选文件；`cacheLastEdited` 超 1MB 不缓存
- **轻量 toast**：替代状态栏文字，居中底部浮层反馈（复制成功/无待处理等）

**实测验证**：浏览器打开确认渲染、节点点击挂批注、角标计数、建议中心搜索/筛选/状态切换、LLM 提示复制、重启后恢复上次文件均可用。未起 subagent 复核（改动全前端、无接口变动，R5 判断架构图无需改）。

**偏差说明**：`view_server.py` 未变；评论记录新增 `status` 字段（`open`/`closed`），旧无此字段记录按未定义处理，不影响导出。架构图未涉及模块/数据流变动，未改。

**待办更新**：
- [ ] 实测 `--host 0.0.0.0` 后手机读写评论
- [ ] 用毕切回 loopback

### 2026-09-13 · 支持手机/局域网访问（--host 0.0.0.0）

**背景**：viewer 默认仅绑 `127.0.0.1`，手机无法访问。需加 `--host` 参数供同网设备打开，并由 dev-console 启动时带上 `--host 0.0.0.0`。

**已完成**：
- `viewer_server.py`：新增 `--host` 参数（默认 `127.0.0.1` 保持 loopback 语义）；`ThreadingHTTPServer` 使用 `args.host` 绑定；新增 `_lan_ip()`（UDP connect 探测默认网卡 IP，不发真实流量），`--host 0.0.0.0` 时打印手机/局域网访问地址
- dev-console `services.json`：mermaid-viewer 条目 `args` 追加 `--host 0.0.0.0`；`frontend` 仍为 `127.0.0.1`（供本机仪表盘 "打开" 按钮）；notes 注明手机访问方式
- 文档同步（R5）：AGENTS.md（loopback 默认/LAN 显式开启 + `_lan_ip` 代码地图 + 访问命令）、tech 文档安全边界（0.0.0.0 暴露风险警示）

**实测验证**：待用户在 dev-console 重启后在手机验证（见待办）。

**偏差说明**：架构图未改动——`--host` 属服务启动绑定层变更，不涉及架构图内的模块/数据流/依赖，符合 R5 判断依据（架构图节点仍可回溯源码符号）。

**待办更新**：
- [ ] dev-console 重启后实测手机经 `http://<电脑局域网IP>:8123/...` 访问与评论读写
- [ ] 用毕将服务切回 loopback（`--host` 不传即默认），避免全仓文件暴露到局域网

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