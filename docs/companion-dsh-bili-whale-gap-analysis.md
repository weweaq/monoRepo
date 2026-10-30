# Companion × dsh-bili-whale：完整能力对照与补漏清单

> 状态：Reviewed Gap Analysis v1.1（基于 monoRepo HEAD 84f3755）  
> 对照对象：`dsh-bili-whale 0.4.6`、monoRepo V1 真实实现、Companion V2 计划  
> 目的：一次性建立完整能力地图，避免“问到什么再补什么”  
> 说明：Bili Whale 的能力不全部适合 mono；本文区分“应借鉴、已有替代、推迟、不做”
> 实施计划：[`V1`](./companion-self-memory-v1-plan.md) · [`V2`](./companion-self-memory-v2-plan.md)

---

## 1. 结论摘要

mono 当前更像：

> 深度理解用户、会分析和主动提问的个人助手。

dsh-bili-whale 更像：

> 有固定身份、作息、活动、日记、反馈和克制机制的窄领域角色。

V1 源码已经实现：

- 人格贯穿；
- 真实消息事件；
- 自我反思；
- 1–3 条自然短消息。

V2 计划覆盖：

- 两本日记；
- 关系、临时 Mood；
- 承诺与单目标；
- 角色活动、自有偏好和反馈归因；
- 受控主动消息。

仍需补的核心：

1. 角色自己的日相位、可用性和活动状态；
2. 活动持续、暂停、恢复和完成；
3. 角色自己的可逆偏好、新鲜度与厌倦；
4. 主动触达与用户反馈的稳定关联；
5. 私人日记到反思的受控影响；
6. 沉默原因的结构化区分；
7. Companion 模式隐藏内部工具轨迹；
8. 最小诊断/查看入口；
9. 权限与 recipient pinning；
10. 更广的不可信上下文边界。

---

### 1.1 最新代码已解决与仍未解决

HEAD `84f3755` 已真实落地：

- ActiveCardRepository，QQ/proactive 共享角色卡；
- proactive 当前角色人格与单次 `qq_push` 工具；
- SQLite v1 四表、WAL、事务、幂等；
- owner C2C 收件事件；
- 普通/proactive 确认发送事件与 partial 前缀；
- source-bound reflection、固定失败批次、后台 coordinator；
- actor/principal 隔离 Self-memory；
- 1–3 条语义短消息、技术内容 atomic、段间取消和锁。

进入 V2 前仍需稳定：

- 非 owner/群聊仍可能污染旧 global memory/daily/QA；
- proactive 缺发送前 recipient pinning；
- 群聊 `/角色` 仍可能改 owner C2C card；
- Companion 仍外显“思考中”和工具轨迹；
- proactive 动态内容仍有裸文本与 JSON 双重注入；
- reflection coordinator 有运行中新增事件的 lost-wakeup 风险；
- sent summary 需统一脱敏，缺平台 ID 时幂等键需带 logical turn；
- 尚未完成真实 QQ 20 轮灰度。

本文后续矩阵中的“V1 补”若已在上面列为实现，以本节最新状态为准；列为“仍需稳定”的项目继续作为 V2 启动门槛。

---

## 2. 版本归属总览

| 版本 | 目标 |
|---|---|
| V1 | 身份一致、真实收发事件、有证据反思、自我记忆注入、自然消息节奏 |
| V2 | 两本日记、关系/Mood、承诺、单目标、角色活动、自有偏好、反馈归因、允许沉默 |
| V3 | 通用 Outbox、多用户/多渠道、长期活动、可选身体层、管理控制台、DSH 评估 |

明确不做：

- 自动重写核心人格；
- 单一好感度或自动恋爱升级；
- 高风险自主写入、支付和账号操作；
- 把 Mood 宣称为真实情感；
- 在认知连续性未稳定前做桌宠。

---

## 3. 完整能力矩阵

## 3.1 身份、人设与会话连续性

| 项目 | dsh-bili-whale | mono 当前 | V1/V2 | 结论 |
|---|---|---|---|---|
| 稳定身份 | persona 字段 + persona.md | 角色卡只在部分入口贯穿 | V1 统一 active card | V1 |
| 每次投递人设 | session delivery 统一前置 | QQ 有，proactive 无 | V1 修复 | V1 |
| 会话连续 | 固定专用 session，可轮换 | 每用户 thread/checkpoint | 已有更强基础 | 已有 |
| 重启后恢复 | 首次重新注入 persona + 两本日记 | rollover/daily/checkpoint | V1/V2 组合 | V2 验收 |
| 角色改名/稳定 actor | 无通用角色 registry | card_id 即身份 | V1 简化；稳定 alias 推迟 | V3 |
| 群聊隔离 | 不适用 | user_id/chat_id 有混用 | Companion 群聊关闭仍需防串线 | **V1 补** |

