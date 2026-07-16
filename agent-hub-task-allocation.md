# Agent Hub 多 Agent 开发任务分配

## 1. 模型与角色

| 开发 Agent | 模型 | 主要角色 | 最终审查人 |
|---|---|---|---|
| Claude Code | Claude Opus 4.8，high；架构和疑难审查使用 xhigh | 架构契约、Planner、React Flow GUI | Codex |
| Codex | GPT-5.6 xhigh | 核心运行时、安全、数据库、API、最终集成 | Claude Code 交叉审查高风险改动 |
| OpenCode | MiMo-V2.5-Pro | CLI Runner、OpenCode Adapter、Console 流 | Codex |
| Hermes | MiMo-V2.5-Pro | Context/Artifact、测试体系、失败注入、CI 和验收 | Codex 或对应模块 Owner |

Claude Opus 4.8 固定配置到 Claude Code。Codex 是 integration owner，只有 Codex 执行最终跨模块合并和发布验收。

## 2. 协作规则

1. 首个任务 `HUB-000` 完成前，其他 Agent 不写业务代码。
2. `HUB-010` 协议冻结前，只允许开发脚手架、调研报告和 fixture。
3. 每个任务使用从最新 `main` 创建的独立 branch/worktree。当前活动任务为 `agent/hermes-context-artifacts` / `E:\agent_hub_worktrees\hermes-context-artifacts`；历史 Agent worktree 基于重写前提交，只保留证据，不能复用。
4. 同一文件只能有一个 Owner；Reviewer 只提交审查意见，不直接抢改 Owner 文件。
5. 每个任务必须携带 base commit、允许路径、验收标准和测试命令。
6. 每个任务一个提交，格式：`type(HUB-xxx): summary`。Agent 不直接 merge 或 push。
7. 任务完成时输出：修改文件、测试结果、残余风险、建议后续任务。
8. 协议变更必须先提交 ADR，由 Claude Code 提案、Codex 确认后才能修改冻结模型。

## 3. 阶段任务

### 当前执行看板（2026-07-16）

| 状态 | 任务 |
|---|---|
| completed | `HUB-000`、`HUB-010`、`HUB-020`、`HUB-030`、`HUB-100`、`HUB-120` |
| ready | `HUB-130`：简报、交接提示词和 Hermes 独立 worktree 已准备，等待执行 |
| queued | `HUB-110`：在 `HUB-130` 审查合并后实现 Compiler/Executor/MockAgent 纵向闭环 |
| later | `HUB-200` 及后续任务；等待阶段 1 门槛通过 |

当前进度：阶段 0 为 `4/4`，阶段 1 为 `2/4`，总任务为 `6/25`。主分支最近一次组合验证为 Python `210 passed, 4 skipped`，前端 Vitest `7 passed`、Playwright `1 passed`，Ruff、构建和依赖审计均通过。

协议冻结点为 `contracts-frozen-v1`。阶段 0 和第一波阶段 1 任务已集成到 `main`；下一执行顺序为 `HUB-130 -> 审查/合并 -> HUB-110 -> 阶段 1 CLI/Mock gate`。HUB-130 简报位于 `development-tasks/next-wave/HUB-130-hermes.md`，交接提示词位于 `development-tasks/handoffs/HUB-130-hermes-prompt.md`；`main` 只用于已审查任务的最终集成。

### 阶段 0：基线与契约

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-000 | L | Codex | Hermes | 初始化 git、Python/Vite 骨架、目录、基础测试命令和数据目录配置 | 无 |
| HUB-010 | H | Claude Code | Codex | 冻结 StrictModel、AuthorGraph/CompiledGraph、Node/Edge、ChangeSet、Approval、Event 和 API 数据契约 | HUB-000 |
| HUB-020 | L | Hermes | Codex | 建立 lockfile、CI、lint、pytest/Vitest/Playwright 基线和 fixture repo | HUB-000 |
| HUB-030 | M | OpenCode | Codex | 验证本机 OpenCode CLI capability、JSON 输出、pure/legacy 差异，产出兼容性报告 | HUB-000 |

阶段门：HUB-010 已经 Codex 审查并提交 `contracts-frozen-v1` tag。阶段 1 可开始；HUB-020/HUB-030 作为阶段 0 收尾与 HUB-100/HUB-120 同波并行，但必须在阶段 1 集成验收前合并。

### 阶段 1：确定性纵向切片

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-100 | H | Codex | Claude Code | SQLite migration、Repository、状态 CAS、idempotency、master/workspace lease | HUB-010 |
| HUB-110 | H | Codex | Claude Code | Compiler、PolicyInjector、ExecutableValidator、NodeRegistry、DurableScheduler、GraphExecutor 和 MockAgent 闭环 | HUB-100 |
| HUB-120 | H | Claude Code | Codex | RuleBasedPlanner、PlannerContextBundle、AgentRouter、Workflow lineage、Planner fallback 和 fixture graph 契约 smoke | HUB-010 |
| HUB-130 | M | Hermes | Codex | ContextPack、TaskContextBundle、ArtifactStore、EventRegistry 及其单元测试 | HUB-100 |

