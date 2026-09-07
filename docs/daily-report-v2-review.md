---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 74fca65fe87f9b5d6900c56ec5512fbd_abfc2cbaa6fb11f1ac80525400aeaaa3
    ReservedCode1: 1tt6wKMttFlNqgRRxarxcNZy/ElL8rxm0say5w4WG03bGyzpn0v2jXSyv7wDQ8VLXmSHtASANHyAOQoWfZ25O4yY8CuQCnIDROhouwqiUeQMR02rRLyPROAeb+vlI0dye3Ghl7GpCX8qmcaQMkED1SXsPRXcLgUdk5aKZpd30sBDL1ccumiGtoE00xQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 74fca65fe87f9b5d6900c56ec5512fbd_abfc2cbaa6fb11f1ac80525400aeaaa3
    ReservedCode2: 1tt6wKMttFlNqgRRxarxcNZy/ElL8rxm0say5w4WG03bGyzpn0v2jXSyv7wDQ8VLXmSHtASANHyAOQoWfZ25O4yY8CuQCnIDROhouwqiUeQMR02rRLyPROAeb+vlI0dye3Ghl7GpCX8qmcaQMkED1SXsPRXcLgUdk5aKZpd30sBDL1ccumiGtoE00xQ=
---

# 日报重构 v2 实现 — 代码审核报告

审核人：独立审核员（subagent 审核循环第 1 轮）
审核日期：2026-09-03
审核对象提交方：file-agent（实现方）

## 一、审核范围与依据

| 对象 | 状态 |
| --- | --- |
| `src/gacore/daily_info_pack.py` | 新模块，495 行，全文审阅 |
| `src/gacore/scheduler.py` | 改动点：`_build_job_prompt`（L252）、`run_job` 接入（L306）、`_default_graph_runner`（L349） |
| `config/schedule.json` | daily-report prompt 精简（确认未失语义） |
| `tests/test_daily_info_pack.py` | 22 例，逐条核对 |
| `tests/test_scheduler.py` | 41 例，名单核对 |
| 交叉核验文件 | `context.py`（_MEMORY_BG_RULE/时间禁令/fold_history/trim_messages/build_system_prompt）、`graph.py`（run_once）、`state.py`（new_state）、`tools/*.func` |

## 二、逐维度结论

### 维度 1：高内聚低耦合 — 通过

- `daily_info_pack.py` 职责单一：只做"当日确定性信息聚合 + 裁剪 + 熔断 + 降级"，对外仅暴露 `build_info_pack(date, cfg=None)` 与 `PACK_BUDGET`，`__all__` 收敛。
- 对下游零渗透：经实际比对，tools 的 `.func`、`context.py`、config、GAState **均未被反向修改**；scheduler 仅新增 `_build_job_prompt` 一个注入点（主选通道约定内）。
- 依赖方向正确：复用 `tools/bili_history|browser_history|langTrack_tools|ncm_tools` 的 `.func`；长期画像延迟 import `scheduler._long_term_insight/_summarize_long_term`。`daily_info_pack` 顶导 tools、scheduler 函数内延迟导 `daily_info_pack`、`daily_info_pack` 函数内延迟导 scheduler —— 双向均函数内延迟，**无 import 环**。
- 硬编码未散落：信息包相关常量全部在模块内；scheduler 仅保留"daily 关键字判定 + 拼接格式"两个编排点。

### 维度 2：正确性（主选通道核验）— 通过

- **信息包确已完整送达 LLM**，链路为：
  `_build_job_prompt` → `run_job` → `_default_graph_runner` → `run_once(graph, prompt)` → `state.new_state` 生成 `HumanMessage(prompt)`（信息包 + 原 prompt 为一条整体 user 消息）→ graph invoke。
  追踪 `trim_messages`（单轮仅一条 HumanMessage，完整不过滤）与 `fold_history`（只对 system prompt 里"历史回顾"折叠到 200 字，不动原消息）——信息包主体**不会**被折叠/截断丢失。
- 分支隔离到位：非 daily job 由 `if "daily" in job.name.lower()` 天然隔离，`job.prompt` 原样返回；headless 之外的 QQ 前端（qq.py）不走 `_build_job_prompt`，完全孤立；`build_info_pack` 内外双保险（模块内逐源保护永不抛 + `_build_job_prompt` 外层 try/except 回退原 prompt）。
- **消费指令与 context 禁令兼容性（已实读 context.py 核对，假设成立）**：
  - `_MEMORY_BG_RULE` 约束的是"系统提示里注入的记忆背景不背诵/复述"；信息包在 user message 且自带"引用关键点、不整段复制"指令，方向与铁律一致，无冲突。
  - 时间禁令约束"历史记忆/每日笔记/历史对话中的时间为陈旧记录"；信息包携带 ["时间戳语义=当日数据时间戳，可直接引用为当日事实"] 的显式消费指令，且 date 取 `now 当天`，时间戳与【当前真实时间】同日，不构成"陈旧时间冒充当下"。
  - 残余弱点（P2）：禁令措辞对"任何时间/日期"无差别覆盖，当前靠 user message 权重 + 自带指令压制。若未来把信息包挪进 system prompt 注入，此兼容性即失效，需在迁移时同步改 context.py；本轮主选通道（user 前置）判定成立。

### 维度 3：健壮性 — 通过（含 P2 备注）

