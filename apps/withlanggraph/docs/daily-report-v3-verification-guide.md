# 日报链路 v3 实测体验指南（2026-10-04）

> 对象：2026-10-03/04 落地的**全部改动**（提交 `25cf74d`→`6c7d1f9`）。§1~§7 是日报链路 v3 主线；**§8 是两天内其余改动的点验路径**（智谱 provider / target_day / 假阴性兜底 / mermaid-viewer 锚点 / dev-console / 慢测试 / 体检定时任务）。
> 用法：按 §0 准备一次，然后按 §8 索引表挑感兴趣的点；每步都给了"预期看到什么"，对不上就是问题。
> 注意：**"原样替换"模式已设计、尚未实施**——评审页批注框里目前只有"提交修订"（LLM 改写）一种方式，找不到属正常。

## §8 索引：两天改动 → 验证路径速查

| 改动 | 提交 | 怎么验证 | 章节 |
|------|------|---------|------|
| 评审页闭环 + health 归因 + rerun | `1476d35`~`ab7daaf` | 浏览器点 §1/§2，或 `--no-email` rerun | §1/§2/§4 |
| 智谱 GLM provider | `e650284` | llm_requests.jsonl 看 provider/model | §8.1 |
| 日报补跑 target_day 切片 | `25cf74d` | rerun 后邮件讲的是目标日的事 | §8.2 |
| langTrack 历史日假阴性兜底 | `5fda986` | langTrack dashboard 看历史日 | §8.3 |
| 日报 prompt 禁自调 send_email | `2c6f41c` | 例行日报只收一封、主题规范 | §5 |
| mermaid-viewer 锚点符号+行号 | `44a27c3` | 打开 viewer 点节点/复制 LLM 提示词 | §8.4 |
| dev-console langtrack"打开"按钮 | `909764e` | dev-console 点按钮直达 dashboard | §8.5 |
| 慢测试修复（他人）+ 提速验证 | — | 跑全量 pytest 计时 | §8.6 |
| 仓库体检 skill + 每天 9:00 定时任务 | `7f95750` + automation | 次日 9:00 看体检报告 | §8.7 |

---

## §0 开始前的一次性准备（约 2 分钟）

1. **确认 token**：`apps/withlanggraph/.env` 里已有 `REVIEW_TOKEN=<你的密钥>`（已配好，跳过）。
2. **重启全部受管服务**：跑一次 `start.bat`（杀旧拉新）。目的：
   - gacore 进程必须加载 v3 代码（信息包/修复阶梯/版本号都在里面）；
   - review 服务当前是手工拉起的临时实例，start.bat 会接管为标准方式；
   - langtrack 服务的 dashboard 用上新版 fact_card。
3. **60 秒自检**（都对再往下走）：

| 检查 | 操作 | 预期 |
|------|------|------|
| review 活着 | 浏览器开 `http://127.0.0.1:8010/review` | 自动跳转到 `/review/最近日期`（307 跳转，不是 404） |
| 数据在 | 同页能看到日报正文和 `[节-序号]` 角标 | — |
| dev-console | 开 dev-console 页 | 多出 `review` 服务卡片，running=True，有"打开"按钮 |
| 其余服务 | langtrack `:8000/dashboard`、mermaid-viewer `:8123/.../viewer.html` | 均能打开（§8.3/§8.4 要用） |

---

## §1 评审页闭环（核心体验，约 10 分钟）

入口：dev-console → review 卡片"打开"按钮，或直接 `http://127.0.0.1:8010/review`。

### 1.1 LLM 最小修订（默认动作）

1. 点日报里任意一条 `[节-序号]` 角标（比如 `[工作日志-3]`）→ 右侧弹出批注框
2. 选"事实订正"，输入改写内容（比如把那句话换个说法），点**提交修订**
3. 等待约 10~30 秒（真实调用智谱 API 做零工具单轮修订）

**预期**：
- 页面出现**分节 diff 视图**：只有你点名的那个节高亮，其余节原样
- 收到邮件，主题 `[gacore] daily-report · {日期}（重生成 v2）`，正文头部一行"本版为 v2 重生成，依据 M 条人工订正"
- **diff 门禁**（自动执行，无需你检查）：非目标节的文字逐字不变
- 响应里 `"fallback": false` 表示门禁一次通过（若模型违规改动会自动重试 1 次，仍失败降级为"订正原文追加到节末"并在页面注明）

### 1.2 版本号递增

再提交一条不同锚点的修订 → 主题变 `（重生成 v3）`。每次修订/重生成 +1，改到哪版一目了然。

### 1.3 整体重生成（③，重口味动作）

点**整体重生成**按钮 → 确认弹窗（列出生效订正条数）→ 状态页轮询 → 完成后收到**整版重写**的邮件。