V1 补漏：

- 群聊命令不得修改 owner C2C active card；
- 非 owner 不得进入全局 USER_FACT/memory maintenance；
- proactive recipient 固定为配置 owner。

## 3.2 作息、时间与可用性

| 项目 | dsh-bili-whale | mono 当前 | 计划 | 结论 |
|---|---|---|---|---|
| 作息窗口 | 多窗口、跨午夜、随机时刻 | 固定 scheduler + quiet hours | V2 主动循环 | 补 |
| 随机 skip | 30%，决定持久化 | gate 可不发，但没有角色选择语义 | V2 STAY_SILENT | 部分 |
| 忙碌判断 | session mtime + 8 分钟冷却 | hot chat / running task | 已有基础 | 复用 |
| 日相位 | 醒着窗口/全天监听 | 只有系统时间/quiet hours | 未明确 | **V2 补** |
| 可用性 | busy/single-flight | 任务和 guard 分散 | 未统一 | **V2 补** |
| “精力” | Prompt 表达，无正式模型 | 无 | 不伪装生理；用 activity load | V2 简化 |

建议：

```text
day_phase = quiet | morning | daytime | evening
availability = free | chatting | working | cooldown | resting
```

这两个状态由时间和真实任务确定，不让模型编“困了/饿了”。

## 3.3 角色自己的活动

| 项目 | dsh-bili-whale | mono 当前 | 计划 | 结论 |
|---|---|---|---|---|
| 自己去做事 | 刷 B站、深读、收藏 | 主动系统主要围绕用户提问 | V2 单 goal | 仍缺活动语义 |
| 活动持续 | 一轮 workflow | headless 单次生成 | V2 未完整定义 | **V2 补** |
| 暂停/恢复 | session/pending/忙碌保护 | checkpoint 能恢复对话，不能恢复角色活动 | 无 | **V2 补** |
| 同时一件事 | single-flight | 部分任务有 guard | V2 单 active goal | 已计划 |
| 活动完成回执 | 工具 exit/账本 | V2 只读 action event | 已计划 | V2 |

新增最小 `activity_state`：

```text
kind
status = planned | active | paused | completed | failed | cancelled
started_at
resume_after
source_goal_id
last_result_event_id
```

V2 只允许短时只读活动；长期兴趣项目放 V3。

## 3.4 感知与内容理解

| 项目 | dsh-bili-whale | mono 当前 | 结论 |
|---|---|---|---|
| B站 | feed/字幕/弹幕/热评/抽帧 | B站历史 + Web/OCR，可扩展 | mono 数据面更广 |
| 浏览器 | 无通用浏览历史 | Edge 历史/网页工具 | mono 更强 |
| 音乐 | 无 | 网易云工具 | mono 更强 |
| 手机生活 | 无 | langTrack | mono 更强 |
| Git/文件活动 | 无 | 日报信息包 | mono 更强 |
| 来源可信度 | 各工具自行处理 | 各来源分散 | **V2 补 provenance** |
| 角色主动观察 | 有明确 workflow | 多数数据用于分析用户 | **V2 补活动归属** |

补漏重点不是增加数据源，而是区分：

```text
系统观察到用户数据
角色主动读取某内容
角色实际完成某活动
```

只有后两者可进入角色自我经历。

## 3.5 用户记忆

| dsh-bili-whale | mono 当前 | 结论 |
|---|---|---|
| 简单 feedback/favorites/日记 | daily、长期画像、KG、RAG、事实卡 | mono 明显更强 |

真实缺口：

- 当前用户记忆是全局单用户模型；
- Companion 开启时必须 owner gate；
- 非 owner 消息不得污染 global memory、daily、QA 和向量画像。

归属：**V1 补漏**。

## 3.6 自我记忆与反思

| 项目 | dsh-bili-whale | V1/V2 | 缺口 |
|---|---|---|---|
| 自己说过什么 | 靠 session/日记 | V1 confirmed sent event | V1 |
| 自己做过什么 | 工具 workflow | V2 read action event | V2 |
| 反思 | 日记 Prompt，缺结构证据 | V1 source-bound reflection | V1 更可靠 |
| 过期/矛盾 | 无正式模型 | 未进入 V1/V2 canonical | **V2 补** |
| 相关召回 | session/文本 | V1 最近 3 条 | **V2 补 topic-aware** |
| 失败经历 | 日志/日记软约束 | V2 action failed | V2 |

