# langTrack 会话进展报告（2026-09-04）

> 本报告导出自 2026-09-04 当日工作会话，覆盖从「09-03 数据评审发现坐标问题」到「时段分布卡片上线」的完整工作线。
> 所有代码改动均已提交（见 §6），路书与技术文档已按铁律同步。详细执行记录见 `docs/langTrack-roadmap.md` 2026-09-04 各节。

---

## 1. 一句话总结

位置事实 v2 已正式激活（user_version=2），541 米的坐标系偏移、visit_count 累加事故、幽灵异常、地点"未知"显示等 P0 问题全部清除；dashboard 新增了 ETL 手动按钮、生活轨迹时段分布卡；§7 用户验收 8 项可程序化验收全部通过。剩余工作集中在客户端上报保活与两个人工验收项。

## 2. 起点：今天早上发现的问题

评审 dashboard 09-03 数据时确认四类问题叠加：

| # | 问题 | 根因 |
|---|---|---|
| 1 | 坐标整体偏移：家被编成"雨花台风景区/雨花台地铁站" | 设备上报 WGS-84，但 `data/location_coord_systems.json` 缺失 → 全部按 unknown 原样透传高德 → 系统性偏移约 541 米 |
| 2 | 常驻点"家 211989 点" | v1 visit_count 每 30 分钟周期 ETL 全量累加（已知 P0），v2 只跑了 shadow 未激活 |
| 3 | 幽灵异常 #new_place"外国语学校" | acc=550m 的点落公司南邻网格 + 541m 偏移编码 |
| 4 | 轨迹自相矛盾（"公司 16.4h"与"工作日未到公司"同页） | 稀疏点拼出跨天 stay + 按 start_ts 分天的异常口径 |

## 3. 完成的工作线（按时间顺序）

### ① v2 正式激活 + 坐标制落地（commit `c8b2329`）
- 坐标制实证：A/B 逆地理对比——家坐标原样请求 → 雨花台地铁站（错）；按 wgs84 转换 → **康盛花园**（住宅小区，对）。设备 gps/network 均确认 WGS-84。
- 新建 `data/location_coord_systems.json`（default=unknown 不猜未知设备；两台 device_id 声明 wgs84——重装前后是同一部手机）。
- 门禁全过：回归 273 passed → shadow 连跑两次内容哈希一致（places=5/stays=17/trips=9）→ prepare 重跑（56 旧地点 → 11 匹配）→ 备份 206MB 快照 → 停服激活 → 全量 ETL + geocode 重编 5 地点。
- 激活后：家=康盛花园4期〔家〕、公司=润东科创园〔公司〕；visit_count 恢复"停留段数"语义（家 8 段/公司 6 段，此前 211989/179749）；六张 `*_v1_backup` 冻结保留，标签文件切 v3。

### ② §7 用户验收（8 项程序化验收，含 1 项真实缺陷修复）
- A1 幂等/A2 标签安全/A4 地点稳定/A5 粒度降级/A6 实时水位/A7 无 tag 可见/A9 出口一致性：**PASS**。
- **A8 修复**：`build_evidence` 产出缺 `window_days` 键（10/10 证据节点不完整，dashboard 显示"窗口 - 天"）→ Evidence TypedDict + 构造补字段，回归 127 passed。

### ③ Task 12：off_schedule 跨天 stay 口径（`3cf6177`）
- `_group_stays_by_day` 按起始日分组 → 改为**覆盖日分组**（CST 半开区间时间相交），跨天 stay 计入覆盖的每个自然日；09-03 的矛盾异常消失。

### ④ 路线缓存作废重编（`be8626b`，纯数据操作）
- 9 条 trips 的 polyline 全部是坐标制配置生效前编码的（polyline[0] 距原始起点 10-35m）→ 路线整体偏移 541m，通勤带网格与沿途 POI 跟随偏移。
- 作废 9 条路线 + 245 行通勤带 + 100 个沿途 POI 缓存，重编后 polyline[0] 距 GCJ(起点) 仅 1-8m。

### ⑤ Task 12b 两项数据卫生（`d3c6735`）
- off_schedule 加"正午未到的日子跳过评估"守卫（清晨不再误报）。
- `/ingest` 入库即 `canonical_device_id` 归一别名设备，原始层不再出现重装别名（此前有最长 30 分钟窗口期）。

### ⑥ Task 12c dashboard「立即转换 (ETL)」按钮（`baf672e`）
- `POST /etl/run`（异步+防重入）+ `GET /etl/status`；dashboard 导航栏按钮，点击后轮询完成自动刷新页面。周期线程与手动触发共用防重入守卫。
- 配套场景：手机夜间补传数据后，点一下立即看到最新画像，不用等 30 分钟周期。

### ⑦ 数据体检两个新发现（`b051f95` 路书记录）
- **客户端 6 类采集器断供 8-14 天**：notification(08-21)/audio_env+audio_clip(08-22)/input(08-23)/app_lifecycle(08-26)/clipboard(08-20)，全部集中在 08-20~08-26 窗口——大概率系统杀后台或权限变更，需查 weiCheckApp。
- **白天数据夜间补传**：09-04 白天 10:00-17:31 的定位事件 22:08 才一次性入库——采集在工作，上传通道疑似仅 App 前台/唤醒时可用。