**预期差异（重要）**：③ 的邮件里各节措辞都会变（模型重新写作），你订正的事实会被采纳但**措辞可能被转述**——这正是 ② 是默认路径的原因。昨天 10-03 的 v2（最小修订）vs v3（整版重写）两封邮件就是现成对比样例。

### 1.4 偏好反馈（C8）

批注框里选"偏好"类型提交（比如"个人观察节不要罗列数据"）→ 不改正文，落进 `data/feedback/preferences.json`，**从下次日报起**注入〔用户偏好〕块影响写作风格。

---

## §2 /health 源体检页（观测层）

`http://127.0.0.1:8010/health`。

### 2.1 总览矩阵

- 行 = 信息源（9 个），列 = 最近 14 天（有数据的日期）
- 色块语义：**绿 ok**（有数据进包）/ **黄 empty**（当日无数据）/ **红 failed**（取数失败）/ **灰 missing_data**（明确知道原因的空，如"未登录"）
- 悬浮显示 note；点单元格进单源详情

### 2.2 单源详情（逐层归因的核心页）

`/health/source/{日期}/{源key}`，三节并列：

| 节 | 是什么 | 用来看什么 |
|----|--------|-----------|
| 完整取数详情 | 当日查询的**全部**结果（不挑选不压缩） | "原始数据里到底有没有这个信息" |
| 渲染文本 | 挑选/压缩后、截断前 | "挑选规则有没有漏掉重要条目" |
| 实际进包 | 再经行级截断/预算熔断后 | "是不是被预算挤掉了"（整块丢弃会标"未进包"） |

页面还有**三级字符对比条**（detail_chars vs full_chars vs chars）和**"查看最终 LLM 输入"外链**（该日 scheduled 存档 + llm_requests.jsonl）。

**归因演练示例**：怀疑"某信息没进日报"时——先看完整取数详情：没有 → 源头/取数问题；有但渲染文本没有 → 挑选规则问题；渲染文本有但实际进包没有 → 预算牺牲；进了包还没写进日报 → prompt/消费层问题。每层都有明确出口，不用再猜。

### 2.3 _FACT_CARD.md（事实卡支线）

`/health/source/{日期}/_FACT_CARD`：事实卡 compact 全文 + **compact_omitted**（因 900 字预算被省略的 section——"预算悄悄省掉睡眠 section"这类事从此可见）。

---

## §3 QQ 端（回归 + 新行为）

1. **老路径回归**：QQ 发 `订正 [工作日志-2] 改成……` → 回"确认" → 锚点存在时走**确定性补丁**（秒回，不经 LLM）→ 回"确认重发"收邮件
2. **新行为——自动升级**：订正一个日报里不存在的锚点 → 确认 → 预期提示 **"精确补丁不可用，已改用最小修订改写…并重发"**（自动走 ②，不再报"生效失败"）
3. **审计可查**：每次提交即落 `apps/withlanggraph/data/feedback/corrections/{日期}.json`（同锚点重复订正自动 superseded 旧的）

---

## §4 信息包与 rerun（免打扰跑法）

不想收邮件的话，用 `--no-email` 触发一次完整链路：

```
cd apps\withlanggraph
uv run python -m gacore.rerun --day 2026-10-03 --no-email
```

跑完检查（约 1~2 分钟的 agent 真实运行）：