- 逐源 try/except：8 个源（长期画像/langTrack/bili/Edge/git/文件/ncm/记忆）各自独立保护，外加 `build_info_pack` 装配循环兜底（builder 抛异常也落降级块）+ 极端全败后的最小可用包兜底，**全源失败仍返回合法字符串，永不抛**。
- 2000 熔断：头部指令块计入整包 budget；单源 `_cap_text` 按字符边界截断 + "…"，整包 `_assemble_pack` 按 `remain-1` 预留省略号再生成，`remain<=16` 直接丢弃该块，**不会裁出超限残缺，total 恒 ≤2000**（`within_budget` 测试证实）。
- 空/满双向：单测对每源均做了空与满两种驱动（见维度 4）。
- P2 备注：`_cap_text` 截断点在 URL/数字/半句中间时语义残缺（不崩坏）；极端空白 body 会输出孤立 "…"（`_build_*` 实际恒有文本，风险极低）。

### 维度 4：测试充分性 — 覆盖良好，3 处 P2 漏测

- 已覆盖：各源"空/满"双向、langtrack error、bili error & no-today、Edge db_not_found/empty/full 域名归并、git empty/failed、ncm error/静默跳过、整包 within_budget、never_raises —— 22 例 + scheduler 41 例无回归。
- **漏测 1（P2）**：整体熔断分支只断言 `len<=2000`，未对"被挤出的尾部板块缺失 / clipped 行以 … 结尾 / 熔断后无残缺"做切面断言。建议加一例多源灌满超预算的用例断言上述三点。
- **漏测 2（P2）**：Edge 真实故障形态 `database is locked`（确认真实路径返回 `{"error":...}` 或抛 OperationalError 两条都降级，但测试仅覆盖 db_not_found），建议补 locked 形态。
- **漏测 3（P2）**：scheduler 层无 `_build_job_prompt` 专项测试（daily 注入 / 非 daily 隔离 / build 异常回退原 prompt）。现有 run_job 测试用 fake runner，未联动信息包。

### 维度 5：遗留风险复核

| 风险 | 复核结论 | 处置 |
| --- | --- | --- |
| Edge 数据库锁（browser_history 复制兜底未生效） | 本模块已正确降级为"- 该源失败"行，不阻断日报；根因在工具层 | 接受，非本模块职责；工具层修复后可自然恢复，无需改本模块 |
| ncm / 前日日报被 2000 字挤出 | 预算熔断的设计行为（自测已观察） | 接受；若需保留，可调 PACK_BUDGET→2400 或缩小 bili top 数，非阻塞 |
| bili 8s 耗时 | 来自双接口取数；单次调度可容忍 | 接受；可选优化（结果缓存 / 降 limit），非阻塞 |

## 三、问题分级清单

### P0（阻塞合入）
- 无。

### P1（建议本轮必修）
- 无。

### P2（可选，建议下轮修复或记录）

| # | 位置 | 问题 | 建议写法 |
| --- | --- | --- | --- |
| 1 | `daily_info_pack.py` build_info_pack 的 builders 列表 | 死代码：`("_HEAD_", lambda _d,_c: ...)` 因循环内 `if key=="_HEAD_"` 而永不被调用，`_HEAD_CAP` 实际未生效（头部未做 cap 判定） | 删除该 lambda 项，头部逻辑收敛到循环头分支；或抽 `_HEAD_KEY` 常量统一 |
| 2 | `tests/test_daily_info_pack.py` within_budget | 整体熔断分支无切面断言 | 灌满多源超预算，断言：pack≤2000、被挤掉的板块不存在、clipped 行末端为 "…" |
| 3 | `tests/test_daily_info_pack.py` edge 系 | 未覆盖 `database is locked` 真实故障形态 | 加 `monkeypatch _BROWSER_FN` 返回 `{"error": "query failed", "message": "database is locked"}` 断言降级行 |
| 4 | `tests/test_scheduler.py` | `_build_job_prompt` 无专项测试（daily 注入/非 daily 隔离/build 异常回退） | 补 3 个用例：daily 命中、非 daily 原样、monkeypatch build_info_pack 抛异常回退原 prompt |
| 5 | `scheduler.py` L263/L326 | `"daily" in job.name.lower()` 魔法字符串出现 2 处 | 抽 `JOB_DAILY_REPORT="daily-report"` 常量或 `is_daily_job(name)` helper |
| 6 | `daily_info_pack.py` `_latest_prev_report` | 取 latest mtime 的 daily-report_*.md，同日多次运行可能取到当天而非严格前日 | 当前措辞"前一次日报"已可自洽；如需严格前日，按文件名时间戳过滤 `<当天` 再取最新 |
| 7 | `daily_info_pack.py` `_build_bili` | `str(viewed_at)[:10]==date` 依赖工具层返回 ISO 格式，unix 时间戳边界无防护 | 若工具层会返回非 ISO，加格式归一（已确认当前工具返回 ISO，仅备忘） |

## 四、合入结论

**本轮可合入。** 无 P0/P1；主选通道正确性（信息包完整送达 LLM）、消费指令与 context 禁令兼容性、逐源降级与双保险、模块内聚与零渗透均经实码核验成立。7 项 P2 中 1-5 建议于下一轮修复后回归（补 4 类测试 + 清理死代码 + 抽常量），6-7 为备忘级记录，遗留风险 3 项均复核为可接受处置。
*（内容由AI生成，仅供参考）*
