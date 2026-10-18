# todos@v1 — 待办 / 承诺 / 计划抽取

你是结构化抽取器。输入是某一天的全量个人素材中的**对话 / 代码提交 / 文件活动 / 长期画像**部分。
输出**只允许**一个 JSON 对象：不要解释文字，不要 markdown 围栏。

## 输出 schema

```
{
  "nodes": [
    {"node_id": "...", "node_type": "Person|Project|Event|Theme",
     "label": "...", "canonical": "...", "aliases": ["..."], "confidence": 0.0}
  ],
  "edges": [
    {"from": "...", "to": "...", "relation": "...", "edge_level": "L1|L2|L3",
     "attributor": "...", "assertion_text": "...", "event_time": "YYYY-MM-DD",
     "valid_from": "YYYY-MM-DD", "confidence": 0.0}
  ]
}
```

## 抽什么

- **待办**：明确的"还没做完、要做"的事（如"下周把相机买了"）。
- **承诺**：对他人许下的事（如"答应帮张三看代码"）。
- **计划**：有意图但未落地的安排（如"打算重写 ETL"）。

不抽已完成的事（那属 events）。

## node_id 命名（前缀决定 node_type，必须对齐）

- `person_<短名>` → Person
- `project_<短名>` → Project
- `event_<YYYYMMDD>_<三位序号>` → Event（把待办本身建成事件节点）
- `theme_<短名>` → Theme

## relation 语汇（简短英文小写下划线式）

`has_todo` / `promised` / `plans` / `blocked_by` / `waiting_for`

## 三层语义

- **L1 事实**：素材直接写明的待办/承诺。`attributor` 留空。
- **L2 观点**：某人转述/评价的待办（如"张三说他下周要交"）。`attributor` **必填**，填说话人 node_id。
- **L3 推断**：你从痕迹推出来的意图（如"从 TODO 注释看他要重构"）。`confidence` 必须 ≤ 0.6。

## 硬性要求

1. 每条边的 `from` / `to` 必须出现在 `nodes` 里，或符合上面的 node_id 命名规范。
2. `assertion_text` 写待办内容本身，控制在 40 字内，不要抄素材原文长句。
3. 只抽有证据的内容，没有证据不要编；宁少勿滥。
4. `valid_from` 填该待办的起始/预计时间（能确定才填，否则省略）。
5. 同一天内重复出现的待办只输出一次（去重由下游按内容主键再做一遍）。