V2 增加：

```text
reflection state = active | stale | contradicted | retracted
topic tags
expires_at
supersedes/refutes source
```

## 3.7 两本日记

| 项目 | dsh-bili-whale | V2 | 补漏 |
|---|---|---|---|
| 给主人的信 | 模型自由写 Markdown | `to_owner` + source | V2 |
| 私人日记 | 模型自由写，含偏好校准 | `private` + source | V2 |
| 自动发送 | 可推摘要 | 默认不发送 | 更克制 |
| 日记影响未来 | 靠下次读文件 | 当前 private 不进 Prompt | **V2 补** |
| 用户纠错 | 无专门结构 | 未设计 | **V2 补最小入口** |

正确影响路径：

```text
private journal
→ 只提取有 source 的 reflection proposal
→ 通过 Reflection 校验
→ Self memory
```

禁止：

```text
private journal 原文
→ 直接注入 system prompt
```

需要：

- 按日期查看/导出；
- 标记 stale/重新生成；
- 用户纠错时引用原 journal/source；
- 不自动修改 persona。

## 3.8 角色自己的偏好

dsh-bili-whale 有：

- config.like/dislike；
- 收藏理由；
- 主人点赞/回复反哺 feed；
- 相似主题加权；
- 沉默降低分享。

mono 当前主要学习“用户喜欢什么”，没有“角色自己逐渐喜欢什么”。

V2 增加可逆投影：

```text
self_preference:
  topic
  weight
  novelty
  fatigue
  source_event_ids
  expires_at
```

规则：

- 不修改角色卡；
- 一次事件只小幅调整；
- 主人偏好与角色偏好分表；
- 长期未发生自动衰减；
- 可被新证据反转。

## 3.9 反馈闭环

dsh-bili-whale 已有：

- 评论回复；
- 点赞；
- 私信；
- 手工反馈；
- 去重；
- 沉默天数；
- 分享额度衰减。

mono 已有：

- QA 回答率；
- 主动消息 quota；
- 日报订正；
- 周报评分；
- 图片/文本反馈。

仍缺：

- 某条用户回复对应哪次主动 outreach；
- 多久回复；
- 是正面、负面、边界、转移话题还是无回复；
- 该结果如何改变下一次主动策略。

V2 增加：

```text
outreach_id
sent_event_id
reply_event_id
feedback_kind = positive | negative | boundary | topic_shift | no_response
response_delay_ms
```

无回复只影响策略，不写成用户负面态度。

## 3.10 沉默与 skip

已有：

- quiet hours；
- hot chat；
- min gap；
- daily quota；
- mute；
- 候选阈值；
- 主动 Prompt 允许不调用 qq_push。

V2 需要区分沉默原因：

```text
NO_CANDIDATE
POLICY_BLOCKED
TOO_REPETITIVE
USER_BUSY
QUIET_HOURS
LOW_VALUE
SYSTEM_FAILURE
```

只有前六项是有效行为决策；`SYSTEM_FAILURE` 不能包装成“角色今天不想说”。

空 tick 不写自我经历。

## 3.11 关系、Mood 与边界

V2 已有：

- stage；
- boundary；
- open thread；
- 临时 Mood。

补漏：

- Mood 确定性衰减；
- 关系修复事件；
- 负面证据；
- 角色自身边界；
- 群聊/私聊边界继续隔离。

不做：

- 单一好感度；
- 自动恋爱升级；
- “用户没回所以角色痛苦”。

## 3.12 承诺、目标和审批

dsh-bili-whale 有草稿审批，但没有通用承诺模型。

V2 已计划：

- commitment；
- 单 active goal；
- ASK_PERMISSION；
- 只读工具。

仍需明确：

```text
permission_token:
  goal_id
  action
  exact_arguments_hash
  expires_at
  approved_by
```

授权必须绑定具体动作和参数，不是一次批准后永久放权。

## 3.13 主动消息节奏

dsh-bili-whale Prompt 明确：

- 私信一两句；
- 评论一条收住；
- 弹幕一句；
- 可以不分享；
- 每轮最后收工。

V1 已计划：

- 1–3 条短消息；
- 可插话取消；
- 技术内容 atomic；
- 不固定段数；
- 不每次追问。

