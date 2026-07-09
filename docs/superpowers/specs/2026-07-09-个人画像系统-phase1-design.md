# 个人画像系统 · Phase 1 设计文档

> 2026-07-09 | 状态：确认中
> 前置文档：`docs/个人画像-设计文档与路书.md`、`docs/个人画像-实践计划.md`

---

## 一、目标

Phase 1 MVP：跑通"数据读取 → 分析 → 输出 Markdown"全链路。将 trae 和 marvis 聊天记录转化为一份个人画像文档，输出到 Obsidian。

---

## 二、整体架构

三层分离：**IO 层 → 分析层 → 输出层**，通过 CLI 入口组装。

```
trae JSONL ──→ TraeReader ──→ list[ChatRecord] ──┐
                                                   ├──→ 分析层（direction/activity/decision/topic）
marvis SQLite → MarvisReader → list[ChatRecord] ──┘             │
                                                                 ↓
                                                           dict 结果
                                                                 │
                                                                 ↓
                                                      writer.py → Obsidian md
```

### 目录结构

```
checkSelf/
├── docs/                                  # 设计文档
│   ├── 个人画像-设计文档与路书.md
│   ├── 个人画像-实践计划.md
│   └── superpowers/
│       └── specs/
│           └── 2026-07-09-个人画像系统-phase1-design.md
├── pyproject.toml
├── profile/                               # 核心包
│   ├── __init__.py
│   ├── config.py                          # 路径配置，独立于业务逻辑
│   ├── models.py                          # @dataclass 数据结构
│   ├── io/                                # 数据读写层
│   │   ├── __init__.py
│   │   ├── base.py                        # Reader 抽象基类
│   │   ├── trae_reader.py                 # trae JSONL → list[ChatRecord]
│   │   └── marvis_reader.py               # marvis SQLite → list[ChatRecord]
│   ├── analysis/                          # 分析层（纯函数）
│   │   ├── __init__.py
│   │   ├── direction.py                   # 方向漂移检测
│   │   ├── activity.py                    # 活跃时段分析
│   │   ├── decision.py                    # 决策模式分析
│   │   └── topic.py                       # 主题/诉求聚类
│   ├── output/                            # 输出层
│   │   ├── __init__.py
│   │   └── writer.py                      # Markdown 生成 + 写入
│   └── cli/                               # 命令行入口
│       ├── __init__.py
│       ├── analyze_trae.py
│       ├── analyze_marvis.py
│       └── generate.py                    # 合并生成个人画像-v0.1.md
└── tests/
    ├── __init__.py
    ├── test_direction.py
    ├── test_activity.py
    ├── test_decision.py
    └── test_topic.py
```

---

## 三、各层设计

### 3.1 配置层（config.py）

```python
from pathlib import Path

TRAE_MEMORY_DIR = Path("C:/Users/17734/.trae-cn/memory/projects")
MARVIS_DATA_DIR = Path("C:/Users/17734/AppData/Roaming/Tencent/Marvis/User/2E51332FD5D7CCFEA611C89585433078/database")
OBSIDIAN_OUTPUT_DIR = Path("d:/AAAmyPrj/gitee/obsidian/我的文档/AI使用/画像产出")

CLAIMED_DIRECTION = {"主": "Agent", "次": "Memory"}
```

独立于业务逻辑，只放常量。Phase 2 加数据源时只改此文件。

### 3.2 数据模型（models.py）

```python
@dataclass
class ChatRecord:
    time: datetime
    content: str
    actions: list[str]    # 默认空列表
    outcome: str          # 默认空串
    learned: list[str]    # 默认空列表
    source: str           # "trae" | "marvis"
```

### 3.3 IO 层（io/）

**base.py** — Reader 抽象基类：

- `read() -> list[ChatRecord]`：从数据源读取，返回统一结构
- `is_available() -> bool`：检查数据源是否可访问（前置校验）
- `source_name: str`（属性）：数据源标识

**trae_reader.py** — TraeReader：

- 数据路径：`C:\Users\17734\.trae-cn\memory\projects\*\*\session_memory_*.jsonl`
- 字段映射：`message_summary_time → time`, `intent → content`, `actions → actions`, `outcome → outcome`, `learned → learned`
- 时间格式：`%Y-%m-%d %H:%M:%S`
- source 固定 `"trae"`

**marvis_reader.py** — MarvisReader：

- 读取前先复制 `data.db` 到临时目录，避免 WAL 锁
- 用 sqlite3 直连复制后的文件
- 查询 messages 表，每行映射为 ChatRecord
- source 固定 `"marvis"`

### 3.4 分析层（analysis/）

四个纯函数模块，每个接受 `list[ChatRecord]`，返回 `dict` 结果。

| 模块 | 函数 | 输入 | 输出结构 |
|------|------|------|---------|
| `direction` | `analyze(records, claimed)` | ChatRecord 列表 + 声称方向 | `{TOP10主题, 方向占比, 周趋势, 漂移度}` |
| `activity` | `analyze(records)` | ChatRecord 列表（可多源合并） | `{24h分布, 峰值时段, 低谷时段, 日均活跃}` |
| `decision` | `analyze(records)` | ChatRecord 列表（含 actions） | `{调研/动手/讨论占比, 模式判断, 想法到动手间隔}` |
| `topic` | `analyze(records)` | ChatRecord 列表 | `{TOP10诉求, 诉求分类}` |

