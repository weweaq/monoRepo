# Companion Self-Memory V1：逐步实施计划

> 状态：Ready  
> 前置设计：[`companion-self-memory-mvp.md`](./companion-self-memory-mvp.md)  
> 实施规则：一次只完成一个 Step；测试通过并人工确认后再进入下一步  
> V1 目标：人格贯穿、真实收发事件、有证据的自我反思、有限 Prompt 注入、自然短消息节奏

---

## 0. 硬边界

V1 只支持：

- 单 owner；
- QQ 私聊；
- 现有 LangGraph Runtime；
- `actor_id = active_card or "assistant-default"`；
- 消息事件与反思；
- 最多三条短消息的 Companion 闲聊。

V1 不实现：

- 群聊 Companion memory；
- 多 owner；
- 工具经历；
- Relationship、Mood、Commitment、Goal；
- 通用 Outbox/exactly-once；
- 主动生活循环；
- 桌宠；
- DSH。

实现 Agent 看到未来设计也不得顺手加入。

## 0.1 每步提交规则

每个 Step 至少两个独立提交：

1. 代码 + 测试提交；
2. R5 文档提交：
   - `docs/ROADMAP.md`
   - `docs/langTrack-tech.md`
   - 架构变化时更新 `docs/architecture-flow.mmd`

每步结束必须：

- 运行该步测试；
- 运行受影响模块回归；
- `git diff --check`；
- 停止，不继续下一步。

---

## Step 1：共享 Active-Card Repository

### 目标

QQ 与 proactive 从同一个实现读取当前角色卡，避免各自解析 JSON。

### 当前代码

- `frontends/qq.py::_card_state_file`
- `frontends/qq.py::_load_user_cards`
- `frontends/qq.py::_save_user_cards`
- 进程内 `_user_card`

当前写入使用普通 `write_text`，没有原子替换；proactive 无共享读取入口。

### 修改文件

```text
src/gacore/companion/__init__.py
src/gacore/companion/identity.py
src/gacore/frontends/qq.py
tests/test_companion_identity.py
tests/test_qq.py
```

### 实现

```python
class ActiveCardRepository:
    def get(self, openid: str) -> str | None: ...
    def set(self, openid: str, card_id: str | None) -> None: ...
    def snapshot(self) -> dict[str, str]: ...
```

要求：

- 路径仍为现有 `data/active_cards.json`。
- 写入使用临时文件 + `os.replace`。
- 读到不存在文件返回空。
- JSON 损坏时记录告警并返回空，不覆盖损坏文件。
- QQ 进程内 cache 只能通过 repository 更新。
- 不改变当前命令行为。

### 测试

- 空文件/不存在。
- 正常读取。
- set/remove。
- 损坏 JSON 不覆盖。
- 原子替换。
- QQ 角色切换仍通过。

### 验收

```powershell
uv run pytest apps/withlanggraph/tests/test_companion_identity.py
uv run pytest apps/withlanggraph/tests/test_qq.py
```

### 提交

```text
refactor(gacore): centralize active card persistence
docs(gacore): record active card repository refactor
```

---

## Step 2：Owner 配置与 Proactive 人格贯穿

### 目标

Owner 的普通聊天与 proactive 使用相同角色卡；移除“韩立口吻”硬编码。

### 当前调用链

```text
run_proactive_job
  → build_proactive_prompt
  → _headless_run
  → run_once
```

只有 `run_once()` 已支持 `**extra_state`；其他函数尚未透传 `active_card`。

### 修改文件

```text
src/gacore/companion/config.py
src/gacore/companion/identity.py
src/gacore/proactive.py
tests/test_proactive.py
tests/test_character_system_prompt.py
```

### 配置

```text
COMPANION_MVP_ENABLED=0
COMPANION_OWNER_QQ_OPENID=<owner user_openid>
```

缺失时：

- Companion 功能关闭；
- proactive 保持现有中性行为；
- 不猜 owner。

### 参数链

```python
active_card = active_cards.get(target_openid) if is_owner(target_openid) else None
actor_name = card_name(cfg, active_card) if active_card else "助手"

prompt = build_proactive_prompt(..., actor_name=actor_name)
result = _headless_run(..., active_card=active_card)

# _headless_run 内
run_once(graph, prompt, ..., active_card=active_card)
```

要求：

