# gacore 架构说明 (Architecture)

本文档解释 gacore 的图拓扑、状态通道，以及每个关键设计决策"为什么这么做"。
阅读对象是正在学习 LangGraph 的读者：对照 GenericAgent（下称 GA）的
[`agent_loop.py`](https://github.com/lsdefine/GenericAgent/blob/main/agent_loop.py)，
看同一个 Agent 循环如何从手写 `while` 变成官方 `create_agent` + middleware。

---

## 1. 图拓扑 (Graph Topology)

gacore 的图由官方 `langchain.agents.create_agent` 组装（`graph.py`），不再手写
节点：`model` 节点（官方模型调用）加预置 `ToolNode`（`tools`），`END` 是终结
状态。GA 的回合控制逻辑全部搬进两个 `AgentMiddleware`（`middleware.py`），
它们经 `hook_config(can_jump_to=[...])` 声明跳转能力，`_add_middleware_edge`
据此把钩子节点接成条件边，读取状态里的 `jump_to` 通道改变走向。

```mermaid
graph TD
    START(["START"]) --> bm["GATurnLogic.before_model<br/>短路 / max_turns 守卫"]
    bm -->|"exit_reason 已设置 或 超轮"| END(["END"])
    bm --> model["model 节点<br/>官方 create_agent 模型调用"]
    model --> am["GATurnLogic.after_model<br/>空重试 / done_hooks / 完成判定"]
    am -->|"jump_to=model（重试 / 续接）"| bm
    am -->|"AIMessage 含 tool_calls"| tools["tools 节点<br/>预置 ToolNode 执行工具"]
    am -->|"正常回答 → CURRENT_TASK_DONE"| END
    tools -->|静态边| model
```

流程（对照 GA `agent_loop.py` 的主循环）：

1. **GATurnLogic.before_model**（`@hook_config(can_jump_to=["end"])`）：进入模型前
   检查——`exit_reason` 已设置（ask_user 中止后恢复）或 `current_turn` 超过
   `max_turns`，返回 `{"jump_to": "end"}` 直接收尾，不再消耗一次模型调用；
   否则 `current_turn + 1` 继续。
2. **model**：官方节点。`GAPromptMiddleware.wrap_model_call` 用
   `ModelRequest.override(system_message=...)` 注入 GA 每轮动态提示词
   （系统规则 + working checkpoint + 周期提示 + 折叠历史），再调用模型。
   工具列表由 create_agent 在内部 `bind_tools`（`llm.py` 的 `get_llm`
   支持 `bind_tools=False` 返回未绑定模型）。
3. **GATurnLogic.after_model**（`@hook_config(can_jump_to=["model", "end"])`）：
   GA 的 `no_tool` 最终校验就在这里：
   - 消息带 `tool_calls` → 返回 `None`，官方默认路由送去 `tools`；
   - 空回答 → 追加纠正 HumanMessage 并 `jump_to="model"` 重试（最多 3 次，
     耗尽则 `EXITED`）；
   - `done_hooks` 非空 → 弹出第一条作为 HumanMessage，`jump_to="model"` 续接；
   - 正常回答 → `exit_reason="CURRENT_TASK_DONE"`（默认路由到 END）。
4. **tools**：langgraph 预置 `ToolNode` 执行模型发出的 `tool_calls`。需要写状态
   通道的工具（`ask_user` 的 `exit_reason`、`update_working_checkpoint` 的
   `working`）返回 `Command(update=...)`，由 LangGraph 原生处理。

**控制流改写的机制**（本次迁移学到的核心）：middleware 钩子本身只返回状态更新，
不直接控制流；真正改道的是 `create_agent` 为声明了 `can_jump_to` 的钩子生成的
**条件边**——它们读状态里的 `jump_to` 通道（`EphemeralValue`，每步后自动清空），
`{"jump_to": "end"}` 短路、`{"jump_to": "model"}` 回环。这是"用官方机制表达
自定义控制流"的标准姿势，比手写条件边函数更贴近 create_agent 的设计意图。

---

## 2. 状态通道 (State Channels)

`GAState`（`src/gacore/state.py`）继承官方
`langchain.agents.middleware.AgentState`：`messages`（带 `add_messages` 归约器）
和 `jump_to`（临时控制通道）由官方提供，其余 gacore 通道用 LangGraph 的默认
覆盖语义：节点返回的最新值直接替换旧值，节点必须自己带全量写入。

| 通道 | 来源 | 归约器 | 语义 |
| :--- | :--- | :--- | :--- |
| `messages` | 官方 AgentState | `add_messages`（追加） | 全量对话历史。LLM 的 AIMessage、工具结果 ToolMessage、校验注入的 HumanMessage 都按序追加 |
| `jump_to` | 官方 AgentState | `EphemeralValue`（临时） | middleware 控制流通道。`"end"` 短路 / `"model"` 回环，每步后自动清空，不进输入输出 |
| `working` | gacore | 覆盖 | 工作记忆。`update_working_checkpoint` 返回 `Command(update={"working": ...})` 写入 `key_info` / `related_sop` |
| `current_turn` | gacore | 覆盖 | 当前轮数，`before_model` 自增（`state.current_turn + 1`），供周期提示与守卫用 |
| `max_turns` | gacore | 覆盖 | 轮数上限，启动时由 `cfg.max_turns` 写入，`before_model` 只读 |
| `done_hooks` | gacore | 覆盖 | 收尾提示队列。`after_model` 弹出第一条作为 HumanMessage，再把剩余列表写回 |
| `retry_count` | gacore | 覆盖 | 空回答的连续重试计数，`after_model` 维护 |
| `exit_reason` | gacore | 覆盖 | 终止原因。取值 `CURRENT_TASK_DONE` / `EXITED` / `MAX_TURNS_EXCEEDED` / `AGENT_ERROR`。一经设置，`before_model` 就短路到 END |

关键观察：

- `messages` 是唯一的累积通道，天然对应 GA 里"历史在 Session 对象"的职责。
- `done_hooks` 没有归约器，所以 `after_model` 必须写回完整剩余列表
  （`"done_hooks": done_hooks[1:]`），这是覆盖语义的典型写法。
- `exit_reason` 是整个图的"急停开关"：`before_model` 先查它，查到了就
  `jump_to="end"`，不依赖消息内容。
- `jump_to` 是 `EphemeralValue`：middleware 写入后立即被条件边消费，不会残留
  在终态里——`get_state` 看到的终态不含它，测试断言更干净。

---

## 3. 关键设计决策 (Design Decisions)

### a. delta → full-history 迁移

**GA 的做法**（`agent_loop.py:104`）：

```python
messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]  # just new message, history is kept in *Session
```

GA 每轮只把**新的一轮消息**发给 LLM（新的 user prompt + 本轮的 tool_results），
完整历史存放在 Session 对象里，由 `turn_end_callback` 负责把历史折叠成摘要注入。

**gacore 的做法**：LangGraph 没有"Session"概念，状态必须自包含。
所以 `state.messages` 持有**全量历史**，`add_messages` 负责追加；
每轮提示词由 `context.build_system_prompt` + `fold_history` 现场重建，
`GAPromptMiddleware.wrap_model_call` 用 `ModelRequest.override(system_message=...)`
把全新构造的 `SystemMessage`（内含系统提示词 + 折叠后的历史摘要 + 周期提示）
注入模型请求——**这是官方 middleware API 替换原 `build_turn_prompt` 的地方**
（直接赋值 `request.system_message` 已弃用，`override` 是 1.3.14 的非弃用姿势）。

**为什么这个迁移是安全的**：`SystemMessage` 从不写入 `state.messages`，
每轮都是"临时拼接、用完即弃"，所以不会被 `add_messages` 重复累积；
而真正的对话消息全量保留在状态里，折叠只发生在"送入 LLM 的提示词"这一层。
副作用是 token 成本比 GA 高（全量重发历史），换来的是状态自包含、可恢复、
可测试。这对一个学习项目是正确取舍。

### b. StepOutcome 映射

GA 的每个工具返回 `StepOutcome(data, next_prompt, should_exit)`，主循环据此决定走向
（`agent_loop.py:90-98`）：

```python
if outcome.should_exit:      # → EXITED
if not outcome.next_prompt:  # → CURRENT_TASK_DONE
next_prompts.add(outcome.next_prompt)  # 否则把下一段提示带回循环
```

gacore 把 `StepOutcome` 的三元语义拆进两个地方：

| GA `StepOutcome` 字段 | gacore 落点 |
| :--- | :--- |
| `should_exit = True` | `ask_user` 工具返回 `Command(update={"exit_reason": "EXITED", ...})` |
| `next_prompt` 为空（任务完成） | `GATurnLogicMiddleware.after_model` 正常回答分支置 `exit_reason = "CURRENT_TASK_DONE"` |
| `next_prompt` 非空（继续干活） | 由 LLM 自己生成下一段内容；提示注入走系统提示词 / HumanMessage 通道 |

**为什么"何时算完成"的判定放在 middleware**：GA 里"任务完成"的信号来自工具
返回空 `next_prompt`；但模型不调工具、直接给纯文本回答时（GA 的 `no_tool`
伪调用），GA 语义是 `next_prompts` 为空则查 `_done_hooks`，没有 hook 就退出。
gacore 把这条"no_tool"路径的校验逻辑放进 `after_model` 钩子：正常回答 →
`CURRENT_TASK_DONE`，空回答 → `jump_to="model"` 重试（最多 3 次）后 `EXITED`，
done_hooks 非空 → 弹出续接。这样"何时算完成"的判定集中在一个可单测的
middleware 类里，也省掉了原先 final 节点和手写路由函数。

### c. 为什么用预置 ToolNode + Command，而不是自定义工具节点

GA 的工具结果里带着**控制信号**：`ask_user` 的 `should_exit` 要写进
`exit_reason`，`update_working_checkpoint` 的 `key_info` 要写进 `working`。
早期实现写了一个自定义 `GAStatefulToolNode` 来做这件事（本项目的
`nodes/tools.py`）。重构后**改用 langgraph 预置 `ToolNode` + `Command`**：

- langgraph 的 `ToolNode` 自动处理工具调用的配对、错误转 `ToolMessage`、
  `interrupt` 传播，不需要手写；
- 需要写非消息通道的工具，直接返回 `Command(update={...})`，LangGraph 原生
  把 update 合并进状态（`nodes/tools.py` / `nodes/final.py` 因此整个删掉）；
- `tool_call_id` 用 `Annotated[str, InjectedToolCallId]` 注入，`Command.update`
  里的 `ToolMessage` 用它配对回原始调用（`_validate_tool_command` 会校验）。

这是"能使用 LangGraph 的地方就使用 LangGraph，不重复造轮子"原则的直接体现：
自定义节点只在"预置原语覆盖不了"的时候才写。

### d. interrupt 语义

GA 的 `ask_user` 是同步等待用户输入；LangGraph 的等价物是 `interrupt()`：

- `ask_user` 工具（`tools/ask_user.py`）调用 `interrupt({"question": ..., "options": ...})`，
  第一次调用即暂停图执行。
- 图必须用 `MemorySaver`（或其它检查点）编译，暂停时的状态才能被序列化保存；
  `build_graph` 默认就带一个 `MemorySaver`。
- 恢复时用 `Command(resume=answer)` 继续，`interrupt()` 的返回值就是用户的回答
  （cli.py 的 `_run_turn` 循环处理）。
- 回答落在 `{abort, exit, quit, stop, cancel}` 里时，`ask_user` 返回
  `Command(update={"exit_reason": "EXITED", ...})`，`before_model` 钩子顶部
  短路返回 `{"jump_to": "end"}`（不再调 LLM），图直接收尾。

**langgraph 1.2.10 的版本特性**：单个中断在 `graph.invoke` 返回的 dict 里以
`__interrupt__` 键出现，**不抛异常**；`cli.py` 同时兼容了旧行为（捕获
`GraphInterrupt` 并归一化成同样的 dict 形式），因为中断可能从工具调用内部
以两种表象浮出水面。这是"跟随 langgraph 版本行为走"的一个实例。

### e. 工具异常处理

预置 `ToolNode` 在 langgraph 1.2.10 里的默认 `handle_tool_errors` 是一个只把
`ToolInvocationError`（参数校验错误）转成错误 `ToolMessage`、**其它异常原样重抛**
的处理器。这与 GA 的行为不符：GA 里单个工具失败绝不炸掉整个图。所以
`build_graph` 显式传 `handle_tool_errors=True`，让**所有**工具异常都变成错误
`ToolMessage` 送回给 agent 继续循环。

`ask_user` 的 `interrupt` 是例外：它在 langgraph 内部以 `GraphBubbleUp` 传播，
`ToolNode` 对它有专门分支原样上抛，不会被错误处理器吞掉——这是预置节点
替我们正确处理的场景之一。

### f. recursion_limit

LangGraph 默认 `recursion_limit = 25`，对 Agent 循环太低了：一轮任务在图上
大约要跑 2 步（agent → tools，纯回答 1 步），40 轮就是 80 步，还没算
空回答重试和 done_hooks 续接。`graph.py` 提供

```python
suggested_recursion_limit(max_turns) -> max_turns * 2 + 50
```

2 倍覆盖每轮的典型步数，+50 给重试和续接留余量；`run_once` 和 REPL
默认用 `DEFAULT_RECURSION_LIMIT = 200`。碰到 `RecursionLimit` 报错时，
第一反应应该是查这里，而不是改图结构。

### g. _cfg 注入缝的限制

工具需要配置（`code_run` 要 asset/temp 目录、`start_long_term_update` 要
memory 目录），但 LangChain 的 `@tool` 会从函数签名生成 JSON schema：
**pydantic 会丢弃下划线开头的参数**。所以 `_cfg: Config | None = None`
能通过签名注入、但不会出现在工具 schema 里（LLM 永远看不到它）。

后果：生产运行时工具只能回退到 `Config.default()`（读进程环境变量），
无法在测试中按工具粒度注入临时目录。解决方式是测试用 `Config.for_tests(tmp_path)`
构造 Config，并通过 `GACORE_MEMORY_DIR` 等环境变量让它成为 `Config.default()`
的真实来源，或者直接 patch 工具模块的 `build_tool_list` / `_default_cfg`。
这是"用 LangChain 工具装饰器就要接受它的 schema 约束"的学费。

---

## 4. GA parity 核对 (Parity Check)

逐条对照 GA `agent_loop.py:42-107`（`agent_runner_loop`）与 gacore 实现。
`[忠实]` = 语义完整移植；`[简化]` = 行为有差异或裁剪。

| # | GA `agent_loop.py` | gacore 实现 | 判定 |
| :--- | :--- | :--- | :--- |
| 1 | :44-47 初始化 `messages=[system, user]` | `new_state` 造 `[HumanMessage(user_input)]`；系统提示词每轮现拼 | `[忠实]` 语义（system 不落历史，见 3a） |
| 2 | :48 `turn=0; handler.max_turns=max_turns` | `state.py` 初始化 `current_turn=0, max_turns=cfg.max_turns` | `[忠实]` |
| 3 | :50 `while turn < handler.max_turns` | `before_model` 进入时 `if turn > max_turns → MAX_TURNS_EXCEEDED` | `[忠实]`（守卫提前到 LLM 调用之前，比 GA 更省一次调用） |
| 4 | :51 `turn += 1` | `before_model` `current_turn = state.current_turn + 1` | `[忠实]` |
| 5 | :56 `if turn%10==0: client.last_tools=''` | 无对应（create_agent 每次重建绑定，无工具描述缓存） | `[简化]`（机制不存在，无需重置） |
| 6 | :59 `client.chat(messages, tools=tools_schema)` | create_agent 内部 `bind_tools` + `GAPromptMiddleware` 注入系统提示词 | `[忠实]`（llmcore → llm.py → create_agent） |
| 7 | :69 无 `tool_calls` → `no_tool` 伪调用 | `after_model` 内联完成校验（空回答重试 / done_hooks / 收尾） | `[忠实]`（no_tool 分支并入 middleware） |
| 8 | :74-98 逐个 `dispatch` 工具 → `StepOutcome` | 预置 `ToolNode` 执行 `tool_calls`；控制信号经 `Command(update=...)` 回写 | `[忠实]` |
| 9 | :90 `outcome.should_exit` → `EXITED` | `ask_user` 返回 `Command(update={"exit_reason": "EXITED", ...})` | `[忠实]` |
| 10 | :92 `not outcome.next_prompt` → `CURRENT_TASK_DONE` | `after_model` 正常回答 → `exit_reason="CURRENT_TASK_DONE"` | `[忠实]`（判定从工具结果移到 middleware，见 3b） |
| 11 | :95-97 `outcome.data` → `tool_results`（带 `tool_use_id`） | `ToolMessage(content=..., tool_call_id=call_id)` | `[忠实]`（LangChain 原生配对） |
| 12 | :99-101 `next_prompts` 为空且非 EXITED → 弹 `_done_hooks[0]` 续接 | `after_model` done_hooks 分支：第一条 hook 转 HumanMessage 后 `jump_to="model"` 继续循环 | `[忠实]`（`_done_hooks` 队列 → `done_hooks` 通道） |
| 13 | :102 `turn_end_callback(...)` 返回下一条 prompt | `context` 纯函数组装 + `GAPromptMiddleware` 注入 | `[简化]`（见下） |
| 14 | :104 `messages = [新 user 消息]`，历史在 Session | `state.messages` 全量追加（`add_messages`） | `[简化]`（核心差异，见 3a） |
| 15 | :105 exit 后再调一次 `turn_end_callback` | 无对应（`after_model` 已含校验） | `[简化]` |
| 16 | :107 `return exit_reason or MAX_TURNS_EXCEEDED` | `exit_reason` 通道 + `before_model` 守卫兜底 | `[忠实]` |

### turn_end_callback 的核对（ga.py:570）

GA 的 `turn_end_callback` 做四件事，gacore 在 `context.py` 里的对应：

| GA `turn_end_callback` 职责 | gacore 对应 | 判定 |
| :--- | :--- | :--- |
| 从回复里提取 `<summary>`（`extract_summaries` 的正则同源） | `context.extract_summaries` | `[忠实]`（正则与协议一致） |
| 把 `[Agent] {summary}` 追加进 `history_info` 供下轮折叠 | 完整 AIMessage 已在 `state.messages`，无需摘要历史；折叠发生在送 LLM 前 | `[简化]`（GA 用摘要省 token，gacore 全量保留） |
| 周期提示：turn%10 记忆刷新、%13 打工作检查点、%31 写文件、%175 ask_user | `context.periodic_hints`（额外加 turn>100 反循环警告） | `[简化]`（触发点一致，文案简化；GA 的 plan 模式提示、master 注入未移植） |
| `get_global_memory`（读 L1 洞察索引拼提示） | 折叠历史 + `build_system_prompt` 的 working checkpoint | `[简化]`（不读 `insight_fixed_structure` 模板） |

### LLM 异常处理（gacore 新增）

GA 没有显式处理 LLM 调用异常；gacore 用官方 `ModelRetryMiddleware`
（`max_retries=0, on_failure=format_agent_error`）把 provider 异常转成
`[Agent error: ...]` 说明的 AIMessage，`after_model` 识别该前缀后以
`exit_reason="AGENT_ERROR"` 干净退出（`middleware.py`）。这是有意的偏差：
学习项目优先可测试、可诊断的终态，而不是让图硬崩溃或无限重试。
`max_retries=0` 保留 GA 的 fail-fast 语义，`on_failure` 只是借官方异常捕获
通道做错误格式化——异常处理本身不再手写 try/except。

---

## 5. 简化与未移植项 (Simplifications)

- **全量历史 vs 增量消息**：3a，最大的结构性差异，token 成本换状态自包含。
- **LLM 异常**：GA 无处理，gacore 用官方 `ModelRetryMiddleware` 以
  `AGENT_ERROR` 干净退出。
- **空回答重试**：`after_model` 内联实现（GA 只有空回答的 `_retry_or_exit` 规则；
  早期版本针对 provider `finish_reason` 的**截断续写检测已删除**——不同 provider
  的 `finish_reason` 行为不一致，判据不可靠，移除后空回答重试仍保留）。
- **master 注入 / 干预通道**：GA 的 `_keyinfo` / `_intervene` 文件注入、plan 模式
  提示、`_turn_end_hooks` 全部未移植。
- **`client.last_tools` 重置**：GA 每 10 轮清空工具描述缓存以省 token，机制不存在，跳过。
- **未移植模块**：reflect 自治模式、插件 hooks、IM 前端、L4 归档、
  Mixin 多模型切换、TMWebDriver 真实浏览器（见 README 范围说明）。

---

## 6. 三层记忆系统 (Three-Layer Memory)

gacore 的记忆分为三层，职责不同，不可混用。设计灵感来自另一个 agent 的
策略——每日笔记作为"原始日志"层，填补了工作记忆和长期记忆之间的断层。

```
工作记忆 (update_working_checkpoint)
  ↓ 每轮可覆盖，只放当前任务的关键信息
每日笔记 (read_daily / edit_daily / search_daily)
  ↓ 按日期组织，记重要决策/教训/事实，不记闲聊
长期记忆 (start_long_term_update)
  ↓ 从每日笔记蒸馏精华，按主题组织，去重精炼
```

### 层级职责

| 层 | 工具 | 存储 | 生命周期 | 记什么 |
| :--- | :--- | :--- | :--- | :--- |
| 工作记忆 | `update_working_checkpoint` | `state.working`（RAM） | 单轮 | 当前任务的关键信息 |
| 每日笔记 | `edit_daily` / `read_daily` / `search_daily` | `memory/daily/{date}.md` | 跨会话、按天 | 重要决策、教训、关键事实、用户偏好 |
| 长期记忆 | `start_long_term_update` | `memory/global_mem.txt` + `global_mem_insight.txt` | 永久 | 蒸馏精华：用户画像、长期目标、重要教训 |

### 每日笔记的三个工具

| 工具 | 作用 | 调用时机 |
| :--- | :--- | :--- |
| `read_daily(date)` | 按日期读取笔记全文 | 需要回顾某天发生了什么 |
| `edit_daily(date, old_str, new_str)` | 精确字符串替换编辑 | 记录当天事件、教训、决策 |
| `search_daily(query)` | 跨所有笔记关键词搜索 | 模糊查找历史记录 |

`edit_daily` 是精确替换模式（非追加）：`old_str` 必须在文件中唯一匹配。
新建笔记时 `old_str=""`，文件自动创建并带日期头。支持 `"today"` / `"yesterday"`
日期快捷方式。

### 会话启动时的上下文恢复

`context.build_system_prompt` 每轮注入最近两天每日笔记的** bullet 标题行**
（`load_recent_daily_summaries`），而非全文——控制 token 成本的同时恢复
跨会话连续性。注入位置在系统规则之后、工作检查点之前：

```
[系统规则]
=== Recent daily notes ===
[2026-08-03]
- 完成了每日笔记功能开发
- 学到 edit_daily 要用精确替换而非追加

[Working checkpoint]
...
```

### 为什么用关键词搜索而非语义搜索

`search_daily` 做的是大小写不敏感的关键词匹配，不是向量语义搜索。这是
有意的简化：学习项目不需要引入 embedding 模型和向量数据库的依赖，
关键词搜索已经能覆盖"上次聊 X 是什么时候"这类典型查询。

---

## 7. 定时任务调度器 (Scheduler)

gacore 的定时任务是一个**单进程轮询调度器**（`scheduler.py`），能在指定时间
触发 agent 执行自包含任务（如每日报告、周报），无需人工交互。

### 设计取舍

| 维度 | 选择 | 理由 |
| :--- | :--- | :--- |
| 调度模型 | 单进程 `time.sleep` 轮询 | 学习项目，不引入 Redis/APScheduler |
| 调度格式 | `"HH:MM"`（每日）或 `"every N<m\|h\|d>"`（间隔） | 覆盖"每天发周报"场景，不需要完整 cron |
| 会话模型 | 单轮 headless：`run_once` 跑完即结束 | 定时任务无人在场，不能 ask_user |
| 状态持久化 | `memory/schedule_state.json` | 重启后不补跑漏掉的 job（防雷鸣群效应） |
| 输出 | `logs/scheduled/{job}_{ts}.md` + 每日笔记 bullet | 文件可追溯，每日笔记自动记录 |

### 核心组件

```
config/schedule.json          ← job 定义（name/schedule/prompt/enabled）
memory/schedule_state.json    ← 运行时状态（last_run/run_count）
logs/scheduled/               ← 每次 job 执行的完整输出

scheduler.run_loop()
  │
  ├── load_jobs()         ← 每轮重新读 schedule.json（支持热更新）
  ├── is_due(job, state)  ← 计算 next_run_time，判断是否到期
  ├── run_job(job)         ← 构建独立 graph，run_once 执行
  │    ├── _default_graph_runner → build_graph + run_once
  │    ├── _write_output → logs/scheduled/{job}_{ts}.md
  │    └── _write_daily_note → edit_daily("today", ...)
  └── save_state()        ← 持久化 last_run，防止重复触发
```

### 会话隔离

每个定时 job 是一个**全新的 agent 会话**：
- 独立的 `thread_id`（`sched-{uuid}`），不与 REPL 共享 MemorySaver 状态
- prompt 作为唯一的 user message，agent 跑完一轮就结束
- agent 仍然能读写工作区文件、每日笔记、长期记忆——这些都是文件系统共享的
- 但 agent **看不到** REPL 里的对话历史（对话历史隔离）

这对应了用户分析中的"文件系统共享，对话历史隔离"模型。

### 防雷鸣群效应

重启时不会补跑漏掉的 job：`next_run_time` 计算"上次运行时间 + 间隔"，
如果结果已过期，就向前步进到未来；对于每日 `"HH:MM"`，如果今天已运行过，
`last_run` 会被设为今天，下一次触发就是明天。

### 运行方式

```bash
# 启动调度器（前台运行，Ctrl-C 停止）
python -m gacore.scheduler

# 或通过环境变量
GACORE_MEMORY_DIR=/path/to/memory python -m gacore.scheduler
```

`config/schedule.json` 支持热更新——调度器每轮重新读取，改了配置不用重启。