阶段门：CLI 能创建 Session、Plan、Validate，并运行无副作用 Mock workflow。

### 阶段 2：共享工作区与安全链

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-200 | H | Codex | Claude Code | GitManager、WorkspaceTransaction、canonical ChangeSet、LockManager、PatchGuard、CommandGuard 和 RiskClassifier | HUB-110 |
| HUB-210 | H | Codex | Claude Code | ApprovalManager、CapabilityBroker、MergePatch、cancel 线性化和 RecoveryManager | HUB-200 |
| HUB-220 | M | Hermes | Codex | 路径逃逸、dirty workspace、租约过期、测试污染、审批冲突和崩溃恢复测试 | HUB-200 |

阶段门：Mock 写任务完整通过 Guard、Test、Approval、Merge；失败路径恢复 clean。

### 阶段 3：真实 CLI Agent

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-300 | H | OpenCode | Codex | 通用 CliAgentSpec/CliAgentRunner、固定 argv、binary hash、JSONL、timeout/cancel 和进程树回收 | HUB-030,HUB-200 |
| HUB-310 | H | OpenCode | Codex | OpenCode Executor/Planner Adapter、runtime permission、专用 profile、resolved config 验证和 configure-opencode | HUB-300 |
| HUB-320 | M | Hermes | OpenCode | fake CLI、流式输出、脱敏边界、异常退出和兼容版本测试 | HUB-300 |
| HUB-330 | H | Codex | Claude Code | 对 Adapter 的环境隔离、权限策略、凭据、路径和 subprocess 安全做阻断式审查 | HUB-310,HUB-320 |

阶段门：fake CLI 全测试通过；真实 OpenCode 可完成一次只读规划和一次受控写入尝试。

### 阶段 4：API 与 GUI 数据闭环

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-400 | H | Codex | Claude Code | FastAPI、protected router、scheduler API、WebSocket ticket/续传、并发冲突和同源 serve | HUB-110,HUB-210 |
| HUB-410 | H | Claude Code | Codex | React Flow Author/Compiled 双图、Agent 分配、节点编辑、Validate/Run 和状态同步 | HUB-010,HUB-400 |
| HUB-420 | M | OpenCode | Codex | ConsoleStream、StreamingRedactor、console chunk artifact 与前端实时输出对接 | HUB-310,HUB-400 |
| HUB-430 | M | Hermes | Codex | API 鉴权、幂等、分页、WebSocket 断线续传和前端组件测试 | HUB-400,HUB-410 |

阶段门：GUI 可以完成 Plan、编辑图、分配 Agent、Validate、Run 和审批。

### 阶段 5：核心产品体验

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-500 | H | Claude Code | Codex | Diff/Risk/Approval/Console 面板、workflow history、冲突状态、移动端与可访问性 | HUB-410,HUB-420 |
| HUB-510 | M | Hermes | Claude Code | Playwright 主流程、桌面/移动截图、长文本和断线恢复回归 | HUB-500 |

阶段门：React Flow 保持可编辑核心体验，桌面和移动视口无重叠或文本溢出。

### 阶段 6：硬化与交付

| ID | 等级 | Owner | Reviewer | 任务 | 依赖 |
|---|---|---|---|---|---|
| HUB-600 | H | Hermes | Codex | 将开发计划验收标准映射到自动化测试，补齐取消、崩溃、资源上限和事件回放场景 | HUB-220,HUB-430,HUB-510 |
| HUB-610 | M | OpenCode | Codex | OpenCode 真实 smoke、compatibility manifest、安装配置文档和失败降级验证 | HUB-330,HUB-420 |
| HUB-620 | H | Claude Code | Codex | 全局架构、Planner 输出、CompiledGraph 可视化和 GUI 安全审查 | HUB-500,HUB-600 |
| HUB-630 | H | Codex | Claude Code | 合并所有已审查分支，运行完整测试、E2E、安全检查并生成 Demo 发布说明 | HUB-600,HUB-610,HUB-620 |

## 4. 文件所有权

| Owner | 主要路径 |
|---|---|
| Claude Code | `protocol/`、`master/planner.py`、`master/router.py`、`workflow/graph_model.py`、`web/frontend/`、架构 ADR |
| Codex | `workflow/`（除 graph_model）、`workspace/`、`security/`、`storage/db.py`、`storage/repositories.py`、`migrations/`、`app/`、`web/backend/` |
| OpenCode | `adapters/`、`console/console_manager.py`、`console/console_stream.py`、`console/streaming_redactor.py`、`console/console_repository.py` |
| Hermes | `context/`（不含 `planner_bundle.py`）、`storage/artifact_store.py`、`storage/artifact_repository.py`、`storage/event_registry.py`、`storage/event_repository.py`、`tests/fixtures/`、CI、测试报告和使用文档 |

`console/approval_manager.py` 属于 Codex。跨 Owner 修改必须拆成接口提案和 Owner 实现两个任务。

## 5. 任务交付模板

```text
Task ID:
Base commit:
Goal:
Owned paths:
Forbidden paths:
Required behavior:
Required tests:
Acceptance criteria:
Output: changed files + tests + risks + next step
```