- `build_proactive_prompt()` 删除固定“韩立口吻”。
- `_target_users()` 可以保持现状，但只有 owner 使用 Companion card。
- 非 owner 不读取 owner card。
- 更新测试中的 `_headless_run` fake 签名。

### 测试

- owner=林晚时 Prompt/GAState 使用 `linwan`。
- owner 切卡后下一次 proactive 使用新卡。
- 非 owner `active_card=None`。
- owner 配置缺失时不报错。
- Prompt 不再包含固定“韩立口吻”。

### 验收

```powershell
uv run pytest apps/withlanggraph/tests/test_proactive.py
uv run pytest apps/withlanggraph/tests/test_character_system_prompt.py
```

### 提交

```text
fix(gacore): keep proactive persona consistent with owner card
docs(gacore): document proactive persona continuity
```

---

## Step 3：最小 Companion SQLite Store

### 目标

建立 event/reflection 的事实源，但不接入 QQ 行为。

### 修改文件

```text
src/gacore/config.py
src/gacore/companion/store.py
tests/test_companion_store.py
```

### 数据库

按 MVP 设计创建四表：

- `companion_event`
- `companion_reflection`
- `companion_reflection_source`
- `companion_cursor`

必须遵守 R6：

```sql
created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
```

### API

```python
append_event(...) -> AppendResult
events_after(...)
events_between(...)
latest_reflections(...)
get_cursor(...)
commit_reflection_batch(...)
```

`commit_reflection_batch()` 必须在一个事务完成：

- reflection；
- reflection_source；
- cursor/failure 状态。

### 连接规则

- 每连接执行 foreign_keys/WAL/busy_timeout。
- migration 使用 `BEGIN IMMEDIATE`。
- `PRAGMA user_version=1` 只在全部成功后设置。
- 并发 writer 使用独立连接。
- busy timeout 后回滚并报错。
- DB 路径相对 `cfg.root` 解析，可由环境变量覆盖。

### 测试

- migration 幂等。
- migration 失败回滚。
- user_version。
- 每连接 PRAGMA。
- idempotency key。
- FK。
- actor 隔离。
- 并发独立连接。
- reflection/source/cursor 全事务回滚。

### 验收

```powershell
uv run pytest apps/withlanggraph/tests/test_companion_store.py
```

### 提交

```text
feat(gacore): add minimal companion event store
docs(gacore): document companion store v1
```

---

## Step 4：Owner C2C 收件事件

### 目标

只记录 owner QQ 私聊文本，不调用反思。

### 修改文件

```text
src/gacore/frontends/qq.py
src/gacore/companion/config.py
src/gacore/companion/identity.py
src/gacore/companion/store.py
tests/test_companion_events.py
tests/test_qq.py
```

### 接入点

`QQApp.on_message()`：

```text
message ID 去重
→ 解析 content/user
→ allowlist
→ 图片分支返回
→ 空文本返回
→ 写 owner C2C 文本事件   ← 此处
→ chat archive / command / mute / resume / QA / feedback / trivial / Agent
```

因此命令、反馈、trivial 和普通 Agent 输入都会记一条；图片不记。

### 写入

```python
append_event(
    kind="user.message.received",
    actor_id=current_actor,
    principal_id="owner",
    source_id=qq_message_id,
    idempotency_key=f"qq-user:{qq_message_id}",
    summary=minimize_user_message(content, max_chars=200),
)
```

Store 异常：

- 记录日志；
- 不阻断 QQ 聊天；
- 不同步调用 LLM。

### 测试

- owner 的 command/mute/QA/trivial/普通输入各写一次。
- 群聊、非 owner、图片、空文本不写。
- 重复 message ID 不重复。
- summary 脱敏和 200 字限制。
- store 异常不阻断聊天。

### 提交

```text
feat(gacore): record owner chat events
docs(gacore): document companion ingress events
```

---

## Step 5：发送回执基础设施

### 目标

普通最终回复和 proactive 都返回真实发送结果；本步不写 Companion event。

### 修改文件

```text
src/gacore/frontends/qq.py
src/gacore/tools/qq_tools.py
src/gacore/langTrack/qq_push.py
tests/test_companion_delivery.py
tests/test_qq.py
tests/test_tools_qq_push.py
tests/test_proactive.py
```

### 普通 QQ

新增：

```python
@dataclass(frozen=True)
class SegmentReceipt:
    confirmed: bool
    actual_text: str
    provider_message_id: str | None
    outcome_unknown: bool

@dataclass(frozen=True)
class SendReceipt:
    confirmed: bool
    segments: tuple[SegmentReceipt, ...]
```

