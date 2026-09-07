---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 74fca65fe87f9b5d6900c56ec5512fbd_aa3a6801a6fb11f1b87f525400461939
    ReservedCode1: BxJYTrinh9CAY40tjVyrBebyJLPZlNDoGQTzacWFg/VZiizZgTNHN40QR52Y6+OG+buo4I2AI0SawsIOXxMRed+X01jC6IAOPek2F1lvyYkFmMZSl/oGmWEMCcL4gzmbpC3+vNkIGGsuEXICthv3Qg5tkFr+/vX/HhzL7ZFkc4BJcBBz2zKD3GprVw0=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 74fca65fe87f9b5d6900c56ec5512fbd_aa3a6801a6fb11f1b87f525400461939
    ReservedCode2: BxJYTrinh9CAY40tjVyrBebyJLPZlNDoGQTzacWFg/VZiizZgTNHN40QR52Y6+OG+buo4I2AI0SawsIOXxMRed+X01jC6IAOPek2F1lvyYkFmMZSl/oGmWEMCcL4gzmbpC3+vNkIGGsuEXICthv3Qg5tkFr+/vX/HhzL7ZFkc4BJcBBz2zKD3GprVw0=
---

# 维护日志：日报重构 v2 ——「当日信息包」实现

> 记录人：file-agent / 日期：2026-09-02
> 关联设计文档：`output/daily-report-redesign-v2.md`（会话产物，定稿）
> 关联代码库：`D:\AAAmyPrj\github\myrepos\WithLangGraph`

## 1. 模块职责

新增 `src/gacore/daily_info_pack.py`，对外只暴露 `build_info_pack(date, cfg=None) -> str`：

- 在 scheduler 侧**确定性地预取**当日各信息源，裁剪后拼成一段 ≤2000 字的「当日信息包」，
  随 daily-report job 的 user prompt 前置注入，让 LLM 只需做动态检索（search_daily）与写档。
- 只做编排，不重复实现取数：长期画像复用 `scheduler._long_term_insight` / `_summarize_long_term`；
  其余源复用 `tools/*.func`（bili_history / browser_history / langTrack_stats / ncm_me / ncm_playlist_list）。
- **逐源 try/except 兜底**：单源失败只在本板块标注「该源失败/无今日数据」，绝不中断整包、
  不导致 run_job 失败（有「全源失败也返回最小可用包」的最后防线）。
- 输出自带消费指令模板：①时间戳语义（当日数据时间可引用）；②素材须引用不入文、不整段复制；
  ③整包 ≤2000 字硬控（单源硬上限 + 整体熔断）。

## 2. 改动点

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `src/gacore/daily_info_pack.py` | 新增 | 信息包构建模块（预算/各源 builder/装配，模块内聚） |
| `src/gacore/scheduler.py` | 修改 | 新增 `_build_job_prompt(job, cfg)`：daily 类 job 将 `build_info_pack(today)` 拼为 user prompt 前置段；`run_job` 改用它传给 graph_runner。未改 GAState / context.py（主选通道） |
| `config/schedule.json` | 修改 | daily-report prompt 精简：删除 bili/browser/git/文件扫描取数步骤，改为「读〔当日信息包〕→ search_daily/read_daily 补动态 → 画像五线写人 → edit_daily → 结构化 Markdown 分节输出」；保留原三层目标与写人不写账基调 |
| `config/schedule.json.bak` | 备份 | 改动前快照 |
| `tests/test_daily_info_pack.py` | 新增 | 各源空/满双向单测 + 预算 + 永不抛（见 §4） |

不触碰：采集链路、langTrack ETL、事实卡底层算法、长期记忆蒸馏流程、其他 job。

## 3. 如何跑单测

```powershell
cd D:\AAAmyPrj\github\myrepos\WithLangGraph
.\.venv\Scripts\python.exe -m pytest tests/test_daily_info_pack.py -q
```

