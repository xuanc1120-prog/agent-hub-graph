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
3. 每个任务使用从最新 `main` 创建的独立 branch/worktree；历史 worktree 只保留任务证据，不能作为新任务基线。
4. 同一文件只能有一个 Owner；Reviewer 只提交审查意见，不直接抢改 Owner 文件。
5. 每个任务必须携带 base commit、允许路径、验收标准和测试命令。
6. 每个任务保留清晰的提交边界，允许必要修复提交，格式：`type(HUB-xxx): summary`。Agent 不直接 merge 或 push；不为凑单次提交改写他人历史。
7. 任务完成时输出：修改文件、测试结果、残余风险、建议后续任务。
8. 协议变更必须先提交 ADR，由 Claude Code 提案、Codex 确认后才能修改冻结模型。

## 3. 阶段任务

### 当前执行看板（2026-10-21 同步）

接手先读 [修正确认与交接](development-tasks/handoffs/2026-09-20-course-correction.md)。完整任务总表保留为路线图，不是首次 Demo 必须一次完成的清单。

| 状态 | 任务 |
|---|---|
| completed | `HUB-000`、`HUB-010`、`HUB-020`、`HUB-030`、`HUB-100`、`HUB-110`、`HUB-120`、`HUB-130`、`HUB-200`、`HUB-210`、`HUB-220`、`HUB-300`（PR #7，merge `1f1715d`，源 head `eee5241`） |
| integration in progress | 本会话：遗留 TestRunner 生命周期/输出修复与纠偏资料，分支 `codex/integrate-course-correction`；新任务基线须包含这些修复 |
| next primary task | `HUB-400` 最小 API 切片由用户另开的 Codex 会话实施；先接 Mock，不等待 HUB-310；HUB-410 随实际可用接口接最小 GUI |
| independent follow-up | OpenCode：HUB-310 真实 Adapter；Hermes：HUB-320 按缺口补测，随后接 HUB-430 最小 API 验收；HUB-330 在真实执行入口启用前审查 |

当前进度：阶段 0 为 `4/4`，阶段 1 为 `4/4`，阶段 2 为 `3/3`，总任务已集成为 `12/25`。`HUB-200` 的最终独立复审结论为无 P0/P1/P2，源提交 `0e82e694` 已通过 merge commit `d9d60ddd` 合入 `main`；`HUB-210` 已通过 PR #3 合入 `b5ee36e`；`HUB-220` 已通过 PR #4 合入 `540b57f`，收尾文档已通过 PR #5 合入 `dc789dc`。HUB-300 已通过 PR #7 合入 `1f1715d`，源 head `eee5241` 的 Python 3.11/3.12/3.13 与前端 CI 均通过。

协议冻结点为 `contracts-frozen-v1`。阶段 2 历史 gate 保持关闭；本轮修正是增量维护，不重开旧审查。下一里程碑为“选择测试仓库 → 简单图 → 执行 → diff → 批准/拒绝”，可先 Mock 再接一个真实 Agent。API/UI 与 Adapter 按人力顺序或并行推进，不强制并行，也不设无依赖的硬等待。完整恢复面板、WebSocket 续传、移动端精修后置。`12/25` 只表示已集成任务数，不表示产品可用度。

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

阶段门：已通过。Mock 写任务完整通过 Guard、Test、Approval、Merge，失败路径恢复 clean；HUB-220 Phase A/B 已覆盖路径、租约、测试污染、审批 CAS、Merge/Cancel 竞争和 RecoveryManager 故障注入。

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

`workflow/approval_manager.py` 属于 Codex。协议、安全语义或并发事务边界变更先记录接口影响；小型跨模块修复可由集成负责人在同一任务协调完成，列明涉及路径，不强制拆成两个任务。本轮用户已授权 Codex 修复审阅中确认的问题。

### 开发期验收分层

- 文档：检查状态、链接及 diff，不重建环境或跑全量测试。
- 普通代码：相关 lint/format、受影响行为测试，不要求待开发功能提前通过验收。
- 文件写入、审批、凭据、进程生命周期：增加直接覆盖改动边界的负向测试，保留关键回归。
- 合并/发布：使用完整 CI；按实际支持范围验证跨平台兼容性。历史“无 P0/P1/P2”只代表当次审阅，不是每个开发任务的零缺陷承诺。

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
