# langTrack ETL 数据清洗详解

> 对应代码：`src/gacore/langTrack/etl.py`
> 本文档逐条对应代码实现，讲清楚"每一条脏数据从哪来、为什么被清、清了之后数据变成什么样"。
>
> **数据基准**：本文所有"当前库"数字均为 2026-08-19 全量重跑结果（events 6360 / 脏数据 77 / sessions 413 / daily_stats 4 / places 14）。历史运行数字单独标注「历史值」。

> **位置口径（2026-09-03 更新）**：places 计数**由 stays 主导**，不再以「原始点访问次数」为口径。长期空间画像、停留聚合、直达点判断均以 `stays_v2`/`places_v2` 为准（详见 langTrack-tech.md §4.2/§5.5/§6.1）；v1 `places.visit_count` 仅作兼容保留。
> **数字可信度标注**：`[代码]` 可从源码直接推导；`[实测]` 为真实数据运行结果；`[推断]` 为设计假设。

---

## 1. 清洗规则速查表（先看这个再细读）

| # | 规则 | 代码位置 | 生效范围 | 数字来源 |
|---|---|---|---|---|
| 1 | `ts < 1e12` 读时丢弃 | `clean_ts` / `load_events` | **全量事件** | [代码] |
| 2 | `--purge` 物理删除 ts 脏数据 | `purge_dirty` | events 表 | [实测] 948(8-17)+77(当前) |
| 3 | payload 非 JSON 丢弃 | `load_events` | **全量事件** | [代码]（当前 0 条） |
| 4 | 系统 App 42 个过滤 | `is_noise` | sessions 构建 + daily_stats 通知排行 | [实测] 含漏网 `com.android.launcher` |
| 5 | 自家 App 过滤 | `is_noise` | sessions 构建 | [实测] "用机时长"不进排行 |
| 6 | `fg_ms < 5s` 碎片丢弃 | `build_sessions` | **仅 sessions 构建** | [实测] 阈值演进见 §5 |
| 7 | 同 app 间隔 < 2min 合并 | `build_sessions` | sessions 构建 | [推断] 阈值见 §6 TODO |
| 8 | app_switch / screen_off 断会话 | `build_sessions` | sessions 构建 | [代码] |
| 9 | 位置网格 0.001° 聚类 | `build_places` | places 构建 | [代码] |
| 10 | 标签 upsert 保留 + 持久化恢复 | `run` / `apply_labels` | places 表 | [实测] 家/公司不丢 |

---

## 2. 总体数据流（两条清洗路径）

**关键认知**：清洗分两条路径，不是一条流水线。

```mermaid
flowchart LR
    subgraph 原始层
        E[events 表<br/>6360 条]
    end

    subgraph 全局清洗（所有事件都过）
        A[时间戳 clean_ts<br/>ts > 1e12]
        B[载荷 json.loads<br/>失败丢弃]
    end

    subgraph sessions 专用清洗（仅会话构建）
        C[噪音 is_noise<br/>系统App/自家App]
        D[碎片 fg_ms < 5s]
    end

    subgraph 事实层
        S[sessions<br/>413 条]
        D2[daily_stats<br/>4 天]
        P[places<br/>14 网格点]
    end

    E --> A --> B
    B -->|全量清洗后 6283 条| D2
    B -->|全量清洗后| P
    B -->|进入会话构建| C --> D --> S
```

**为什么拆两条**：`clean_ts` + `json.loads` 是**全量事件**都要过的（daily_stats 和 places 直接消费原始事件流）；`is_noise` + `fg_ms<5s` **只影响 sessions 构建**——因为 daily_stats 的通知统计也要用 `is_noise` 滤掉系统通知，但碎片阈值只对"前台会话"有意义。

---

## 3. 第一道：时间戳清洗（全量）

### 3.1 读时过滤（`clean_ts`）

```python
# etl.py:121-123
def clean_ts(ts: int) -> bool:
    return ts > 1_000_000_000_000   # 阈值 1e12
```

**阈值 `1e12` 与时间戳年份对照：**

```mermaid
timeline
    title 时间戳数量级判断（毫秒）
    1e11 : 无效<br/>1973年
    1e12 : ✅ 有效起点<br/>2001-09-09
    1.7e12 : 2023-11-15
    1.787e12 : 当前 2026-08-19
```

**为什么会有 `ts < 1e12`？** 客户端无障碍服务 `AccessibilityEvent.timeStamp` 偶发为 0（8-17 实测 input 199 + screen_content 736 全中招），落库即 1970 时间戳。

```mermaid
flowchart LR
    A[无障碍 eventTime=0] --> B[input/screen_content<br/>ts=0]
    B -->|上报| C[events 表<br/>1970-01-02]
    C -->|clean_ts| D[❌ 分析跳过]
    C -->|purge_dirty| E[❌ 物理删除]
```

### 3.2 物理删除（`purge_dirty`，`--purge`）

```python
# etl.py:268-284
c1 = conn.execute("DELETE FROM events WHERE ts < 1000000000000")
# payload 非 JSON 的（防御）
...
```