回归（scheduler 未破坏）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_daily_info_pack.py -q
```

当前结果：`tests/test_daily_info_pack.py` 22 passed in 0.12s。

## 4. 如何手动触发一次日报回归

```powershell
cd D:\AAAmyPrj\github\myrepos\WithLangGraph
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from gacore.config import load_dotenv,Config; from gacore.daily_info_pack import build_info_pack; load_dotenv(); print(build_info_pack('2026-09-02', Config.default()))"
```

或运行调度器真实触发（需 .env 已配好 SMTP / 登录态）：

```powershell
.\.venv\Scripts\python.exe -m gacore.scheduler
```

## 5. 自测观察（真实数据，2026-09-02）

- **整包 2000 字符，恰好控制在预算内**，8 个板块全部装配成功。
- `bili_history` 成功（账号「祁伟的小日常」，total=50，取数耗时约 8s，可接受）。
- **Edge 降级命中真坑**：`browser_history` 报 `OperationalError: database is locked`，其内置
  「锁时自动复制 temp」逻辑本次未兜住；被 `_build_edge` 的 try/except 降级为「该源失败：
  database is locked」，整包未中断。——已记录为遗留风险。
- `langTrack_stats` 当日无数据 → 降级标注「该日无 langTrack 手机数据」，正常。
- git 当日提交、文件活动（tests/src/data/memory/config 等真实目录）均正确取出。
- 因预算 2000 熔断，排在最后的 **ncm 歌单基线与前日日报摘要未进入本日包**（属设计行为）。

## 6. 遗留风险 / 优化点

1. **Edge 数据库锁**：`browser_history` 的「自动复制 temp」未覆盖本次 `database is locked`
   （查询语句已执行却抛 OperationalError）。当前靠信息包降级兜底，日报不中断；若要真正补上
   Edge 当日浏览信号，需在 `browser_history` 层加强复制兜底（不在本模块职责内）。
2. **2000 字预算偏紧**：真实数据下 B站/画像/git/文件活动即接近上限，ncm 与前日日报摘要经常被
   熔断挤出。若需保留这两类信号，可考虑：(a) 调小画像 compact 行数 / B站条数；(b) 调整板块
   顺序让「前日日报摘要」考前；(c) 在预算不变前提下进一步裁剪单源。当前保持 v2 既定顺序。
3. **取数耗时**：bili limit=50 逐条查询约 8s，叠加在 scheduler 预取内，单次触发总耗时可接受；
   若触发时段网络差需设置更长超时（当前 subprocess/CLI 无显式超时，bili 为本机 CLI）。
4. **文件活动含今日刚写入的产物**（如本模块与测试文件本身），属正常真实信号，未做排除。

## 7. 代码审核整改记录（2026-09-03）

> 依据 `docs/daily-report-v2-review.md` 的 P2 清单，修复可执行项并全量自测。git diff 留痕（工作树）。

### 7.1 修复的可执行项（P2-1 ~ P2-5）

| 项 | 修复内容 |
| --- | --- |
| P2-1 | `daily_info_pack.build_info_pack` 移除 `("_HEAD_", lambda...)` 死代码与 `if key=="_HEAD_"` 分支：头部改为单独装配 `_cap_text(_instruction_head(date), _HEAD_CAP)`，`_HEAD_CAP=220` 现真实作用于头部（手动触发可见头部第三行被截断为"降…"） |
| P2-2 | `within_budget` 用例补齐切面断言：预算耗尽后整包以省略号结尾（clipped 行）、尾部板块（〔记忆·前日日报〕）缺失；画像放宽到 80 行确保熔断路径真正走到 |
| P2-3 | 补 2 例 Edge `database is locked` 故障用例：①工具返回 `{"error":"db_open_failed","message":"database is locked"}` → 板块降级标注；②整包级 mock 抛 `sqlite3.OperationalError` → 整包不中断、Edge 板块标注"该源失败" |
| P2-4 | `test_scheduler` 新增 `TestBuildJobPrompt` 3 例：daily job 注入信息包 / 非 daily job 不注入（且 build_info_pack 不被调用）/ 信息包构建异常回退原 prompt |
| P2-5 | `scheduler.py` 新增常量 `_DAILY_JOB_MARKER: Final = "daily"` 与 helper `_is_daily_job(job)`，替换 `_build_job_prompt` 与 `run_job` 中 2 处 `"daily" in job.name.lower()` 魔法字符串 |

### 7.2 仅备忘、不改码（P2-6 / P2-7）

- **P2-6**：`_latest_prev_report` 按 `st_mtime` 取"最新"可能同日取到当天早先跑过的报告；当前 daily-report 每日仅触发一次，实际影响低，保持现行为，备忘不改码。
- **P2-7**：`_build_bili` 按 `viewed_at[:10] == date` 过滤当日记录，格式依赖工具返回；若 fields 变化导致 `viewed_at` 缺失会整日为空，降级为"今日无 B 站观看记录"，备忘不改码。

### 7.3 整改后全量自测

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_daily_info_pack.py -q      # 24 passed in 16.71s
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q           # 44 passed in 48.37s
```

手动触发真实整包（2026-09-03，`Config.default()`）：

- 整包 **1982 字符 ≤ 2000**，8 个板块全部装配成功。
- 长期画像 / 文件活动 / ncm 基线 / 前日日报摘要：正常。
- langTrack 当日无数据、B站当日无观看记录：正常降级标注。
- **Edge `database is locked` 真实复现降级**：该板块标注"该源失败：database is locked"，整包未中断。
- 头部经 `_HEAD_CAP` 截断（"降…"），裁剪路径验证到位。
*（内容由AI生成，仅供参考）*
