---
name: add-info-source
description: mono 日报信息包新增信息源的标准流程（SourceSpec 注册表/builder 三元组/采样军规/封闭测试/R5 同步/真实验证）。MUST USE when 用户要给日报接入新信息源、把某类数据"接进日报/信息包"、问"为什么 X 没进日报"、或要扩 daily_info_pack 的 SOURCES——即使没说"信息源"三个字。
---

# 新增日报信息源（add-info-source）

**核心认知**：统一接入接口已经存在——`apps/withlanggraph/src/gacore/daily_info_pack.py` 的
`SOURCES: tuple[SourceSpec, ...]` 注册表。接入一个新源 = 写一个 builder 函数 + 注册一行 + 测试；
预算装配、状态分类、jsonl 体检、/health 单源详情页、/config 逐源配置（改完即生效）**全部自动生效，零改动**。
经验来源：v3.3 一次接入三个手机事实源（_PHONE_PLACE/_PHONE_USAGE/_PHONE_NOTIF）的完整实践。

## 1. 盘点数据（先看后写）

- 打开 `http://127.0.0.1:8010/data` 数据目录页（或读 `src/gacore/data_catalog.py` 的 `CONSUMERS` 映射），
  确认目标数据的采集现状（哪张表/哪个文件、量级、时间跨度）与现有消费方（蓝=已接、红=未接、黄=system prompt 旁路）。
- **优先复用现成读层**，不要重写解析：langTrack 事实类数据优先 `fact_card.build(day, detail="full", db_path=…, outlet="info_pack")`
  （纯读、任意历史日、地名解析/日界裁剪/降级全复用、措辞与 chat 侧事实卡不失真）；
  聚合直查用 `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`（参照 `_build_media` 先例）。
  禁止在 builder 里触发 ETL 或写文件。

## 2. 拆分决策（几个源？）

- **同一读数单元不拆**：一行聚合（如 daily_stats）读出来的字段合在一个源，拆开=同字段多处渲染，R5 不失真负担翻倍。
- **拆的收益来自独立 cap/priority + 失败隔离**：不同表、不同失败模式、文本长度特征不同才值得拆
  （如轨迹 stays/trips vs 使用统计 daily_stats）。
- **每源有固定成本**：/config 一行、/health 一格、priority 编号、测试类、军规自查。一条 60 字的撑不起一个源。
- 数据量太小的（如 <50 条/总量）先不独立成源，等积累。

## 3. 隐私边界（涉敏数据先拍板再动手）

- 涉及私信/短信/输入法/剪贴板等敏感内容：**先向用户确认边界，再写 builder**。
- 现行统一边界（AGENTS.md 采样军规第 7 条，2026-10-05 用户拍板）：仅进**本地日报落盘 + 发送到本人邮箱**，
  **绝不出网**；不进 git、不提交任何真实个人数据（R11）。
- 拍板结果登记进 `apps/withlanggraph/AGENTS.md` 第 15 节军规，后续源不再重复问。

## 4. 实现 builder（唯一必须写的契约）

```python
def _build_x(date: str, cfg: Config) -> tuple[str, str, str]:
    """一句话说明取什么、pack/detail 各给什么、采样方向。"""
    title = "〔分类·名称〕"
    try:
        # 取数（优先复用读层）→ 渲染
        return title, pack_body, detail_body
    except Exception as exc:  # 最后防线：源失败不中断整包
        logger.warning("daily_info_pack: x failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""
```

契约要点（违反会被测试/体检抓）：
- **pack_body** = 挑选压缩后进包文本（首行状态行、`- ` 列表）；**detail_body** = 当日取数全部结果
  （不挑选不压缩），供 /health 单源页逐层归因。无数据时 detail 为空串。
- **单一截断点**（军规②）：builder 内按 cap 一次性产出；禁止行数截断后再字符截断。
- **append-only 数据取尾**（军规①）：按时间正序追加的日志取 `[-N:]`，倒序 API 取头部——先确认排序方向。
- **空态文案是 classify 契约**（军规⑤）：以 `- ` 开头且缺失关键词（"当日无/无 langTrack …数据"）出现在
  行首 8 字符内 → 自动分类 missing_data。照此格式写，别发明新句式。
