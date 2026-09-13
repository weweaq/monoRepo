# ROADMAP — mermaid-viewer 执行记录与进度

## 待办清单

- [ ] 在 dev-console 仪表盘实测启动→运行→停止/重启三态（迁移后）
- [ ] 评论数据迁移（旧 tools/mermaid-viewer/data/reviews.db 若存在需搬入新路径）
- [ ] 补充 tests/（viewer_server 的 HTTP 行为测试：API 读写、路径逃逸防护）

## 执行记录

### 2026-09-13 · 修复「点有建议的节点侧栏不展示建议」（符号匹配 bug）

**背景**：上一轮改动「点有建议的节点直接展示右侧栏建议」在真机（含无痕）上不生效——点击节点后右侧栏仍显示「这个位置还没有评论」，且左下角仍弹写批注框。角标数字能正常显示在节点上，但侧栏定位不到。

**根因**：`nodeCommentSym()` 在 `currentNodeMap` 未命中时 fallback 用 `nodeEl.id`，而 mermaid 对（子图内）节点会改写 SVG 内部 id（如 `flowchart-SC-8`），不等于建议里存的源符号 id（如 `scheduler.run.job`）。于是：
- `svg click` 里「该节点是否有待处理建议」判断 `r.sym === sym` 为 false → 误判无建议 → 弹写批注框而非展示侧栏
- 侧栏 `renderSideList` 用 `r.sym === activeSym` 全等过滤 → 恒为空 → 空提示

**已完成**（`viewer.html`，纯前端）：
- 新增 `symDisp(sym)`（提取 `节点 ID（显示文本）` 的显示文本，跨渲染稳定）与 `sameSym(a,b)`（两个符号是否指向同一节点：都有 id 则按 id 判等；id 缺失/不齐时回退显示文本判等）
- `svg click` 的 openAt 判断改为 `sameSym(r.sym, sym)`
- `renderSideList` 的过滤条件改为 `sameSym(r.sym, activeSym)`（替代 `r.sym === activeSym`）

**实测验证**：走查语义：点击 `scheduler.run.job`（内部 id `flowchart-SC-8`）时，与建议（源 id `scheduler.run.job`）经 disp 兜底匹配 → openAt=true → 展示右侧栏、不再弹写批注框。edge 建议（sym 为边 title，无 id）不会被误混入 node 过滤。真机复测待用户验证。

**偏差说明**：无接口/数据流变化，架构图无须改；ROADMAP 记录。同一显示文本有多个节点时 disp 兜底会用 display 全匹配（罕见、可接受），id 精确优先。

**待办更新**：
- [ ] 手机（重启服务 + 强刷）复测：点带橙红角标的节点 → 右侧栏立即列出该节点建议
- [ ] 确认角标颜色为橙红（若仍蓝可能仍是旧缓存，需确认 no-cache 生效）

### 2026-09-13 · 角标更醒目 + 点有建议的节点直接展示建议

**背景**：用户反馈两点：(1) 节点建议数字角标不明显（当前红点 `r=8.5`、无描边，缩放下看不清，视觉上被误认偏蓝）；(2) 点击有建议的节点，右下角 composer 抽屉（z40）会盖住右侧建议栏（z30），导致看不到已有建议。

**已完成**（`viewer.html`，纯前端）：
- **角标醒目**：橙红 `#f4511e`，圆半径 8.5→11、描边 1.2→2，数字加大到 12px，并加白描边 halo（`paint-order:stroke`）保证叠在线条上清晰
- **点有建议的节点展示建议**：`svg click` 里先查该符号是否有待处理建议（`r.status!=="closed" && r.sym===sym`）；有 → 只打开右侧栏、`activeSym` 过滤聚焦，不弹 composer（避免遮挡）；无 → 维持弹 composer 引导写第一条
- **侧栏头新增「＋批注」按钮**：当处于某符号过滤态时显示，点它再开 composer 给同一位置追加新批注；`renderSideList` 按 `activeSym` 显隐

**实测验证**：改动走查无语法问题。真机（手机点节点/角标醒目度）待 dev-console 重启后验证（纯前端，刷新页面即可生效，无需重启服务）。

**偏差说明**：无接口/表结构/数据流变化，架构图无须改。R5 仅 ROADMAP 记录。

**待办更新**：
- [ ] 手机刷新后验证：橙色角标醒目、点有建议节点右侧栏立即列出建议、「＋批注」可再追加

### 2026-09-13 · /api/mmds 只收 mermaid 源文件（去掉 .txt）

**背景**：第一版 `scan_mmds` 收集 `.mmd/.mermaid/.txt`，用户实测电话下拉里混入大量 `.txt` 非图文件（47 项），过滤条件不对。用户期望「最起码也应该是 .mmd 结尾」。

**已完成**：
- `viewer_server.py`：`scan_mmds` 白名单改为仅 `.mmd/.mermaid`，排除 `.txt/.png` 等非图文件（docstring 说明）
- `viewer.html`：下拉计数文案 `共 N 个 .mmd/.txt` → `共 N 个 .mmd`
- `tests/test_scan_mmds.py`：断言 `.txt` 不再被收集（补注释）；4 用例仍全过