**清理量拆分（避免歧义）**：
- **948 条**：8-17 首次 `--purge` 的历史清理量（当时累计的）
- **77 条**：8-18 后新增残留（客户端持续产生，根因未除，见 §11-P0）

| 方式 | 位置 | 作用 | 触发 |
|---|---|---|---|
| `clean_ts` | 读时过滤 | 分析跳过，**不删原表** | 每次 ETL 自动 |
| `purge_dirty` | 物理删除 | 彻底清除 | `--purge` 手动 |

---

## 4. 第二道：载荷清洗（全量）

```python
# etl.py:133-136
try:
    payload = json.loads(payload_raw)
except json.JSONDecodeError:
    continue
```

**现状**：0 条失败（客户端 `JSONObject.toString()` 保证格式），纯防御代码。

---

## 5. 第三道：噪音过滤（sessions + 通知排行）

```python
# etl.py:147-148
def is_noise(pkg: str) -> bool:
    return pkg in SYSTEM_PACKAGES or pkg in OWN_PACKAGES
```

**黑名单 42 个包**（去重后，`com.heytap.quicksearchbox` 曾重复现已修）：

```python
# etl.py:24-68（节选）
SYSTEM_PACKAGES = {
    "android", "com.android.systemui",
    "com.android.launcher3", "com.android.launcher",  # OPPO 实际桌面（实测漏网）
    "com.oplus.launcher", "com.oppo.launcher",
    ...
    "com.android.permissioncontroller",  # 权限弹窗（实测新增）
    "com.oplus.securitypermission",       # OPPO 安全中心（实测新增）
    ...
}
```

**is_noise 的两处用途（易漏）**：

```mermaid
flowchart TB
    subgraph 用途1: sessions 构建
        A1[usage 事件] --> B1{is_noise?}
        B1 -->|是| C1[❌ 不进会话]
        B1 -->|否| C2[✅ 构建会话]
    end
    subgraph 用途2: daily_stats 通知排行
        A2[notification 事件] --> B2{is_noise?}
        B2 -->|是| C3[❌ 不进通知Top榜]
        B2 -->|否| C4[✅ 计入通知来源]
    end
```

**漏网案例**（`com.android.launcher`）：客户端黑名单只写了 `launcher3`/`oplus.launcher`，漏了 OPPO 实际用的裸 `launcher`——服务端实测抓到并补齐。

---

## 6. 第四道：碎片过滤（仅 sessions）→ 会话拼接

### 6.1 碎片阈值演进（[实测] 历史值，非当前）

```python
# etl.py:174-176
if fg_ms < 5_000:   # 当前阈值 5s
    continue
```

| 阈值 | sessions 数 | 问题 | 阶段 |
|---|---|---|---|
| `<= 0` | 925 | 0-5s 碎片全进，总时长虚高 | 8-17 初版（历史） |
| `< 30s` | 89 | 真实短会话被砍太狠 | 8-17 中间（历史） |
| `< 5s` | 248 | 平衡 | 8-17 最终（历史） |
| `< 5s` | **413** | 当前库（数据累积更多） | 8-19 当前 |

> 阈值 5s 为 [推断] 设计值（非精确推导），调参依据见 §11-TODO。

### 6.2 会话合并规则

```python
# etl.py:151-192
if cur and cur[0] == pkg and abs(ts - cur[4]) < 2 * 60_000:  # 同app + 间隔<2min
    cur = (..., max(cur[4], end_ms), cur[5] + fg_ms)          # 合并累加
elif type_ == "session" and kind in ("app_switch", "screen_off"):
    sessions.append(...); cur = None                          # 边界断开
```

```mermaid
flowchart TB
    subgraph 输入（时间排序）
        U1["usage 微信 08:00-08:05"]
        U2["usage 微信 08:06-08:08<br/>(间隔<2min)"]
        U3["session app_switch 08:08"]
        U4["usage 抖音 08:08-08:15"]
    end
    subgraph 输出
        S1["会话1 微信 08:00-08:08<br/>duration=5+2=7min"]
        S2["会话2 抖音 08:08-08:15"]
    end
    U1 --> 合并; U2 --> 合并
    U3 -->|边界| 断开
    U4 --> S2
    合并 --> S1; 断开 --> S2
```

> 2 分钟合并阈值为 [推断] 设计值，TODO 见 §11。

---

## 7. 按天汇总（daily_stats）

**双数据源**：

```mermaid
flowchart LR
    subgraph 来源1: sessions
        S -->|duration累加| T[total_screen_ms]
        S -->|app分组| R[app_ranking]
    end
    subgraph 来源2: 原始事件流（全量清洗后）
        N[notification] -->|计数| NC[notification_count]
        N -->|clicked| CK[notification_clicked]
        SE[session] -->|kind| ON[unlock/switch/screen_on/off]
        LO[location] -->|计数| LC[location_count]
        AC[audio_clip] -->|计数| CC[audio_clip_count]
    end
```

Top-N 存 JSON 列（前 10 app / 前 5 通知），report/dashboard 直接读。

---