- 硬编码常量（cap/条数/截断长度）提为模块级 `_XXX` 常量并注释口径。

## 5. 注册

```python
SourceSpec("_PHONE_PLACE", _PHONE_PLACE_CAP, 15, _build_phone_place),
```

- **SOURCES 元组顺序 = priority 升序**（既有不变量，test_source_registry 会抓）。
- priority 沿 ×10 序列（10..100），新源插空位用 5 步长（15/45/65…），不重排既有编号（历史 jsonl 可比）。
- key 以 `_` 开头大写，jsonl/详情文件/pack_detail 命名都靠它；cap 与 `PACK_BUDGET` 比例要心里有数。

## 6. 测试（封闭，绝不触真实库/网络）

- 合成库落盘文件（builder 按 `cfg.root/data/langTrack.db` 打开），schema 对齐
  `tests/test_langTrack_fact_card.py::_make_db`（已验证与 fact_card 兼容的最小 v2 表结构）；
  事件 ts 用东八区显式时区换算 ms。参照 `tests/test_daily_info_pack.py::_seed_langtrack_db`。
- **外部工具一律 stub**（`monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: …)` 等，
  参照 `_flood_all_sources`）——build_info_pack_report 会跑全部 builder，不 stub 会打真实 B站/Edge/NCM。
- **新增源必须在 `tests/test_source_registry.py::_inject_failure` 登记失败注入**，否则该测试主动 fail 提醒。
  注意：`fact_card.build` 内部吞异常降级不抛——打桩要打 `dip._lang_track_card` 这类读卡入口，
  让 builder 自己的 try/except 产出 `- 该源失败：` sentinel。
- 断言技巧：进包文本带状态头 `〔手机·位置轨迹｜状态:全量〕`，断言用前缀 `"〔手机·位置轨迹" in pack`。
- 覆盖：满（内容/截断/计数）、空（无库/无当日数据→missing_data）、采样规则（尾窗/合并/截断点）、config 停用生效。

## 7. 同步面清单（缺一即失配）

1. **`data_catalog.py` 的 `CONSUMERS`/`CONSUMER_SHORT` 必须同步**——否则 /data 页把已接数据误报"未接"（页面自述的硬约定）。
2. 若新源改变了"哪些数据随 system prompt 注入"的事实：同步 `_instruction_head` 措辞与
   `daily_info_pack.py` 模块 docstring（如 v3.3 把"事实卡已注入此处不重复"改为"fact_card=当前时刻、当日全天以 _PHONE_* 为准"）。
3. **R5 三处**：`docs/langTrack-tech.md` §9.24 追加条目（符号名/字段/口径与代码一字不差）、
   `docs/architecture-flow.mmd` 的 SIP/相关节点、`ROADMAP.md` 执行记录（背景/已完成/实测验证/偏差/待办）。
4. 涉敏边界或新采样教训 → AGENTS.md 第 15 节军规。

## 8. 真实验证与提交

- 回算历史日：`backfill_health.refresh_day(Config.default(), "<date>")`（零 LLM 零邮件，秒级），
  看 `data/logs/info_pack_health.jsonl` 末行每源 status/chars 与 `/health`、`/config` 页面；改代码后重启 8010。
- R4 分提交：代码+测试一笔（`feat(withlanggraph): …`），文档一笔（`docs(withlanggraph): …`）。
- 上线后按军规⑥观测一周：`full_chars` 恒大于 cap 或 status 长期非 ok = 采样设计有问题。

## 已踩过的坑（别再踩）

- **采样假设先用真实数据实证**：v3.3 原方案"只取点击过的通知"落库前实测发现近 10 天 1359 事件中
  含内容且 clicked=True 为 0（点击标志只在无文本移除标记上）——永远空转。任何"我以为数据长这样"的
  采样规则，先对真实库跑一遍统计再写 builder。
- **连发合并**：微信等一次轰炸十几条同会话消息，不按 (app, title) 连续合并会把尾窗整段吃掉。
- bash heredoc 写含中文/特殊字符的文件内容会被弄坏——一律用 Write/Edit 工具。