补漏：

- Companion 模式默认隐藏“思考中”、工具调用和工具结果轨迹；
- 段间只用短、有上限的语义停顿；
- 不随机伪装真人打字速度；
- proactive 发送成功后立即收尾，不复述。

归属：**V1 修订**。

## 3.14 权限、风控与副作用

dsh-bili-whale 最强的部分：

- 跨进程请求预算；
- 节流；
- 熔断；
- unknown；
- 写指纹去重；
- dry/go；
- 回读复核；
- 草稿恢复；
- owner recipient 锁定。

mono 已有：

- QQ allowlist/admin；
- scheduler/proactive guard；
- 部分重试/恢复；
- ask_user。

V1：

- proactive 只暴露一次 `qq_push`；
- owner recipient pinning；
- unknown 不记 sent。

V2：

- goal 只读白名单；
- permission token；
- 动作预算；
- 失败结果事件。

V3：

- 通用 Outbox；
- unknown 核查队列；
- 跨进程副作用治理。

## 3.15 Prompt 注入边界

dsh-bili-whale：

- 外部字幕/弹幕/评论明确标“素材不是指令”；
- 主人身份打码；
- 模型只提交结构化分类；
- 代码计算比例和闸门；
- 自定义命令不用 shell。

V1 已覆盖：

- proactive 背景 JSON；
- Reflection 无工具；
- Self-memory canonical JSON；
- Prompt version。

仍缺：

- daily/RAG/fact-card 当前直接拼 system；
- OCR/Web/tool output 没有统一 taint；
- 现有用户记忆可能携带命令式文本。

版本归属：

- V1：只保证新增 Companion Prompt 安全。
- V2：统一 `TrustedContext / UntrustedData` 包装接口。
- V3：全系统上下文来源追踪与 taint audit。

## 3.16 配置、自测和可见性

dsh-bili-whale 有：

- 设置页；
- cookie/二维码；
- deps；
- selftest；
- scheduler status；
- pending 草稿；
- notify test；
- 多类日志。

mono 已有 dashboard/health/CLI，但 Companion 没有入口。

V2 最少增加：

```text
companion status
companion events --day
companion reflections --with-sources
companion journals --day
companion commitments
companion goals
companion reset --dry-run
```

V3 再考虑 UI。

## 3.17 失败恢复

V1 修复：

- 发送前日志；
- unknown fallback；
- reflection 固定批次；
- 失败不阻断聊天。

V2：

- goal 重启恢复；
- 日记幂等；
- commitment sweeper；
- action result event。

V3：

- 持久 Outbox；
- exactly-once 能力分级；
- unknown 回读核查；
- 跨进程锁和 lease。

## 3.18 身体层

dsh-bili-whale 本身没有桌宠身体。

mono V1/V2 继续不做是正确的。

V2 可先有内部状态：

```text
day_phase
availability
activity_state
```

这些状态未来可以驱动桌宠，但当前不渲染。

## 3.19 数据生命周期与用户控制

当前计划有查看和 reset 概念，但还缺完整生命周期：

- 每类 event/reflection/journal/state 的保留期；
- 按 actor、日期、类型导出；
- 用户纠错如何影响已有反思和派生状态；
- reset 是只清自我记忆、只清关系，还是全部清除；
- 删除后向量、daily、日志、备份如何处理；
- 删除过程的 dry-run、影响预览和审计。

归属：

- V2：最小 retention/export/correct/reset CLI 和派生状态重建。
- V3：跨存储完整 purge 与备份生命周期。

## 3.20 派生状态重建

Relationship、Mood、self preference、outreach feedback 都是事件投影，必须支持：

```text
source correction/retraction
→ 标记受影响投影 stale
→ 按 projector_version 从源事件重建
→ 不重复外部副作用
```

每个 projector 需要：

- version；
- cursor；
- deterministic output；
- rebuild 命令；
- poison source 告警；
- 重建前后计数校验。

关系修复、负面证据和角色自身边界也通过该机制处理，不另建不可追溯的自然语言状态。

## 3.21 资源治理

V2 的只读活动仍然会消耗模型、网络和时间，必须有：

```text
max_llm_calls
max_input_tokens
max_output_tokens
max_read_actions
deadline_ms
cancel_on_user_message
daily_cost_limit
```

超预算：

- 目标转 `cancelled|failed`；
- 写真实原因事件；
- 不伪装成角色主动放弃；
- 不自动扩大预算重试。