**实测验证**：`ruff` 过、`pytest` 4 passed。`scan_mmds(ROOT)` 全仓实扫 → **mmd count= 6**（5 `.mmd` + 1 `.mermaid`）。旧版 47 项大半是 `.txt`。

**偏差说明**：手输路径 / `?file=` 深链仍允许 `.txt`（前端 `openUrlPath` 复用为查看任意文本图源），仅扫描列表过滤非图文件——tech 文档已注明此边界。架构图无变化（接口/节点未动）。

**待办更新**：
- [ ] 重启 mermaid-viewer 服务后手机实测下拉只剩 .mmd/.mermaid

### 2026-09-13 · 扫描全部 .mmd 供下拉选择（/api/mmds）

**背景**：上一版「从电脑加载」需要手输仓库相对路径，手机上输入麻烦、也记不住路径。用户希望自动扫描电脑上有多少 `.mmd`，然后下拉选择。

**已完成**：
- `viewer_server.py`：新增 `GET /api/mmds` → `{"mmds": [...]}`。扫描逻辑提升为模块级 `scan_mmds(root)`（`os.walk` 递归收集 `.mmd/.mermaid/.txt` 相对路径，统一 `/` 分隔），handler `_scan_mmds()` 复用；默认跳过 `.git/node_modules/.venv/vendor/data/__pycache__/.ruff_cache/.pytest_cache/dist/build/htmlcov/*.egg-info`
- `viewer.html`：弹出层改为「下拉框（打开时自动拉取列表填充 + 计数显示「共 N 个」）+ 手输框 + 刷新按钮」；`url-go` 用下拉选中路径调 `render`；保留 `?file=` 深链
- 新增 `tests/test_scan_mmds.py`：扩展名收集、垃圾目录过滤、`/` 分隔、（真实仓库根含自家架构图）4 用例全过；补齐 ROADMAP 待办「补充 tests/」的部分（scan_mmds 层）

**实测验证**：`uv run --all-packages pytest apps/mermaid-viewer/tests` → 4 passed；`uv run ruff check apps/mermaid-viewer` → All checks passed。真实功能（手机下拉实测）待 dev-console 重启后验证（`/api/mmds` 返回全仓 mmd 列表）。

**偏差说明**：架构图已同步——新增 `MMDS` 节点（`do_GET /api/mmds`）与 `PICK` 节点（从电脑加载）及 `GET /api/mmds`、`GET /<path>` 两条数据流；tech 文档接口表新增一行 + 数据流小节改写。服务端仅新增只读扫描接口，无写文件/写库变化，安全边界不变（docroot 未变）。

**待办更新**：
- [ ] dev-console 重启后手机实测：点「从电脑加载」→ 下拉看到全部 .mmd → 选中渲染
- [ ] 用毕切回 loopback（`--host` 不传）
- [x] ~~补充 tests/（scan_mmds 扫描逻辑）~~
- [ ] 可选：tests/ 覆盖 viewer_server 完整 HTTP 行为（API 读写、路径逃逸、/api/mmds 端点）

### 2026-09-13 · 手机端按仓库路径加载 .mmd（从电脑拉取）

**背景**：手机经 `http://<电脑IP>:8123/...` 打开 viewer 能看界面，但「打开 .mmd」走文件选择器 / File System Access，只能挑手机自身的文件，够不到电脑上的 `.mmd`。而 `viewer_server.py` docroot 本就是仓库根、电脑上的所有 `.mmd` 都可通过 URL 访问，缺的只是前端入口。

**已完成**（`viewer.html`，纯前端，改完即生效，服务端零改动）：
- 工具栏新增「**从电脑加载**」按钮 → 弹出输入框，填仓库根相对路径（如 `apps/withlanggraph/docs/architecture-flow.mmd`），前端 `fetch('/'+path)` 拉取并渲染；`fileKey` 记为相对路径，复用同一评论主子键
- 地址栏**深链** `?file=<相对路径>`：打开即加载指定 .mmd，便于把链接存书签/发手机
- 路径强制 `.mmd/.mermaid/.txt` 后缀；空/非法/404 有状态栏提示；点遮罩/Esc 相关处可关闭弹层
- 空状态提示补一句「手机上点『从电脑加载』…」引导发现

**实测验证**：代码走查无语法问题，改动自洽。功能实测待用户经 dev-console 重启后在手机验证（见待办）。

**偏差说明**：架构图未改——本改动属前端「加载数据源」分支扩展，复用既有 docroot 静态能力，服务端模块/数据流无变化（tech 文档数据流新增一小节说明，符合 R5 不失真）。

**待办更新**：
- [ ] 手机经 dev-console 重启后实测「从电脑加载」路径与 `?file=` 深链
- [ ] 用毕切回 loopback（`--host` 不传）

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