要求：

- `_send_markdown()` 返回 API 结果。
- markdown 明确未发送才允许 plain fallback。
- timeout/连接不明为 unknown，不 fallback。
- 4500 安全分块逐段记录。
- 全段明确成功才 `SendReceipt.confirmed=True`。
- 最终失败向上返回，不再吞掉。
- chat log 在 receipt 后记录实际确认文本。

### Proactive

`qq_push` 结果增加：

```text
actual_message
ids
to
failures
status
```

`_qq_push_sent()` 必须验证：

- owner 在成功目标；
- owner 不在 failures；
- 有 owner 对应结果；
- 返回实际发送文本；
- 不以总 `ok>0` 代替 owner 成功。

### 测试

- markdown 成功。
- markdown 明确失败后 plain 成功。
- markdown timeout 不 fallback。
- 全部失败。
- 多段部分成功。
- API message ID 为空但明确成功。
- proactive 其他人成功、owner 失败时总体不算成功。
- 截断后的 actual_message。

### 提交

```text
refactor(gacore): return structured QQ delivery receipts
docs(gacore): document confirmed delivery semantics
```

---

## Step 6：已确认发送事件

### 目标

基于 Step 5 receipt 写入真实 sent event。

### 修改文件

```text
src/gacore/frontends/qq.py
src/gacore/proactive.py
src/gacore/companion/store.py
tests/test_companion_events.py
tests/test_proactive.py
```

### 普通回复

只有 `SendReceipt.confirmed=True`：

```text
agent.message.sent
summary = 按顺序合并 receipt.segments.actual_text
source_id = 本地 attempt ID 或平台 IDs 的稳定 hash
```

partial/unknown/failed：

- 若存在按顺序确认成功的前缀段，写一条 `agent.message.sent`，summary 只包含成功段，并明确标记“仅成功发送 N/M 段”；
- 未确认、失败和取消的段落绝不进入 summary；
- 一段都未确认时不写 sent event；
- 写诊断日志；
- 不自动重试。

### Proactive

使用 `qq_push` 的 `actual_message` 和 owner message ID：

```text
proactive.message.sent
```

不得使用 `_headless_run()` 的最后 AIMessage 代替发送正文。

### 测试

- confirmed 写一次。
- 部分成功只记录确认前缀，不记录失败/unknown/取消段。
- 一段都未确认的 partial/unknown/failed 不写。
- proactive 实际正文一致。
- 重放相同 attempt 不重复。

### 提交

```text
feat(gacore): persist confirmed companion deliveries
docs(gacore): document companion sent events
```

---

## Step 7：Evidence-Bound Reflection

### 目标

至少三条新事件生成 0–2 条带来源反思。

### 修改文件

```text
src/gacore/companion/reflection.py
src/gacore/companion/coordinator.py
src/gacore/companion/store.py
src/gacore/frontends/qq.py
src/gacore/proactive.py
tests/test_companion_reflection.py
```

### 模型

```python
get_llm([], bind_tools=False)
```

提供 model/runner 注入 seam，测试不访问真实模型。

### 执行

- 在后台 executor 运行，不阻塞 `on_message`。
- 同 actor/principal lock。
- 固定最早 10 条 batch。
- 至少 3 条才运行。
- 每角色东八区每天最多一次。
- reflection/source/cursor 原子提交。
- 有效空结果推进 cursor。
- 首次失败重试同一批。
- 第二次失败跳过该批并写 cursor.last_error + 日志。

### 触发接线

`coordinator.py` 提供：

```python
def schedule_reflection_check(actor_id: str, principal_id: str) -> None:
    """提交后台 single-flight 检查；同 actor/principal 同时最多一个任务。"""
```

接线位置：

- Step 4 的 `user.message.received` 插入结果 `inserted=True` 后；
- Step 6 的 `agent.message.sent/proactive.message.sent` 插入结果 `inserted=True` 后。

要求：

- duplicate event 不提交检查；
- coordinator 异常只记录日志；
- 应用关闭时停止接收新任务并有限等待正在运行任务；
- 测试可注入同步 executor/fake runner；
- 反思模型调用不在 QQ event loop 中同步执行。

### 测试

