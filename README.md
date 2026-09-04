# mono — 个人 Python Monorepo

以 uv workspace 统一管理的多项目仓库：`apps/` 放可独立运行的应用，`packages/` 放共享包。
全仓规则见 [AGENTS.md](AGENTS.md)，完整背景与迁移路线图见 [docs/monorepo-guide.md](docs/monorepo-guide.md)。

## 快速开始

```powershell
uv sync                            # 安装全部成员与 dev 依赖
uv run pytest                      # 全仓测试
uv run ruff check apps packages tests
tools/scripts/check.ps1            # 一键质量门禁（R7）
```

## 项目地图

### 仓内成员

**apps/**（dev-console 已迁入；其余计划迁入，阶段划分见指导第 7 章）

| 应用 | 来源项目 | 阶段 | 说明 |
|---|---|---|---|
| **dev-console** | dev-console | ✅ 已迁入（2026-09-04） | 服务管理器（首个迁移试点，subtree 保留 9 条历史） |
| withlanggraph | WithLangGraph | Phase 2 | gacore / langTrack 数据管线，旗舰项目 |
| checkself | checkSelf | Phase 2 | 画像与自追踪 |
| generic-agent | GenericAgent | Phase 3 | Agent 框架，前端矩阵最广 |
| claw1 | claw1 | Phase 3 | Agent 项目（ruff + mypy） |
| claw0 | claw0 | Phase 3 | 教学 agent gateway |
| py-wei | py-wei (WeiTracker) | Phase 3 | 桌面行为采集 |
| my-test-crew | my_test_crew | Phase 3 | crewAI JSON 项目 |

**packages/**（共享包，按 Rule of Three 晋升，清单见指导第 6 章）

| 包 | 批次 | 来源 |
|---|---|---|
| amap-sdk | 1 | WithLangGraph 的 geocode / routes / 坐标转换 |
| ga-logging | 1 | checkSelf 脱敏日志 + dev-console JsonlLogger |
| ga-config | 2 | etl_config 深合并 + 模板/local 双文件制 |
| llm-client | 2 | claw1 + checkSelf 的 LLM 封装 |
| sqlite-kernel | 2 | WithLangGraph 迁移函数群（user_version / shadow 表） |
| im-bridge | 3 | QQ/飞书消息桥（按需） |

### 仓外关联项目

| 项目 | 位置 | 关系 |
|---|---|---|
| weiCheckApp | ../weiCheckApp | langTrack 数据采集客户端（Android），经 HTTP 契约对接 |
| xiaozhi-esp32-server-java | ../xiaozhi-esp32-server-java | 独立部署的 Java 服务，不进仓（R1） |
| py-xiaozhi | ../py-xiaozhi | 第三方 fork，不进仓（R1） |
| github/ 下 13 个克隆仓库 | ../.. | 学习参考，结论沉淀为 ADR，不搬代码（R1） |

## 目录结构

```
mono/
├── AGENTS.md               # 全仓规则（宪法）
├── README.md               # 本文件：项目地图
├── pyproject.toml          # workspace 定义 + ruff/pytest 统一配置
├── uv.lock                 # 全仓唯一锁文件
├── apps/                   # 应用（待迁入）
├── packages/               # 共享包（待晋升）
├── tools/scripts/          # 仓库级脚本（纯 ASCII）
├── docs/
│   ├── monorepo-guide.md   # 建仓指导与迁移路线图
│   ├── decisions/          # ADR 架构决策记录
│   ├── playbooks/          # 运维手册（承接 WithLangGraph 的 sre/）
│   └── template-package/   # 新成员包模板
├── tests/                  # 仓库结构性测试（业务测试在各 app 内）
└── data/                   # 运行时数据，gitignore
```

## 文档索引

- [AGENTS.md](AGENTS.md) — 全仓强制规则
- [docs/monorepo-guide.md](docs/monorepo-guide.md) — 建仓指导与迁移路线图（本仓库的立仓依据）
- [docs/decisions/ADR-0001-uv-workspace.md](docs/decisions/ADR-0001-uv-workspace.md) — 为什么选 uv workspace
- [docs/template-package/](docs/template-package/) — 新成员包模板
- [data/README.md](data/README.md) — 运行时数据约定