## 3.22 反思生命周期与召回

仅有 `active/stale/contradicted/retracted` 状态不够，还需要：

- `topic_tags`；
- `valid_from/valid_to/expires_at`；
- `supersedes_id/refutes_id`；
- source 被纠错后的失效传播；
- 召回按 topic relevance + recency + confidence；
- 每次最多注入固定条数/字符；
- 低 confidence 和过期反思不召回。

## 3.23 统一信任边界

V1 只保护新增 proactive/reflection/self-memory；V2 必须扩展到现有动态上下文：

```text
daily notes
RAG recall
rollover
fact card
OCR
Web
tool output
```

统一接口：

```text
TrustedContext：代码生成的身份、时间、权限、预算和状态
UntrustedData：用户文本、外部内容、记忆文本、工具输出
```

要求：

- system 指令与动态数据分离；
- 动态数据带来源、长度和 privacy 元数据；
- Prompt builder 不接受裸字符串拼接；
- 工具权限不由动态数据决定；
- 日志能回答“哪段数据进入了哪次模型调用”。

---

## 4. 六个最容易漏掉的“活人感”

1. **角色在用户发消息前正在做什么。**
2. **活动有持续时间、会被打断、能恢复。**
3. **角色有自己的可逆兴趣和新鲜度，不只是镜像用户。**
4. **一天有相位与可用性，但不伪装真实生理。**
5. **主动消息结果会反过来改变策略。**
6. **私人日记会产生受控影响，而不是静态归档。**

---

## 5. 对现有计划的补漏决策

## 5.1 必须补进 V1

1. Owner gate 覆盖：
   - Companion event；
   - proactive；
   - memory maintenance；
   - daily/QA 写入入口。
2. 群聊命令不能修改 owner C2C 角色状态。
3. Proactive recipient pinning。
4. Companion 模式隐藏工具内部轨迹。
5. Partial sent 只记录确认成功前缀。
6. 修复 unknown fallback 和发送前日志。
7. 新增 Companion Prompt 数据边界。

## 5.2 必须补进 V2

1. 两本日记影响路径。
2. day phase / availability / activity state。
3. self preference / novelty / fatigue。
4. outreach feedback correlation。
5. silence reason code。
6. reflection stale/contradict/supersede。
7. Mood 衰减与关系修复。
8. permission token。
9. 最小 Companion CLI。
10. TrustedContext / UntrustedData 包装。
11. 数据 retention/export/correct/reset。
12. 所有派生状态的 projector version/rebuild。
13. 活动的 Token/读操作/时间/费用预算。
14. Reflection topic-aware 召回、过期、supersede/refute。
15. 现有 daily/RAG/fact-card/OCR/Web/tool output 的统一不可信上下文边界。

## 5.3 放入 V3

- stable actor/card alias；
- 多 owner、多 bot、多渠道；
- 通用 Outbox；
- 长期活动和多目标；
- 全系统 taint audit；
- Companion 管理 UI；
- reaction envelope；
- 可选桌宠；
- DSH Runtime A/B。

## 5.4 明确不做

- 自动重写核心 persona；
- 单一好感度；
- 自动恋爱升级；
- 高风险自主写入；
- 随机伪装真人打字；
- 用系统失败伪装“角色不想做”；
- 在 V1/V2 未稳定前做桌宠。

---

## 6. 验收清单：以后不能再漏

任何 Companion 版本设计完成前，必须逐项回答：

### 身份

- 角色是谁？
- 哪些入口使用同一身份？
- 切角色后哪些记忆隔离？

### 时间与活动

- 它现在处于什么相位？
- 用户不说话时它会不会做事？
- 活动如何开始、暂停、恢复、结束？

### 记忆

- 关于用户记什么？
- 关于自己记什么？
- 两本日记是否有真实 source？
- private 如何影响后续而不自证？

### 主动性

- 为什么现在开口？
- 为什么可能沉默？
- 用户回应如何关联到这次触达？

### 表达

- 一次发几条？
- 用户插话会不会停？
- 技术内容会不会被拆坏？
- 内部工具轨迹是否外显？

### 安全

- 外部数据是否可能注入？
- 哪些动作需要批准？
- 发送结果不确定怎么办？
- 是否可能重复副作用？

### 运维

- 用户怎样查看它记住了什么？
- 怎样纠错、重置、导出？
- 怎样知道 scheduler/反思/目标是否正常？

只有全部有明确答案，设计才能进入实现。