- 少于三条 0 次模型调用。
- 两个并发触发只调用一次。
- Step 4/6 新事件会触发 coordinator，重复事件不会触发。
- duplicate event 不触发。
- timeout 不影响聊天。
- source 不存在/跨 actor 拒绝。
- 非 JSON 拒绝。
- 空结果推进。
- 失败批次固定。
- 原子回滚。

### 提交

```text
feat(gacore): add evidence-bound companion reflection
docs(gacore): document reflection batching and evidence
```

---

## Step 8：Self-Memory Prompt 注入

### 目标

Owner C2C 的完整 Agent 轮读取当前角色自己的最多三条反思。

### 修改文件

```text
src/gacore/state.py
src/gacore/frontends/qq.py
src/gacore/context.py
src/gacore/companion/context.py
tests/test_companion_context.py
tests/test_state.py
tests/test_context.py
```

### State

```python
class CompanionTurnContext(TypedDict):
    actor_id: str
    principal_id: str

class GAState(...):
    companion_context: CompanionTurnContext | None
```

`new_state(..., companion_context=None)`。

Owner C2C `_run_agent` 同时传：

```python
active_card=current_card
companion_context={
    "actor_id": current_card or "assistant-default",
    "principal_id": "owner",
}
```

群聊/非 owner 为 None。

### 注入位置

`context.build_system_prompt()`：

```text
base rules
→ active role card
→ Self memory              ← 此处
→ daily/RAG/rollover/fact card
```

限制：

- 最多 3 条。
- 1,000 字符。
- store 失败 fail-open。
- 不从 active_card 反推 owner。
- trivial reply 不进 graph，因此 V1 不使用 self-memory。

### 测试

- owner/非 owner/群聊。
- 默认 actor。
- 切卡隔离。
- store 异常。
- 3 条/1000 字。
- 注入顺序。
- checkpoint resume 保留 context。

### 提交

```text
feat(gacore): inject bounded companion self memory
docs(gacore): document self-memory prompt context
```

---

## Step 9：Companion 短消息节奏与 E2E

### 目标

Owner C2C 的闲聊以自然的 1–3 条短消息发送；技术输出保持完整。

### 修改文件

```text
src/gacore/frontends/qq.py
src/gacore/companion/reply_batch.py
tests/test_companion_delivery.py
tests/test_companion_e2e.py
```

### 放置位置

```text
QQApp._stream_agent
→ 得到最终 AIMessage
→ 去 summary
→ proposal handling
→ plan_final_reply(text, mode)     ← 语义分段
→ deliver_reply_batch(...)        ← 锁、间隔、取消
→ Step 5 SendReceipt
→ Step 6 event
```

不得放到：

- graph/LLM 通用层；
- 全局 send_text；
- scheduler/operator；
- command/trivial/tool trace。

### ReplyBatch

```python
@dataclass(frozen=True)
class ReplyBatch:
    logical_turn_id: str
    segments: tuple[str, ...]  # 1–3
```

规则：

- 一个意思一条。
- 不从句中硬切。
- 不强制凑三条。
- 代码块、表格、命令、路径、栈、多方案、技术报告保持原子。
- 不确定是否适合拆分时保持原子。
- 4500 字安全切块仍作为底层渠道兜底，不等于聊天节奏分段。

### 锁与取消

- 每 chat 一个 delivery lock。
- batch 持有 cancellation token。
- 新消息到达时先取消旧 batch 未发送的低优先级段。
- 已确认段保留事实。
- 新回复批次不得与旧批次交错。

### Transcript 验收

固定 20 轮 fixture + 人工记录：

- 无超过 3 条；
- 无句中硬切；
- 无强拆单句；
- 无技术内容拆坏；
- 插话取消未发送段；
- 不连续多轮固定相同段数；
- 不逐句复述用户原话；
- 不每轮都追加问题。

### 回归

```powershell
uv run pytest apps/withlanggraph/tests
uv run ruff check apps/withlanggraph
```

### 提交

```text
feat(gacore): add natural companion reply batches
docs(gacore): record companion v1 end-to-end verification
```

---

## 完成定义

V1 完成必须同时满足：

- proactive 与普通聊天人格一致；
- 只记录 owner C2C；
- 失败/unknown 不记为 sent；
- reflection 全部有有效 source；
- Self memory 不跨角色；
- 无事件时不调用 reflection LLM；
- 20 轮 transcript 验收通过；
- 全量测试与 lint 通过；
- R5 文档与架构图同步。

完成后至少观察两周，再决定是否进入 V2。