**direction 子模块设计说明：**

- 方向关键词集：
  - Agent 相关：agent, 智能体, mcp, langgraph, multi-agent, tool, function calling
  - Memory 相关：memory, 记忆, vector, embedding, rag, knowledge graph
- 对 content 字段做词频统计，计算每个方向的提及次数占比
- 按 ISO 周分组，统计每周的方向提及分布（周趋势）
- 漂移度判断逻辑：Agent 占比 > 20% 为 "低漂移"，10-20% 为 "中漂移"，< 10% 为 "高漂移"

**activity 子模块设计说明：**

- 从所有记录提取 time 字段，按小时分组：`Counter(record.time.hour)`
- 峰值时段：频次最高的连续 3 小时区间
- 低谷时段：频次最低的连续 3 小时区间
- 日均活跃次数：总记录数 / 天数

**decision 子模块设计说明：**

- 取所有 records 中有 actions 的记录
- actions 分类关键词：
  - 调研类：搜索, 查找, 调研, 了解, 阅读, 查看
  - 动手类：写, 创建, 实现, 安装, 运行, 修改, 部署
  - 讨论类：讨论, 分析, 确认, 询问
- 统计三类占比
- 模式判断：调研占比 > 动手占比 则为"先调研后动手"，反之为"先动手后查"，相近则为"混合"
- 想法到动手间隔：取 content（intent）中提到的项目，在后续记录中找第一次出现"动手类" action 的时间差

**topic 子模块设计说明：**

- 对 content 字段做词频统计（过滤停用词）
- 主题分类：技术问题 / 工具使用 / 生活诉求 / 其他
- 返回 TOP10 高频词 + 分类占比

### 3.5 输出层（output/writer.py）

- 输入：分析结果 dict + 数据概况
- 输出：Markdown 字符串，写入 Obsidian `画像产出/` 目录
- 文件命名：单个源的报告命名为 `<源名>画像报告.md`，综合画像命名为 `个人画像-v0.1.md`

### 3.6 CLI 层（cli/）

| 入口 | 用法 | 组装 |
|------|------|------|
| `analyze_trae.py` | `python -m profile.cli.analyze_trae` | TraeReader → direction + activity + decision → writer |
| `analyze_marvis.py` | `python -m profile.cli.analyze_marvis` | MarvisReader → topic + activity → writer |
| `generate.py` | `python -m profile.cli.generate` | 两个 Reader → 合并 → 全部分析函数 → writer（v0.1） |

---

## 四、错误处理

| 场景 | 处理 |
|------|------|
| 数据源不可用 | `is_available()` 返回 False，CLI 打印提示并退出 |
| 数据量不足（< 30 条） | 分析函数返回 `{status: "样本不足", "count": N}`，输出层标注警告 |
| SQLite 被锁 | marvis reader 先 copy 再读；copy 失败则提示用户关闭 Marvis |
| JSON 行解析失败 | 跳过该行，stderr 记录行号，不中断读取 |
| 产出目录不存在 | 自动创建目录 |
| 写入权限不足 | 捕获异常，stderr 打印路径和错误 |

---

## 五、测试策略

### 分析层（核心测试重点）

每个分析函数写单元测试，用 mock 的 `list[ChatRecord]` 覆盖：

- **空数据**：返回合理默认值，不抛异常
- **极少量数据（< 10 条）**：返回"样本不足"
- **正常数据（50+ 条）**：验证输出结构完整、数值合理
- **单源数据**：只含 trae 或只含 marvis，验证不依赖多源

### IO 层

不写自动测试（依赖实际数据文件路径）。`is_available()` 方法提供运行时自查。

### CLI 层

手动验证：`python -m profile.cli.generate` 产出 md，在 Obsidian 中查看。

---

## 六、产出文件

| 文件 | 内容 |
|------|------|
| `画像产出/trae画像报告.md` | trae 的方向漂移 + 活跃时段 + 决策模式 |
| `画像产出/marvis画像报告.md` | marvis 的诉求 TOP10 + 活跃时段 |
| `画像产出/个人画像-v0.1.md` | 合并 trae + marvis，含方向真实度、决策模式、活跃时段、日常诉求 4 个维度 |

---

## 七、依赖

- Python 3.9+（标准库：sqlite3, json, glob, collections, dataclasses, pathlib, datetime）
- 零第三方依赖

---

## 八、Phase 2 扩展点

本设计已为 Phase 2 预留：

- `models.py` 中已定义 `ContentRecord` dataclass（Phase 1 不使用，但结构就位）
- `io/` 目录可直接添加 `bilibili_reader.py`、`netease_reader.py`
- `config.py` 中添加新数据源路径即可
- 分析层不受影响，新源的 CLI 入口复用现有分析函数