| 检查 | 位置 | 预期 |
|------|------|------|
| 体检行 | `data\logs\info_pack_health.jsonl` 末行 | `trigger: "rerun"`，9 个源各带 `status/chars/full_chars/detail_chars`，**无 `_LANGTRACK`**（A′ 已移除） |
| 详情文件 | `data\logs\pack_detail\2026-10-03\` | 9 个源 `.md` + `_FACT_CARD.md`，每个三节 |
| 订正注入 | scheduled 存档（见命令输出的 output_path）里搜 `〔人工订正·` | 包首有订正块，`correction_chars > 0` |
| A′ 单出口 | `_FACT_CARD.md` | compact 含"睡眠""时段×应用"section |

---

## §5 今晚起自然验证（不用动手）

- **23:50 例行日报**：jsonl 新增 `trigger: "scheduled"` 一行；邮件主题无 vN 标记、无"（补跑）"字样（C5 已废除补跑标记，改为版本号体系）
- **连续 3 天**：对照 jsonl 里 `status:ok` 的源与日报正文，验证消费覆盖规则（每个 ok 源至少被正文消费一次，或在 daily note 归档节说明跳过原因）
- **偏好生效**：提交过偏好的话，观察后续日报风格变化

---

## §6 状态清理与故障排查

| 现象 | 原因 | 处置 |
|------|------|------|
| 提交类操作全部 401 | REVIEW_TOKEN 未配/不一致 | 检查 `.env`，改后重启服务 |
| `/review` 返回 404 | 服务在跑旧代码 | 重启 review 服务（start.bat） |
| 修订报 `no_delivered` | 该日没有已投递存档 | 先跑一次该日日报/rerun |
| 重生成一直 running | agent 还在跑（历史经验 10~15 分钟） | 状态页轮询即可，同日重复提交会 409 |
| `2026-10-03` 的旧验证订正还在生效 | `c-20261003-01`（"部署验证…"）是 active | 会影响以后对 10-03 的重生成；在评审页对同锚点再提交一条覆盖，或手动把该记录 `status` 改为 `superseded` |

---

## §7 验收对照清单

- [ ] §0 自检三项通过
- [ ] 评审页提交修订 → diff 视图只高亮目标节 + 邮件 v2，其余节逐字未动
- [ ] 二次修订 → 邮件 v3
- [ ] 整体重生成 → 确认弹窗 → 完成后整版邮件
- [ ] 偏好提交 → preferences.json 落盘
- [ ] /health 矩阵可读，单源详情三节 + L0 外链可打开
- [ ] QQ 订正→确认 老路径不回归；坏锚点自动升级 ②
- [ ] `--no-email` rerun 后 jsonl 9 源齐全、无 _LANGTRACK、pack_detail 10 文件
- [ ] 今晚例行日报：jsonl trigger=scheduled、主题无 vN 无"补跑"

---

## §8 两天内其余改动点验

### 8.1 智谱 GLM provider（`e650284`）

- **配置确认**：`apps/withlanggraph/.env` 里 `LLM_PROVIDER=zhipu`、`ZHIPU_API_KEY=...`（有值）
- **实际生效证据**：开 `apps/withlanggraph/logs/2026-10-03/llm_requests.jsonl`（当天有日报/对话就有），任意一行看 `"provider": "zhipu", "model": "GLM-5.3-Flash"`——真实请求走的智谱
- 切回 deepseek 只需改 `.env` 的 `LLM_PROVIDER` 并重启 gacore

### 8.2 日报补跑 target_day 切片（`25cf74d`）

- 补跑 9-11~9-20 中任意一天：`uv run python -m gacore.rerun --day 2026-09-15 --no-email`
- **预期**：生成的日报讲的是 **9-15 那天**的事（langTrack 事实卡按目标日切片），而不是运行日的空轨迹——修复前补跑会"只字不提目标日行程"
- 佐证：scheduled 存档 User Prompt 里的生活事实卡 `day=2026-09-15`

### 8.3 langTrack 历史日假阴性兜底（`5fda986`）

- 打开 langtrack 服务的 dashboard：`http://127.0.0.1:8000/dashboard`
- 任选一个历史日（如 2026-09-28）：显示"屏幕 3.5h / 哔哩哔哩…"即读取正常——修复前这种日会被误报"无手机数据"
- 兜底逻辑平时不触发（数据健康时），它的价值是"日报构建时刻汇总表缺行时自动补建"，从 dashboard 的稳定输出间接确认

### 8.4 mermaid-viewer 评审锚点改版（`44a27c3`）

1. dev-console → mermaid-viewer 卡片"打开"（`http://127.0.0.1:8123/apps/mermaid-viewer/viewer.html`），加载 `apps/withlanggraph/docs/architecture-flow.mmd`
2. **点任意节点** → 左下弹出的批注锚点预期是 **`节点 SC（行 9）`** 这种「符号+行号」格式，不再是 `flowchart-SC-2` 这种无意义 DOM id
3. **点边标签**写一条意见 → 锚点是 `连线 <源码行原文>（行 N）`
4. 点**一键复制 LLM 修复提示词** → 每条意见带 `loc: 节点 XX（行 N）` + `源码行:` 原文；历史遗留的旧格式锚点（如 `edge label`）会保持原样（已知边界）
5. 逻辑回归：`node apps/mermaid-viewer/tests/check_prompt_logic.js` → 13 项全 PASS

### 8.5 dev-console langtrack"打开"按钮（`909764e`）

- dev-console → langtrack 卡片 → "打开"按钮 → 直达 `http://127.0.0.1:8000/dashboard`
- 对照：gacore（无 HTTP 端口）与 py-wei 卡片**没有**"打开"按钮——这是有意为之，不是缺失

### 8.6 测试提速验证（慢测试已由他人修复）

```
uv run --all-packages pytest -q
```

- **预期：全量 1244 passed / 5 skipped，总时长约 1~2 分钟**（修复前 5~10 分钟）
- test_scheduler 的 TestRunJob 每个用例 24s → 亚秒级；不再有真实 LLM 调用和 Edge/bili 真实读取

### 8.7 仓库体检定时任务（`7f95750` + automation）

- 每天 **9:00** 自动执行：提交完整性盘点 → ruff/pytest 门禁 → 文档代码三处同步抽查 → 输出体检报告
- 验证：次日 9 点后在 Automations 页看运行记录与报告；也可随时手动触发一次
- 它会自动盯住：工作区是否干净、架构图与代码是否失真（比如 §8.4 那类改动若忘了同步文档会被点名）
