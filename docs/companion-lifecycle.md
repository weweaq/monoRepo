# Companion 数据生命周期与信任边界（V2 配套策略）

> 对应 `docs/companion-self-memory-v2-plan.md` V2-Step 7 / 完成定义。本文回答四个问题：
> **留多久、怎么导出、纠错怎么传播、reset 与重建的边界在哪**。
>
> 唯一事实源：`data/companion.db`（SQLite，`PRAGMA user_version = 8`）。
> 所有派生状态（关系 / Mood / 承诺 / 目标 / 活动 / 日记）都只从 `companion_event` 投影而来，
> 投影器版本记录在 `companion_projection_state`。

## 1. 保留期

| 数据 | 位置 | 保留 | 说明 |
|---|---|---|---|
| `companion_event` | companion.db | **永久** | 唯一事实源；其余全部可重建，只有它不可重建 |
| `companion_reflection` | companion.db | 永久（带状态） | `active/stale/contradicted/retracted`；不物理删除，靠状态退出注入 |
| `companion_journal` | companion.db | 永久 | 两本日记；`stale` 可重生成，历史 day 不覆盖 |
| `companion_commitment` / `companion_goal` | companion.db | 永久 | 终态保留，作为可靠性证据 |
| `companion_activity` | companion.db | 永久 | 含预算与花费；终态不可改 |
| `companion_self_preference` | companion.db | `expires_at_ms` 到期即不参与读取 | 可反转、可衰减 |
| `companion_outreach_feedback` | companion.db | 永久 | 触达与回复的归因证据 |
| `memory/daily/*.md`、`memory/global_mem_*.txt` | 文件 | 既有策略不变 | **不是** Companion 事实源，继续由旧链路维护 |
| 向量库（episodic） | 外部 | 既有策略不变 | 可从 daily 与事件重建，不属于 Companion 决策依据 |
| `logs/<day>/app.jsonl` | 文件 | 既有 7 天窗口 | 只做诊断，不参与决策 |
| `data/qq_chat_log.jsonl` | 文件 | 既有策略不变 | 第一人称原始聊天，不复制进 companion.db |

## 2. 导出

```powershell
python -m gacore.companion.cli export --actor hanli --day 2026-10-30
```

导出为 JSON：当日事件（含 `seq`/`kind`/`summary`/`occurred_at_ms`）、两本日记（含 source）、
承诺、目标、以及当前投影版本状态。`--actor` 缺省为环境变量解析出的当前角色卡，
再缺省为 `assistant-default`。

## 3. 纠错与失效传播

```powershell
python -m gacore.companion.cli correct --source <event_id> --dry-run
```

- 预览：列出**引用了该事件**的反思与日记（blast radius），不做任何修改。
- 人工确认后，用 `reflection.mark_stale()` / `retract()` / `refute()` / `supersede()`
  之一改状态；状态变更即时生效，下一次构造 Prompt 就不再注入该条。
- 事件本身**不物理删除**：只要事件还在，派生状态永远可以重建（见第 4 节）。
- `retracted` 表示主人明确要求忘记；`contradicted` 表示有新证据反驳；两者都不删除文本，
  便于审计与重建。

## 4. 重建

```powershell
python -m gacore.companion.cli rebuild --projection relationship --dry-run   # 预览
python -m gacore.companion.cli rebuild --projection relationship --apply     # 从事件重算
```

- 只允许 `relationship` / `mood`：两者是纯函数投影，重算 **只读** `companion_event`。
- 重建**绝不**发送消息、绝不重放外部动作、绝不重新调用模型。
- 日记/承诺/目标的重建需要模型或外部结果，属于人工操作：先 `correct` 预览，
  必要时重跑对应 job，禁止在 `rebuild` 里偷偷调用模型。
- 每次重建都会刷新 `companion_projection_state` 的 `projector_version` 与 `cursor_seq`，
  因此"这条状态是谁、用哪版投影器算出来的"可回溯。

## 5. reset 的作用范围

```powershell
python -m gacore.companion.cli reset --dry-run
```

- 只输出"会删除多少行"的清单（events / reflections / commitments / goals / activities /
  preferences / outreach），**不带 `--dry-run` 会直接拒绝执行**。
- 真正的删除按以下顺序手工执行（先 `export` 备份）：
  1. 停止 gacore；
  2. 备份 `data/companion.db`（连同 `-wal`/`-shm`）；
  3. 按 actor 删除子表 → 删 `companion_event`；
  4. 重启后由投影器重建关系/Mood（若无事件则自然为空）。
- 不动 `memory/daily`、向量库、`qq_chat_log` 与日志：它们属于旧链路，不归重置范围。

## 6. 信任边界

| 类别 | 例子 | 允许出现在 | 禁止 |
|---|---|---|---|
| TrustedContext | 身份/角色、时间锚、权限、投影状态 | system prompt、`wrap_trusted()` 块 | 不得由外部文本拼入 |
| UntrustedData | 网页、OCR、工具输出、日记正文、用户文本 | user message 里的 `wrap_untrusted()` 数据块 | 不得插入 system 指令正文 |

`companion/trust.py` 提供两个包装器；`reflection`/`journal`/`outreach` 的动态材料一律走
user message 的数据块，system prompt 只承载规则与版本常量。

## 7. 已知边界（V2 未覆盖）

- 桌宠 / Live2D / TTS 未接入，DSH Runtime 未耦合。
- 多 owner、群聊关系、通用 Outbox 不在范围。
- 高风险动作（发信、写文件、支付、账号操作）永远不会自主执行：`ALLOWED_TOOLS` 只读。
- 真实两周观察期从 2026-10-30 首篇日记起算，结论以 `outreach_feedback` 与人工观感为准。