## 8. 常驻点聚类（places）

### 8.1 网格聚类（不用 DBSCAN 的理由）

> **口径演进（2026-09-03）**：本节 `×87次/×46次` 是**网格聚类输入样本数**（原始定位点计数），**不是** places 语义。旧纪要曾把 `places.visit_count` 当作「原始点访问次数」（如「公司 264 次 / 家 56 次」）——**该口径已废弃**。位置智能起（Task 1-6）places 三计数统一**由 stays 主导**：`places_v2.visit_count`=canonical 停留段数（总和=stays 数）、`stay_ms`=累计停留时长、`point_count`=聚入地点的驻留采样点数；v1 `places.visit_count` 仅作兼容保留。

```python
# etl.py:246-265
gk = (round(lat * 1000) / 1000, round(lon * 1000) / 1000)  # 0.001° ≈ 110m
```

单设备一天几十条定位、聚集 2-3 点 → 网格 O(n) 一次遍历即聚类，DBSCAN 需要距离矩阵，效果等价且更简。

```mermaid
flowchart LR
    X1["(31.9750,118.7671) ×87次"] --> G1["grid: 31.975,118.767"] --> P1["公司(新华汇)"]
    X2["(31.9928,118.7829) ×46次"] --> G2["grid: 31.993,118.783"] --> P2["家(雨花街道)"]
```

### 8.2 标签持久化 + is_primary（重要设计）

```python
# etl.py:336-343  top2 主点标记
top2 = conn.execute("SELECT id, grid_key, label FROM places ORDER BY visit_count DESC LIMIT 2").fetchall()
for tid, _, _ in top2:
    conn.execute("UPDATE places SET is_primary=1 WHERE id=?", (tid,))
```

**要点**：`is_primary=1` 是**只置标记、不覆盖 label**——按访问次数（注：此处 visit_count 为 v1 旧口径=原始点访问次数，位置智能起以 stays 主导、该字段仅兼容保留）选出 top2 主点，但标签仍由 `place_labels.json` 持久化决定。**主点标记是 ETL 算的，家/公司标签是用户确认的**，两者独立。

```mermaid
flowchart TB
    U[用户确认一次<br/>label_places.py] -->|写| CFG[place_labels.json]
    E[ETL build_places] -->|upsert 不覆盖| P[places.label]
    E -->|visit_count top2| M[is_primary=1 标记]
    CFG -->|apply_labels 恢复| P
    P --> R["report: 公司264次/家156次（历史口径=原始点访问次数，已废弃；v2 起由 stays 主导）"]
```

---

## 9. 一次 `--purge` 全流程

```mermaid
sequenceDiagram
    participant CLI as python -m etl --purge
    participant DB as events 表
    participant M as 内存
    participant F as 事实表
    CLI->>DB: purge_dirty: DELETE ts<1e12
    DB-->>CLI: 删 77 条（8-18 新增残留）
    CLI->>DB: SELECT 全量事件
    DB-->>CLI: 事件流
    loop 全量清洗
        CLI->>M: clean_ts? json.loads?
    end
    CLI->>M: build_sessions(噪音+碎片+合并) → 413 条
    CLI->>M: build_daily_stats → 4 天
    CLI->>M: build_places → 14 点 + is_primary
    CLI->>F: apply_labels 恢复家/公司
```

---

## 10. 已知限制与 TODO

### P0：脏数据根因（最大的优化点）
- **现象**：客户端 `AccessibilityEvent.timeStamp=0` 持续产生 ts 脏数据（每次 ETL 前 `--purge` 才能清干净）
- **根治**：客户端 `WeiAccessibilityService` 对 `eventTime <= 0` 用 `System.currentTimeMillis()` 兜底（路书 R7）
- **根治后**：`--purge` 退化为纯防御，无需每次跑

### P1：app_switch 切换数虚高
- 603 次/天偏高，OPPO 系统组件切换频繁，噪音过滤后仍偏高
- [推断] 会话拼接按 usage 事件粒度，可能需合并更细粒度

### P2：places 标签自动推断弱
- 关键词规则（"大厦/小区"）对"新华汇 B1 座"误判，依赖用户一次确认 + 持久化

### P3：ETL 全量重建
- sessions/daily_stats 每次 DELETE 重建，>100 天数据后需改增量

### TODO（调参实验）
- [ ] 碎片阈值 5s：调参依据来自实测分布，留待更大样本验证
- [ ] 会话合并 2min 阈值：同上

---

## 11. 修订记录

| 日期 | 内容 |
|---|---|
| 2026-08-19 | 按 review 修订：统一数字基准(6360/77/413/4/14)、修时间戳年份(1.7e12=2023)、拆全局/会话清洗路径、补 is_primary/通知过滤/purge 拆分、删重复包名(43→42)、速查表提前+来源标注、阈值标注 TODO |
| 2026-09-03 | 删除「places=原始点访问次数」旧口径（§8.1/§8.2 标注废弃），写明 places 计数由 stays 主导（visit_count=停留段数、stay_ms=累计时长），头部补位置口径声明 |