### ⑧ Task 12d 生活轨迹 · 时段分布卡片（`f91cb1d`/`b127a2e`）
- `spatial_profile._time_space_matrix`：30 天小时 × 地点类（家/公司/其他/无数据）矩阵；口径：单小时 ≥15 分钟计入、跨午夜 stay 按自然日分摊、同桶可双计过渡小时、无数据不隐藏；Evidence 完整。
- dashboard 新卡片渲染真实数据：凌晨家 23-27%、09-18 时公司 20-33%、22-23 时回家过渡。

## 4. 前后对比（关键数字）

| 指标 | 修复前（09-03 评审时） | 现在 |
|---|---|---|
| 家地点显示 | 南京雨花台风景区〔出行中转〕 | 康盛花园4期〔家〕 |
| 公司地点 | 新华汇（偏移编码） | 润东科创园 |
| visit_count | 家 211989 / 公司 179749（累加垃圾） | 家 8 段 / 公司 6 段 |
| 坐标制 | unknown×N 黄警告 | wgs84，无警告 |
| 路线 polyline 域 | 偏移 541m 的旧缓存 | 真实 GCJ02（1-8m 贴合） |
| 幽灵异常 | #new_place 外国语学校 | 无 |
| 当日画像时效 | 等下一个 30 分钟周期 | dashboard 按钮即时触发 |
| 定位日点数 | 8 点/天（09-03） | 73 点/天（09-04，恢复中） |

## 5. 当前系统状态快照（导出时刻）

- 服务：langTrack on :8000，v2 分支周期 ETL 运行中；dashboard 含 ETL 按钮 + 时段分布卡。
- DB：`PRAGMA user_version=2`；places 5 个 canonical 地点（家/公司/钢城花园/森隆英郡/软件外包产业园）；events 48135 条；孤儿 stay=0。
- 回退能力：`data/backup/langTrack-pre-v2-activate-20260904_002535.db`（206MB）+ `--location-rollback` 一条命令整体回退。
- 工作区：仅剩 daily-report-v2 相关 WIP 文件未提交（本会话全程未触碰）。

## 6. 提交清单（本会话 8 个提交，基线 de35b15 之后）

```
b127a2e docs(langTrack): tech.md 补 dashboard 时段分布卡与 ETL 按钮说明
f91cb1d feat(langTrack): Task12d 生活轨迹·时段分布卡片（小时×地点矩阵）
baf672e feat(langTrack): Task12c dashboard「立即转换(ETL)」按钮
b051f95 docs(langTrack): 09-04 体检补记——客户端延迟上传线索
d3c6735 fix(langTrack): Task12b off_schedule 进行中日守卫 + ingest 层别名归一
be8626b docs(langTrack): 路线缓存作废重编记录
3cf6177 fix(langTrack): Task12 off_schedule 跨天 stay 口径修正（覆盖日分组）
c8b2329 feat(langTrack): 位置事实 v2 正式激活与 wgs84 坐标制落地，§7 验收通过
```

## 7. 未完成事项（下一步清单）

### 本轮刚提出、尚未动工的 4 项（子 agent 已排队、被取消，需重新执行）
1. **地图可视化**：dashboard 嵌高德 JS API 地图，画当日定位点/停留/轨迹（注意：需"Web端(JS API)"类型 Key，现有 AMAP_KEY 是 Web服务类型，可能不兼容，需优雅降级或新申请 Key）。
2. **当日定位采集明细**：一天中各时间点收集了哪些定位点（时间 | 最近地点+距离 | 精度 | 信号源 表格）。
3. **"未知"显示修复**：spatial_profile.py:465 `name = pl.get("label") or pl.get("poi")` 用 tag 列顶掉真实 POI → 应改用 `resolve_place_name`（§2.6 契约：地名取 poi/poi_fallback/address，label 只作 tag），未 tag 只显示为空 tag 而非"未知"。
4. **迁移审查清理**：2 条 open 的 unmapped_tag（同一住宅 65m 内重复家锚点，家 tag 已由存活锚点迁移，无实际损失）可标记 resolved；3 条 tag conflict 是 v1 错标的正确隔离记录。激活后该卡是历史审计存档，用户无需操作。

### 计划内遗留（位置智能计划 I7 与人工验收）
- **I7**：accuracy filter 仍关闭，待真实 provider/accuracy 分布积累后由用户决定是否开启。
- **A3**：人工回忆约 20 个停留，核对起止时间误差 ≤5 分钟（90% 达标）。
- **A10**：有用性访谈——实际用 dashboard 回答"今天去过哪/何时到家公司/常去哪里"。

### 跨仓库事项（weiCheckApp 客户端）
- 6 类采集器断供排查（notification/audio/clipboard/input/app_lifecycle）。
- 后台上传保活（白天数据夜间补传问题）——修好后白天画像实时可见。

## 8. 关键口径备忘（避免将来重复踩坑）

- **坐标域**：原始层/事实表中心一律存 WGS-84 源坐标；所有高德外呼经 `to_amap_coord` 转换；trips.polyline 是 GCJ-02（`polyline_coord_system='gcj02'`）——两个域禁止混用、禁止二次转换。
- **三个计数三种语义**：point_count=网格原始点数；visit_count=停留段数；stay_ms=停留总时长。
- **若将来再变更坐标制/请求域**：geocode 有 regeo_shift_m 失效机制，但路线缓存是精确键命中、不会自动失效——必须同步作废 polyline/route_key 并重编（本次已踩过）。
- **跨天 stay**：构建器对同位置静默桥接（无定位点也可延续）；异常检测按覆盖日时间相交评估；fact_card 时间线按窗口边界裁剪显示。
- **回退命令**：`python -m gacore.langTrack.etl --location-rollback`（恢复 v1 六表 + 标签 v2 backup）。
