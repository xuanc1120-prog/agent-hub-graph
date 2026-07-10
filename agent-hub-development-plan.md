# Agent Hub：多 Coding Agent 可视化调度平台开发方案

## 1. 项目定位

项目名：`agent-hub`

这是一个 **多 Coding Agent 可视化调度平台**，目标不是重新做一个 Coding Agent，而是做一个统一平台，用来连接、调度、审查 Claude Code、Codex、OpenCode、Aider 等不同 Coding Agent。

它可以理解为：

- Multi Coding Agent Hub
- 多 Coding Agent 调度平台
- 可视化 AI 编程工作流编排平台
- ComfyUI for Coding Agents

这里的 ComfyUI 类比指的是**可视化流程编排和依赖关系表达**，不是要求把所有 Coding Agent 的输出都标准化成同一种数据流。Agent 脱敏后的原始输出、日志、长文本报告和非结构化分析应作为 artifact 保存；节点之间传递的是标准化任务元数据、状态、diff/report 引用和风险结果。

项目要解决的问题：

用户不需要在 Claude Code、Codex、OpenCode 等多个工具之间来回切换。用户在本平台输入任务，Master 负责拆任务、分配 Agent、保存上下文、审查 diff、控制权限和风险。

---

## 2. 核心目标

用户可以在一个平台里完成：

1. 输入开发任务。
2. 让 Master 自动拆解任务。
3. 生成类似 ComfyUI 的可视化节点图。
4. 用户可以拖动节点、连接流程。
5. 用户可以把 Claude Code / Codex / OpenCode 拖到任务节点上分配任务。
6. 未分配的节点由 Master 自动分配。
7. 执行过程中显示每个 Agent 的日志、输出、diff、风险和审批请求。
8. 高危操作必须走提权审批。
9. 所有代码修改必须经过 diff / patch / 安全检查 / 测试 / 审批。

---

## 3. 核心原则

必须严格遵守：

1. Master 是唯一调度中心。
2. Master 可以包含 `PlannerAgent` / `Planning Engine`，用于拆分任务、生成任务节点、推荐 Agent，但它只能产出结构化 workflow 草案，不能直接执行代码修改、shell 命令或 merge。
3. Master 的执行层必须是确定性的状态机：`DraftValidator`、`WorkflowCompiler`、`PolicyInjector`、`ExecutableValidator`、`AgentRouter`、`GraphExecutor`、`ApprovalManager`、`CapabilityBroker` 共同决定是否能执行。
4. Agent 不能互相直接调度。
5. Agent 只能执行 Master 分配的任务节点。
6. Agent 可以查看上下文、日志、diff、测试结果，但不能越权行动。
7. 用户可以通过节点图手动分配任务，也可以让 Master 自动分配。
8. Agent 的修改必须经过 PatchGuard / CommandGuard / TestGate / Approval。
9. 高危操作不能自动执行，必须提交提权请求。
10. 提权不是给 Agent 永久权限，而是给当前 task 一次性、限时、限范围的能力授权。
11. Agent Console 可以显示 Agent 的执行界面，但不能绕过 Master。
12. Demo 阶段使用 SQLite + 本地文件系统 + Git CLI。
13. 第一版可行 demo 采用 CLI + GUI 结合：CLI 跑通执行闭环，React Flow GUI 是核心产品体验，必须支持节点图展示、基础编排编辑、Agent 分配、节点状态、console、diff 和 approval。
14. Demo 阶段与其他 Agent 平台的连接方式必须先实现 CLI 连接层：Master 通过本地 CLI Adapter 启动、监听和回收 Agent 子进程。
15. 第一版真实 Agent 只接入 OpenCode CLI；Codex / Claude Code / Aider 先作为 CLI Adapter 骨架或 disabled agent 展示。
16. 用户和 Planner 编辑的是 `AuthorGraph`；后端通过确定性的 `WorkflowCompiler` 生成 `CompiledGraph`，只有经过校验并固化到 run snapshot 的 `CompiledGraph` 可以执行。
17. 每个 `node_type` 只能由一个明确的 `NodeHandler` 执行；安全节点不能既出现在图中，又被隐藏在 `agent_task` 内重复执行。
18. workflow 定义不保存运行状态；所有运行状态只存在于 `workflow_runs` 和 `node_runs`。
19. 用户和 Planner 提供的 `risk_level_hint`、`allowed_files_candidate`、`new_files_candidate`、`allowed_commands_candidate` 都只是候选范围，不能降低系统策略给出的最低风险和权限边界。
20. Demo 安全目标是约束可信 CLI Agent 中由模型发起的工具操作和意外越权，不声称抵御恶意本地用户、管理员、被篡改的 CLI 可执行文件或操作系统级攻击。

---

## 4. 总体架构

```text
用户 / Web GUI / CLI
        ↓
Master Planner / PlannerAgent
        ↓
WorkflowDraft
        ↓
生成用户可编辑 AuthorGraph
        ↓
用户拖动节点 / 连线 / 分配 Agent
        ↓
保存 draft（允许暂时不完整）
        ↓
DraftValidator 结构校验
        ↓
WorkflowCompiler / PolicyInjector 自动补安全节点
        ↓
ExecutableValidator 执行级校验
        ↓
CompiledGraph 预览，系统节点只读
        ↓
用户确认 + run 前重新编译
        ↓
固化 AuthorGraph / CompiledGraph 不可变快照
        ↓
Graph Executor
        ↓
NodeHandler
        ↓
TaskPackage + ContextPack / ChangeSet / Approval
        ↓
OpenCode / Mock Agent / Disabled Agent Skeletons
        ↓
Shared Session Workspace / Task Context & Runtime Bundle
        ↓
Artifact Store / SQLite Event Log
        ↓
PatchGuard / CommandGuard / TestGate / RiskClassifier / Approval
        ↓
Agent Console 展示执行过程
        ↓
用户确认 / 拒绝 / 安全恢复 / Master 本地合并
```

---

## 5. 系统分层

项目分 10 层：

1. 用户入口层：CLI / Web GUI
2. Master 任务规划层
3. Workflow Graph 节点图层
4. 任务分配层：Agent Assignment
5. 上下文通信层：Context Bridge
6. Agent Adapter 层
7. 共享工作区与 Git 管理层
8. 日志与产物层：Artifact Store
9. 安全边界层：Policy / Guard / Approval
10. Agent Console 审查界面层

---

## 6. 用户使用流程

用户输入：

```text
帮我修复登录接口 Redis 缓存失效问题。
```

系统流程：

1. Master 分析用户目标。
2. Master 自动拆解任务。
3. Master 生成节点图。
4. 节点图展示给用户。
5. 用户可以拖动节点、连接流程、删除节点、新增节点。
6. 用户可以把 Codex / Claude Code / OpenCode 拖到任务节点上。
7. 用户可以锁定某个节点必须由某个 Agent 执行。
8. 未分配节点由 Master 自动分配。
9. 用户保存 `AuthorGraph`；编辑中的不完整草图可以保存，但不能运行。
10. `DraftValidator` 检查节点、边、ID、引用和规模限制。
11. `WorkflowCompiler / PolicyInjector` 确定性插入安全节点，生成 `CompiledGraph`。
12. `ExecutableValidator` 检查最终可执行路径，并把只读系统节点展示给用户。
13. 用户确认后，后端在 run 前按当前 semantic_version 重新编译并固化不可变快照；纯布局变化不影响执行确认。
14. Graph Executor 只执行快照中的 `CompiledGraph`。
15. 每个 Agent 和系统节点的执行过程显示在 Agent Console。
16. 高危操作进入审批。
17. 任务完成后显示 diff、日志、测试结果和最终报告。

---

## 7. 节点图核心设计

### 7.1 节点不要写死成某个平台 Agent

不要直接设计成：

```text
CodexNode
ClaudeCodeNode
OpenCodeNode
```

正确设计是：

```text
TaskNode + assigned_agent
```

也就是说：

- 任务节点表示“要做什么”。
- Agent 分配表示“由谁来做”。

AgentOutputEnvelope 中的 `PrivilegeRequestProposal` 示例；task/session/node/agent ID 由 Master 持久化时补充：

```json
{
  "id": "node_fix_cache",
  "node_type": "agent_task",
  "task_kind": "implement",
  "title": "修复 Redis 缓存降级逻辑",
  "assigned_agent": "opencode",
  "assignment_mode": "manual",
  "instruction": "根据分析结果修复 Redis client 初始化失败和登录缓存降级逻辑",
  "allowed_files_candidate": [
    "src/auth/login.py",
    "src/cache/redis_client.py"
  ],
  "new_files_candidate": [],
  "allowed_commands_candidate": [
    ["pytest", "tests/test_auth.py"]
  ],
  "risk_level_hint": "L2"
}
```

这样同一个任务节点可以在当前可用 Agent 之间改派；disabled Agent 只能展示，不能保存为可执行分配。

### 7.2 节点分配模式

每个任务节点有三种分配模式：

```text
auto：Master 自动分配
manual：用户手动分配
locked：用户锁定分配，Master 不能改
```

示例：

```json
{
  "id": "node_review",
  "node_type": "agent_task",
  "task_kind": "review",
  "title": "Review 本次修改",
  "assigned_agent": "mock",
  "assignment_mode": "locked"
}
```

### 7.3 用户交互

GUI 需要支持：

1. Master 自动生成初始任务节点图。
2. 用户拖动节点位置。
3. 用户新增节点。
4. 用户删除节点。
5. 用户连接节点。
6. 用户删除连线。
7. 用户从左侧 Agent 列表拖 Agent 到任务节点上。
8. 用户设置节点 `assigned_agent`。
9. 用户设置节点 `assignment_mode`。
10. 用户修改 `allowed_files_candidate`。
11. 用户修改明确的新建路径 `new_files_candidate`。
12. 用户修改 argv 形式的 `allowed_commands_candidate`。
13. 用户设置 `risk_level_hint`，只能提高风险提示。
14. 用户保存工作流。
15. 用户查看并确认后端生成的 CompiledGraph。
16. 用户运行工作流。

### 7.4 节点类型

Demo 支持这些节点类型：

```text
input：用户输入节点
agent_task：Agent 执行任务节点
context_builder：上下文构建节点
patch_guard：diff / patch 检查节点
command_guard：命令检查节点
test：测试节点
risk_classifier：风险分类节点
approval：用户审批节点
merge_patch：合并 patch 节点
output：输出报告节点
if：条件分支节点
```

其中 AuthorGraph 允许用户创建和连线的节点只有：

```text
input / agent_task / context_builder / if / output
```

`patch_guard / command_guard / test / risk_classifier / approval / merge_patch` 只存在于 CompiledGraph，由 Compiler 根据 `agent_task` 的候选权限、测试 argv 和策略确定性生成。用户通过编辑任务节点上的候选字段改变编译结果，不能直接创建、删除或绕过这些系统节点。

后续可扩展：

```text
retry
loop_until
parallel
join
custom_shell
custom_python
webhook
mcp_tool
```

`PrivilegeRequest` 是运行时记录和 AgentTask side gate，不是 AuthorGraph/CompiledGraph 中可执行的 node_type。运行中出现提权请求时不得修改已固化图结构。

分析、实现、Review、文档等所有需要 Agent 的工作统一使用 `agent_task + task_kind`，不再创建独立 ReviewNode。这样 Agent 分配、ContextPack、runtime policy 和结果协议只有一套。

---

## 8. 节点图编译与执行策略

用户编辑的是 `AuthorGraph`，执行的是后端确定性生成的 `CompiledGraph`。两者必须分别保存，不能把系统注入节点混入用户草图后再让用户任意改线。

```text
AuthorGraph
  ↓ DraftValidator
NormalizedAuthorGraph
  ↓ WorkflowCompiler / PolicyInjector
CompiledGraph
  ↓ ExecutableValidator
ValidatedCompiledGraph
  ↓ snapshot
GraphExecutor + NodeHandlers
```

每次 `validate` 和 `run` 都必须在短时 workspace lease 下确认 repo clean、固定 `integration_base_commit`，再从 `AuthorGraph + policy_version + agent_catalog_snapshot + integration_base_commit` 重新编译。编译结果使用确定性节点 ID；相同四项输入必须生成相同 `CompiledGraph` 和 hash。AgentRouter 在编译期解析所有 Agent 分配，执行期不得重新路由。

### 8.1 DraftValidator 检查

`DraftValidator` 只检查草图结构，不要求编辑中的图已经满足全部安全路径：

1. 节点和边 ID 唯一。
2. 边引用的节点存在。
3. `node_type` 已注册。
4. 没有非法环；Demo 只接受 DAG。
5. 节点数不超过 100，边数不超过 300，单字段长度和 graph JSON 大小受限。
6. `agent_task` 的 assignment 配置合法。
7. 路径和命令字段能通过语法级解析，但此阶段不授予权限；文件路径必须精确、无通配符，existing/new candidate 不重叠。存在性由 Compiler 相对每个节点的 projected file state 判断，而不是一律相对 run 初始 base。
8. 不允许 AuthorGraph 设置 `system_managed`、`source_node_id`、`system_rule_id`、`resolved_agent_id`、`resolved_agent_spec_sha256`、`effective_allowed_files`、`effective_new_files`、`effective_allowed_commands`、`policy_risk_floor`、`requires_changeset_approval`、`test_kind` 或 `test_argv`，这些字段只能由 Compiler 生成。
9. AuthorGraph 禁止直接创建 `patch_guard / command_guard / test / risk_classifier / approval / merge_patch` 等 system-only node_type；PrivilegeRequest 只允许作为运行时记录。用户通过候选测试 argv 和 hint 表达意图，由 Compiler 生成静态系统节点。
10. `agent_task` 必须设置 task_kind；其他 node_type 不允许携带 task_kind 或 Agent assignment 字段。
11. `agent_task.allowed_commands_candidate` 中的每一项必须是 argv 数组并通过语法级校验；`test_kind/test_argv` 只能由 Compiler 写入 CompiledGraph。if 节点必须使用白名单 IfCondition，禁止自然语言/脚本条件。
12. if 节点 validate 时必须各有一条 matched/not_matched 正常出边，可选一条 failure 错误出边，不能用 success/failure 表示真假；非 if 节点不能发出 matched/not_matched。编辑中的不完整 draft 仍可保存，但不能 Compile/Run。

不完整草图可以保存；只有通过完整编译和执行级校验后才能运行。

### 8.2 WorkflowCompiler / PolicyInjector

如果用户连了：

```text
Codex 修改代码 → Output
```

系统必须在 `CompiledGraph` 中自动改成：

```text
Codex 修改代码
  ↓
PatchGuard
  ↓
CommandGuard
  ↓
Test
  ↓
RiskClassifier
  ↓
Approval
  ↓
Merge
  ↓
Output
```

规则：

1. 任何产生 diff 的 `agent_task` 后必须接 `patch_guard`。
2. `test_kind=command` 前必须接 `command_guard`；`test_kind=docs_static` 只允许 Compiler 为纯文档 scope 生成，执行内建校验且不启动进程。Demo 不提供通用 `run_command` 节点。
3. L0 只读节点不需要审批。
4. 每个可合并 ChangeSet 都必须在 `merge_patch` 前生成一次最终 `ChangeSetApproval`；L1 写入不再增加额外的执行前风险审批，但不能省略这次合并确认。
5. L2/L3 ChangeSet 使用同一个最终 `ChangeSetApproval`，但 evidence、风险说明和 scope 必须完整展示；执行期间请求新增能力时，另走 runtime `PrivilegeApproval`。
6. 任何 `merge_patch` 前必须接来源于同一写任务的 `approval`，且运行时绑定同一个 ChangeSet/base/patch/evidence hash。
7. AgentResult 中的任何 `PrivilegeRequest` 必须创建 runtime `PrivilegeApproval` side gate；不得动态修改 CompiledGraph。
8. 不引入 `auto-approve` 语义；只有不产生 ChangeSet 的 L0 只读路径可以没有审批节点。省略额外前置审批不等于省略最终 ChangeSetApproval，也不能伪造“用户已批准”的记录。
9. `risk_classifier` 必须是已注册的系统节点，并位于 PatchGuard / Test 之后、Approval 判定之前。
10. Demo 不提供通用 `run_command` 节点；`test` 节点的命令必须先经过 `command_guard` 并由 Master TestRunner 执行。OpenCode Planner/Executor 的 bash 永久 deny，候选测试 argv 不转换成 Agent shell 权限。
11. 用户或 Planner 给出的风险只能提高，不能降低 `PolicyEngine` 的最低风险：`effective_risk = max(user_hint, classifier_result, policy_minimum)`。
12. 系统节点使用确定性 ID，并标记 `system_managed=true`；前端只读展示，不能删除或重连其内部安全边。
13. Compiler 为代码/配置写入任务的每个获准测试 argv 生成串行 command test；如果没有任何通过 CommandGuard 的 argv，则生成确定性的阻断 test 并返回 `blocked_by_guard/test_command_missing`。只有 task_kind=docs、effective scope 全部匹配 `.md/.mdx/.rst/.txt`，且不命中 requirements/constraints/manifest/config/policy 等敏感 basename 时可生成 docs_static test，检查 UTF-8、非空、无 NUL、单文件/总大小和基本链接格式；混合路径不能借此跳过命令测试。
14. Compiler 为每个系统节点写入 `source_node_id` 和 `system_rule_id`，保证 PatchGuard/Test/Risk/Approval/Merge 都追溯到同一个写入型 agent_task。
15. Compiler 调用 AgentRouter：auto 根据 agent catalog 解析；manual/locked 校验用户选择。结果和 agent spec hash 写入 CompiledGraph。snapshot 后 Agent 不可用只能 blocked_by_guard，不能重新选择。
16. Compiler 分别校验 existing/new 精确路径并与 PathPolicy/系统策略求交，固化为 `effective_allowed_files / effective_new_files`；二者并集才是 OpenCode edit 最大范围，PatchGuard 分别校验 modified/created。命令通过 CommandGuard 后固化 `effective_allowed_commands / policy_risk_floor / requires_changeset_approval`。TaskPackage 只能从这些 compiled effective 字段构造，执行时不得重新采用 AuthorGraph 候选值。
17. 每个写入型 agent_task 使用独立、不可共享的密封安全链。Compiler 暂存该节点原有 success 出边，改接为 `agent_task -> guard/test/risk -> approval(approved) -> merge_patch -> 原 success 后继`；原 failure 出边保留。approval rejected、guard/test/risk/merge failure 若没有用户显式终止分支，则确定性接到唯一 output 的终止报告分支，绝不能触发原 success 后继。
18. Demo 不支持把另一个用户 agent_task 插入 ChangeSet 安全链内部；AuthorGraph 中写任务的直接 success 后继经过编译后只会在 Merge 成功后运行。需要 Agent 参与 pre-merge review 属于后续可扩展的专用 review gate，当前由 GUI 中的人类 diff/risk/approval 审查承担。
19. 因 Demo 是无 fan-out/join 的单序列路径，Compiler 对每条 outcome 路径维护 `ProjectedFileState`：初始为 run base tree，成功 Merge 后只加入该任务声明的 new_files_candidate。后续 `allowed_files_candidate` 可以引用在所有到达路径上由支配前序声明创建的文件；`new_files_candidate` 必须在对应 projected state 中不存在。Compiler 不预测 Agent 是否真的创建、删除或 rename 文件；每个 AgentTask 启动前仍以最新 integration HEAD 复核，缺失/已存在冲突时 blocked_by_guard，不自动改权限意图。

### 8.3 ExecutableValidator 检查

`ExecutableValidator` 对最终 `CompiledGraph` 棘手路径做全路径校验：

1. 有且只有合法 input/output 边界。
2. 无孤立节点和未知 `node_type`。
3. 每个写入型 `agent_task` 的所有成功路径都经过 `patch_guard`。
4. 每个 command test 前都经过 `command_guard`；docs_static test 必须来自 docs task、只覆盖纯文档 effective scope 且无可执行 argv。
5. 每个可合并 ChangeSet 路径都经过图中的最终 `approval`；L2/L3 必须携带增强风险 evidence，L4 直接拒绝。动态 PrivilegeRequest 由 AgentTask runtime side gate 强制审批。
6. 每个 `merge_patch` 前都有来源于同一 `source_node_id` 的 approval 数据依赖；运行时 Approval 再绑定该 source node 实际生成的 `change_set_id` 和 patch hash。
7. 失败、拒绝和 guard-blocked 路径能到达 output 终止报告分支或明确终止；它们不能汇入 Merge 后的原 success 后继。
8. 禁止用户控制边绕过系统安全链。
9. 每个 agent_task 都有合法 resolved_agent_id/spec hash 和完整 effective permission 字段；无法解析、disabled 或权限为空但任务要求写入时拒绝运行，而不是在执行中静默换 Agent。
10. 编译后节点不超过 300、边不超过 600，且所有 source_node_id/system_rule_id 引用合法。
11. Demo 每个节点对同一 outcome 最多一条可激活出边，禁止两条 success 或两条 failure 造成 fan-out；if 的 matched/not_matched、approval 的 approved/rejected 等互斥 outcome 各一条允许。if 还可有一条只处理求值错误的 failure 边，不能把条件不匹配伪装成失败。所有可能 active 的路径仍必须是单序列，不能伪装 parallel。
12. matched/not_matched 只能来自 if，approved/rejected 只能来自 approval；其他 condition/node_type 组合拒绝，避免永远无法满足的边。

### 8.4 NodeHandler 唯一执行语义

Demo 必须注册以下处理器：

```text
input              -> InputNodeHandler
context_builder    -> ContextBuilderNodeHandler
agent_task         -> AgentTaskNodeHandler
patch_guard        -> PatchGuardNodeHandler
command_guard      -> CommandGuardNodeHandler
test               -> TestNodeHandler
risk_classifier    -> RiskClassifierNodeHandler
approval           -> ApprovalNodeHandler
merge_patch        -> MergePatchNodeHandler
if                 -> IfNodeHandler
output             -> OutputNodeHandler
```

`AgentTaskNodeHandler` 只负责调用 Agent、捕获输出和生成 `ChangeSet`，不能在内部再次执行 PatchGuard、Test、RiskClassifier、Approval 或 Merge。每个系统节点的输入输出都通过 artifact / typed reference 连接，并分别产生 node_run 和 event。

`OutputNodeHandler` 在非取消路径生成最终报告 artifact，但不会把失败伪装成成功：正常 success/approved 路径返回 `outcome=success`；rejected 路径生成“用户拒绝、未合并”报告后返回 `outcome=rejected`；failure/blocked 路径保留对应 outcome。GraphExecutor 仅在 output outcome=success/rejected 且无未完成 active 节点时把 workflow 标记 completed；output outcome=failure/blocked 分别映射为 failed/blocked。用户 cancel 直接终止 run，不再调度 OutputNode。

---

## 9. Master 自动拆任务与用户手动调整

Master 负责根据用户目标生成初始图，但 Master 不应该变成一个自由执行的 Agent。

正确边界是：

```text
PlannerAgent / Planning Engine：负责智能拆任务，生成 WorkflowDraft
DraftValidator：负责草图结构校验
WorkflowCompiler / PolicyInjector：负责确定性补安全节点
ExecutableValidator：负责最终执行路径校验
AgentRouter：负责规则化推荐 / 选择 Agent
GraphExecutor：负责确定性状态机调度执行
```

也就是说，Master 可以有智能规划能力，但 PlannerAgent 只能产出结构化草案，不能直接执行节点、调用 Agent、修改文件、运行命令或合并代码。

PlannerAgent 输出格式必须是结构化 `WorkflowDraft`：

```json
{
  "schema_version": "1",
  "session_id": "session_001",
  "goal": "修复登录接口 Redis 缓存失效问题",
  "planner_id": "opencode_planner",
  "planner_type": "open_code",
  "nodes": [
    {
      "id": "input_goal",
      "node_type": "input",
      "title": "用户目标"
    },
    {
      "id": "analyze_auth_cache",
      "node_type": "agent_task",
      "task_kind": "analyze",
      "title": "分析登录缓存相关代码",
      "requires_write": false,
      "recommended_agents": [
        {"agent_id": "opencode", "score": 92, "reason": "需要读取并分析相关源码"},
        {"agent_id": "mock", "score": 20, "reason": "仅用于无凭据回退演示"}
      ]
    },
    {
      "id": "fix_auth_cache",
      "node_type": "agent_task",
      "task_kind": "implement",
      "title": "修复 Redis 缓存降级逻辑",
      "requires_write": true,
      "risk_level_hint": "L2",
      "allowed_files_candidate": [
        "src/auth/login.py",
        "src/cache/redis_client.py"
      ],
      "allowed_commands_candidate": [
        ["pytest", "tests/test_auth.py"]
      ],
      "recommended_agents": [
        {"agent_id": "opencode", "score": 96, "reason": "需要生成受控代码修改"}
      ]
    },
    {
      "id": "output_report",
      "node_type": "output",
      "title": "生成执行报告"
    }
  ],
  "edges": [
    {
      "id": "edge_input_analyze",
      "from_node": "input_goal",
      "to_node": "analyze_auth_cache",
      "condition": "success"
    },
    {
      "id": "edge_analyze_fix",
      "from_node": "analyze_auth_cache",
      "to_node": "fix_auth_cache",
      "condition": "success"
    },
    {
      "id": "edge_fix_output",
      "from_node": "fix_auth_cache",
      "to_node": "output_report",
      "condition": "success"
    }
  ],
  "assumptions": [
    "这是一个 bugfix 工作流",
    "修改前需要先分析相关模块"
  ]
}
```

WorkflowDraft 必须经过：

```text
PlannerAgent 生成 AuthorGraph 草案
  ↓
用户编辑并保存 draft
  ↓
DraftValidator
  ↓
WorkflowCompiler / PolicyInjector
  ↓
ExecutableValidator
  ↓
用户查看 CompiledGraph 并确认
  ↓
run 前按 expected_semantic_version 重新编译并保存 snapshot
  ↓
GraphExecutor 执行 CompiledGraph
```

Demo 的规划实现包含两层：

1. `OpenCodePlannerAdapter`：在只读、禁止 bash/edit/task/web 的运行时权限下分析目标并生成真实 `WorkflowDraft`。
2. `RuleBasedPlanner`：作为确定性 fallback，至少提供 bugfix、feature、refactor、docs 四种模板。

每次规划都创建 `planner_run`。OpenCode 规划失败后启用 RuleBasedPlanner 时创建新的 fallback planner_run，并通过 `fallback_from_run_id` 关联；两次运行各自保存状态、console、runtime policy、输出 artifact 和事件，不能覆盖失败证据。

同一 Session 的多次规划使用 immutable lineage，不覆盖已有 AuthorGraph：

1. 每个最终成功的 planner_run 都创建新的 workflow row，`semantic_version=1`，并把 `result_workflow_id/result_semantic_version` 写回 planner_run。
2. 用户从现有 workflow 发起 replan 时传 `parent_workflow_id`；新 workflow 保存该 lineage 和 `source_planner_run_id`。旧 workflow、其编辑历史和 run snapshots 保持不变。
3. OpenCode 失败后 RuleBased fallback 时，只有成功的 fallback planner_run 创建 workflow；失败 run 通过 fallback_from_run_id 保留，但不产生半成品 workflow。Planner 输出可先保存 raw artifact，只有通过严格 schema 和最小 DraftValidator 后才持久化 AuthorGraph。
4. 同一 Session 可以有多个 draft workflow，但最多一个 active workflow_run。GUI 默认打开最新 workflow，并提供 workflow/history selector；用户必须明确选择要 Validate/Run 的 workflow。
5. 对同一 workflow 的日常节点编辑仍使用 semantic_version CAS；“重新规划”与“编辑当前图”是两个不同命令，不能用 planner 结果直接覆盖当前 semantic_version。

Planner 输出属于不可信输入，必须使用 `extra="forbid"` 的 Pydantic schema 校验，并限制节点数量、文本长度和可选枚举。Planner 只能推荐 `allowed_files_candidate / new_files_candidate / allowed_commands_candidate`，Master 必须与用户策略和系统策略取交集，Planner 不能给自己或执行 Agent 授权。

例如用户输入：

```text
帮我修复登录接口 Redis 缓存失效问题。
```

Master 生成：

```text
Input
  ↓
分析登录和 Redis 相关模块
  ↓
修复 Redis 降级逻辑
  ↓
PatchGuard
  ↓
CommandGuard / 运行登录测试
  ↓
RiskClassifier
  ↓
Approval
  ↓
Merge
  ↓
Output
```

同时给每个任务节点推荐 Agent：

```text
分析项目结构：推荐 OpenCode / Mock Agent
修改代码：推荐 OpenCode
独立只读 Review：推荐 OpenCode / Mock Agent
合并：Master
审批：用户
```

用户可以：

1. 接受 Master 的自动分配。
2. 手动把 OpenCode 拖到“修复代码”节点。
3. 手动把 Mock Agent 拖到“分析”或独立只读 Review 节点。
4. 锁定某个节点。
5. 删除 Master 生成的某个节点。
6. 手动连线改变执行顺序。

---

## 10. Agent 自动分配逻辑

实现 `AgentRouter`。

`AgentRouter` 只在 `assignment_mode = auto` 时生效：

```text
auto：Master 根据规则和 Agent 可用性自动选择
manual：使用用户选择的 assigned_agent
locked：必须使用用户锁定的 assigned_agent，如果该 Agent 不可用，则节点 blocked_by_guard，不能自动换人
```

AgentRouter 在 validate/run 编译期读取不可变 `agent_catalog_snapshot`。auto 选择结果只写入 CompiledGraph 的 `resolved_agent_id/resolved_agent_spec_sha256`，不覆盖 AuthorGraph；manual/locked 也要解析为相同字段。`auto-assign` API/CLI 只返回预览，不修改 semantic_version；用户若要固定结果，显式保存为 manual/locked。

Demo 阶段 AgentRouter 规则：

```text
requires_write=true 且 OpenCode 可用：优先选择 opencode
requires_write=true 且 OpenCode 不可用：节点进入 blocked_by_guard，并记录 agent_unavailable event，不能静默切换为 mock
task_kind in {analyze, review, docs}：OpenCode 可用时优先 opencode
Mock 只用于自动化测试、fixture 和用户明确选择的模拟运行
output：始终使用 Master 的确定性汇总，不参与 AgentRouter
codex / claude_code / aider：第一版作为 disabled agent skeleton，不参与自动分配
```

根据以下信息打分：

1. 任务类型。
2. 是否需要读大量代码。
3. 是否需要写代码。
4. 是否需要生成 patch。
5. 是否需要跑测试。
6. task_kind 是否为 review/docs/test_fix。
7. 风险等级。
8. Agent 是否可用。

用户历史偏好、成本模型、延迟和 Agent 历史成功率属于后续路由优化；Demo 不为这些尚未采集的数据伪造评分。

初始规则：

```text
分析项目结构 / 架构理解：OpenCode 优先，Mock 仅模拟 fallback
代码修改 / patch 生成：OpenCode 优先
测试失败修复：OpenCode / Mock Agent 优先
安全 Review：OpenCode 优先，Mock 仅模拟 fallback
本地批量修改：OpenCode 优先
文档生成：OpenCode，Mock 仅模拟 fallback
```

输出推荐格式：

```json
[
  {
    "agent_id": "opencode",
    "score": 88,
    "reason": "该节点需要代码修改和 patch 生成"
  },
  {
    "agent_id": "mock",
    "score": 12,
    "reason": "仅在用户明确选择模拟运行时使用"
  }
]
```

---

## 11. 上下文通信设计

不同 Agent 不直接互相聊天。所有上下文都通过 Master 管理。

核心对象是：

```text
ContextPack
```

每个 Agent 执行前，Master 根据当前节点生成 ContextPack。

ContextPack 包含：

1. 用户最终目标。
2. 当前节点任务。
3. 上游节点输出。
4. 上一个 Agent 的关键结论。
5. 相关日志。
6. 相关 diff。
7. 测试结果。
8. 风险提示。
9. 权限边界。
10. 验收标准。

不要只传自然语言摘要，必须同时保存：

```text
脱敏后的原始日志
结构化语义事件
diff / patch / report 引用
```

ContextPack 的标准化范围必须收敛在**任务元数据和 artifact 引用**，不能强行把不同 Agent 的所有输出改造成同一种结构。

第一版 ContextPack 至少标准化：

```text
task_id
node_id
task_kind
session_goal
current_node_title
current_task
upstream_summaries
artifact_refs
effective_allowed_files
effective_new_files
active_capability_grant_id / granted_existing_files（仅 privilege-assisted attempt）
effective_allowed_commands
forbidden_paths
acceptance_criteria
max_prompt_chars
```

非结构化内容处理规则：

1. Agent 输出、长日志、长报告、trace、JSON event stream 必须先脱敏，再进入 artifact store。
2. ContextPack 只保存 artifact id / path / hash / size / type，不直接嵌入大段原文。
3. 上游节点给下游节点传递 `summary + artifact_refs + diff_refs + risk_refs`。
4. 如果 Agent 输出无法解析，不能阻塞 artifact 保存；只将结构化摘要标记为 `parse_status=failed`，原始 artifact 仍然可追踪。
5. `ArtifactRef`、`NodeOutputRef`、`RiskRef`、`TestResultRef` 必须是 typed model，不能用任意 `dict` 重新塞入大日志。
6. ContextBuilder 必须设置字符数、artifact 数量和 token 预算；超出预算时保留引用并生成可追踪摘要，不能无限扩张 CLI prompt。
7. `effective_allowed_files`、`effective_new_files` 和 `effective_allowed_commands` 由 Master 根据用户候选范围、Planner 建议和系统策略取交集后生成，ContextPack 不能自行扩大权限。

CLI Agent 不能反向调用 Master API，因此 ContextBuilder 还必须生成 `TaskContextBundle`：

1. 在 `workspaces/agent-runs/<task_id>/context/` 创建 task 专属目录，只物化本节点选中的脱敏 artifact、摘要和 manifest；quarantine 原件永不物化，只允许脱敏预览。
2. 每个文件使用服务端生成名称，重新校验 ArtifactRef hash/size，拒绝 symlink/hardlink 和路径逃逸。
3. 目录 ACL 只允许当前用户，文件对 Agent 工具只读；runtime policy 的 external_directory 默认 deny，只对 manifest 列出的精确 context 文件开放 read，edit/list/glob 永久 deny。
4. manifest 记录 source artifact id/hash、materialized path、bundle hash 和过期时间；bundle hash 写入 TaskPackage/event。
5. prompt 只内联短摘要和 bundle manifest path，不把长 diff/log 放入 argv。
6. Agent 结束后删除物化副本；原始脱敏 artifact 仍由 ArtifactStore 保存。清理失败写 security_event。
7. 不同 task/session 的 context 目录不能互相读取。

Planner 使用独立 `PlannerContextBundle`，不能直接让 OpenCode Planner 在真实 Session repo 中自由搜索：

1. PlannerContextBuilder 在短暂 workspace lease 内，从固定 base commit 导出经过 PathPolicy 的 tracked 文本文件、目录清单和语言/测试框架摘要；排除敏感路径、二进制、大文件、`.git`、`.env*`、Agent/CLI 项目配置和所有 symlink/reparse point。
2. bundle 写入 `workspaces/agent-runs/<planner_run_id>/planner-view/`，不包含 Git 元数据，设置只读 ACL，并受 `max_planner_files=5000 / max_planner_bytes=20 MiB / max_file_bytes=1 MiB` 限制；超限时只保留 manifest、目录树和按规则选取的入口文件，并写 truncation event。
3. 在 planner_run 从 pending claim 为 running 时取得 lease，固定 clean integration HEAD，生成并校验 bundle manifest/hash，把 base/artifact/hash 写入 planner_run 后释放 lease，再以 planner-view 为 `--dir` 运行 OpenCode Planner；因此长时间规划不会持有共享 repo 租约。RuleBased fallback 复用同一 base 和已验证 manifest，不重新漂移到新 HEAD。
4. Planner 可在 planner-view 内使用 read/glob/grep/list，但 edit/bash/task/web/external_directory 全部 deny。真实 repo 路径、其他 bundle 和 ArtifactStore 不进入 prompt 或权限。
5. Planner 输出中的 candidate path 必须重新映射到真实 repo 并做 canonical PathPolicy 校验；首个写节点相对 base 校验，后续节点相对 Compiler 的 ProjectedFileState 校验。new path 的最近现有祖先必须安全位于 repo 内，缺失父层级只作为该精确 new file 的隐式容器；bundle 中的合成 manifest/摘要路径不能成为可写候选。
6. planner-view 在运行后清理，清理失败写 security_event；原始 manifest/hash 和脱敏规划输出仍以 artifact 保存。

---

## 12. 共享工作区策略

Demo 阶段使用：

```text
demo-only Session 共享集成仓库 + clean-only + 单写租约 + ChangeSet 事务
```

目录：

```text
workspaces/
├── shared/
│   └── <session_id>/
│       └── repo/              # Session 共享集成仓库，只包含已批准本地提交
└── agent-runs/
    ├── <task_id>/runtime/
    └── <task_id>/temp/
```

创建 Session 时，`GitManager` 必须从用户指定的 source repo 和 base commit 创建专用共享集成仓库，并切换到 `agent-hub/session/<session_id>` 本地分支；`sessions.base_commit` 保存最初来源，`integration_head_commit` 初始化为同一 commit，之后只允许 Master Merge 更新。Demo 只允许独立 `--no-hardlinks` 本地 clone，不提供 git worktree 模式，避免共享 source repo 的工作树或 `.git` 元数据。source repo 必须是可解析 base commit 的 Git 仓库，未提交修改不会进入 Session，GUI/CLI 必须明确提示。Demo 不支持 submodule/LFS filter 仓库，检测到 gitlink、`.gitmodules` 或必需外部 filter 时拒绝创建 Session。Agent 不直接修改用户原始工作树；同一 Session 的 Agent 都以该集成分支的最新已批准 commit 为基线，因此能够看到其他 Agent 已接受的修改。

Demo 使用严格 clean-only：执行前只要共享集成仓库存在 staged、unstaged 或非 ignored untracked 变更，就进入 `blocked_by_guard`。ignored 文件可以预先存在，但必须在受监控路径清单中建立 baseline；敏感 ignored 文件只允许存在、禁止 Agent 读取或修改。`.pytest_cache`、coverage、编译缓存等显式 `workspace_ephemeral_paths` 可在测试后按白名单清理；其他 ignored 文件发生变化必须进入 ChangeSet，并由 PatchGuard 直接拒绝合并。本版不实现 dirty-owner 差分扣除，因为重叠文件和未跟踪文件会使 task 归属不可可靠证明。

共享写入区必须满足：

1. 同一 Session 同一时间只有一个有效 workspace 租约；AgentTask、Test、Merge、读取 repo 的 ContextBuilder 和短时 PlannerContextBuilder 必须取得该独占租约，避免任何读取看到写任务的瞬态 dirty 状态。AgentTask 的最小 ContextPack 文件读取可在同一次 AgentTask lease 内完成；OpenCode Planner 只读取 lease 内生成并校验完成的 planner-view，运行期间不持有 repo 租约；纯 artifact Review、Risk、Approval 不持有。租约包含 `owner_kind`、`owner_operation_id`、`owner_process_id`、`lease_expires_at`、heartbeat 和单调递增 `fencing_token`。
2. 取得租约后再次校验 repo path、branch、index 和完整 clean 状态；实际 HEAD 必须等于 `sessions.integration_head_commit`，active run 内还必须等于 `workflow_runs.current_commit`。任何不匹配都视为外部/崩溃漂移并 blocked/orphaned，不能自动把数据库追到文件系统。
3. `AgentTaskNodeHandler` 记录 `base_commit` 和执行前文件清单后才允许启动 Agent。
4. Agent 退出后生成完整 `ChangeSet`，保存 patch、preimage、文件清单和 artifact，再把共享仓库恢复到执行前 clean 状态。
5. 恢复只能依据本 task 的 ChangeSet 逐项处理：tracked 路径使用带显式 pathspec 的 `git restore --source=<base_commit> --staged --worktree`；只删除 ChangeSet 标记为本 task 新建且再次通过 PathPolicy 的 untracked 路径；之后按深度倒序只删除本 task 创建且当前为空的 `created_directories`，非空立即 orphaned，绝不递归删除；ignored 修改使用保存的 preimage 恢复。禁止 `git reset --hard`、无 pathspec 的 restore、`git clean -fdx` 等全局破坏命令。
6. 恢复完成后必须再次验证 HEAD、index、tracked/untracked/ignored 清单与执行前一致；失败则标记 `orphaned` 并保持锁定，要求人工恢复。
7. `PatchGuardNodeHandler` 在 clean workspace 外对不可变 ChangeSet artifact 做检查。
8. `TestNodeHandler` 重新取得租约，在相同 base 上临时应用 ChangeSet 并记录 applied-state test baseline，再执行受控测试。测试结束后捕获相对该 baseline 的 test-side-effect manifest；任何非 ephemeral 修改都使测试 `blocked_by_guard/test_mutated_workspace`，不得并入 Agent ChangeSet。Handler 先按 side-effect preimage/精确新路径恢复测试副作用，再反向应用 Agent ChangeSet，最后验证 base clean；任一步失败都 orphaned。
9. 等待 Approval 时共享仓库必须是 clean，不能持有写租约。
10. `MergePatchNodeHandler` 在批准后重新取得租约，校验实际 HEAD = session integration_head = run current_commit = ChangeSet base、patch hash 和 Approval，再用 CAS 设置 `merge_finalizing_at`，条件是 cancel_requested_at 为空、run/node 状态匹配且 workspace fencing token 有效。该事务是 cancel 的线性化点：设置成功后 cancel 返回 409；设置前已提交 cancel 则 Merge 失败。随后执行 `git apply --check`、应用 patch 并由 Master 创建本地 commit；状态/event 事务同时把 session integration_head_commit 和 run current_commit 更新为新 commit 并清除 finalizing 标记。apply/commit 失败要逐项恢复 clean 并清除标记；进程崩溃则 orphaned，由 RecoveryManager 核对 HEAD、commit trailer 和 ChangeSet。Agent 永远不能 commit、merge 或 push。
11. base 已变化或 patch 无法 clean apply 时进入 `blocked_by_guard`，不能自动三方合并。
12. 测试失败或 Approval 拒绝时保留 ChangeSet artifact，但集成仓库保持 clean，patch 不进入本地集成分支。
13. timeout、用户 cancel 和 Agent 非零退出也必须进入 WorkspaceTransaction 的 capture/restore finally；进程失败不等于可以跳过 workspace 核对。
14. `workspace_ephemeral_paths` 只能来自后端策略注册表，必须位于 Session repo 且不得与 source/effective/sensitive 路径重叠。TestTransaction 记录其中执行前清单，只删除本次新建且逐项通过 no-symlink containment 的 descendants，并恢复 preexisting 文件；不能对整个目录执行无清单递归删除。

### 12.1 ChangeSet 最小内容

```text
change_set_id
session_id / workflow_run_id / node_run_id / task_id
base_commit
pre_state_hash / post_state_hash
canonical_apply_patch_ref / canonical_patch_sha256
status_manifest_ref / staged_evidence_diff_ref / unstaged_evidence_diff_ref
created_files / created_directories / modified_files / deleted_files / renamed_files
untracked_files / ignored_files_touched
file_sha256_before / file_sha256_after
preimage_refs
workspace_ephemeral_paths
captured_at
```

ChangeSet 枚举必须组合使用 `git status --porcelain=v2 -z`、unstaged diff、cached diff、untracked 文件枚举、受监控 ignored 路径扫描和执行前后文件清单，不能只依赖普通 `git diff`。但 staged/unstaged diff 仅作为审计 evidence，不能拼接成 Merge 输入，因为同一文件可能同时含有 index 和 worktree 修改。

唯一可应用 patch 必须通过专用临时 `GIT_INDEX_FILE` 构造：以 `git read-tree <base_commit>` 初始化临时 index，把排序后的精确 changed path 清单通过 NUL pathspec 执行受控 `git add -A -f`，再用 `git diff --cached --binary --full-index --find-renames <base_commit> --` 生成 canonical bytes。该过程不得修改真实 index；生成后在 clean base 上 `git apply --check` 并验证应用结果的 post_state_hash，随后删除临时 index。`patch_sha256` 只对这份 canonical bytes 计算，Approval/Test/Merge 都只能引用它；status/staged/unstaged evidence 不能被应用。超大 ignored/ephemeral 目录必须由策略明确排除且禁止 Agent edit；`.git/**`、submodule 元数据和 repo 外路径一律不允许进入 ChangeSet。

PatchGuard 的 scope 判定按动作区分：modify/delete 的路径必须在 effective existing scope；create 必须在 effective_new_files；rename 的 source 必须在 existing、destination 必须在 new，Demo 不允许覆盖式 rename 到已存在文件。大小写/NFC 规范化后的同路径 rename 直接拒绝，避免 Windows 大小写改名绕过。

### 12.2 崩溃恢复

Master 启动时必须扫描 `running` node_run、`merge_finalizing_at`、过期租约和 dirty 集成仓库。只有在确认 owner 进程已结束、fencing token 未变化且 ChangeSet 足以恢复时才能自动恢复；否则标记 `orphaned` 并阻止该 Session 继续写入。若存在 merge_finalizing，RecoveryManager 还要核对 HEAD commit trailer 中的 session/run/change_set/approval ID 和 tree hash：完全匹配才可在同一事务补写 session integration_head/run current_commit 和完成事件；确认未 commit 且能恢复 base clean 时才可清除标记并等待用户 retry/cancel；其他情况保持 orphaned。任何自动恢复动作都写入 `security_events`。

### 12.3 GitManager 安全执行约束

Master 调用 Git 也必须使用固定绝对 binary path、`shell=False`、显式 cwd 和最小环境：

1. 设置 `GIT_TERMINAL_PROMPT=0`，禁用 askpass、pager、external diff 和 textconv，不允许 Git 弹出交互或读取凭据。
2. 所有命令使用空的 `core.hooksPath`；Master commit 不能执行项目或用户 Git hooks。
3. commit 禁用 GPG signing，使用固定本地身份 `Agent Hub <agent-hub@local>`，message/trailer 记录 session、run、change_set 和 approval ID。
4. 文件操作使用 NUL 分隔的显式 pathspec；禁止无 pathspec 的 restore/add/rm。
5. diff 使用 `--no-ext-diff --no-textconv --binary`，路径解析使用 `-z` 输出。
6. Demo 永久拒绝 Agent 修改 `.gitattributes`、`.gitmodules`、`.git/**`；`.gitignore` 视为 L2，必须单独显示风险。
7. `git push/fetch/pull/merge/rebase/checkout` 不属于 Demo Master 执行白名单；创建 Session 的本地 clone、最终本地 commit 和只读 export-patch 是例外且分别由专用方法实现。
8. Git 子进程设置 `GIT_CONFIG_NOSYSTEM=1`、专用空 HOME/XDG_CONFIG_HOME，并以 `-c` 显式覆盖 credential.helper、core.hooksPath、diff/filter/pager/signing；不读取用户 system/global Git config、alias 或 credential helper。
9. Session 只接受 canonical 本地目录作为 source，不接受 URL/scp-like remote；使用专用方法执行 `git clone --no-hardlinks --no-checkout`，不递归 submodule、不共享 object hardlink，再以隔离配置检出已解析 commit。创建 integration branch 后立即移除 origin/所有 remote 并验证 remote 列表为空，防止后续误 fetch/push 或泄露 source path。
10. 用户 base_ref 必须限长且不能以 `-` 开头，使用 `rev-parse --verify --end-of-options <ref>^{commit}` 解析为 GitObjectId；所有 path 参数放在显式 `--` 后并使用 NUL pathspec。任何不支持 `--end-of-options` 的 Git 版本不得进入已验证兼容列表。

后续升级：

```text
共享主工作区 + 每个 Agent 独立 task workspace + patch 合并
```

---

## 13. 风险边界设计

### 13.1 观察权

Agent 可以看：

1. 当前任务描述。
2. 相关源码文件。
3. 上游节点结果。
4. 指定日志。
5. 指定 diff。
6. 指定测试报告。

默认禁止看：

```text
.env
.env.*
*.pem
*.key
id_rsa
~/.ssh/*
生产配置
其他 session 数据
```

路径策略必须先于文件读取和修改执行：

1. 对 repo root 和候选路径做 canonical resolve，并使用 `commonpath` 验证仍位于 Session repo 内。
2. Windows 下按大小写不敏感和 Unicode NFC 规则比较，拒绝 UNC、设备路径、NTFS alternate data stream、DOS 保留名、尾随点/空格、8.3 别名歧义、junction 和任何 reparse point；POSIX 也拒绝 NUL、控制字符和无法稳定 UTF-8 编码的路径。
3. 拒绝 symlink 文件和任何包含 symlink/junction 的父目录；执行前和捕获 ChangeSet 时各检查一次，防止 TOCTOU。
4. 永久禁止 `.git/**`、`.agent-hub/**` 运行时文件、密钥模式和其他 Session 目录。
5. Demo 的 candidate/effective/readonly file scope 只接受 exact repo-relative file path，禁止 `* ? [] **` 通配符、目录授权和字符串前缀匹配。`allowed_files_candidate` 只允许 projected state 中已存在的普通文件，`new_files_candidate` 只允许 projected state 中不存在的精确路径；其最近现有祖先必须在 repo 内且无 symlink/reparse，缺失中间目录名逐段通过相同 Windows/Unicode 规则，并只隐式授权作为该精确新文件的父容器。existing/new 按 casefold/NFC 规则不能重叠，合并后的 effective scope 最多 100 个文件。

### 13.2 修改权

Demo：

```text
Agent 可以在受控工作区中修改代码
但最终是否接受由 Master 决定
```

必须检查：

```text
effective_allowed_files / effective_new_files
forbidden_files
PatchGuard
RiskClassifier
TestGate
Approval
```

`risk_level_hint` 是用户/Planner 的风险提示，不是授权。`RiskClassifier` 和 `PolicyEngine` 计算 `effective_risk`；用户可以提高风险，但不能把系统判定的 L2/L3 降为 L1。

Demo 的最低风险规则必须配置化、确定性并按最高项取值：

```text
L4：敏感文件、repo 外路径、.git/**、Master/runtime policy、命令/权限绕过意图 -> 直接拒绝
L3：auth/security/payment/permission 代码、binary 变更、删除/rename、超过 20 个 changed paths
L2：依赖 manifest/lockfile、配置/schema/migration、Dockerfile、CI、.gitignore、测试/启动脚本、6-20 个 changed paths
L1：1-5 个 exact scope 内普通源码或纯文档变更
L0：无 ChangeSet 的只读任务
```

Compiler 根据 candidate scope 计算 `policy_risk_floor`；ChangeSet 捕获后 RiskClassifier 再按实际路径/动作/规模计算。`effective_risk = max(policy_risk_floor, user_hint, planner_hint, agent_risk_hint, runtime_classifier)`，其中所有 hint 只能上调。匹配 L4 时不创建 Approval，直接 blocked_by_guard 并写 security_event。

推荐后续：

```text
Agent 只能 propose_patch
Master 检查后 apply_patch
```

### 13.3 命令执行权

默认只允许后端注册的命令模板和参数级白名单。用户填写的 `allowed_commands_candidate` 必须是 argv 数组，并由 `CommandGuard` 校验，禁止通过 shell 字符串执行。

允许：

```text
pytest
python -m pytest
npm test
pnpm test
go test ./...
```

测试命令本身会执行仓库代码，因此 Demo 明确假设 source repo 是用户信任的本地项目。对于不可信仓库，测试节点必须禁用或移入后续容器/OS sandbox 模式。

还必须承认：测试会加载 Agent 刚修改的源码，命令白名单无法证明这些代码安全。Demo 的 TestRunner 必须使用固定 executable/argv、`shell=False`、Session repo cwd、最小环境变量、流式脱敏、输出上限、timeout/cancel 和完整进程树回收，且绝不传入 Hub/API/provider/SSH 凭据；但这些措施不是文件系统或网络沙箱。用户若不信任仓库、已注册 Agent 或待测 ChangeSet，必须禁用测试并让 workflow `blocked_by_guard/test_requires_sandbox`，不能以“跳过测试”继续 Merge。

OpenCode 内部 bash 不由事后日志检查提供安全性；Demo 直接把 Planner/Executor 的 bash 全部设为 `deny`。通过 CommandGuard 的测试 argv 只交给独立 Master TestRunner，headless run 不使用可能等待交互的 `ask`。

禁止：

```text
rm -rf
sudo
curl | bash
wget
chmod 777
git push
cat .env
cat ~/.ssh/id_rsa
docker run --privileged
```

### 13.4 调度权

Agent 不能直接调度其他 Agent。

允许 Agent 输出：

```json
{
  "next_suggestion": {
    "suggested_agent": "codex",
    "reason": "该修改需要另一个 Agent review"
  }
}
```

Demo 运行中只把该建议保存为 artifact/event 并展示，不能动态修改当前 CompiledGraph。用户显式采纳后，Master 可以基于当前 workflow lineage 创建新的 AuthorGraph draft，重新 Compile/Confirm/Run；Agent 的建议本身没有调度权。

### 13.5 合并权

Agent 永远不能直接合并代码。

合并流程：

```text
Agent 修改
  ↓
生成 diff
  ↓
PatchGuard
  ↓
CommandGuard / Test
  ↓
RiskClassifier
  ↓
Approval
  ↓
Master apply + local commit
```

Demo 中 `Master merge` 的精确定义是：Master 在专用 Session 集成分支上应用已批准 ChangeSet 并创建本地 commit。它不修改用户原始分支、不执行 `git push`，最终由用户决定如何取得该本地分支或 patch。

### 13.6 Demo 威胁模型

Demo 信任：

1. 本机操作系统和当前登录用户。
2. 用户主动注册并确认过路径和 hash 的 CLI 可执行文件。
3. 用户指定的 source repo；运行测试意味着信任其测试代码。
4. 用户显式启用测试时，接受已注册 Agent 生成的待测代码会在当前低权限边界内执行；Demo 不把该代码视为敌对程序。

Demo 防护目标：

1. 在受支持 Adapter 的直接工具调用范围内，防止模型误操作或 prompt injection 让 Agent 读取 repo 外文件、执行未授权命令、直接调度、合并或推送。
2. 防止 Agent 意外越过 effective existing/new file scope、污染其他 Session、伪造 Approval 或覆盖用户原工作树。
3. 保证关键状态、变更集、审批和安全事件可审计。

Demo 不承诺防御恶意本地用户、管理员权限进程、恶意/被替换的 CLI binary、被 Agent 写入并由测试间接执行的敌对代码、内核攻击或完全隔离的网络外泄；这些属于后续容器、低权限账户和网络策略范围。文档和 GUI 必须把这一限制展示为运行测试前的安全边界，不能把 CommandGuard 描述成代码执行沙箱。

---

## 14. 高危操作提权机制

高危操作不能自动执行。Agent 只能提交 `PrivilegeRequest`。

示例：

```json
{
  "requested_capability": "modify_dependency",
  "requested_action": "edit_dependency_manifest",
  "requested_resource": "requirements.txt",
  "reason": "需要先声明 Redis 客户端依赖才能完成缓存功能",
  "risk_level_hint": "L2",
  "expected_impact": [
    "可能修改依赖文件",
    "可能影响项目依赖解析"
  ],
  "rollback_plan": "如果失败，回滚依赖文件和本次 patch",
  "related_files": [
    "requirements.txt"
  ]
}
```

提权流程：

```text
Agent 提交 privilege_request
  ↓
Agent 进程结束，WorkspaceTransaction 捕获并恢复 clean
  ↓
RiskClassifier 判断风险等级
  ↓
PolicyEngine 判断是否可申请
  ↓
node attempt.status = waiting_approval
创建 PrivilegeApproval runtime side gate（不修改 CompiledGraph）
  ↓
Agent Console 在当前 agent_task 节点显示审批覆盖层
  ↓
用户批准原请求 / 拒绝
  ↓
批准：旧 attempt = superseded，原子创建新 attempt + target task + 一次性 grant
  ↓
新 attempt 的 runtime policy 只扩展该 grant 的精确 file edit/action；Demo 不扩展 bash
  ↓
原子消费 grant，操作完成后权限立即失效
  ↓
记录 security_event
```

拒绝时，当前 attempt 进入 `blocked_by_guard` 并按 failure 边推进。Demo 不允许用户在 Approval 上编辑 capability/action/resource；只能批准原始 immutable subject 或拒绝。任何范围变更都必须拒绝旧请求，并由新 attempt 重新提出新的 PrivilegeRequest/hash/Approval。Scheduler 只以同一 node_id 的最新非 superseded attempt 判断逻辑节点状态。

如果 privilege_requested attempt 已产生部分 ChangeSet，该 ChangeSet 标记为 `abandoned_partial`，只作为新 attempt 的只读 diff reference，不得进入 PatchGuard/Test/ChangeSetApproval/Merge。新 attempt 从 clean base 重新执行并生成新的 ChangeSet，避免把半成品隐式带入已批准权限范围。

提权原则：

1. 提权不是给 Agent 永久权限。
2. 提权只针对 task + action + resource + time。
3. 默认只能使用一次。
4. 必须有过期时间。
5. 必须记录审计日志。
6. L4 风险直接拒绝，不进入审批。
7. Approval 必须绑定不可变 subject。ChangeSetApproval 绑定 `workflow_run_id`、`node_run_id`、`change_set_id`、`base_commit`、`patch_sha256`、测试/风险 evidence hash、scope 和过期时间；PrivilegeApproval 绑定 `privilege_request_id`、完整 request hash、effective_risk、scope 和过期时间。
8. approve / reject 使用 compare-and-swap：只有 `status=pending` 且 version 匹配时才能决策；approve/reject/expire/invalidate 每次状态变化都在同一 UPDATE 中 `version=version+1`。相同幂等请求返回原结果，不重复推进 GraphExecutor；renew 创建新 approval version=1，不修改旧记录。
9. `CapabilityGrant` 必须绑定精确 file action、已存在 resource、source request、target_task_id 和 expires_at，并通过条件 UPDATE 原子消费一次；target task 必须属于同一 node 的 attempt+1，不能绑定发出请求的旧 task。Demo 的 PrivilegeRequest schema 不接受 argv，grant 永远不开放 bash。如果 action 触碰 workspace，消费事务先取得新租约，再记录该次 `consumed_fencing_token`。
10. ChangeSetApproval 后到实际 apply 前必须重新校验 base commit、patch hash、测试/风险 artifact hash；PrivilegeApproval 消费前必须重新校验 request/action/resource hash。任一变化都使批准失效并重新审批。
11. Demo 每个逻辑节点最多允许 2 次 privilege-assisted attempt，防止提权循环。
12. 每个 attempt 最多提交 1 个 PrivilegeRequest；多个请求使 AgentOutputEnvelope 校验失败，不能创建部分 Approval。
13. Demo 不批准 package install、curl/wget、网络访问、系统级写入等命令；依赖提权只允许扩展对 manifest/lockfile 的精确 edit scope。实际安装命令留到容器/低权限 sandbox 阶段。
14. capability/action 必须严格配对：`modify_dependency -> edit_dependency_manifest`，resource 仅允许 base 中已存在且策略登记的 manifest/lockfile；`modify_config -> edit_project_config`，resource 仅允许 base 中已存在、repo 内非 CI、非 Agent Hub、非 auth/security 的项目配置。自由 action、空 resource、目录 scope、new file 和 glob scope 全部拒绝。

ChangeSetApproval 的 `evidence_sha256` 是 canonical evidence manifest 的 hash，manifest 至少包含 compiled_snapshot_hash、policy_version、ChangeSet patch hash、PatchGuard report hash、全部 TestResult hash、Risk report hash 和 runtime policy hash；privilege-assisted attempt 还必须包含 request/grant hash、target_task_id、resource 和 consumed_fencing_token。Approval 卡片展示这些引用；任一引用变化都需要重新校验和重新审批。

风险等级：

```text
L0：读取普通文件、查看日志、生成分析
L1：修改 effective existing/new scope 内普通业务代码、运行测试
L2：新增依赖、修改配置、数据库 schema、Dockerfile、CI
L3：认证、权限、支付、安全逻辑、大规模重构、删除文件
L4：读取密钥、git push、绕过权限、修改 Master 策略、rm -rf /
```

L4 直接拒绝。

---

## 15. Agent Console 审查界面

每个任务节点执行时，需要在 GUI 中显示 Agent Console。

Agent Console 显示：

1. 当前 Agent。
2. 当前节点任务。
3. 当前权限边界。
4. ContextPack。
5. Agent 脱敏后的原始输出 artifact。
6. stdout / stderr。
7. 命令日志。
8. 文件变更。
9. diff 预览。
10. patch 预览。
11. 测试结果。
12. 风险提示。
13. 提权请求。
14. 审批卡片。

用户操作：

1. 暂停任务。
2. 终止任务。
3. 对允许的终态节点重新运行或创建新 workflow_run。
4. 查看上下文。
5. 查看完整日志。
6. 查看 diff。
7. 批准继续。
8. 拒绝结果。
9. 在执行前的 AuthorGraph 中改派 Agent；执行后改派必须创建新 attempt/run。
10. 通过统一 Approval 卡片批准或拒绝提权。

未审批 ChangeSet 通过 reject 保持不合并即可，不存在对共享 repo 的“回滚按钮”。已经由 Master 创建本地 commit 的修改如需撤销，必须创建新的 revert workflow 和 ChangeSet，再走 Guard/Test/Approval，不能直接 `git reset/revert` 绕过图。

Agent Console 初期只做只读模式：

```text
Agent CLI stdout / stderr
        ↓
StreamingRedactor + ConsoleStream
        ↓
拆成 <=64 KiB 脱敏 chunk，原子写 ArtifactStore
        ↓
SQLite console_messages 只写 artifact_id + stream + monotonic seq + size
        ↓
WebSocket（console 使用 after_console_seq 断线续传）
        ↓
前端展示
```

Console / artifact 安全规则：

1. 脱敏必须发生在持久化之前；Demo 默认不保存未脱敏 raw output。
2. StreamingRedactor 在内存维护覆盖最长 secret pattern 的有限 rolling overlap，处理跨 stdout/stderr read 边界的匹配；只有确定不再与下一块组成 secret 的前缀才可输出，EOF/cancel 时经过同一规则 flush。未脱敏 bytes 不能写临时文件、异常日志或 crash dump。
3. 每个 chunk 最大 64 KiB，总输出默认 10 MiB；超出后写 truncation chunk/event 并终止进程，不能丢弃中段后仍把任务标成成功。
4. 去除 ANSI 控制序列、OSC hyperlink 和不可见控制字符；前端按纯文本渲染，不执行 Agent 输出中的 HTML/Markdown script。
5. 每个 chunk 先以临时文件写入、hash、原子 rename，再在事务中插入 artifact + console_message；提交后才广播。console 客户端重连时携带 `after_console_seq`，服务端按 seq 鉴权读取 chunk artifact 补齐。崩溃遗留但无 DB 引用的临时/孤儿 chunk 由启动清理器按 TTL 删除并写 security_event。
6. pause / cancel / rerun 必须调用 Master API 并落 event，不能只改变前端状态。

后续再做受控输入：

```text
用户输入补充要求
  ↓
Master 接收
  ↓
创建新的 follow-up AuthorGraph/workflow lineage
  ↓
重新 Compile/Confirm 并生成 ContextPack
  ↓
创建新的 workflow_run 执行
```

禁止用户输入直接透传给 Agent 原生终端，也禁止通过受控输入修改正在运行的 CompiledGraph snapshot。

---

## 16. Demo 技术栈

```text
语言：Python 3.11+
后端：FastAPI
CLI：Typer
数据库：SQLite
SQLite 驱动：aiosqlite，Repository 统一短事务
数据模型：Pydantic
Git 操作：subprocess 调 git CLI
子进程树管理：psutil + Windows process group / POSIX process group
Python 依赖：`pyproject.toml` + 带 hash 的 `requirements.lock`，CI 使用 `pip install --require-hashes`
前端：React + `@xyflow/react` + `@dagrejs/dagre` + `lucide-react`（以 package-lock.json 固定实际版本，CI 使用 npm ci，并导入官方 CSS）
前端终端展示：xterm.js 可后续加入
实时通信：WebSocket
本地存储：artifacts/
后端测试：pytest
前端测试：Vitest + React Testing Library
端到端测试：Playwright
运行模型：单 Master 进程 / Uvicorn workers=1
```

Demo 禁止启动多个 API worker 或多个 Master 实例；scheduler、进程管理、workspace lease heartbeat、内存 ws_ticket 和实时广播都归属于唯一 Master。SQLite 事务仍负责幂等和崩溃恢复，但不把 Demo 描述成分布式调度系统。

Demo 默认配置必须集中在 `app/config.py` 并可通过受校验的本地配置覆盖，不能散落 magic number：

```text
scheduler_poll_ms = 250
master_lease_ttl_seconds = 15
workspace_lease_ttl_seconds = 30
lease_heartbeat_seconds = 5
agent_default_timeout_seconds = 900
changeset_approval_ttl_seconds = 86400
privilege_approval_ttl_seconds = 600
capability_grant_ttl_seconds = 300
ws_ticket_ttl_seconds = 30
idempotency_ttl_seconds = 86400
max_graph_json_bytes = 2 MiB
max_http_body_bytes = 2 MiB
max_console_bytes_per_run = 10 MiB
max_console_chunk_bytes = 64 KiB
max_patch_bytes = 20 MiB
max_changed_paths = 500
max_task_created_bytes = 100 MiB
max_artifact_bytes = 100 MiB
max_session_artifact_bytes = 1 GiB
max_source_tracked_paths = 50000
max_source_git_bytes = 2 GiB
```

超过 source repo 上限时拒绝创建 Session；task 运行中超过输出、临时目录或新建文件预算时终止进程并进入 capture/restore，ChangeSet 超过路径/patch/artifact 上限时 `blocked_by_guard/resource_limit_exceeded`，不能截断后继续审批。资源限制是防误用的 fail-closed 上限，不是磁盘配额或恶意进程隔离。

Demo 不要引入：

```text
PostgreSQL
Redis
Kafka
Celery
完整 MCP Server
复杂 Docker 编排
```

HTTP Remote Agent、MCP Server、云端 Agent Worker 后续作为扩展点。Demo 必须先实现本地 CLI Adapter 连接层。

---

## 17. 目录结构

```text
agent-hub/
├── app/
│   ├── main.py
│   ├── config.py
│   └── api.py
│
├── master/
│   ├── planner.py
│   ├── durable_scheduler.py
│   ├── orchestrator.py
│   ├── router.py
│   └── decision.py
│
├── workflow/
│   ├── graph_model.py
│   ├── node_registry.py
│   ├── draft_validator.py
│   ├── executable_validator.py
│   ├── policy_injector.py
│   ├── compiler.py
│   ├── executor.py
│   ├── runtime.py
│   └── handlers/
│       ├── agent_task.py
│       ├── guards.py
│       ├── approval.py
│       └── merge_patch.py
│
├── protocol/
│   ├── task.py
│   ├── context.py
│   ├── result.py
│   ├── event.py
│   ├── workflow.py
│   ├── console.py
│   └── privilege.py
│
├── adapters/
│   ├── base.py
│   ├── cli_spec.py
│   ├── cli_runner.py
│   ├── runtime_policy.py
│   ├── mock_agent.py
│   ├── opencode_planner.py
│   ├── codex_cli.py
│   ├── claude_code_cli.py
│   ├── opencode_cli.py
│   └── aider_cli.py
│
├── context/
│   ├── builder.py
│   ├── task_bundle.py
│   ├── planner_bundle.py
│   ├── retriever.py
│   └── normalizer.py
│
├── console/
│   ├── console_manager.py
│   ├── console_stream.py
│   ├── streaming_redactor.py
│   ├── console_repository.py
│   └── approval_manager.py
│
├── security/
│   ├── capability_broker.py
│   ├── policy_engine.py
│   ├── command_guard.py
│   ├── test_runner.py
│   ├── patch_guard.py
│   ├── path_policy.py
│   ├── risk_classifier.py
│   └── privilege_manager.py
│
├── workspace/
│   ├── git_manager.py
│   ├── clone_manager.py
│   ├── lock_manager.py
│   ├── change_set.py
│   ├── transaction.py
│   └── patch_manager.py
│
├── storage/
│   ├── db.py
│   ├── repositories.py
│   ├── idempotency_repository.py
│   └── artifact_store.py
│
├── web/
│   ├── backend/
│   │   ├── routes_sessions.py
│   │   ├── routes_tasks.py
│   │   ├── routes_workflows.py
│   │   ├── routes_console.py
│   │   ├── routes_approvals.py
│   │   └── websocket.py
│   └── frontend/
│       ├── src/
│       │   ├── pages/
│       │   │   └── SessionPage.tsx
│       │   ├── components/
│       │   │   ├── WorkflowCanvas.tsx
│       │   │   ├── AgentPalette.tsx
│       │   │   ├── NodeConfigPanel.tsx
│       │   │   ├── AgentConsolePanel.tsx
│       │   │   ├── DiffViewer.tsx
│       │   │   ├── RiskPanel.tsx
│       │   │   └── ApprovalPanel.tsx
│       │   └── api/
│       ├── package.json
│       ├── package-lock.json
│       └── vite.config.ts
│
├── migrations/
│   └── init.sql
│
├── tests/
│   ├── test_policy_engine.py
│   ├── test_command_guard.py
│   ├── test_patch_guard.py
│   ├── test_changeset_lifecycle.py
│   ├── test_path_policy.py
│   ├── test_draft_validator.py
│   ├── test_executable_validator.py
│   ├── test_policy_injector.py
│   ├── test_context_builder.py
│   ├── test_workspace_transaction.py
│   ├── test_cli_agent_runner.py
│   └── test_graph_executor.py
│
├── README.md
├── .gitignore
├── requirements.lock
└── pyproject.toml
```

运行数据默认不能放在 Agent Hub 源码树内，避免 OpenCode 从项目祖先目录自动加载 `.env`、配置或插件。`AGENT_HUB_DATA_DIR` 默认取 Windows `%LOCALAPPDATA%\AgentHub`、POSIX `~/.local/share/agent-hub`，逻辑结构为：

```text
AGENT_HUB_DATA_DIR/
├── agent-hub.db
├── artifacts/{logs,diffs,patches,reports,test-results}
├── workspaces/
│   ├── shared/<session_id>/repo
│   └── agent-runs/<task_or_planner_run_id>/
└── profiles/opencode/
```

该目录及其从 data root 到 task cwd 的祖先路径在任何 OpenCode 启动前都要扫描真实 `.env/.env.*`、OpenCode/Claude 启动配置和 reparse/symlink；发现时 fail closed。开发测试若覆盖 data dir，只能指向专用临时目录，不能直接使用仓库根目录。

---

## 18. 核心 Pydantic 模型

所有协议模型继承 `StrictModel`，默认 `extra="forbid"`。固定状态和类型使用 `StrEnum`，list / dict 使用 `Field(default_factory=...)`，字符串和集合设置长度上限。Planner、Adapter 和 API 输入不得使用未约束的任意 `dict` 作为执行配置。

### 18.1 公共类型

```python
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field


EntityId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )


class RiskLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class PlannerType(StrEnum):
    RULE_BASED = "rule_based"
    OPEN_CODE = "open_code"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    ARCHIVED = "archived"


class PlannerRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PRIVILEGE_REQUESTED = "privilege_requested"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED_BY_GUARD = "blocked_by_guard"
    PARSE_FAILED = "parse_failed"
    ORPHANED = "orphaned"


class SecuritySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class ActorType(StrEnum):
    USER = "user"
    MASTER = "master"
    AGENT = "agent"
    SYSTEM = "system"
    LOCAL_CLI = "local_cli"


class ConsoleStreamKind(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"


class ConsoleOwnerType(StrEnum):
    TASK = "task"
    PLANNER_RUN = "planner_run"


class ArtifactType(StrEnum):
    LOG = "log"
    CONSOLE = "console"
    DIFF = "diff"
    PATCH = "patch"
    REPORT = "report"
    TEST_RESULT = "test_result"
    RUNTIME_POLICY = "runtime_policy"
    CHANGE_PREIMAGE = "change_preimage"


class CapabilityType(StrEnum):
    MODIFY_DEPENDENCY = "modify_dependency"
    MODIFY_CONFIG = "modify_config"


class PrivilegeAction(StrEnum):
    EDIT_DEPENDENCY_MANIFEST = "edit_dependency_manifest"
    EDIT_PROJECT_CONFIG = "edit_project_config"


class NodeType(StrEnum):
    INPUT = "input"
    AGENT_TASK = "agent_task"
    CONTEXT_BUILDER = "context_builder"
    PATCH_GUARD = "patch_guard"
    COMMAND_GUARD = "command_guard"
    TEST = "test"
    RISK_CLASSIFIER = "risk_classifier"
    APPROVAL = "approval"
    MERGE_PATCH = "merge_patch"
    OUTPUT = "output"
    IF = "if"


class TaskKind(StrEnum):
    ANALYZE = "analyze"
    IMPLEMENT = "implement"
    REVIEW = "review"
    DOCS = "docs"
    TEST_FIX = "test_fix"


class TestKind(StrEnum):
    COMMAND = "command"
    DOCS_STATIC = "docs_static"


class IfOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"


class AssignmentMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    LOCKED = "locked"


class NodeRunStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED_BY_GUARD = "blocked_by_guard"
    FAILED = "failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class NodeOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class EdgeCondition(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    APPROVED = "approved"
    REJECTED = "rejected"


class ArtifactRef(StrictModel):
    artifact_id: EntityId
    artifact_type: ArtifactType
    relative_path: str
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
```

为缩短后续示例，部分字段仍写作 `str`；实际实现中所有 `*_id`、agent/node/edge ID 必须使用 `EntityId`，所有 `*_sha256/hash` 使用 `Sha256Hex`，Git commit/ref 解析结果使用 `GitObjectId`。用户可见 title/instruction/reason 使用各自长度受限文本；路径必须经过专用 PathPolicy 类型，不能拿 EntityId 正则代替路径校验。

### 18.2 TaskPackage

```python
class TaskPackage(StrictModel):
    task_id: str
    session_id: str
    workflow_run_id: str
    node_run_id: str
    node_id: str
    agent_id: str
    task_kind: TaskKind
    instruction: str = Field(min_length=1, max_length=20_000)
    repo_path: str
    base_commit: str
    effective_allowed_files: list[str] = Field(default_factory=list, max_length=100)
    effective_new_files: list[str] = Field(default_factory=list, max_length=100)
    active_capability_grant_id: str | None = None
    granted_existing_files: list[str] = Field(default_factory=list, max_length=1)
    readonly_files: list[str] = Field(default_factory=list, max_length=2_000)
    effective_allowed_commands: list[list[str]] = Field(default_factory=list, max_length=20)
    workspace_ephemeral_paths: list[str] = Field(default_factory=list, max_length=100)
    forbidden_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    effective_risk: RiskLevel = RiskLevel.L1
    requires_changeset_approval: bool = False
    runtime_policy_ref: ArtifactRef
    context_bundle_path: str
    context_bundle_sha256: str
    timeout_seconds: int = Field(default=900, ge=1, le=3600)
```

`TaskPackage.effective_allowed_files/effective_new_files/effective_allowed_commands`、初始 `effective_risk` 和是否需要 ChangeSetApproval 必须来自 workflow_run 的 CompiledGraph snapshot；AuthorGraph 的 candidate 字段仅供下一次编译使用。运行时可以因 PatchGuard/RiskClassifier 进一步收紧权限或提高风险，不能扩大 compiled scope 或降低 `policy_risk_floor`。唯一例外是已经原子消费且绑定当前 target_task_id 的 CapabilityGrant：其一个精确已有 resource 单独写入 `active_capability_grant_id/granted_existing_files`，不能改写 compiled effective 字段；PatchGuard 和最终 Approval evidence 必须同时校验 grant。

### 18.3 ContextPack

```python
class NodeSummary(StrictModel):
    node_run_id: str
    status: NodeRunStatus
    summary: str = Field(max_length=8_000)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)


class ContextPack(StrictModel):
    task_id: str
    node_id: str
    task_kind: TaskKind
    session_goal: str = Field(max_length=20_000)
    current_node_title: str = Field(max_length=500)
    current_task: str = Field(max_length=20_000)
    upstream_summaries: list[NodeSummary] = Field(default_factory=list, max_length=100)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=500)
    effective_allowed_files: list[str] = Field(default_factory=list, max_length=100)
    effective_new_files: list[str] = Field(default_factory=list, max_length=100)
    active_capability_grant_id: str | None = None
    granted_existing_files: list[str] = Field(default_factory=list, max_length=1)
    effective_allowed_commands: list[list[str]] = Field(default_factory=list, max_length=20)
    forbidden_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    max_prompt_chars: int = Field(default=12_000, ge=1_000, le=24_000)
```

### 18.4 AgentResult

```python
class AgentResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PRIVILEGE_REQUESTED = "privilege_requested"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED_BY_GUARD = "blocked_by_guard"
    PARSE_FAILED = "parse_failed"


class AgentResult(StrictModel):
    task_id: str
    node_run_id: str
    agent_id: str
    status: AgentResultStatus
    summary: str = Field(default="", max_length=8_000)
    raw_output_ref: ArtifactRef | None = None
    change_set_id: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    privilege_request_ids: list[str] = Field(default_factory=list, max_length=1)
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=8_000)
```

### 18.5 WorkflowNode / WorkflowEdge

```python
class NodePosition(StrictModel):
    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)


class NodeLayout(StrictModel):
    node_id: str
    position: NodePosition


class WorkflowLayout(StrictModel):
    nodes: list[NodeLayout] = Field(default_factory=list, max_length=100)


class AgentRecommendation(StrictModel):
    agent_id: str
    score: int = Field(ge=0, le=100)
    reason: str = Field(max_length=1_000)


class IfCondition(StrictModel):
    upstream_node_id: str
    field: str = Field(pattern=r"^(status|outcome|effective_risk|tests_passed)$")
    operator: IfOperator
    value: str | bool | list[str] | None = None


class WorkflowNode(StrictModel):
    id: str
    node_type: NodeType
    task_kind: TaskKind | None = None
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4_000)
    instruction: str | None = Field(default=None, max_length=20_000)
    assigned_agent: str | None = None
    assignment_mode: AssignmentMode = AssignmentMode.AUTO
    resolved_agent_id: str | None = None
    resolved_agent_spec_sha256: str | None = None
    recommended_agents: list[AgentRecommendation] = Field(default_factory=list)
    allowed_files_candidate: list[str] = Field(default_factory=list, max_length=500)
    new_files_candidate: list[str] = Field(default_factory=list, max_length=100)
    allowed_commands_candidate: list[list[str]] = Field(default_factory=list, max_length=50)
    effective_allowed_files: list[str] | None = Field(default=None, max_length=100)
    effective_new_files: list[str] | None = Field(default=None, max_length=100)
    effective_allowed_commands: list[list[str]] | None = Field(default=None, max_length=20)
    policy_risk_floor: RiskLevel | None = None
    requires_changeset_approval: bool | None = None
    test_kind: TestKind | None = None
    test_argv: list[str] | None = Field(default=None, max_length=64)
    if_condition: IfCondition | None = None
    risk_level_hint: RiskLevel = RiskLevel.L1
    requires_write: bool = False
    system_managed: bool = False
    source_node_id: str | None = None
    system_rule_id: str | None = None


class WorkflowEdge(StrictModel):
    id: str
    from_node: str
    to_node: str
    condition: EdgeCondition = EdgeCondition.SUCCESS
    system_managed: bool = False
```

`WorkflowNode` 不包含 status 和 position；运行状态只记录在 `node_runs`，画布位置只记录在独立 `WorkflowLayout`。NodeRegistry 还必须按 `node_type` 调用对应的 node config model，禁止把任意 config dict 直接交给 NodeHandler。

`WorkflowLayout` 只保存最多 100 个 AuthorGraph 节点坐标。CompiledGraph 的 system node 坐标不持久化、不参与任何 hash；前端以固定节点尺寸、LR 方向和按 node/edge ID 稳定排序的 `@dagrejs/dagre` 计算只读 preview layout。切回 AuthorGraph 时恢复用户坐标，不能把 dagre 结果回写 AuthorGraph。

字段约束：`effective_allowed_files / effective_new_files / effective_allowed_commands / policy_risk_floor / requires_changeset_approval / test_kind / test_argv` 都是 compiler-only；AuthorGraph/Planner/API 客户端提交时拒绝。command test 必须有一个通过 CommandGuard 的 `test_argv`；docs_static test 必须无 argv 且只覆盖纯文档 scope。`if_condition` 只允许 AuthorGraph 的 if 节点，并且只能引用在所有到达该 if 的路径上都已完成的 transitive predecessor；`is_true/is_false` 要求 bool 字段且 value 为空，`in` 要求非空同类型列表，`eq/ne` 要求标量同类型值，risk/status/outcome 值还必须属于对应 Enum。if 求值为真/假分别返回 matched/not_matched；字段缺失或类型异常才返回 failure。Compiler 为写任务的每条批准测试 argv 生成一个串行 command test；仅纯 docs scope 可生成 docs_static，其他无 argv 情况生成明确 blocked 的 test 节点。

### 18.6 WorkflowDraft

PlannerAgent / RuleBasedPlanner 只能输出 `WorkflowDraft`，执行层不得直接执行自然语言计划。

```python
class WorkflowDraft(StrictModel):
    schema_version: str = "1"
    session_id: str
    goal: str = Field(min_length=1, max_length=20_000)
    planner_id: str
    planner_type: PlannerType
    planner_model: str | None = None
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=100)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=300)
    assumptions: list[str] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=100)
    required_user_inputs: list[str] = Field(default_factory=list, max_length=50)
```

持久化图使用独立模型，不能把 Planner 元数据混进执行图：

```python
class AuthorGraph(StrictModel):
    schema_version: str = "1"
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=100)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=300)


class CompiledGraph(StrictModel):
    schema_version: str = "1"
    source_author_hash: str
    integration_base_commit: GitObjectId
    policy_version: str
    agent_catalog_snapshot_hash: str
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=300)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=600)
```

`WorkflowDraft` 通过显式转换生成 AuthorGraph；CompiledGraph 只能由 Compiler 构造，API 不接受客户端提交 CompiledGraph。

### 18.7 ChangeSet

```python
class ChangeSetStatus(StrEnum):
    CAPTURED = "captured"
    GUARD_PASSED = "guard_passed"
    GUARD_REJECTED = "guard_rejected"
    TEST_PASSED = "test_passed"
    TEST_FAILED = "test_failed"
    POLICY_REJECTED = "policy_rejected"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    STALE = "stale"
    MERGED = "merged"
    ABANDONED_PARTIAL = "abandoned_partial"
    QUARANTINED = "quarantined"


class ChangeSet(StrictModel):
    change_set_id: str
    session_id: str
    workflow_run_id: str
    node_run_id: str
    task_id: str
    base_commit: str
    pre_state_hash: str
    post_state_hash: str
    patch_sha256: str
    status: ChangeSetStatus = ChangeSetStatus.CAPTURED
    canonical_patch_ref: ArtifactRef
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    created_files: list[str] = Field(default_factory=list)
    created_directories: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    renamed_files: list[str] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    ignored_files_touched: list[str] = Field(default_factory=list)
    preimage_refs: list[ArtifactRef] = Field(default_factory=list)
```

`ChangeSetStatus` 不是展示标签，而是独立于 NodeRun/WorkflowRun 的持久状态机。`guard_rejected` 只表示 PatchGuard 拒绝，`test_failed` 表示测试命令失败或测试污染工作区，`policy_rejected` 表示 RiskClassifier/PolicyEngine 判定 L4，`rejected` 只表示用户拒绝最终 ChangeSetApproval，`cancelled` 表示完整 ChangeSet 所属 workflow 在 Merge 线性化点前被用户取消。非零退出、解析失败、超时、运行中取消或 privilege request 前已经产生的半成品修改统一进入 `abandoned_partial`，不得继续进入 Guard/Test/Approval/Merge。

### 18.8 Approval / PrivilegeRequest / CapabilityGrant

```python
class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class PrivilegeRequestStatus(StrEnum):
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    DENIED = "denied"


class ApprovalBase(StrictModel):
    approval_id: str
    workflow_run_id: str
    node_run_id: str
    subject_sha256: str
    effective_risk: RiskLevel
    scope: list[str] = Field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    version: int = Field(default=1, ge=1)
    expires_at: datetime


class ChangeSetApproval(ApprovalBase):
    subject_type: Literal["change_set"] = "change_set"
    change_set_id: str
    base_commit: str
    patch_sha256: str
    evidence_sha256: str


class PrivilegeApproval(ApprovalBase):
    subject_type: Literal["privilege_request"] = "privilege_request"
    privilege_request_id: str
    evidence_sha256: str


Approval = Annotated[
    ChangeSetApproval | PrivilegeApproval,
    Field(discriminator="subject_type"),
]


class PrivilegeRequestProposal(StrictModel):
    requested_capability: CapabilityType
    requested_action: PrivilegeAction
    requested_resource: str | None = None
    reason: str = Field(max_length=4_000)
    expected_impact: list[str] = Field(default_factory=list, max_length=50)
    related_files: list[str] = Field(default_factory=list, max_length=100)
    rollback_plan: str | None = Field(default=None, max_length=4_000)
    risk_level_hint: RiskLevel = RiskLevel.L2


class PrivilegeRequest(PrivilegeRequestProposal):
    request_id: str
    session_id: str
    task_id: str
    node_run_id: str
    agent_id: str
    requested_resource: str
    effective_risk: RiskLevel = RiskLevel.L2
    status: PrivilegeRequestStatus = PrivilegeRequestStatus.PENDING


class NextSuggestion(StrictModel):
    suggested_agent: str | None = None
    reason: str = Field(max_length=1_000)


class AgentOutputEnvelope(StrictModel):
    summary: str = Field(max_length=8_000)
    risk_hints: list[str] = Field(default_factory=list, max_length=50)
    next_suggestion: NextSuggestion | None = None
    privilege_requests: list[PrivilegeRequestProposal] = Field(default_factory=list, max_length=1)


class CapabilityGrant(StrictModel):
    grant_id: str
    request_id: str
    target_task_id: str
    action: PrivilegeAction
    resource: str
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_fencing_token: int | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = Field(default=None, max_length=200)
```

`CapabilityGrant` 使用 model validator 保证 consumed_at/consumed_fencing_token 同时为空或同时存在，consumed 与 revoked 互斥，revoked_at/revocation_reason 同时为空或同时存在。Grant 过期不改写历史字段，但消费查询必须同时校验 expiry 和 revoked_at。

`subject_sha256` 对 canonical subject manifest 计算：ChangeSetApproval 覆盖 workflow/node/change_set/base/patch/evidence/scope/expiry，PrivilegeApproval 覆盖 workflow/node/request/capability/action/resource/effective_risk/scope/expiry。API/CLI 的 `confirm_subject_hash` 必须与该字段恒等；任何组成项变化都创建新 subject/Approval，不能原地改 hash。

### 18.9 Event / Artifact / ConsoleChunk

```python
from typing import Generic, TypeVar


class Artifact(StrictModel):
    artifact_id: EntityId
    session_id: EntityId
    task_id: EntityId | None = None
    planner_run_id: EntityId | None = None
    artifact_type: ArtifactType
    relative_path: str
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    redacted: bool
    created_at: datetime


PayloadT = TypeVar("PayloadT", bound=StrictModel)


class EventEnvelope(StrictModel, Generic[PayloadT]):
    event_id: int = Field(ge=1)
    session_id: EntityId
    workflow_id: EntityId | None = None
    workflow_run_id: EntityId | None = None
    run_seq: int | None = Field(default=None, ge=1)
    event_type: EntityId
    actor_type: ActorType
    actor_id: EntityId | None = None
    payload: PayloadT
    created_at: datetime


class ConsoleChunk(StrictModel):
    console_session_id: EntityId
    seq: int = Field(ge=1)
    stream: ConsoleStreamKind
    artifact_ref: ArtifactRef
    size_bytes: int = Field(gt=0, le=65_536)
    created_at: datetime
```

`EventRegistry` 必须把每个 `event_type` 映射到一个 `StrictModel` payload class；Repository 先按 registry 校验，再限制 canonical payload JSON <=64 KiB 后落库。未知 event_type、任意 dict 或超限 payload 拒绝；大内容只放 ArtifactRef。`workflow_run_id/run_seq` 必须同时为空或同时非空，run event 还必须有 workflow_id。ConsoleChunk 的 artifact_type 必须为 console、size/hash 必须匹配文件，Artifact 的 task/planner owner 不能同时存在。

---

## 19. SQLite 表设计

`migrations/init.sql` 需要实现：

- `schema_migrations`
- `agents`
- `sessions`
- `workflows`
- `planner_runs`
- `workflow_runs`
- `node_runs`
- `tasks`
- `task_permissions`
- `change_sets`
- `events`
- `artifacts`
- `idempotency_keys`
- `master_leases`
- `file_locks`
- `console_sessions`
- `console_messages`
- `approvals`
- `privilege_requests`
- `capability_grants`
- `security_events`

数据库启动时执行：

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

`init.sql` 不能只留下抽象实体名，至少实现以下列和约束：

```text
schema_migrations(
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
)

agents(
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, adapter_type TEXT NOT NULL,
  enabled INTEGER NOT NULL, executable_path TEXT, executable_sha256 TEXT,
  detected_version TEXT, capabilities_json TEXT NOT NULL, created_at TEXT NOT NULL
)

sessions(
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, source_repo_path TEXT NOT NULL,
  shared_repo_path TEXT NOT NULL UNIQUE, base_commit TEXT NOT NULL,
  integration_branch TEXT NOT NULL, integration_head_commit TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)

workflows(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  parent_workflow_id TEXT REFERENCES workflows(id),
  source_planner_run_id TEXT REFERENCES planner_runs(id),
  semantic_version INTEGER NOT NULL, layout_version INTEGER NOT NULL,
  author_graph_json TEXT NOT NULL, author_graph_hash TEXT NOT NULL,
  layout_json TEXT NOT NULL, layout_hash TEXT NOT NULL,
  last_compiled_graph_json TEXT,
  last_compiled_graph_hash TEXT, last_compiled_semantic_version INTEGER,
  last_compiled_agent_catalog_hash TEXT,
  last_compiled_base_commit TEXT, policy_version TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)

planner_runs(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  planner_id TEXT NOT NULL, planner_type TEXT NOT NULL, planner_model TEXT,
  integration_base_commit TEXT,
  context_bundle_artifact_id TEXT REFERENCES artifacts(id),
  context_bundle_sha256 TEXT,
  status TEXT NOT NULL, runtime_policy_artifact_id TEXT REFERENCES artifacts(id),
  output_artifact_id TEXT REFERENCES artifacts(id), error_code TEXT,
  fallback_from_run_id TEXT REFERENCES planner_runs(id),
  result_workflow_id TEXT REFERENCES workflows(id), result_semantic_version INTEGER,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
)

workflow_runs(
  id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
  session_id TEXT NOT NULL REFERENCES sessions(id),
  integration_base_commit TEXT NOT NULL,
  current_commit TEXT NOT NULL,
  workflow_semantic_version INTEGER NOT NULL, workflow_layout_version INTEGER NOT NULL,
  author_snapshot_json TEXT NOT NULL,
  author_snapshot_hash TEXT NOT NULL, compiled_snapshot_json TEXT NOT NULL,
  compiled_snapshot_hash TEXT NOT NULL, layout_snapshot_json TEXT NOT NULL,
  layout_snapshot_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
  agent_catalog_snapshot_json TEXT NOT NULL,
  agent_catalog_snapshot_hash TEXT NOT NULL,
  planner_run_id TEXT REFERENCES planner_runs(id), planner_id TEXT,
  planner_model TEXT, status TEXT NOT NULL,
  next_event_seq INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  started_at TEXT, cancel_requested_at TEXT,
  merge_finalizing_at TEXT, finished_at TEXT
)

node_runs(
  id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
  node_id TEXT NOT NULL, node_type TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, outcome TEXT, assigned_agent_id TEXT REFERENCES agents(id), input_hash TEXT,
  output_artifact_id TEXT REFERENCES artifacts(id),
  change_set_id TEXT REFERENCES change_sets(id), error_code TEXT,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  UNIQUE(workflow_run_id, node_id, attempt)
)

tasks(
  id TEXT PRIMARY KEY, node_run_id TEXT NOT NULL UNIQUE REFERENCES node_runs(id),
  agent_id TEXT NOT NULL REFERENCES agents(id), base_commit TEXT NOT NULL,
  runtime_policy_artifact_id TEXT REFERENCES artifacts(id),
  active_capability_grant_id TEXT REFERENCES capability_grants(id),
  status TEXT NOT NULL,
  created_at TEXT NOT NULL, finished_at TEXT
)

task_permissions(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id), allowed_files_json TEXT NOT NULL,
  new_files_json TEXT NOT NULL,
  granted_existing_files_json TEXT NOT NULL,
  readonly_files_json TEXT NOT NULL, allowed_commands_json TEXT NOT NULL,
  forbidden_paths_json TEXT NOT NULL, ephemeral_paths_json TEXT NOT NULL,
  effective_risk TEXT NOT NULL,
  permissions_hash TEXT NOT NULL
)

change_sets(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  base_commit TEXT NOT NULL, pre_state_hash TEXT NOT NULL,
  post_state_hash TEXT NOT NULL, patch_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  patch_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)

artifacts(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  task_id TEXT REFERENCES tasks(id),
  planner_run_id TEXT REFERENCES planner_runs(id), artifact_type TEXT NOT NULL,
  relative_path TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  redacted INTEGER NOT NULL, created_at TEXT NOT NULL
)

events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  workflow_id TEXT REFERENCES workflows(id),
  workflow_run_id TEXT REFERENCES workflow_runs(id), run_seq INTEGER,
  event_type TEXT NOT NULL, actor_type TEXT NOT NULL,
  actor_id TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(workflow_run_id, run_seq),
  CHECK((workflow_run_id IS NULL AND run_seq IS NULL) OR
        (workflow_run_id IS NOT NULL AND run_seq IS NOT NULL)),
  CHECK(workflow_run_id IS NULL OR workflow_id IS NOT NULL)
)

idempotency_keys(
  actor_scope TEXT NOT NULL, operation_scope TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
  response_status INTEGER NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  PRIMARY KEY(actor_scope, operation_scope, idempotency_key)
)

master_leases(
  lease_key TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
  process_id INTEGER NOT NULL, fencing_token INTEGER NOT NULL,
  heartbeat_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL
)

file_locks(
  resource_key TEXT PRIMARY KEY, owner_kind TEXT NOT NULL,
  owner_operation_id TEXT NOT NULL,
  owner_process_id INTEGER NOT NULL, fencing_token INTEGER NOT NULL,
  acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL, released_at TEXT
)

console_sessions(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
  workflow_run_id TEXT REFERENCES workflow_runs(id),
  created_at TEXT NOT NULL, closed_at TEXT,
  UNIQUE(owner_type, owner_id)
)

console_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  console_session_id TEXT NOT NULL REFERENCES console_sessions(id),
  seq INTEGER NOT NULL, stream TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  size_bytes INTEGER NOT NULL CHECK(size_bytes > 0 AND size_bytes <= 65536),
  created_at TEXT NOT NULL, UNIQUE(console_session_id, seq)
)

approvals(
  id TEXT PRIMARY KEY,
  workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
  node_run_id TEXT NOT NULL REFERENCES node_runs(id),
  subject_type TEXT NOT NULL,
  change_set_id TEXT REFERENCES change_sets(id),
  privilege_request_id TEXT REFERENCES privilege_requests(id),
  subject_sha256 TEXT NOT NULL, base_commit TEXT,
  patch_sha256 TEXT, evidence_sha256 TEXT NOT NULL,
  effective_risk TEXT NOT NULL, scope_json TEXT NOT NULL,
  status TEXT NOT NULL, version INTEGER NOT NULL,
  decision_actor TEXT, decision_idempotency_key TEXT,
  expires_at TEXT NOT NULL, decided_at TEXT, created_at TEXT NOT NULL
)

privilege_requests(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  node_run_id TEXT NOT NULL REFERENCES node_runs(id),
  capability TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
  effective_risk TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL
)

capability_grants(
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES privilege_requests(id),
  target_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  action TEXT NOT NULL, resource TEXT NOT NULL,
  expires_at TEXT NOT NULL, consumed_at TEXT,
  consumed_fencing_token INTEGER, revoked_at TEXT, revocation_reason TEXT,
  CHECK((consumed_at IS NULL AND consumed_fencing_token IS NULL) OR
        (consumed_at IS NOT NULL AND consumed_fencing_token IS NOT NULL)),
  CHECK(NOT(consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
  CHECK((revoked_at IS NULL AND revocation_reason IS NULL) OR
        (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL))
)

security_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  workflow_run_id TEXT REFERENCES workflow_runs(id),
  task_id TEXT REFERENCES tasks(id), event_type TEXT NOT NULL,
  severity TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
)
```

所有 `status`、`outcome`、`risk`、`node_type`、`artifact_type`、`capability`、`action`、`actor_type`、console `owner_type/stream` 列必须使用 `CHECK (...)` 与协议 Enum 保持一致。Approval 用两个真实 FK 和 CHECK 保证：change_set subject 只有 change_set_id 且必须有 base/patch hash；privilege subject 只有 privilege_request_id 且 base/patch 为空。console owner_type 只允许 task/planner_run 并由 Repository 校验 owner 存在；artifact 的 task_id/planner_run_id 不能同时非空。所有持久时间使用固定宽度 UTC ISO-8601 微秒格式 `YYYY-MM-DDTHH:MM:SS.ffffffZ`，禁止混用本地时区或 `+00:00` 文本，确保 SQLite TTL 比较一致；进程内 sleep/timeout 使用 monotonic clock。所有 JSON 在写入前经过对应 StrictModel 验证。扩展型 ID（agent_id、event_type、error_code）仍为字符串，但必须有长度和字符集限制。

大日志、diff、patch、测试报告不直接塞 SQLite；SQLite 只保存相对路径、hash、大小和归属。artifact 路径由服务端生成并做 repo containment 校验，不能接受 Agent 提供的任意落盘路径。

ArtifactStore 根目录和专用 OpenCode profile 必须使用仅当前用户可读写的权限/ACL，不能作为 FastAPI 静态目录直接暴露。API 通过 artifact_id 鉴权后流式读取。PatchGuard 若检测到疑似 secret，ChangeSet 进入 quarantine：只保留 owner-only 原件和脱敏预览，禁止 Approval/Merge，直到用户在本地处理。

security_events 同样使用 event_type -> StrictModel registry、64 KiB payload 上限和持久化前脱敏；检测到的 secret 只记录规则 ID、文件相对路径和内容 hash，绝不把匹配原文写入 SQLite/event/console。

数据库事务约束：

1. node/workflow 状态变化与对应 event 必须在同一事务提交；plan/edit 等运行前事件使用 session/workflow 归属且 workflow_run_id 为空。
2. run_seq 在 workflow_run 内单调递增；同一 `BEGIN IMMEDIATE` 事务读取 `next_event_seq`、将其加一并插入 event，任何一步失败整体回滚，不能用进程内计数器。非运行事件按全局 event id 排序。WebSocket 只广播已提交事件。
3. 获取 workspace 写租约使用 `BEGIN IMMEDIATE` 和条件更新，成功后递增 fencing token；`file_locks` 的 resource 行永久保留，release 只写 `released_at/lease_expires_at`，不能 DELETE 后让 token 回绕。acquire/heartbeat/release 都必须匹配 resource、owner 和 token。
4. Master 启动时先原子获取 `lease_key='scheduler'` 的 singleton master lease；已有未过期实例时 fail fast。该行永久保留：首次 INSERT 使用 token=1，过期接管或正常释放后的再次获取都必须条件 UPDATE 并递增 token，不能通过 DELETE/重建把 token 归零。heartbeat/release 必须匹配 instance_id + token。
5. GraphExecutor 每次 claim node、推进 scheduler 状态或提交 scheduler event，都必须在同一事务先验证 master lease 未过期且 instance_id/token 匹配；新 claim 还必须验证 workflow `cancel_requested_at IS NULL`。event payload 记录 `master_fencing_token`。验证失败立即停止调度，旧 Master 即使仍存活也不能继续写入。
6. 所有可重试的 Application Service mutation 先在同一 `BEGIN IMMEDIATE` 事务查询 `actor_scope + operation_scope + idempotency_key`：同 request hash 返回已存 response，不同 hash 返回 409；不存在时执行状态变化并把 response 一起写入。不能先提交业务状态再单独写幂等记录。
7. approve/reject 使用 `UPDATE ... SET status=?, version=version+1 ... WHERE status='pending' AND version=?`，并验证所属 workflow `cancel_requested_at IS NULL`；同一事务完成 approval、node_run、workflow_run、event 和 idempotency response 更新。expire/invalidate 使用同样 CAS 增量规则。
8. capability 消费使用 `UPDATE ... WHERE target_task_id=? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now AND action/resource matches`，并验证 `tasks.active_capability_grant_id` 指回同一 grant 且 workflow `cancel_requested_at IS NULL`，受影响行数必须等于 1；workspace action 必须在同一 `BEGIN IMMEDIATE` 事务内取得租约并写入 consumed_fencing_token。
9. 语义 mutation 必须携带 `expected_semantic_version`；只有当前 semantic_version 匹配时更新并递增，否则返回 409。旧 compiled preview 可以保留用于对比，但 source semantic/catalog/policy/base commit 任一项与当前值不同时必须标记 stale，不能确认运行。
10. 布局 mutation 单独携带 `expected_layout_version`，只更新 layout_json/layout_hash/layout_version，不清除 compiled preview，也不改变 semantic_version。
11. 启动恢复扫描 running/orphaned node、过期租约和 dirty workspace，恢复结果必须写 event/security_event。

必须为 `node_runs(workflow_run_id,status)`、`events(session_id,id)`、`events(workflow_run_id,run_seq)`、`console_messages(console_session_id,seq)`、`approvals(status,expires_at)`、`idempotency_keys(expires_at)`、`security_events(session_id,created_at)` 建索引。Demo 额外建立 partial unique index，保证每个 Session 最多一个处于 `pending/running/waiting_approval/paused/blocked/orphaned` 的 active workflow_run，并分别保证同一 change_set/privilege_request 同时最多一个 pending Approval。

Workflow 持久化必须支持版本和运行快照：

1. `workflows` 分别保存 AuthorGraph 语义、WorkflowLayout 和最后一次成功编译预览；preview 同时记录 source semantic version、agent catalog hash、policy version 和 integration base commit，运行状态不写回 graph JSON。
2. 节点/边/权限/Agent 分配修改递增 semantic_version；仅节点坐标变化递增 layout_version。
3. `POST /workflows/{workflow_id}/run` 必须取得 workspace lease、确认 clean/HEAD、根据 `expected_semantic_version` 重新编译并校验，然后固化 AuthorGraph、CompiledGraph、integration base commit 和当时的 Layout。
4. `workflow_runs` 保存三个 graph/layout snapshot/hash、integration base commit、agent catalog snapshot/hash、semantic/layout version、policy_version、planner 信息和时间戳。
5. `GraphExecutor` 只执行 `workflow_runs.compiled_snapshot_json`，不能执行可变 workflow 或前端传入的图。
6. 审计、事件回放、diff 查看必须能关联到当时执行的精确节点图，而不是用户后来编辑过的最新图。

AuthorGraph/CompiledGraph/agent catalog hash 使用稳定 canonical JSON：UTF-8、object key 排序、固定分隔符、nodes/edges/recommendations 按 ID 排序，集合语义的路径/forbidden/action 列表规范化后去重排序；argv token 顺序、串行测试顺序和用户 instruction 原文等有序字段保持原序。图 hash 不包含浮点布局数据，Compiler system node ID 由 `sha256(source_node_id + system_rule_id + deterministic_ordinal)` 截取生成，不能用 UUID。Layout 单独按 node_id 排序后 hash；移动节点不能改变 compiled hash 或使已确认运行失效。OpenCode permission 规则因“最后匹配生效”不得套用该排序，按第 20.1 节实际发出的有序 bytes 单独 hash。

---

## 20. Agent Adapter 设计

统一接口：

```python
class BaseAgentAdapter:
    name: str

    async def is_available(self) -> bool:
        raise NotImplementedError

    def build_prompt(
        self,
        task_package: TaskPackage,
        context_pack: ContextPack
    ) -> str:
        raise NotImplementedError

    async def run(
        self,
        task_package: TaskPackage,
        context_pack: ContextPack,
        console_stream=None
    ) -> AgentResult:
        raise NotImplementedError
```

先实现：

```text
MockAgentAdapter
OpenCodeCliAdapter
OpenCodePlannerAdapter
CodexCliAdapter disabled skeleton
ClaudeCodeCliAdapter disabled skeleton
AiderCliAdapter disabled skeleton
```

Demo 阶段的 Agent 平台连接只做 CLI 连接，不做 HTTP Remote Agent / MCP Server / 云端 Worker。CLI 连接层由 Master 统一控制，Agent 不能反向调用 Master API。

MockAgent 必须能：

1. 接收 TaskPackage 和 ContextPack。
2. 生成模拟且已脱敏的 output artifact。
3. 可选地修改一个 allowed_file，用于测试 diff。
4. 返回 AgentResult。
5. 写 console_stream。

CLI Adapter 是 demo 的正式 Agent 连接方式，必须先做通用框架：

1. `CliAgentSpec` 描述每个 CLI Agent 的命令、版本检测参数、运行参数模板、输出格式、超时、环境变量策略。
2. `CliAgentRunner` 使用 `asyncio.create_subprocess_exec` 或等价 `Popen(..., shell=False)`，任何平台都不能经过 shell。
3. 注册时解析可执行文件绝对路径、版本和 sha256；每次运行前复核，PATH 指向变化或 binary hash 变化时要求用户重新确认。
4. 固定 argv 构造，不允许把用户输入拼接成 shell 字符串；prompt 设置字符上限，防止 Windows argv 长度溢出和进程列表暴露过量上下文。
5. stdout / stderr 必须并发读取并保持有界内存，避免管道死锁。原始 bytes 同时送入增量 JSON decoder 和 StreamingRedactor：console 路径只接收脱敏文本；JSON decoder 只在内存解析，任何 event 字段经过递归脱敏/大小限制后才可写 artifact。
6. JSON 输出按 JSON Lines 增量解析；不把文本替换后的 JSON 再用于解析。未知 event type 保存为脱敏 artifact 并忽略，单条解析失败不能丢失其余事件；未脱敏原始行永不落盘或进入异常日志。
7. timeout、取消、崩溃时终止整个进程树：Windows 使用新 process group + psutil tree kill，POSIX 使用独立 process group。
8. 设置单条输出、总输出、运行时间和临时目录容量上限；超限返回明确 error_code。
9. 执行前使用环境变量白名单，不继承 Master token、通用 API token、SSH agent socket、代理凭据和 CI secret。
10. 返回统一 `AgentResult`，但 ChangeSet 和文件变更只以 Master 的 WorkspaceTransaction 捕获结果为准。

CLI Agent 连接规范：

```python
class CliOutputMode(StrEnum):
    TEXT = "text"
    JSON_LINES = "json_lines"


class CliAgentSpec(StrictModel):
    agent_id: str
    display_name: str
    executable_path: str
    executable_sha256: str
    version_args: list[str] = Field(default_factory=list)
    run_args_template: list[str] = Field(default_factory=list)
    output_mode: CliOutputMode = CliOutputMode.TEXT
    default_timeout_seconds: int = Field(default=900, ge=1, le=3600)
    max_output_bytes: int = Field(default=10_000_000, ge=1_024)
    max_prompt_chars: int = Field(default=12_000, ge=1_000, le=24_000)
    supports_write: bool = False
    enabled: bool = False
```

第一版注册：

```text
opencode：先注册并探测；只有必需 capability 完整，且支持 pure 或用户显式接受已验证 legacy compatibility 时 enabled=true，否则 disabled 并显示原因
codex：enabled=false，只做可用性检测和 disabled 展示
claude_code：enabled=false，只做可用性检测和 disabled 展示
aider：enabled=false，只做可用性检测和 disabled 展示
mock：enabled=true，不走外部 CLI，用于核心闭环测试
```

OpenCodeAdapter 是第一版唯一真实 Agent Adapter，必须满足：

```text
可用性检测：opencode --version
执行命令：Executor 使用 --agent agent-hub-runtime，Planner 使用 --agent agent-hub-planner；支持 pure mode 时命令为 <verified_opencode_path> --pure run --format json --agent <agent_id> --dir <repo_path> <prompt>，旧版本使用无 --pure 的兼容命令但必须满足下述隔离前置条件
stdout / stderr：写入 ConsoleStream 和 console_messages
脱敏后的 JSON events：保存到 artifacts/logs
超时 / 非零退出码：返回明确 AgentResult.status 和 error_message
requires_write=true 且 AgentResult.status=succeeded 但无 ChangeSet：返回 blocked_by_guard；status=privilege_requested 且请求通过 schema/policy 预检时可以在无 diff 时进入 side gate
requires_write=false 的 analyze/review/docs agent_task：允许无 ChangeSet；output 节点不调用 Agent
执行前使用平台白名单：Windows 的 USERPROFILE / LOCALAPPDATA / APPDATA 和 POSIX 的 HOME / XDG_CONFIG_HOME / XDG_DATA_HOME / XDG_CACHE_HOME 必须指向 Agent Hub 专用 OpenCode profile，而不是用户常规 profile；额外只注入 OPENCODE_CONFIG_CONTENT、OPENCODE_CONFIG_DIR、NO_COLOR 和明确的 OPENCODE_DISABLE_* 安全开关
Agent 子进程环境中绝不能出现 AGENT_HUB_DEMO_TOKEN、通用 OpenAI/Anthropic/GitHub token、SSH agent socket 或其他 Master/CI token
执行前检查 forbidden_paths，不允许敏感文件进入 ContextPack
console 和 JSON event 在持久化前流式脱敏
```

### 20.1 OpenCode 每 task 强制运行时权限

Prompt 不是安全边界。`OpenCodeCliAdapter` 必须根据 `TaskPackage` 生成 `AgentRuntimePolicy` artifact，并通过高优先级 `OPENCODE_CONFIG_CONTENT` 注入。运行时策略至少包含：

```text
permission.* = deny
Executor read：只允许 readonly_files + effective_allowed_files + 已创建后的 effective_new_files + TaskContextBundle 中列出的精确文件，敏感模式最后显式 deny
Executor glob / grep / list：全部 deny；所需文件清单由 Master 放入 prompt/TaskContextBundle
edit：requires_write=false 时全部 deny；写任务只允许 effective_allowed_files + effective_new_files，privilege-assisted attempt 再加已消费 grant 的唯一 granted_existing_file
bash：Planner/Executor 永久 deny；测试 argv 只由 Master TestRunner 执行
external_directory：默认 deny，只对当前 task context bundle 目录开放只读访问
Planner：cwd 指向只读 PlannerContextBundle，只允许 bundle 内 read/glob/grep/list；其他工具 deny
task / skill / lsp / webfetch / websearch / question / todowrite = deny
所有未注册 MCP / plugin tool 通配 deny
share = disabled
autoupdate = false
snapshot = false
enabled_providers = 用户为该 Session 明确选择的 provider allowlist
agent.agent-hub-runtime / agent.agent-hub-planner = 本 task 内联定义，权限与上述规则一致
```

实际发给 OpenCode 的 JSON 必须遵循“最后一个匹配规则生效”的顺序，结构至少等价于以下 JSONC；`<...>` 由 Adapter 生成，不能由 Agent 填写：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "share": "disabled",
  "autoupdate": false,
  "snapshot": false,
  "enabled_providers": ["<session-approved-provider>"],
  "permission": {
    "*": "deny",
    "read": {
      "*": "deny",
      "<sorted exact allowed path>": "allow",
      "<task-context-bundle exact file>": "allow",
      "*.env": "deny",
      "*.env.*": "deny",
      "*.pem": "deny",
      "*.key": "deny"
    },
    "edit": {
      "*": "deny",
      "<sorted exact effective writable path>": "allow",
      "*.env": "deny",
      "*.env.*": "deny",
      "*.pem": "deny",
      "*.key": "deny"
    },
    "glob": "deny",
    "grep": "deny",
    "list": "deny",
    "bash": "deny",
    "external_directory": {
      "*": "deny",
      "<task-context-bundle>/**": "allow"
    },
    "task": "deny",
    "skill": "deny",
    "lsp": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "question": "deny",
    "todowrite": "deny"
  },
  "agent": {
    "agent-hub-runtime": {
      "mode": "primary",
      "permission": "<same generated executor permission object>"
    }
  }
}
```

Planner 使用另一份同形配置，只把 planner-view 内的 read/glob/grep/list 改为 allow，且 `external_directory/edit/bash` 保持 deny。路径先经 PathPolicy 规范化为 OpenCode 实际匹配格式；allow 项排序后插入，通配 deny 在前、敏感 deny 在最后。permission 对象的键顺序具有安全语义，必须从有序规则列表构建并对**实际传入环境的 UTF-8 字节**计算 hash，不能用 `sort_keys=True` 重新排序。`OPENCODE_CONFIG_CONTENT` 默认上限 16 KiB、effective file 上限 100、test argv 上限 20；超过时要求用户缩小 scope 并 `blocked_by_guard/runtime_policy_too_large`，不能退回低优先级配置文件。

Adapter 注册和每次 Master 启动时必须用已确认的绝对 binary 分别执行 `opencode --version`、`opencode --help`、`opencode run --help`、`opencode debug --help`，生成 `CliCapabilityManifest`。`run`、`--format json`、`--agent`、`--dir`、`debug config` 和 `debug agent` 是必需能力；任一缺失就 fail closed，不能只按 version 字符串猜测。`--pure` 是单独记录的可选全局能力。manifest 连同 binary/version/help hash 持久化；每次 task 前复核 executable hash/version，变化后必须重新 configure/确认。

能力探测完成后按以下规则选择模式：

1. 支持时，真实 Planner/Executor 命令一律带 `--pure`，禁止外部插件。
2. 不支持时默认 disabled。只有同时满足用户显式 `allow_legacy_opencode=true`、版本位于代码内测试过的 compatibility allowlist（首个目标为本机 1.2.27）、专用 HOME/USERPROFILE、空 task `OPENCODE_CONFIG_DIR`，并设置 `OPENCODE_DISABLE_DEFAULT_PLUGINS=true`、`OPENCODE_DISABLE_CLAUDE_CODE=true`、`OPENCODE_DISABLE_LSP_DOWNLOAD=true`、`OPENCODE_DISABLE_AUTOUPDATE=true` 才可尝试；每次运行显示残余风险并写 security_event。未知版本直接 `unsupported_opencode_compat`。
3. 无论是否支持 pure，在执行任何 `opencode debug/run` 前都静态扫描 Session repo、task cwd 到 data root 的祖先和项目启动文件。存在真实 `.env/.env.*`（`.env.example` 例外）、`opencode.json/jsonc`、`.opencode/**`、`AGENTS.md`、`CLAUDE.md`、`.claude/**` 或其他 Agent instruction/plugin/MCP/command 配置时，真实 Executor 进入 `blocked_by_guard/unsafe_opencode_project_config`。pure 只禁外部插件，不能当作禁项目配置或 dotenv。
4. OpenCode 版本、pure mode 支持状态、compatibility profile、专用 profile path 和启动扫描结果写入 task runtime event；argv 永远不能包含 `--auto`、`--share`、`--attach`、`--continue` 或 `--session`。
5. 正式 run 前在同一环境执行 `opencode debug config` 和 `opencode debug agent agent-hub-runtime`，只解析并校验安全子集；permission/provider/agent 之外，还要确认没有 active plugin、MCP、instructions、commands、formatter、LSP 或远程配置残留。最终结果与预期 policy 不一致时 fail closed。调试输出可能含配置值，不能原样写入 artifact。

规则顺序必须经过单元测试，因为 OpenCode permission 使用匹配规则决定最终结果。headless `opencode run` 不使用 `ask` 作为安全策略，避免进程等待不可见的交互审批；未明确允许的一律 `deny`。

OpenCode 配置来源是合并而非整体替换，inline config 虽然优先级高，仍不能删除未知的非冲突配置；因此 Demo 采用“项目启动文件预扫描拒绝 + resolved config 安全子集验证 + permission wildcard deny”三层策略，而不是假设 `OPENCODE_CONFIG_CONTENT` 会清空其他来源。Adapter 保存实际传入的 runtime policy 内容和 sha256，并把 hash 写入 task/event，便于审计。

OpenCode 实现基线在进入开发前再次对照官方 [CLI](https://opencode.ai/docs/cli/)、[Config](https://opencode.ai/docs/config/) 和 [Permissions](https://opencode.ai/docs/permissions/) 文档，并以本机 help/capability smoke test 为最终依据。文档中的固定 argv 只适用于 capability manifest 已确认的版本；不得因为未来 CLI 改名而降级为 shell 拼接或跳过 runtime policy。当前已知本机 `1.2.27` 不提供 `--pure`，因此只能走用户显式启用的 legacy compatibility 路径，不能作为默认安全模式。

### 20.2 OpenCode 凭据边界

OpenCode 需要读取其自身 provider auth store 才能调用模型，因此“子进程完全不能接触任何凭据”并不现实。Demo 使用 Agent Hub 专用 OpenCode profile：

1. `agent-hub configure-opencode` 在专用 profile 环境和专用空 auth cwd 中启动 OpenCode 官方认证流程，不以 Agent Hub 源码或 Session repo 为 cwd；用户只需为该 profile 登录一次。
2. Master 不解析、复制到其他位置、记录或传递 provider token；专用 profile 目录 ACL 仅允许当前用户。
3. OpenCode 进程通过专用 HOME/auth store 完成认证，模型工具通过 `external_directory=deny` 不能读取 profile。
4. 专用 profile 不加载用户常规全局 config、AGENTS 指令或 plugin 目录。
5. provider token 不进入环境变量、prompt、ContextPack、console、artifact 和 AgentResult。
6. Master API token 使用独立名称和生命周期，绝不进入 Agent 环境。
7. 如果 provider 只能依赖环境变量认证，则该 provider 在 Demo 中默认禁用，除非后续实现专用 credential broker。
8. OpenCode 会在启动期读取项目 `.env` 中的 provider key；工具级 read deny 对此无效。因此第 20.1 节的 `.env`/祖先目录扫描是启动硬前置，发现后不得执行 debug/run。用户必须把 OpenCode 凭据放在 Agent Hub 专用官方 auth store，而不是 Session repo 的 `.env`。

### 20.3 CommandGuard 的强制点

`CommandGuard` 不是 Agent 执行后的日志扫描器，而是三个强制点：

1. Workflow compile 时验证 `allowed_commands_candidate` 是否能映射到后端注册模板。
2. 运行前验证 OpenCode resolved permission 中 bash 仍为 deny；批准 argv 只写入 CompiledGraph 的 system test 节点，绝不传成 Agent bash allow。
3. 独立 `TestNodeHandler` 再次校验 argv，并以 `shell=False` 执行测试。

事后命令日志只用于审计，不能替代运行前阻断。

Privilege-assisted attempt 的 runtime policy 只能在 CapabilityBroker 验证并原子消费 `target_task_id=current_task` 的 grant 后，把其精确已有 resource 加入独立 granted_existing_files；AgentResult 中自报的 scope 永远不能直接进入 permission，bash 始终 deny。grant 在进程启动前消费，启动失败也不自动恢复，必须重新申请，避免重复使用。

Executor 的最终 assistant text 必须是完整 `AgentOutputEnvelope` JSON，并按 Planner 相同方式严格解析。Agent 只能提供 summary/risk_hints/next_suggestion/PrivilegeRequestProposal；Adapter 根据进程结果和 WorkspaceTransaction 构造 `AgentResult`，忽略任何自报 status、files_changed、ChangeSet、risk 或 approval 字段。Envelope 解析失败时保存脱敏 artifact 并返回 `parse_failed`，不能让该 attempt 进入 Merge 路径。

OpenCodeAdapter 构造 prompt 时必须显式写入：

```text
当前 task
effective_allowed_files
effective_new_files
当前已消费 CapabilityGrant 及其唯一 granted_existing_file（如有）
forbidden_files
Master 将在后续 TestNode 执行的测试 argv（仅供知悉，Agent 无 bash 权限）
禁止读取 .env、密钥、SSH key
禁止 git push / git merge / 绕过 Master
只允许完成当前 TaskPackage
最终输出修改摘要、风险、建议后续节点
最终 assistant text 必须只输出 AgentOutputEnvelope JSON，不要 Markdown fence 或额外文字
```

`OpenCodePlannerAdapter` 使用同一 CLI runner，但 cwd 是脱敏只读 PlannerContextBundle，不是真实 Session repo；runtime policy 强制 `edit/bash/task/lsp/webfetch/websearch/external_directory=deny`，只允许在 planner-view 内使用 read/glob/grep/list。Planner 只返回 WorkflowDraft JSON，不能复用执行 Agent 的写权限。

Planner JSON 解析必须从 OpenCode JSONL 事件中选择最终 assistant text event，并对完整文本执行一次严格 `json.loads + WorkflowDraft.model_validate`。不使用正则截取、Markdown fence 猜测或字段自动修补；解析失败保存脱敏 artifact、记录 `planner_output_invalid`，然后显式 fallback 到 RuleBasedPlanner。

---

## 21. GraphExecutor 核心执行闭环

GraphExecutor 调度的是 snapshot 中的 `node_run`，不是直接调度 Agent。Agent 只是 `agent_task` 的一个 NodeHandler 后端；PatchGuard、Test、Risk、Approval 和 Merge 都由各自 NodeHandler 执行。

Demo 使用数据库驱动的单 Master durable scheduler，不使用请求生命周期内的 FastAPI `BackgroundTasks` 充当任务队列：

1. `agent-hub serve` 获取 singleton Master lease 后启动一个 scheduler loop，默认 `max_active_handlers=1`；GUI/API 只创建 `pending` planner_run/workflow_run/node_run 并返回，不直接持有长任务。
2. scheduler 每次 tick 在验证 master fencing token 后，先恢复/推进已有 run，再从数据库原子 claim 一个 pending planner_run 或 ready node_run；内存 wake-up 只用于降低延迟，数据库轮询才是正确性来源。
3. `POST /plan` 和 `POST /run` 成功返回 `202 + planner_run_id/run_id`。OpenCode 进程、console 和状态推进在 scheduler 中执行，因此 HTTP 断开不取消任务。
4. Master 重启并取得新 token 后先执行 RecoveryManager：原 running handler 标记 orphaned，未开始的 pending/ready 记录可继续调度，waiting_approval 保持等待。
5. standalone CLI 若发现有效 `serve` lease，只提交命令并轮询数据库状态；若没有有效 Master，则原子取得临时 Master lease，在当前 CLI 进程中运行同一个 scheduler，直到 terminal 或 waiting_approval 后释放。approve/reject/resume/rerun 在无 `serve` 时同样短暂启动 scheduler 以继续推进。
6. Demo 不承诺并行 handler；多 Session 也按全局单 handler 串行。以后增加并行度时仍必须保留每 Session active-run、workspace lease 和数据库 claim 约束。

节点运行状态：

```text
pending
ready
running
waiting_approval
blocked_by_guard
failed
completed
skipped
superseded
cancelled
orphaned
```

终态是 `blocked_by_guard / failed / completed / skipped / superseded / cancelled / orphaned`。superseded attempt 不参与逻辑节点结果判定，Scheduler 只读取最新非 superseded attempt。其他终态同时记录可选 `outcome`：`success / failure / approved / rejected / blocked / cancelled`。

NodeRun 只允许以下白名单转换，Repository 用 CAS 拒绝其他转换：

```text
pending          -> ready | skipped | cancelled
ready            -> running | cancelled
running          -> completed | failed | blocked_by_guard | waiting_approval | cancelled | orphaned
waiting_approval -> completed | blocked_by_guard | superseded | cancelled
orphaned         -> superseded | cancelled       # 仅 RecoveryManager
其他终态         -> 不再修改；普通 rerun 创建 attempt+1
```

WorkflowRun 只允许：

```text
pending          -> running | cancelled
running          -> waiting_approval | paused | blocked | failed | completed | cancelled | orphaned
waiting_approval -> running | paused | blocked | cancelled
paused           -> running | blocked | failed | completed | cancelled | orphaned
blocked          -> running | cancelled           # 仅合法 rerun
orphaned         -> running | cancelled           # 仅 RecoveryManager 创建新 attempt 后
completed | failed | cancelled -> 不再修改
```

其他持久状态同样使用白名单：PlannerRun `pending -> running -> succeeded|failed|timed_out|cancelled|orphaned`；Task `pending -> running -> succeeded|privilege_requested|failed|timed_out|cancelled|blocked_by_guard|parse_failed|orphaned`。Session `active -> blocked|archived`，只有 RecoveryManager 在 workspace clean 且无 unresolved orphan 后可将 `blocked -> active`；archived 不可执行。Planner/Task 的 retry 一律创建新 run/attempt，不把 terminal 行改回 running。

ChangeSet 只允许以下白名单转换，Repository 必须用 expected-old-status CAS，并在同一事务写对应 event：

```text
captured         -> guard_passed | guard_rejected | abandoned_partial | quarantined | cancelled
guard_passed     -> test_passed | test_failed | stale | cancelled
test_passed      -> pending_approval | policy_rejected | stale | cancelled
pending_approval -> approved | rejected | stale | cancelled
approved         -> merged | stale | cancelled
其他状态         -> 不再修改
```

`guard_rejected / test_failed / policy_rejected / rejected / cancelled / stale / merged / abandoned_partial / quarantined` 都是 ChangeSet 终态。PatchGuard 发现 secret 时进入 `quarantined`；RiskClassifier/PolicyEngine 判定 L4 时进入 `policy_rejected` 且不创建 Approval；base、patch、compiled snapshot、policy、guard/test/risk/runtime evidence 任一失配时进入 `stale`。Approval 过期本身不改变 ChangeSet 状态：证据完全相同可按第 22 节新建 renew Approval，否则先将 ChangeSet 置为 `stale`。失败或取消后若要重试，必须产生新 task attempt 和新 ChangeSet，不能把终态记录改回 `captured`。

approval decision 发生在 workflow paused 期间时，只推进 approval/node 状态，workflow 继续保持 paused，直到显式 resume。每次转换必须与 event 在同一事务，并校验 expected old status；不能用“直接覆盖为目标状态”的通用 Repository 方法。

边满足规则：

```text
success：上游 outcome=success
failure：上游 outcome in {failure, blocked}
matched：if 上游 outcome=matched
not_matched：if 上游 outcome=not_matched
approved：上游 outcome=approved
rejected：上游 outcome=rejected
```

普通非 if/approval 节点的边默认 `success`。if 的正常分支必须显式使用 matched/not_matched，approval 必须显式使用 approved/rejected；Demo 不提供名称含糊的 `always/finally` 边，需要在真正失败后继续时必须显式连接 failure。
`cancelled` 不满足任何边；run cancel 直接取消所有未完成节点并终止 workflow，不能借 failure 边继续执行。

一个节点变成 `ready` 的条件：

1. 当前节点状态是 `pending`。
2. 所有入边都已经能判定为 `satisfied` 或 `inactive`。
3. 至少一条入边 satisfied；input 节点例外，可直接 ready。
4. 如果所有入边都 inactive，则当前节点进入 `skipped`，并继续向下传播分支判定。
5. Demo 不实现 join/parallel；同一 outcome fan-out 或一个节点有多个可能同时 active 的上游都由 ExecutableValidator 拒绝。唯一 output 可以接收由 outcome 条件证明互斥的 success/failure、matched/not_matched 或 approved/rejected 终止分支，这不视为 join。

`if` 节点只能读取 IfCondition 指定的结构化上游字段；条件为真返回 matched，为假返回 not_matched，只有缺失字段、类型不符或内部求值错误才返回 failure。不允许执行自然语言表达式、Python 或 shell，未被选择的正常分支进入 skipped。

GraphExecutor 执行流程：

1. Run API 使用 `expected_semantic_version` 读取 AuthorGraph，在 workspace lease 下固定 clean integration HEAD，冻结 agent catalog，重新 route + compile + validate；Layout 只随 run 保存快照，不参与执行 hash。
2. 保持该短时 lease，在一个事务内创建 workflow_run，将 `integration_base_commit=current_commit=session.integration_head_commit`，保存 Author/Compiled/Layout/catalog snapshot 和全部初始 node_run，提交后释放 lease。
3. Scheduler 按 snapshot 拓扑顺序计算 ready/skipped 节点；Demo 每次只 claim 一个节点执行。
4. 在同一事务校验 master lease 后，使用条件更新把 `ready -> running`；只有受影响行数为 1 的 scheduler claimant 获得执行权。
5. 根据 NodeRegistry 获取唯一 NodeHandler。
6. `agent_task` 在运行前解析 Agent assignment，创建 task、TaskPackage、ContextPack、runtime policy 和 console_session。
7. NodeHandler 返回 typed result 和 artifact refs；GraphExecutor 不在 handler 外偷偷执行其他节点职责。
8. 在一个事务内写入 node status/outcome、workflow status 和 event，再唤醒 scheduler。
9. handler 失败后按 outcome 激活 failure 边；没有合法失败路径时 workflow_run 进入 failed。
10. 所有逻辑节点的最新非 superseded attempt 都已判定且 output outcome 为 success/rejected 时 workflow_run 才能 completed；output 的 failure/blocked outcome 分别映射为 failed/blocked，cancel 则绕过 output 直接终止。
11. 每个 handler 必须支持幂等检查：相同 node_run 已存在成功输出时不得重复产生副作用。

Workflow run 状态：

```text
pending / running / waiting_approval / paused / blocked
failed / completed / cancelled / orphaned
```

pause 只把 workflow_run 改为 paused 并阻止调度新节点；node_run 保持原来的 pending/ready/running 状态，正在运行的 CLI task 不被隐式暂停。cancel API 先原子写 `cancel_requested_at + event`，Runner 通过内存信号和 DB 轮询终止进程树，然后执行 ChangeSet capture/restore；只有 workspace clean 才把 running/未完成 node 标记 cancelled 和 workflow cancelled，kill/恢复失败则 orphaned。重启恢复看到 cancel_requested_at 时优先完成取消，不得继续任务。skipped 只用于已判定不活跃的分支。rerun 只允许对终态节点创建新的 attempt；若已有下游节点启动，必须创建新的 workflow_run，不能原地篡改历史。

取消完成事务还必须把该 run 的全部 pending Approval 以 CAS 改为 `invalidated` 并递增 version，把未终态 PrivilegeRequest 改为 `denied`，把未消费 CapabilityGrant 写入 `revoked_at/revocation_reason=workflow_cancelled`，并把尚未 Merge 的完整 ChangeSet 改为 `cancelled`。这些更新、node/workflow 终态和事件必须同事务提交；已进入 `merge_finalizing_at` 的 run 不允许走该事务。已消费 grant 不改写 consumed 历史，但其 target task 仍取消，不能再次使用。

Approval 节点的恢复规则：

```text
执行到 approval 节点
  ↓
创建 approval
  ↓
node_run.status = waiting_approval
workflow_run.status = waiting_approval
  ↓
用户 approve
  ↓
事务内 CAS approval pending -> approved
重新校验 base_commit / patch_sha256 / evidence_sha256 / expires_at
node_run.status = completed, outcome = approved
写 approval_decided event
workflow_run.status = running
  ↓
GraphExecutor 继续寻找 ready 节点
```

用户 reject 时：

```text
事务内 CAS approval pending -> rejected
node_run.status = completed, outcome = rejected
patch artifact 保留，共享集成仓库保持 clean
存在 rejected 边则继续；否则 workflow_run = blocked
```

Scheduler 每次 tick 将到期的 pending Approval 用 CAS 标记 expired 并写 event。ChangeSetApproval 到期时 node/workflow 保持 waiting_approval，等待用户按第 22 节 renew 或 cancel；PrivilegeApproval 到期时同时把 PrivilegeRequest 标记 expired、当前 agent attempt 标记 blocked_by_guard/outcome=blocked，并按 failure 边推进。过期绝不能按默认批准处理。

Privilege side gate 不创建图节点：AgentTask attempt 返回 PrivilegeRequest 后进入 waiting_approval。批准事务必须同时把旧 attempt 标记 superseded、创建 attempt+1=pending、预创建 target task、创建并双向绑定 CapabilityGrant、写 event；拒绝事务把旧 attempt 标记 blocked_by_guard/outcome=blocked。任何部分失败都整体回滚。Scheduler 只允许该 target task 消费 grant，构造独立 granted_existing_files 后再启动进程。

进程异常退出或 Master 重启时，原 `running` 节点先进入 `orphaned`，不能直接重跑。RecoveryManager 完成 workspace、进程和 artifact 核对后，用户选择 retry 时原 attempt=superseded 并创建新 attempt；选择 cancel 时取消 run。恢复失败保持 orphaned 和 Session active-run 锁定。

---

## 22. 后端 API

Demo 后端也必须有本地安全边界：

```text
所有 HTTP API 必须校验 Authorization: Bearer <AGENT_HUB_DEMO_TOKEN>
Demo token 每次 Master 启动用 `secrets.token_urlsafe(32)` 生成或读取同等强度的显式配置，服务端用 `secrets.compare_digest` 校验；不能硬编码进前端 bundle、URL 或 localStorage
`agent-hub serve` 只在本地终端显示一次随机 token；GUI 首次打开时由用户输入并仅保存在内存，刷新后需要重新输入
CORS 只允许配置中的本地 GUI origin，例如 http://127.0.0.1:<frontend_port>，methods/headers 只开放实际使用的集合（含 Authorization、Content-Type、Idempotency-Key）
禁止 allow_origins=["*"] 搭配凭据
WebSocket 握手必须校验 Origin 和一次性短期 ws_ticket；长期 bearer token 不放查询字符串
默认监听 127.0.0.1，不默认暴露到 0.0.0.0
TrustedHost 只接受 127.0.0.1、localhost、[::1] 及显式本地配置；不信任任意 Host/X-Forwarded-*，Demo 默认关闭 proxy headers
除只返回固定 status/readiness、且不暴露版本/路径/配置的 /healthz 外，路由统一挂在带 bearer dependency 的 protected APIRouter，不能依赖每个 handler 手写鉴权
FastAPI /docs、/redoc、/openapi.json 默认关闭；显式开发模式启用时也必须经过同一鉴权 dependency
所有请求使用 extra=forbid 的 Pydantic request model，所有响应声明独立 response_model；不接收裸 dict/Any，不返回数据库行、内部绝对路径或 traceback
JSON 请求体默认上限 2 MiB，并按 graph/prompt/字段继续执行更小的模型限制；ASGI receive wrapper 对实际流入 bytes 计数，Content-Length 只作提前拒绝，缺失/伪造/chunked body 也不能绕过。Demo 不提供 multipart/upload API，超限在解析大对象前返回 413
artifact/diff 下载只通过 opaque ID 映射服务端路径，设置 nosniff 和安全 Content-Disposition；HTML/SVG/JS 等主动内容一律 attachment，GUI 只把允许的脱敏文本作为 React 文本节点渲染
API/GUI 响应设置 CSP、nosniff、frame-ancestors、Referrer-Policy 和最小 Permissions-Policy；生产 build 不发布公开 source map，不加载第三方 CDN script
所有 approve / reject / run-workflow / pause / cancel / rerun 请求必须写 events
AGENT_HUB_DEMO_TOKEN 只允许存在于 Master/API/GUI 进程，不允许传入任何 Agent 子进程
approve / reject 只能由带 token 的 GUI/API 用户请求或本地 CLI Application Service 触发；AgentResult / output artifact / console event 不能触发审批状态变更
```

实现 FastAPI 路由：

```text
GET /healthz

POST /sessions
GET /sessions
GET /sessions/{session_id}
GET /sessions/{session_id}/workspace-status
POST /sessions/{session_id}/recover-workspace

POST /sessions/{session_id}/plan
request: planner_mode=open_code|rule_based, optional parent_workflow_id；response: 202 + planner_run_id

GET /planner-runs/{planner_run_id}
GET /planner-runs/{planner_run_id}/console
GET /planner-runs/{planner_run_id}/artifacts

GET /sessions/{session_id}/workflows
GET /workflows/{workflow_id}
PUT /workflows/{workflow_id}
PUT /workflows/{workflow_id}/layout
POST /workflows/{workflow_id}/validate
POST /workflows/{workflow_id}/run

POST /workflows/{workflow_id}/auto-assign
POST /workflows/{workflow_id}/nodes/{node_id}/assign-agent
POST /workflows/{workflow_id}/nodes/{node_id}/lock

GET /workflow-runs/{run_id}
GET /workflow-runs/{run_id}/nodes
GET /workflow-runs/{run_id}/events
POST /workflow-runs/{run_id}/pause
POST /workflow-runs/{run_id}/resume
POST /workflow-runs/{run_id}/cancel
POST /workflow-runs/{run_id}/nodes/{node_id}/rerun

GET /tasks/{task_id}
GET /tasks/{task_id}/console
GET /tasks/{task_id}/events
GET /tasks/{task_id}/artifacts
GET /tasks/{task_id}/diff

GET /artifacts/{artifact_id}
GET /artifacts/{artifact_id}/content

GET /approvals
POST /approvals/{approval_id}/approve
POST /approvals/{approval_id}/reject
POST /approvals/{approval_id}/renew

GET /privilege-requests
decision: PrivilegeRequest 的批准/拒绝统一通过其关联 Approval API

POST /ws-tickets
WS /ws/planner-runs/{planner_run_id}/console
WS /ws/tasks/{task_id}/console
WS /ws/workflow-runs/{run_id}
```

请求契约：

Demo 使用显式 Bearer header，不使用 cookie/session 认证，因此不引入 CSRF token；CORS/Origin 只是浏览器侧纵深防御，不能替代 bearer 鉴权。认证失败、请求体超限和 ws_ticket 申请需要本地内存限流，限流状态不作为业务正确性来源；日志只记录脱敏后的 actor/route/error_code，不记录 token、请求正文或绝对文件路径。

GUI 交付采用同源优先：`npm run build` 生成 `web/frontend/dist`，`agent-hub serve` 只读托管该受信 build 目录并在终端打印 `http://127.0.0.1:<port>/`。静态根必须 canonical、位于项目 frontend dist、无 symlink/reparse，且与 ArtifactStore/Session repo 完全分离；SPA fallback 只对 `GET/HEAD + Accept: text/html` 生效，不能吞掉未知 API/WS 路径。开发时才单独运行 `npm run dev -- --host 127.0.0.1`，并把该精确 origin 加入 CORS。生产 build 关闭 source map，所有 script/style 自托管；StaticFiles/Starlette 使用锁定且已审计版本。

所有 create/edit/plan/validate/run/assign/lock/approval/pause/resume/cancel/rerun/recover mutation 必须携带 `Idempotency-Key` header；服务端按 actor + method/route/resource 形成 operation_scope，并把 canonical request hash 与响应写入 `idempotency_keys`。同 key/同请求重放原响应，同 key/不同请求返回 409。`POST /ws-tickets` 每次都必须生成新的随机 ticket，不参加重放。

1. `PUT /workflows/{id}` 接收 `author_graph + expected_semantic_version`；允许保存结构暂不完整的 draft，成功返回新 semantic_version，冲突返回 409。
2. `PUT /workflows/{id}/layout` 接收 `layout + expected_layout_version`，只保存已存在 node_id 的坐标；成功返回 layout_version，不触发重新编译。
3. `POST /validate` 接收 `expected_semantic_version`，短时取得 workspace lease 并固定 clean integration_base_commit，同时冻结 agent catalog，运行 DraftValidator、AgentRouter、Compiler 和 ExecutableValidator；返回 errors、warnings、CompiledGraph、compiled_hash、integration_base_commit、agent_catalog_hash、policy_version。成功且版本/HEAD 仍匹配时原子更新 last_compiled preview/source version/base/catalog/policy，但不递增 semantic_version、不修改运行状态。失败不覆盖上一次成功 preview。
4. `POST /run` body 接收 `expected_semantic_version + confirmed_compiled_hash`，幂等键使用 header。后端再次取得 workspace lease 并冻结 clean HEAD/catalog 后重编译；Agent spec 或 integration base 变化都会改变 hash，必须返回 409 并要求重新确认。成功时只原子创建包含 base 的 snapshot/run/node_runs 并返回 202 + run_id，由 durable scheduler 执行；layout_version 变化不影响执行确认。
5. 同一 Session 已有 active workflow_run 时拒绝创建第二个 run；用户必须先完成、取消或处理 orphaned run。
6. assign-agent / lock 是语义 mutation，必须携带 expected_semantic_version 并递增 semantic_version；auto-assign 只返回当前 catalog 下的预览，不写 AuthorGraph。
7. approve / reject body 包含 `approval_version + confirm_subject_hash`，幂等键使用 header；只能从 pending 原子转换一次。
8. rerun 只对允许的终态节点创建新 attempt；若已有下游执行则返回 409，提示创建新 workflow run。
9. Workflow WebSocket 使用 `after_run_seq` 从 events 补历史；planner/task console WebSocket 使用 `after_console_seq` 从 console_messages/chunk artifacts 补历史，再切换到实时流。两个游标独立，不能混用。
10. 对外 API 不提供单独 `POST /tasks/{task_id}/run`，避免绕过 workflow snapshot、NodeHandler 和 scheduler。
11. ws_ticket 只保存在 Master 内存，随机、一次性、30 秒过期，并绑定 token subject、Origin 和目标 task/run；服务重启后全部失效。
12. recover-workspace body 接收 `expected_fencing_token + resolution=retry|cancel`，幂等键使用 header。只有 owner 进程已结束、ChangeSet 可验证恢复且最终 repo clean 时才提交状态变化；不存在 force/reset 模式。
13. ChangeSetApproval 过期后可调用 renew，body 携带旧 `approval_version + confirm_subject_hash`：只有旧 approval 已 expired 且 version 匹配、repo clean、base/patch/evidence/compiled/runtime-policy hash 全部仍匹配时，事务内创建新 approval_id 并保持 node waiting_approval；旧记录不覆盖。PrivilegeApproval/PrivilegeRequest 过期不允许 renew，当前 attempt 进入 blocked_by_guard，必须通过 rerun 生成全新 request。
14. planner_run 成功后在同一事务创建新的 workflow lineage 并写 result_workflow_id；GET planner-run 返回该 ID。parent_workflow_id 必须属于同一 Session，replan 不允许覆盖 parent；失败/fallback 前序 run 不创建 workflow。
15. 所有 list API 使用 cursor，event API 使用 `after_run_seq + limit`，console API 使用 `after_console_seq + limit`；默认 100、上限 500，不能一次把全部日志读入内存。artifact/diff 通过鉴权后的流式响应读取，服务端复核 size/hash，quarantine 只提供脱敏预览。
16. cancel 对 active run 且 `merge_finalizing_at IS NULL` 时返回 202 cancellation_requested，不提前返回 cancelled；客户端持续观察直到 cancelled/orphaned。workspace clean 后的完成事务原子 invalidate pending Approval、deny pending PrivilegeRequest、revoke 未消费 Grant，并把未 Merge 的完整 ChangeSet 置为 cancelled。Merge 已设置 finalizing 线性化点时返回 409 `merge_finalizing`，用户等待结果后如需撤销只能新建 revert workflow。重复 cancel 通过 idempotency 返回同一结果，已 terminal run 返回当前状态且不重复写事件。

---

## 23. CLI 命令

实现 Typer CLI：

CLI 与 FastAPI 必须调用同一 Application Service / Repository 事务层，不能各自实现状态机或直接拼 SQL。CLI 作为 `actor_type=local_cli` 写审计事件；HTTP token 只保护网络入口。`approve/reject` 默认显示 subject hash、风险和 scope 并要求终端确认，自动化调用必须显式提供 approval version、subject hash 和幂等键，不能提供通用 `--auto-approve`。

CLI mutation 默认为每次用户动作生成 UUIDv4 idempotency key，并允许自动化调用通过 `--idempotency-key` 显式复用；该 key 走与 HTTP `Idempotency-Key` 相同的 Application Service 事务，不能只在 CLI 内存去重。

`plan/run-workflow/approve/reject/resume/rerun-node` 使用第 21 节相同的 scheduler 协议：检测到 `agent-hub serve` 的有效 lease 时提交后轮询数据库；没有有效 Master 时取得临时 singleton lease 并在 CLI 进程内推进。不得另写一套“CLI 直接执行 Agent”的旁路。

```bash
agent-hub serve --host 127.0.0.1 --port 8000 --frontend-dir web/frontend/dist
```

`serve` 取得 singleton Master lease，启动 API/scheduler/同源 GUI，并只在终端显示一次随机 demo token 和 GUI URL。Demo 的 `--host` 只接受 loopback 地址；需要远程访问属于后续身份/TLS/反向代理阶段，不能通过传 `0.0.0.0` 临时绕过。

```bash
agent-hub init-db
```

```bash
agent-hub register-agent --id mock --name "Mock Agent" --type mock
```

```bash
agent-hub configure-opencode
```

该命令在 Agent Hub 专用 OpenCode profile 中启动官方认证，不读取或打印 provider token。

```bash
agent-hub create-session \
  --source-repo C:/path/to/project \
  --base-ref HEAD \
  --goal "修复登录接口 Redis 缓存问题"
```

```bash
agent-hub show-session SESSION_ID
```

显示 source/base、integration repo 绝对路径、branch、HEAD、active run 和 workflow lineage，不输出 token/credential。

```bash
agent-hub plan SESSION_ID --planner open_code
```

从已有图重新规划时显式增加 `--parent-workflow-id WORKFLOW_ID`；命令创建新的 workflow lineage，不覆盖 parent。

```bash
agent-hub show-workflow WORKFLOW_ID
```

`show-workflow` 默认显示 AuthorGraph，并支持 `--compiled` 显示最近一次编译预览。

```bash
agent-hub auto-assign WORKFLOW_ID --expected-semantic-version VERSION
```

```bash
agent-hub validate WORKFLOW_ID --expected-semantic-version VERSION
agent-hub run-workflow WORKFLOW_ID \
  --expected-semantic-version VERSION \
  --confirm-compiled-hash HASH
```

```bash
agent-hub show-console TASK_ID
```

```bash
agent-hub show-events SESSION_ID
```

```bash
agent-hub show-artifacts SESSION_ID
```

```bash
agent-hub export-patch SESSION_ID --output C:/path/to/session-approved.patch
```

仅本地 CLI 可把 base..integration HEAD 的已批准本地 commits 导出为 patch bundle；API 不接受任意服务器输出路径。导出不修改 source repo、不 push，动作写 event 并校验每个 commit trailer 都属于该 Session 的已批准 ChangeSet。

```bash
agent-hub approve APPROVAL_ID --approval-version VERSION --confirm-subject-hash HASH
agent-hub reject APPROVAL_ID --approval-version VERSION --confirm-subject-hash HASH
agent-hub renew-approval APPROVAL_ID --approval-version VERSION --confirm-subject-hash HASH
```

```bash
agent-hub pause RUN_ID
agent-hub resume RUN_ID
agent-hub cancel RUN_ID
agent-hub rerun-node RUN_ID NODE_ID
agent-hub workspace-status SESSION_ID
agent-hub recover-workspace SESSION_ID --fencing-token TOKEN --resolution retry
```

---

## 24. 前端 GUI 要求

使用 React + React Flow。

React Flow GUI 不能被砍掉，它是 demo 的核心创新点。第一版目标不是做完整 ComfyUI 级自由编辑器，而是做**可用的核心编排界面**：后端仍是 workflow 权威源，前端负责可视化、基础编辑、Agent 分配、运行触发和审查。

页面结构：

```text
SessionPage
├── 顶部：用户目标、Workflow/History 选择、重新规划、保存、校验、运行、Author/Compiled 切换
├── 左侧：AgentPalette
│   ├── Claude Code
│   ├── Codex
│   ├── OpenCode
│   ├── Aider
│   └── Mock Agent
├── 中间：WorkflowCanvas
│   ├── 任务节点图
│   ├── 可拖动节点
│   ├── 可连接边
│   ├── 节点颜色显示状态
│   └── Agent 拖拽分配
├── 右侧：NodeConfigPanel
│   ├── 节点标题
│   ├── 指令
│   ├── assigned_agent
│   ├── assignment_mode
│   ├── resolved_agent_id / spec hash（CompiledGraph 只读）
│   ├── allowed_files_candidate
│   ├── new_files_candidate
│   ├── allowed_commands_candidate
│   ├── effective_allowed_files / new_files / commands（CompiledGraph 只读）
│   ├── risk_level_hint
│   ├── policy_risk_floor / runtime effective_risk（只读）
│   └── requires_changeset_approval（系统计算，只读）
└── 底部：AgentConsolePanel / DiffViewer / RiskPanel / ApprovalPanel
```

节点颜色：

```text
灰色：draft
蓝色：ready
黄色：running
绿色：completed
红色：failed / blocked_by_guard
紫色：waiting_approval
橙色：high_risk
青色画布覆盖层：workflow paused（节点本身不改为 paused）
浅灰虚线：skipped / superseded / cancelled
深红描边：orphaned
```

连线颜色：

```text
普通线：默认流程
绿色线：成功分支
红色线：失败分支
青色线：条件匹配分支
灰色线：条件不匹配分支
橙色线：审批分支
紫色线：拒绝分支
虚线：建议流程
```

画布与可访问性约束：

1. 普通/系统节点使用固定 `240x104` 尺寸，handle 位置固定；title 最多两行并 line-clamp，超长单词 `overflow-wrap:anywhere`，完整内容通过可访问 tooltip/右侧面板查看，动态状态不能改变节点尺寸。
2. 状态同时使用 Lucide 图标、文字和颜色，不只依赖颜色；所有交互按钮有可见 focus、`aria-label` 和 tooltip，边/节点选择支持键盘遍历和 Delete 但 system node 拦截删除。
3. 桌面使用左 palette + canvas + 右配置 + 底部审查面板；窄屏改为 canvas 全宽、palette/config/review 三个 tab/drawer，不把四个面板硬挤在同一行。
4. Planner/Agent/用户文本一律按纯文本渲染；禁止 `dangerouslySetInnerHTML`，Markdown 报告如后续启用必须禁 raw HTML 并做 URL scheme 白名单。
5. React Flow 节点、toolbar、minimap/controls 和底部面板使用稳定 min/max 尺寸，缩放/状态/长日志更新不能造成布局跳动或遮挡。
6. API base URL 来自构建期受控本地配置且只允许同机 http/https origin，不能由 query/localStorage/Agent 输出改变；所有 artifact 外链通过同源 opaque ID 获取，禁止把 Agent 提供的 URL 直接放入 href/src/navigation。
7. bearer token 仅保存在 React 内存状态，不写 localStorage/sessionStorage/IndexedDB、日志或错误上报；刷新后重新输入。前端不使用 service worker，不加载第三方 script/tag manager，依赖通过 lockfile 和本地 bundle 固定。

拖拽 Agent 逻辑：

1. 用户从 AgentPalette 拖 Agent 到节点。
2. 前端更新 `node.assigned_agent`。
3. `node.assignment_mode = manual`。
4. 使用当前 `expected_semantic_version` 保存 AuthorGraph；409 时提示语义版本冲突并重新加载/人工合并。

第一版 GUI 必须支持：

1. 从后端读取 AuthorGraph，并可切换查看后端生成的 CompiledGraph；两者必须有明确视觉区分。
2. 拖动节点后通过独立 layout endpoint 保存 `WorkflowLayout`；布局 autosave 只更新 layout_version，不使 CompiledGraph 失效。
3. CompiledGraph 使用 dagre 对固定尺寸节点做 LR 临时布局并 `fitView`；system node 不可拖动，临时坐标不回写、不参与 hash。Author/Compiled 切换不得覆盖 Author 坐标。
4. 从 AgentPalette 拖 MockAgent / OpenCode 到 `agent_task` 节点；disabled Agent 不允许拖入可执行节点。
5. 编辑 `title`、`instruction`、`assigned_agent`、`assignment_mode`、候选文件/命令和 `risk_level_hint`。
6. 编辑普通控制边并保存不完整 draft；校验错误在独立 Validate 操作中展示，不能让“必须始终合法”阻碍用户逐步搭图。
7. CompiledGraph 中的 `patch_guard`、`command_guard`、`test`、`risk_classifier`、`approval`、`merge_patch` 等系统节点锁定，只允许查看配置和来源规则。
8. Validate 后展示 errors / warnings、compiled_hash、agent_catalog_hash、policy_version、auto resolved Agent 和系统注入节点。
9. 上一次 preview 的 source semantic/catalog/policy/integration base 与当前不匹配时显示 stale，允许查看对比但不能确认或 Run；重新 Validate 成功后才恢复。
10. Run 前要求用户确认当前 CompiledGraph；如果后端重新编译 hash 变化，必须重新展示并确认。
11. 节点状态实时更新：`pending / ready / running / waiting_approval / blocked_by_guard / failed / completed / skipped / superseded / cancelled / orphaned`；workflow paused 以画布覆盖层显示，不伪造 node paused 状态。
12. 点击节点时显示 author config、effective config、console、ChangeSet、diff、risk、approval 和事件时间线。
13. 在 approval 节点上执行 approve / reject，提交 approval version 和幂等键。
14. WebSocket 断开后分别按 after_run_seq / after_console_seq 补 event 和 console，不能仅依靠内存中的前端状态。
15. semantic/layout version 冲突、base commit 变化、patch 失效和 orphaned workspace 都必须有明确阻断界面。
16. orphaned 界面显示 owner/fencing/dirty manifest 和安全恢复检查，只提供 retry/cancel，不提供 force reset。

GUI 权威边界：

1. 前端可以编辑风险提示和候选权限，但不能编辑 `effective_allowed_*`、`policy_risk_floor`、runtime `effective_risk`、`requires_changeset_approval`、system_managed 或 runtime policy。
2. 前端不自行插入/删除系统节点；CompiledGraph 始终以后端为权威。
3. 节点运行状态来自 node_run event，不写回 WorkflowNode。
4. GUI 可以保存不合法 draft，但 Run 按钮只在当前 semantic_version 存在已确认且仍有效的 CompiledGraph 时启用；纯布局变化不禁用 Run。

第一版 GUI 明确暂缓：

1. 任意自定义 node_type 插件。
2. parallel / loop / retry / join 的完整图编辑。
3. custom_shell / python / webhook / mcp 节点。
4. 复杂自动布局算法。
5. 前端自行插入安全节点；安全节点只能由后端 `PolicyInjector` 注入。
6. 直接透传用户输入到 Agent 原生终端。

---

## 25. 开发阶段

### 阶段 0：项目和契约基线（3-5 个开发日）

实现：

1. 初始化 Agent Hub git 仓库、pyproject、Vite React、lint/test 命令。
2. 建立 config、目录、artifact 根目录和专用 fixture source repo。
3. 完成 StrictModel、Enums、AuthorGraph / CompiledGraph / ChangeSet / Approval schema。
4. 完成 `init.sql`、schema version、FK/CHECK/partial index、idempotency repository 和 Repository 状态 CAS。
5. 完成 singleton Master lease 的原子获取、heartbeat、release、fencing 校验和重复实例 fail-fast 启动保护。
6. 输出 OpenAPI 草案和前端 TypeScript 类型。
7. 生成并提交 Python hash lock 与 package-lock，记录必要 install script；CI 运行 `pip-audit` 和 `npm audit`，发现未豁免的 high/critical advisory 时失败，豁免必须包含 advisory、影响判断和到期日。

完成标准：后端模型与数据库可初始化，第二个 Master 无法在有效租约期间启动，前端能用 fixture graph 编译通过；后续阶段不得再随意改变核心 ID、状态和 snapshot 契约。

### 阶段 1：确定性 Workflow 纵向切片（6-8 个开发日）

实现：

1. RuleBasedPlanner 的 bugfix / feature / refactor / docs 模板。
2. DraftValidator、WorkflowCompiler、PolicyInjector、ExecutableValidator。
3. NodeRegistry 和无副作用 NodeHandler 基础接口。
4. workflow semantic/layout version、三类 snapshot、node_run/event 状态机。
5. DurableScheduler + GraphExecutor 完成数据库轮询、DAG 顺序执行、条件边、skipped 和失败路径。
6. MockAgentAdapter 先支持只读/结构化结果。
7. CLI 完成 create-session、plan、validate、show-workflow、run-workflow。
8. React Flow 使用 fixture 实现 Author/Compiled 切换、节点拖动和 Agent 拖拽原型，提前验证图数据契约。

完成标准：CLI 可运行无真实写入的 Mock workflow，所有 node_run/event 可回放，snapshot 不受后续编辑影响；GUI 原型能编辑 AuthorGraph 并展示系统节点预览。

### 阶段 2：共享工作区、安全链和审批（8-12 个开发日）

实现：

1. Session 专用 `--no-hardlinks` 本地 clone、共享集成 repo 和本地 integration branch。
2. LockManager lease、heartbeat、fencing token、RecoveryManager。
3. WorkspaceTransaction、完整 ChangeSet 捕获和逐项 clean 恢复。
4. exact existing/new PathPolicy、PatchGuard、CommandGuard、RiskClassifier、command/docs_static TestNodeHandler。
5. ApprovalManager、PrivilegeManager、CapabilityBroker 的 CAS/一次性消费。
6. MergePatchNodeHandler 校验 hash 后由 Master apply + local commit。
7. ConsoleStream、StreamingRedactor、artifact 元数据和事件审计。

完成标准：Mock 写入任务可走 `agent_task -> patch_guard -> (command_guard + command test | docs_static test) -> risk -> approval -> merge_patch`，审批前后共享 repo 都满足定义的 clean 状态；拒绝、越权、测试失败和崩溃恢复均有集成测试。

### 阶段 3：通用 CLI Runner 与 OpenCode（8-12 个开发日）

实现：

1. CliAgentSpec、CliAgentRunner、binary path/hash 确认和进程树回收。
2. 有序 AgentRuntimePolicy 生成、实际 bytes hash、inline size 上限和 `OPENCODE_CONFIG_CONTENT` 注入。
3. OpenCodeCliAdapter JSONL 解析、输出上限、timeout/cancel。
4. PlannerContextBundle、OpenCodePlannerAdapter 只读智能拆解、workflow lineage 和 RuleBasedPlanner fallback。
5. `--pure` 能力检测、legacy 显式 opt-in/compatibility allowlist、项目启动文件拒绝和 Agent Hub 专用 OpenCode profile/auth 流程。
6. provider allowlist、OpenCode auth-store 边界和脱敏。
7. Codex / Claude Code / Aider disabled skeleton 和可用性检测。

完成标准：fake CLI 覆盖全部生命周期；支持 pure 的 OpenCode 可在强制 runtime policy 下完成一次真实只读规划和一次受控写入尝试。本机 1.2.27 只有显式 legacy opt-in 且 compatibility 检查通过时允许同样尝试；无凭据时真实 smoke test skip，但 fake CLI 测试不得 skip。

### 阶段 4：FastAPI 与 GUI 数据闭环（6-8 个开发日；两人协作时可与阶段 3 后半并行）

实现：

1. Sessions、Workflow lineage、Runs、Approvals/renew、Artifacts/pagination API。
2. expected_semantic_version、expected_layout_version、compiled preview stale 检查和持久化 idempotency key。
3. WebSocket ticket、event seq 和断线续传。
4. 前端 API client、semantic/layout version 冲突处理和运行状态同步。
5. AgentPalette 中 Mock enabled；OpenCode 按 capability/pure-or-legacy policy 显示 enabled 或具体禁用原因，其他 Agent 明确 disabled。
6. protected APIRouter、请求体上限、TrustedHost/CORS/WS ticket、安全头，以及 `agent-hub serve` 对 production frontend build 的同源 loopback 托管。

完成标准：GUI 能从真实 API 读取、保存、校验并运行 Mock workflow，刷新或断线重连后状态不丢失。

### 阶段 5：React Flow 核心产品体验（7-10 个开发日）

实现：

1. SessionPage、WorkflowCanvas、AgentPalette、NodeConfigPanel。
2. AuthorGraph 编辑、dagre CompiledGraph 系统节点布局、stale 预览和确认运行。
3. AgentConsolePanel、DiffViewer、ChangeSet/Risk/ApprovalPanel。
4. pause/cancel/rerun、approval、冲突和 orphaned 阻断界面。
5. 组件测试、桌面/移动无重叠截图和 Playwright 主流程。

完成标准：GUI 完成节点图创建/调整、Agent 分配、运行、状态追踪、diff 审查和审批闭环，不能退化成只读展示页。

### 阶段 6：恢复、安全和交付验收（6-9 个开发日）

实现：

1. 全量失败注入、锁竞争、进程崩溃和数据库重启测试。
2. 路径/命令策略 fuzz 或参数化测试。
3. OpenCode permission policy 回归测试。
4. E2E acceptance 脚本、README、Demo fixture 和启动脚本。
5. 性能上限：100 节点图、10 MB console、连续 20 次 run 不丢 event。
6. Master lease 过期接管、旧 token 写入阻断和异常退出恢复测试。

完成标准：第 27 节验收标准全部自动化或有明确人工步骤，工作区无未归属 dirty 状态。

单人顺序开发估算约 44-64 个开发日，即 9-13 周；首次接触 OpenCode 内部配置或 Windows 进程/路径边界时建议另留 20% 风险缓冲。两名开发者在契约冻结后可并行阶段 3 与阶段 4，但共享协议、数据库 migration 和 E2E 仍需串行集成。该估算不包含完整自由工作流编辑器、第二个真实 CLI Agent、容器沙箱、远程 Worker 和生产级身份系统。

---

## 26. 测试要求

必须写测试：

```text
test_storage_schema.py
- init-db 重复执行幂等，schema_migrations 版本正确，较新未知 schema 拒绝降级
- 每个连接启用 foreign_keys/busy_timeout，数据库启用 WAL
- 完整 fixture 插入后 PRAGMA foreign_key_check 为空，删除被引用实体被拒绝
- node assigned_agent、console workflow/session/message、privilege/grant 精确 resource 的 FK/NOT NULL 约束生效
- planner/workflow、task/artifact/change_set、task/capability 的 nullable-first 回填事务可提交，半途失败整体回滚
- active workflow 和 pending approval partial unique index 在并发事务下只有一个成功
- 所有 status/risk/type/capability/action CHECK 拒绝非法值
- UTC 微秒 Z 时间可稳定排序/过期比较，混合时区格式被 Repository 拒绝

test_protocol_models.py
- 未知字段被 extra=forbid 拒绝
- Enum 非法值拒绝
- session/planner/task/node/workflow/change-set/approval/privilege/security 状态均有显式 Enum
- ID/hash/Git object ID 长度与字符集别名拒绝路径、控制字符、非 hex 和错误长度
- Planner 节点/边/文本上限生效
- WorkflowNode 不包含运行状态

test_policy_engine.py
- 禁止 git_push
- 禁止 access_secrets
- 允许 read_file
- 用户 risk hint 不能降低 policy minimum

test_command_guard.py
- 注册模板中的 pytest argv 通过
- 禁止 rm -rf
- 禁止 cat .env
- 禁止 git push
- 拒绝 shell 元字符、重定向、命令替换和危险 pytest/node 参数
- 用户文本不能直接成为 executable argv

test_test_runner.py
- 只执行 CommandGuard 已批准的固定 executable/argv，使用 shell=False 和 Session repo cwd
- 最小环境不包含 Hub/API/provider/SSH 凭据
- stdout/stderr 流式脱敏且受大小上限约束
- timeout/cancel 会终止完整测试进程树并恢复 workspace clean
- test_requires_sandbox 时阻断且不能通过“跳过测试”进入 Approval/Merge
- docs_static 仅校验纯文档 UTF-8/非空/NUL/大小/基本链接且不启动子进程，混合 scope 或 requirements/constraints/config 等敏感 basename 拒绝

test_path_policy.py
- 拒绝 ../ 逃逸、绝对外部路径、UNC、设备路径和 NTFS ADS
- 拒绝 Windows 保留名、尾随点/空格、8.3/Unicode 规范化碰撞、控制字符和不稳定 UTF-8 路径
- 拒绝 symlink、junction、reparse point 和包含它们的父目录
- Windows 大小写变化不能绕过 forbidden path
- .git、密钥模式和其他 Session 路径永久拒绝
- exact path 规则拒绝 glob/目录 scope；existing candidate 必须存在，new candidate 必须不存在且最近现有祖先/缺失父层级安全，二者 casefold/NFC 不重叠

test_patch_guard.py
- 修改 allowed_files 通过
- 修改 .env 拒绝
- 修改 allowed_files 外文件拒绝
- 修改 Dockerfile 标记中高风险
- staged、unstaged、untracked、ignored、deleted、renamed、binary 均进入 ChangeSet
- 非 ephemeral ignored 文件变化直接拒绝合并
- 修改 .git 元数据和 submodule 指针拒绝
- 修改 .gitattributes / .gitmodules 拒绝，.gitignore 标记 L2
- 疑似 secret 的 ChangeSet 进入 quarantine，不能审批或合并
- secret security_event 只含 rule/path/content hash，不含匹配原文
- 普通 git diff 漏掉的文件仍能被检测
- modified/deleted 只能命中 effective existing scope，created 只能命中 effective_new_files；rename source/destination 分别命中 existing/new 且不能覆盖已有文件

test_changeset_lifecycle.py
- 只接受第 21 节 ChangeSet 白名单转换，错误 expected-old-status 和终态回退均被 CAS 拒绝
- PatchGuard 通过/拒绝分别进入 guard_passed/guard_rejected，疑似 secret 直接 quarantined
- command/docs_static 测试通过进入 test_passed，命令失败或测试污染工作区进入 test_failed
- L4 runtime risk 进入 policy_rejected 且不创建 Approval；其余风险进入 pending_approval
- 用户批准/拒绝分别进入 approved/rejected，只有 approved 且证据未变才能 merged
- base、patch、compiled/policy/guard/test/risk/runtime evidence 变化使 ChangeSet stale
- 非零退出、parse_failed、timeout、运行中 cancel 或 privilege request 已留下的部分 diff 进入 abandoned_partial，不能进入 PatchGuard
- 完整 ChangeSet 在 Merge finalizing 前随 workflow cancel 进入 cancelled；finalizing 后 cancel 返回 409 且不能改写状态
- 每次状态转换、updated_at 和对应 event 在同一事务提交或回滚

test_risk_classifier.py
- 1/5 paths 为 L1，6/20 为 L2，21 为 L3，边界无 off-by-one
- dependency/config/schema/Dockerfile/CI/test/bootstrap/.gitignore 至少 L2
- auth/security/payment/permission、delete/rename/binary 至少 L3
- secret/.git/repo 外/Master policy 为 L4 并直接拒绝、不创建 Approval
- policy/user/planner/agent/runtime 风险取 max，任何 hint 都不能降低 floor

test_draft_validator.py
- ID/引用/未知类型/非法环/规模限制
- 普通边默认 success，含糊的 always/finally condition 被协议拒绝
- 不完整 draft 可以保存但 validate 不通过
- AuthorGraph 不能伪造 system_managed 节点
- AuthorGraph 不能直接创建 system-only node_type
- AuthorGraph/Planner/API 不能提交 compiler-only effective permission、policy_risk_floor、requires_changeset_approval、test_kind 或 test_argv 字段
- allowed_files_candidate/new_files_candidate 分别满足 base 存在/不存在，禁止通配符、目录和重叠
- agent_task 缺少 task_kind、非 agent 节点携带 Agent assignment 时拒绝
- if_condition 出现在错误 node_type 时拒绝
- if 只能引用白名单上游字段和有限 operator，脚本/自然语言条件拒绝
- if 引用必须是所有到达路径上的 transitive predecessor，operator/value 类型与字段 Enum 严格匹配
- if 必须显式使用 matched/not_matched 正常分支，可选 failure 错误分支；用 success/failure 表示真假或非 if 发出 matched 均拒绝

test_workflow_compiler.py
- 相同 AuthorGraph + policy_version + agent_catalog_snapshot + integration_base_commit 生成相同 CompiledGraph/hash/系统节点 ID
- 仅 nodes/edges/set-like 路径输入顺序变化不改变 graph hash；argv/串行测试顺序变化会改变 hash
- 写任务补齐 patch_guard/test/risk/approval/merge 链
- command test 前补 command_guard，docs_static 不生成命令节点
- 候选路径/argv 与策略求交后固化 effective scope、policy risk floor 和 ChangeSetApproval 要求
- effective existing/new scope 分开固化，OpenCode edit 取并集但 PatchGuard 不混淆 created/modified
- ProjectedFileState 允许后序任务修改支配前序声明的新文件；前序未实际创建或 new 路径已存在时运行前 fail closed
- 分支上非支配任务声明的新文件不能被另一分支当 existing，禁止跨不确定路径推断
- 每个批准 argv 生成一个确定性串行 test 节点，无批准 argv 时生成阻断 test 节点
- 纯 docs scope 生成无进程 docs_static test；混合/代码路径不能使用 docs_static 绕过 command test
- 写任务原 success 后继只接在 Merge 成功后；rejected/guard/test/merge failure 进入互斥终止报告分支
- 不允许用户 agent_task 被隐式嵌进 pre-merge 系统安全链
- 用户修改 AuthorGraph 后不会复用旧 compiled hash

test_executable_validator.py
- 所有成功路径检查 PatchGuard
- Merge 前无绑定同一 ChangeSet 的 Approval 时拒绝
- 失败/拒绝路径无终点时拒绝
- 用户控制边不能绕过系统安全链
- Demo 拒绝 parallel/join 和多个同时 active 上游
- 同一节点两条相同 outcome fan-out 拒绝，if 的 matched/not_matched 和 approval 的 approved/rejected 互斥分支允许

test_policy_injector.py
- 自动给 agent_task 后插入 PatchGuard
- 自动给高风险节点插入 Approval
- 自动给 merge 前插入 Approval
- risk_classifier 是已注册节点
- 系统节点和边只读、确定性且幂等注入
- 所有写入链都生成最终 ChangeSetApproval，用户/Planner 无字段可移除

test_agent_router.py
- 分析任务推荐 Mock Agent / OpenCode
- 代码修改任务推荐 OpenCode
- 写入任务在 OpenCode 不可用时 blocked_by_guard，不能静默切换为 mock
- review task_kind 在 OpenCode 可用时推荐 OpenCode，Mock 仅显式模拟
- locked agent 不可用时 blocked_by_guard，不能自动换人
- 编译期把 auto/manual/locked 解析为 resolved_agent_id/spec hash
- snapshot 后 Agent 不可用只阻断，执行期不重新路由
- auto-assign preview 不修改 AuthorGraph/semantic_version

test_workspace_transaction.py
- Session repo 从 source base commit 创建，用户原工作树不被修改
- Session integration_head_commit 初始化为 base；所有 repo 操作要求实际 HEAD=session head=active run current_commit
- source dirty 内容不进入 Session；submodule/LFS filter 仓库明确拒绝
- AgentTask 捕获 ChangeSet 后恢复 clean，等待审批时不持锁
- staged/untracked/ignored 文件逐项恢复，不调用 reset --hard / clean -fdx
- 临时 GIT_INDEX_FILE 从 base + 最终 worktree 构造唯一 canonical binary patch，不修改真实 index；staged/unstaged evidence 不被拼接应用
- 同一文件同时有 staged/unstaged 修改时 canonical patch 只表达最终状态一次；应用到 clean base 后精确复现 post_state_hash
- 隐式父目录只服务于 exact new files；恢复按深度删除本 task 创建的空目录，非空目录不递归删并进入 orphaned
- TestNode 临时 apply 后恢复 clean
- TestNode 记录 applied-state baseline；测试修改非 ephemeral 文件时 blocked，副作用与 Agent patch 按顺序分别恢复
- ephemeral policy 只删除本次新建且清单内 descendants，不递归清空预存目录或跟随 symlink
- MergePatchNodeHandler 只应用审批绑定 hash 并由 Master 创建本地 commit
- Merge 完成事务同时更新 session integration_head/run current_commit/event；外部 HEAD 漂移直接阻断
- merge_finalizing CAS 与 cancel 线性化：cancel 先提交则不 apply，finalizing 先提交则 cancel 409
- base 变化或 git apply --check 失败时 blocked_by_guard
- 恢复校验失败进入 orphaned 并阻止后续写入
- finalizing 崩溃时按 commit trailer/tree hash 区分“已 commit 补双 expected HEAD+事件”“未 commit 可恢复”“不确定 orphaned”
- timeout/cancel/non-zero exit 仍执行 ChangeSet capture 和 finally restore
- cancel 先持久化 cancel_requested_at，clean restore 后才 cancelled；kill/restore 失败进入 orphaned
- recover-workspace 仅在进程结束且最终 clean 时允许 retry/cancel，不存在 force/reset
- changed path/patch/created bytes 超过配置上限时终止、完整恢复并 blocked_by_guard，不能截断后审批

test_git_manager.py
- 使用固定 binary、shell=False、显式 cwd 和最小 Git 环境
- hooks、pager、askpass、external diff、textconv 和 commit signing 被禁用
- GIT_CONFIG_NOSYSTEM + 专用 HOME 阻止用户 alias/filter/credential helper 生效
- Session 仅接受 canonical local path，clone 使用 --no-hardlinks 且不递归 submodule/remote URL
- clone 后移除所有 remote，integration repo 的 git remote 列表为空
- base_ref 选项注入被拒绝并通过 --end-of-options 解析固定 commit
- commit 使用固定本地身份和审计 trailer
- restore/add/rm 没有显式 pathspec 时拒绝
- push/fetch/pull/merge/rebase/checkout 不在运行白名单

test_artifact_store.py
- artifact path 由服务端生成，拒绝 ../、绝对路径和 symlink 逃逸
- 写入后 sha256/size 与数据库一致
- owner-only ACL 和 API 鉴权读取生效
- quarantine artifact 只返回脱敏预览，不能进入 Approval

test_console_repository.py
- secret/ANSI/OSC 跨 read/chunk 边界仍被 rolling-overlap redactor 清理，未脱敏 bytes 不落临时文件
- stdout/stderr 脱敏后按 UTF-8 边界拆成 <=64 KiB chunk，SQLite 只存 artifact ref/seq/size
- artifact 原子 rename 与 DB metadata/message 同事务发布，提交前不广播
- after_console_seq 可按序补读 chunk，workflow after_run_seq 与 console cursor 不混用
- 10 MiB 超限写 truncation chunk/event、终止进程且任务不能 succeeded
- 启动清理无 DB 引用的过期 temp/orphan chunk 并写 security_event

test_lock_manager.py
- 两个 writer 竞争时只有一个获得租约
- heartbeat/release 必须匹配 owner + fencing token
- release 不删除 resource 行，下一 owner 的 fencing token 严格递增
- 旧 owner 在 lease 过期后不能继续提交
- Master 重启可识别过期锁和仍存活进程

test_idempotency_repository.py
- mutation 与 idempotency response 在同一事务提交/回滚
- 同 actor/operation/key + 同 request hash 重放原 status/body
- 同 actor/operation/key + 不同 request hash 返回 409
- 不同 actor 或 operation scope 可安全复用相同 key
- 并发相同 key 只创建一个 planner_run/workflow_run/approval decision
- 过期 key 按 TTL 清理，不影响尚在有效期的响应

test_master_lease.py
- 两个 Master 启动时只有一个能持有 scheduler lease，第二个 fail fast
- 正常释放不删除 lease 行；下一实例获取时 fencing token 严格递增
- 租约过期接管后，旧 instance/token 不能 claim node、推进状态或提交 scheduler event
- heartbeat/release 必须同时匹配 instance_id 和 fencing token
- scheduler event payload 记录本次 master_fencing_token

test_durable_scheduler.py
- plan/run API 只创建持久化 pending 记录并返回 202，不在请求 BackgroundTasks 中执行 CLI
- scheduler 从数据库 claim pending planner_run/ready node_run，max_active_handlers=1
- HTTP 客户端断开不取消已入队任务，内存 wake-up 丢失后轮询仍会推进
- 重启接管后 pending/ready 继续，running 先 orphaned，waiting_approval 保持等待
- standalone CLI 在无 serve 时取得临时 Master lease；有 serve 时不争抢 lease，只提交并轮询
- approve/reject/resume/rerun 在无 serve 时可启动同一临时 scheduler 继续推进
- 重启发现 cancel_requested_at 时优先完成 kill/recovery，不继续运行节点

test_cli_agent_runner.py
- 使用 fake CLI 验证 shell=False、固定 argv 和绝对 executable path/hash
- 用户 prompt 中包含 shell 特殊字符时不会被 shell 注入
- stdout / stderr 并发读取、流式脱敏、seq 单调且不会死锁
- timeout/cancel 后终止完整子进程树
- 非零退出码、启动失败、输出超限返回稳定 error_code
- JSONL 未知事件/单行损坏不会丢失后续事件
- 原始 JSONL 仅在有界内存解析，console 与 event 字段分别脱敏后持久化，替换不会破坏 decoder 输入且 raw line 不进异常日志
- 环境变量白名单不包含 AGENT_HUB_DEMO_TOKEN、API token、SSH_AUTH_SOCK
- executable hash 变化时拒绝执行
- disabled CLI Adapter 不参与自动执行

test_opencode_runtime_policy.py
- 默认 permission deny
- read/edit 只允许 task 有效路径，敏感路径最终 deny
- Executor 的 glob/grep/list/bash 永久 deny，批准测试 argv 只进入 system TestNode
- external_directory 默认 deny，仅当前 TaskContextBundle 只读例外；task/skill/lsp/webfetch/websearch/MCP/plugin tool deny
- Planner 只在脱敏 planner-view 内允许 read/glob/grep/list，真实 Session repo 和 external_directory 不可见
- share/autoupdate/snapshot 禁用，provider allowlist 生效
- permission 规则按 deny -> sorted allow -> sensitive deny 顺序输出，hash 对实际有序 UTF-8 bytes 计算
- runtime policy 超过 16 KiB、100 effective files 或 20 test argv 时 fail closed
- runtime policy hash 写入 task/event
- Planner policy 强制只读且禁止 bash/edit/task/web
- capability probe 必须确认 run/format-json/agent/dir/debug-config/debug-agent；缺一项即 disabled，不能仅凭 version 推断
- capability manifest 固化 binary/version/help hash；task 前 hash/version 漂移要求重新 configure
- 支持 --pure 时强制加入 argv
- 不支持 --pure 时默认 disabled；只有显式 allow_legacy_opencode + 测试过的 compatibility version 可尝试并写 security_event，未知旧版拒绝
- pure 与兼容模式都在任何 debug/run 前拒绝 `.env*`、opencode/AGENTS/CLAUDE/.claude 等项目启动文件
- HOME/USERPROFILE 指向专用 profile，不读取用户常规 OpenCode plugin/config
- resolved config/agent 存在 plugin/MCP/instructions/commands/formatter/LSP 或安全子集与预期不一致时拒绝运行

test_graph_executor.py
- 执行简单 workflow
- Repository 只允许第 21 节 NodeRun/WorkflowRun 白名单转换，错误 expected old status 的 CAS 不生效
- 测试节点失败走失败分支
- 未选择分支进入 skipped，不能让 workflow 永久 pending
- if true/false 分别走 matched/not_matched，求值异常才走 failure；正常 false 不把 workflow 标记失败
- OutputNode 对 success/rejected 生成 completed，对 failure/blocked 保留 workflow 终态；cancel 绕过 output，不把失败报告成成功
- approval pending 时暂停
- privilege_request 使当前 attempt waiting_approval，但不修改 CompiledGraph
- privilege approve 原子 supersede 旧 attempt、创建 grant 和 attempt+1
- privilege approve 同事务预创建 target task 并双向绑定 grant；任何一步失败全部回滚
- privilege reject 使当前 attempt blocked_by_guard 并走 failure 边
- privilege attempt 的部分 ChangeSet 标记 abandoned_partial，只作为新 attempt 只读参考
- approval approve 后继续调度
- approval reject 走 rejected 边或进入 blocked
- shared repo dirty 时写入节点进入 blocked_by_guard
- ready -> running claim 只有一个 worker 成功
- pause 只改变 workflow 状态且不启动新节点，node 保持 pending/ready/running；cancel 终止进程树并取消未完成节点
- cancel 完成事务同时 invalidates pending approvals、denies pending privilege requests、revokes 未消费 grants，并取消完整未 Merge ChangeSet
- 已有下游执行时禁止原地 rerun
- running 节点在 Master 重启后进入 orphaned
- paused 期间审批可完成 node，但 workflow 保持 paused，直到显式 resume
- Agent next_suggestion 只保存 artifact/event，不增加当前 run 的 node；采纳时创建新 workflow lineage

test_approval_manager.py
- ChangeSetApproval 绑定 workflow/node/change_set/base/patch/evidence hash
- evidence manifest 覆盖 compiled/policy/guard/tests/risk/runtime policy hash
- privilege-assisted ChangeSet evidence 额外覆盖 request/grant/target task/resource/consumed fencing token
- PrivilegeApproval 绑定 privilege request/action/resource hash，不要求 change_set
- 对应 subject hash 任一变化使 Approval invalidated
- approve/reject CAS 和 idempotency key 防止重复推进
- approve/reject/expire/invalidate 均递增 version，renew 新记录从 version=1 开始
- 过期 Approval 不能批准
- ChangeSetApproval 仅在所有 subject/evidence hash 仍匹配时可 renew 为新记录
- PrivilegeApproval 过期不可 renew，请求/attempt 进入 expired/blocked 路径
- workflow cancel 对 pending Approval 执行 version CAS invalidation，之后 approve/renew 均拒绝

test_opencode_adapter.py
- opencode --version 可检测可用性
- 无凭据或不可运行时只 skip 真实 smoke test，fake CLI 测试不 skip
- Executor/Planner 分别使用固定 agent ID，argv 含可用时 --pure、--format json、--agent、--dir，不走 shell=True，且绝不含 --auto/--share/--attach/--continue/--session
- requires_write=false 允许无 diff
- requires_write=true 且 succeeded 但无 ChangeSet 时 blocked_by_guard；PrivilegeRequest side gate 例外
- 最终 assistant text 严格解析 AgentOutputEnvelope，解析失败不能进入 Merge
- Agent 自报 status/files_changed/risk/approval 字段被 schema 拒绝，Master 结果不受其控制
- 执行环境不会传递非必要敏感环境变量
- 执行环境绝不包含 AGENT_HUB_DEMO_TOKEN 或常见 API token

test_opencode_planner.py
- 只读 Planner 生成合法 WorkflowDraft
- PlannerContextBundle 排除敏感/二进制/大文件/CLI 配置且无 Git 元数据，manifest/hash/只读 ACL 正确
- planner_run 固化 integration base/bundle artifact/hash；RuleBased fallback 复用相同 base/manifest
- Planner 进程 cwd 是 planner-view，构建后释放 Session lease，candidate path 重新映射并校验真实 base commit
- planner-view 超预算时确定性截断并写 event，不能回退读取真实 repo
- 非 JSON、超规模、未知字段和非法 node_type 被拒绝并 fallback
- 只解析最终 assistant text 的完整 JSON，不做正则截取或自动修补
- Planner 推荐权限不能直接成为 effective permission
- OpenCode 失败和 RuleBased fallback 分别持久化 planner_run/console/artifact，并用 fallback_from_run_id 关联
- 每个成功最终 planner_run 创建新 workflow/result_workflow_id；replan 记录 parent lineage 且不覆盖旧 AuthorGraph
- 失败 OpenCode run 不创建 workflow，只有成功 fallback 创建一个

test_api_security.py
- HTTP API 无 Authorization 时拒绝
- protected APIRouter 默认鉴权；只有无敏感信息的 healthz 可匿名，docs/openapi 默认关闭
- 非本地 Host、伪造 X-Forwarded-*、未允许的 CORS method/header 被拒绝，默认只监听 loopback
- WebSocket ticket / Origin 校验失败时拒绝
- WebSocket ticket 一次性、过期、目标或 Origin 不匹配时拒绝
- CORS 不允许通配 origin 搭配凭据
- AgentResult / console event 不能改变 approval 状态
- token 不出现在 URL、前端 bundle 和 Agent 环境
- list/console/event limit 上限生效，artifact/diff 鉴权流式读取且 quarantine 不返回原件
- request/response 均走 extra=forbid 的 Pydantic model；未知字段、超 2 MiB body 和 multipart 分别拒绝
- token 长度不足被配置校验拒绝且比较使用 compare_digest；无/伪造 Content-Length 与分块 body 不能绕过流式请求上限
- artifact 主动内容强制 attachment + nosniff，错误响应不泄露 traceback、token、绝对路径或请求正文
- GUI/API 安全头和 CSP 生效，生产 bundle 无公开 source map、第三方 CDN script 或持久化 bearer token
- serve 只接受 loopback，静态根不能逃逸/含 symlink/reparse；SPA fallback 不吞未知 API/WS，ArtifactStore/Session repo 永不被静态挂载

test_api_concurrency.py
- PUT/assign/auto-assign expected_semantic_version 冲突返回 409
- layout expected_layout_version 独立冲突，移动节点不改变 compiled hash
- run 重新编译 hash 变化时拒绝旧确认
- validate 仅在 expected semantic version 仍匹配时更新 preview；语义修改使旧 preview stale，布局修改不使其 stale
- integration HEAD 变化使 preview stale/run hash 变化并要求重新确认，Validate/Run 固定 HEAD 时持有 workspace lease
- Agent catalog/spec 变化导致 compiled hash 变化并要求重新确认
- run idempotency key 不创建重复 workflow_run
- 同一 Session 并发创建 active run 时只有一个成功
- recover-workspace 校验 fencing token/idempotency，重复请求不创建多个 attempt
- approval version/idempotency 防重复决策
- plan/run 返回 202 和稳定 ID；相同 idempotency key 不重复入队
- 所有 mutation 缺 Idempotency-Key 时返回 400；同 key 不同 request hash 返回 409
- cancel 返回 202 requested 且不提前标 terminal；并发/重复 cancel 只有一个 request event
- merge_finalizing_at 已设置时 cancel 返回 409，不能声称取消已进入本地 commit 的操作

test_event_repository.py
- plan/edit 等运行前事件允许 run_id/run_seq 为空并按全局 id 排序
- 运行事件 run_seq 原子递增且不重复
- 每个 event_type 必须命中 EventRegistry StrictModel；未知类型、未知字段和 >64 KiB payload 拒绝
- run event 必须同时有 workflow_id/run_id/run_seq，pre-run event 的 run_id/run_seq 同时为空
- 状态变化与 event 同事务回滚/提交
- WebSocket 不广播未提交事件

test_cli_services.py
- CLI 与 API 调用同一 Application Service 和状态事务
- CLI approve 记录 local_cli actor，并校验 version/subject hash
- 不存在 auto-approve 或直接更新 approval 表的旁路
- CLI 不存在直接调用 Adapter 的旁路；serve/临时模式都使用 DurableScheduler
- show-session 展示 integration path/branch/HEAD；export-patch 只导出带有效 Session/ChangeSet/Approval trailer 的 commits 且不修改 source repo

test_context_builder.py
- 能从上游节点输出生成 ContextPack
- ContextPack 只嵌入标准化元数据和 artifact refs，不嵌入大日志
- Agent 脱敏后的原始输出不可解析时仍保存 artifact，并标记 parse_status=failed
- prompt/artifact/token 预算生效，typed refs 不嵌入任意大 dict
- TaskContextBundle 只物化选中的脱敏 artifact，manifest/hash/ACL 正确
- runtime 只读当前 bundle，不能读取其他 task/session/profile
- bundle 清理失败写 security_event

test_workflow_snapshot.py
- run 前同时保存 AuthorGraph、CompiledGraph、WorkflowLayout、integration base commit、agent catalog snapshot/hash/policy_version
- run 开始后再编辑 workflow，不影响正在执行和审计展示的 snapshot
- GraphExecutor 只读取 compiled snapshot，不读取可变 workflows.author_graph_json
- TaskPackage 只读取 compiled effective permission，不重新采用 AuthorGraph candidate 字段
- TaskPackage 保留 effective existing/new 区分，新建授权不能被当作修改任意不存在路径
- privilege scope 只通过已消费且绑定 target task 的独立 granted_existing_files 扩展，不能改写 CompiledGraph effective 字段

test_capability_broker.py
- grant 过期后拒绝
- grant 超出 scope 拒绝
- grant action/resource 不匹配时拒绝，workspace action 消费时记录新 fencing token
- grant 只允许绑定/消费 attempt+1 的 target_task_id，旧 source task 或其他 task 不能消费
- tasks.active_capability_grant_id 与 grant.target_task_id 必须双向一致
- 并发消费只有一次成功
- revoked_at 非空的 grant 永远不能消费；workflow cancel 只 revoke 未消费 grant，不抹除已消费审计历史
- 超过 2 次 privilege-assisted attempt 拒绝
- 同一 attempt 返回多个 PrivilegeRequest 时整体 schema 拒绝，不创建部分 Approval
- PrivilegeRequest schema 不接受 command argv；只允许策略注册的 manifest/config 精确 edit action
- capability/action 不匹配、自由 action、空/目录/glob resource 和 auth/security/CI 配置均拒绝
- Approval UI 不能修改 capability/action/resource；任何范围变化都必须拒绝并由新 attempt 重新请求

frontend_workflow_canvas.test.tsx
- 能切换 AuthorGraph / CompiledGraph
- 能拖动节点并保存 WorkflowLayout，且不触发语义重编译
- 能把 MockAgent / OpenCode 拖到 agent_task 节点并保存 assigned_agent
- disabled Agent 不能分配到可执行节点
- 能编辑候选权限和 risk hint，但 effective 字段只读
- 不完整 draft 能保存并显示 validate errors
- 系统注入节点默认锁定，不能在前端直接删除
- approve / reject 只能通过后端 approval API 触发
- semantic version/hash 冲突要求重新加载和确认；layout 冲突单独处理
- workflow/history selector 能切换 lineage，replan 新建 workflow 而不覆盖当前图
- CompiledGraph dagre 布局按 ID 稳定、system node 不重叠且不可拖动，切换视图不覆盖 Author 坐标
- 长 title/单词/状态不改变 240x104 节点尺寸，tooltip/右侧面板可读完整内容
- 状态有图标+文字+颜色，按钮 aria/focus/键盘操作可用，system node 键盘删除被拒绝
- Agent 文本按纯文本渲染，不存在 dangerouslySetInnerHTML/raw HTML XSS

playwright_demo_flow.spec.ts
- plan -> 编辑图 -> validate -> 确认 compiled graph -> run MockAgent
- console/diff/risk 实时展示，刷新和 WebSocket 重连不丢状态
- approve 后本地 commit，reject 后 repo clean 且 patch artifact 保留
- pause/cancel/orphaned/409 冲突有可操作的阻断界面
- 1440x900、1024x768、390x844 下节点/工具栏/底部面板无重叠或文字溢出，Author/Compiled 截图回归通过
```

---

## 27. 验收标准

项目 Demo 完成后必须满足：

1. `init-db` 创建第 19 节规定的表、约束、索引和 schema version。
2. `create-session` 从 source repo/base commit 创建专用共享集成 repo 和本地 Session 分支，初始化可审计 integration_head_commit，不修改用户原工作树。
3. RuleBasedPlanner 至少支持 bugfix、feature、refactor、docs，并在 OpenCode Planner 不可用或输出非法时 fallback。
4. OpenCodePlannerAdapter 只在脱敏只读 PlannerContextBundle 中生成真实 WorkflowDraft，不能访问真实 repo 或执行 bash/edit/task/web；planner_run 固化 integration base/bundle hash，fallback 复用同一上下文且各自有独立 console/artifact 审计。
5. AuthorGraph 可以保存不完整 draft；CompiledGraph 只能由后端确定性生成。
6. 相同 AuthorGraph + policy_version + agent catalog snapshot + integration base commit 生成相同 CompiledGraph、系统节点 ID、resolved Agent 和 hash。
7. DraftValidator / ExecutableValidator 能阻止非法结构和绕过安全链的可执行图。
8. 每次 run 同时保存 AuthorGraph、CompiledGraph、WorkflowLayout、integration base commit、agent catalog、各自 hash、semantic/layout version 和 policy version 不可变快照。
9. GraphExecutor 只读取 compiled snapshot，并通过唯一 NodeHandler 执行每种 node_type；Agent 不可用时阻断，不能在执行期重新路由。
10. success/failure、matched/not_matched、approved/rejected 和 skipped 分支能确定性结束，不会把正常条件 false 误报为失败，也不会永久 pending。
11. pause、cancel、rerun、orphaned 和重启恢复符合第 21 节状态机。
12. 同一 Session 同时最多一个 active workflow_run，所有触碰 repo 的节点使用同一独占 workspace lease。
13. MockAgent 可以完成只读和受控写入 workflow。
14. 写入任务生成覆盖 staged/unstaged/untracked/ignored/deleted/renamed/binary 的完整 ChangeSet，并通过临时 Git index 生成唯一 canonical patch；审计 evidence 不作为应用输入，canonical patch 在 clean base 上可精确复现 post_state_hash。
15. AgentTask 完成后共享 repo 恢复 clean；等待审批期间不持有写租约。
16. command TestNode 可临时应用 ChangeSet、隔离捕获测试副作用、运行受控测试并恢复 clean；纯 docs scope 可使用不启动进程的 docs_static TestNode，代码/混合 scope 不可借此跳过测试。
17. PatchGuard 能拒绝路径逃逸、symlink/junction、`.git`、`.gitattributes`、`.gitmodules`、敏感文件和 allowed scope 外修改。
18. CommandGuard 只接受注册模板和参数级白名单，不执行用户提供的 shell 字符串。
19. effective risk 不能被用户或 Planner 降低，L4 直接拒绝。
20. ChangeSetApproval 与 PrivilegeApproval 分别绑定各自不可变 subject/hash、scope 和过期时间，提权审批不伪造 change_set。
21. approve/reject 具备 version CAS 和 idempotency，不能重复推进节点。
22. PrivilegeRequest 使用 runtime side gate；批准后同事务 supersede 旧 attempt、创建新 attempt/target task 并双向绑定精确 CapabilityGrant，不修改 CompiledGraph。只有 target task 能消费，workspace action 记录新 fencing token，并发消费只有一次成功。
23. MergePatch 只应用已批准且 hash/expected HEAD 未变化的 ChangeSet，由 Master 在 Session 分支创建本地 commit，并同事务更新 session/run expected commit 与 event。
24. 拒绝、测试失败、base 变化和 patch 冲突时 artifact 保留、repo clean、不会 merge。
25. file lock 具备 lease、heartbeat 和 fencing token，旧 owner 不能在租约失效后写入。
26. CLI Agent 连接层实现固定 argv、绝对 binary path/hash、并发 stdout/stderr、输出上限、timeout/cancel 和完整进程树清理。
27. OpenCodeAdapter 检测本机 OpenCode 和 `--pure` 能力；Executor/Planner 使用不同固定 agent ID，支持 pure 时调用 `<path> --pure run ...`。无 pure 版本默认 disabled，只有用户显式允许且命中测试过的 compatibility allowlist（首个目标 1.2.27）才可在专用 profile 下尝试，未知版本 fail closed。
28. 每个 OpenCode task 都通过 `OPENCODE_CONFIG_CONTENT` 注入默认 deny 的 runtime permission，策略内容/hash 可审计。
29. OpenCode Executor 只开放精确 read/edit，glob/grep/list/bash 永久 deny；Planner 只在 planner-view 开放 read/glob/grep/list；external directory 仅当前只读 TaskContextBundle 例外，其他外部目录、task、web、未知 MCP/plugin tool、share 和 autoupdate 禁止。
30. 任何 OpenCode debug/run 前拒绝项目 `.env*` 和 OpenCode/AGENTS/Claude 启动配置；正式运行再验证 resolved config/agent 不含 plugin/MCP/instructions/commands/formatter/LSP，任何偏差都 fail closed，argv 永不含 `--auto`。
31. Master/demo/API/CI token 不进入 Agent 环境、prompt、console 和 artifact；项目 `.env` 不能作为 OpenCode provider auth 来源，provider auth 只走 Agent Hub 专用正常 auth store。
32. 脱敏在 console/raw JSON artifact 持久化前完成；console 正文只存 ArtifactStore chunk，SQLite 仅存 artifact ref/seq/size，输出可追踪且有大小上限。
33. Codex / Claude Code / Aider 第一版作为 disabled CLI skeleton 展示，不参与自动执行。
34. API 使用随机 demo token、严格 CORS、Origin、一次性 WebSocket ticket，不把长期 token 放入 URL/bundle/localStorage。
35. 语义 mutation 使用 expected_semantic_version，布局使用 expected_layout_version；run 使用 semantic version、confirmed_compiled_hash 和 idempotency key。
36. WebSocket 能分别按 after_run_seq / after_console_seq 断线补传 workflow event 和 console chunk，刷新后状态与数据库一致。
37. React Flow GUI 可以编辑 AuthorGraph、查看 CompiledGraph，并把系统安全节点作为只读真实执行路径展示。
38. 前端可以拖动节点、编辑普通边、保存不完整 draft，并分别处理 semantic/layout version 冲突。
39. 前端可以把 Mock/OpenCode 拖到 agent_task，disabled Agent 不能被误分配。
40. 前端只能编辑候选权限和 risk hint，effective scope/risk/approval/runtime policy 只读。
41. 前端可以运行、暂停、取消、审查 ChangeSet/diff/risk/console，并完成幂等 approve/reject。
42. ContextPack 只包含 typed metadata/artifact refs并受 prompt/token 预算限制；Executor 通过隔离只读 TaskContextBundle 获取选中 artifact，Planner 只读取脱敏 PlannerContextBundle，二者都不能访问其他 task/session 或真实敏感文件。
43. 所有状态变化与 event 原子提交，event seq 单调，可完整回放审计。
44. L0 只读路径可以没有 approval；L1 写入只省略额外的执行前风险审批，最终 `ChangeSetApproval` 仍是 `merge_patch` 的硬前置，且不能生成 auto-approved 记录。
45. fake CLI、后端集成测试、前端组件测试和 Playwright 主流程全部通过；真实 OpenCode 仅凭据相关 smoke test 允许 skip。
46. singleton Master lease 在进程启动、heartbeat 和每次 scheduler mutation 时强制校验；租约接管后旧 instance/token 不能继续 claim 或提交状态/event，Uvicorn workers>1 和第二个 Master 都 fail fast。
47. plan/run API 以 202 持久化入队，DurableScheduler 以数据库为正确性来源且全局单 handler；HTTP 断开、内存唤醒丢失或 Master 重启不会遗失 pending 工作，standalone CLI 复用同一 scheduler 实现。
48. TestRunner 只执行批准 argv，使用最小无凭据环境、输出限制、timeout/cancel 和进程树回收；用户选择不信任待测代码时 workflow 明确 blocked_by_guard，不能跳过测试后 Merge，GUI 不把 CommandGuard 宣称为沙箱。
49. CompiledGraph 为每个 agent_task 分别固化 effective existing/new file scope、command scope、policy risk floor 和 ChangeSetApproval 要求；TaskPackage 只从该不可变快照构造，AuthorGraph/Planner/API 不能伪造 compiler-only 字段。
50. NodeRun/WorkflowRun 只按第 21 节白名单和 expected-old-status CAS 转换；普通边默认 success，不存在语义重复的 always 条件，paused workflow 不伪造 node paused 状态。
51. Compiler 对每个写任务生成独立密封安全链：原 success 后继仅在 Merge 成功后运行，rejected/guard/test/merge failure 进入互斥终止报告分支；OutputNode 保留真实终态，不把失败或阻断报告成成功。
52. 所有可重试 mutation 使用持久化 Idempotency-Key 事务：同 key/同请求重放原响应，同 key/不同请求 409，并发请求只产生一次状态变化；workspace/master lease 行不删除且 fencing token 永不回绕。
53. ChangeSetApproval 到期只能在全部不可变证据仍匹配时创建新的 renew 记录；PrivilegeApproval 到期不可续期。repo、ChangeSet、console 和 artifact 超过 Demo 资源预算时 fail closed 并保留可恢复审计。
54. Validate 成功预览固化 source semantic version、integration base commit、agent catalog hash 和 policy version；语义/HEAD/catalog/policy 变化使旧预览 stale 且不可 Run，布局变化不使其失效，失败 Validate 不覆盖最后一次成功预览。
55. 运行数据默认位于源码树外的 AGENT_HUB_DATA_DIR；OpenCode permission 按有序规则生成并对实际 UTF-8 bytes 哈希，超出 inline config 上限时阻断，不能改用会被项目配置覆盖的低优先级文件。
56. 同一 Session 每次成功 Plan/Replan 创建新的 workflow lineage 并回写 planner_run result_workflow_id；parent 和历史 run snapshot 不被覆盖，失败 Planner 不产生半成品 workflow。
57. RuleBased docs workflow 在纯文档 scope 下可由 compiler-only docs_static TestNode 完成内建校验；任何代码、配置或混合写入仍必须有 CommandGuard 批准的真实 command test。
58. AuthorGraph 只持久化用户坐标；CompiledGraph 由固定尺寸 dagre 稳定排版且坐标不进 hash，Author/Compiled 切换不覆盖布局，并通过桌面/移动视口无重叠截图测试。
59. Demo 文件权限只接受 exact repo-relative path；existing/new candidate 显式分离且不能重叠，PatchGuard 只允许 effective_new_files 中的路径被创建。缺失父目录仅作为精确文件的隐式容器，任何 glob/目录 scope 均拒绝。
60. Privilege grant 绑定 attempt+1 的 target_task_id 和一个已存在精确 resource；TaskPackage 以独立 granted_existing_files 扩展，不能改写 compiled scope，最终 ChangeSetApproval evidence 必须覆盖完整 request/grant/消费信息。
61. 节点固定尺寸且长文本不溢出，状态以图标+文字+颜色表达，核心画布控制具备键盘/focus/ARIA；移动端使用 tab/drawer，Agent/Planner 内容不以 raw HTML 渲染。
62. Console 按 <=64 KiB 脱敏 artifact chunk 持久化并在 DB commit 后广播，workflow/console 使用独立续传游标；10 MiB 超限终止任务，SQLite 不存整段日志正文。
63. cancel 先持久化 cancel_requested_at 并返回 202，完成进程树终止和 workspace clean 后才标 cancelled；完成事务同时失效 pending Approval/PrivilegeRequest、撤销未消费 Grant、取消未 Merge 的完整 ChangeSet，清理失败为 orphaned，Master 重启不会继续一个已请求取消的任务。
64. Merge 以 merge_finalizing_at CAS 作为 cancel 线性化点；finalizing 前的 cancel 阻止提交，之后的 cancel 明确 409。崩溃恢复通过 commit trailer/tree hash 判定，不重复 commit 或静默丢失结果。
65. show-session 能展示专用 integration repo/branch/HEAD；export-patch 只导出该 Session 已批准且 trailer 可验证的本地 commits，不修改用户 source repo 或执行 push。
66. TestNode 以 applied-state baseline 区分 Agent patch 与测试副作用；非 ephemeral 修改直接阻断且不进入 ChangeSet，ephemeral 清理只处理本次清单内新建项，最终 repo 必须恢复 clean。
67. Session integration_head_commit 与 active WorkflowRun current_commit 始终匹配实际 HEAD；外部 commit/checkout 漂移 fail closed，Master Merge 和崩溃补偿原子更新两者及审计事件。
68. ChangeSet 按 captured、Guard、Test、Policy、Approval、Merge 的显式白名单状态机推进；Guard/Test/Policy/用户拒绝、stale、partial、quarantine 互不混淆，终态不可回退，每次转换与 event/updated_at 原子提交。
69. FastAPI 默认只监听 loopback，并统一执行 bearer dependency、TrustedHost、严格 CORS/Origin、请求体/模型限制和安全下载响应；docs/openapi 默认关闭，GUI 使用 CSP/安全头且不持久化 token、不加载第三方脚本。
70. OpenCode 注册时生成并持久化 capability manifest，真实 task 前复核 binary/version；run/format-json/agent/dir/debug 能力缺失时 fail closed，`--pure` 与 legacy compatibility 明确区分且不能靠版本号臆测。
71. Python/Node 依赖由已提交 lockfile 可复现安装，CI 使用 hash 校验/npm ci 并阻断未豁免的 high/critical advisory；安全豁免必须可审计且有到期日。
72. `npm run build` 产物可由 `agent-hub serve` 在同一 loopback origin 安全托管并打印 GUI URL/token；开发 Vite 也只监听 loopback，静态根与 ArtifactStore/Session repo 隔离，未知 API/WS 不被 SPA fallback 掩盖。

---

## 28. 最重要的实现约束

1. 不要让 Agent 直接调度其他 Agent。
2. 不要让 Agent 直接 git push。
3. 不要让 Agent 直接合并代码。
4. 不要让 Agent 读取 `.env`、密钥、SSH key。
5. 不要默认允许任意 shell 命令。
6. 不要把大日志直接塞进 SQLite。
7. 不要让 Agent Console 绕过 Master。
8. 不要让用户输入直接透传到原生 Agent 终端。
9. 不要允许未知 `node_type` 执行。
10. 不要允许 MergePatchNodeHandler 绕过 Approval。
11. 不要允许 L4 风险进入审批，直接拒绝。
12. 所有关键动作必须写 events。
13. 所有安全事件必须写 security_events。
14. 所有 Agent 脱敏后的输出必须保存 artifacts。
15. 所有 diff 必须经过 PatchGuard。
16. PlannerAgent 只能输出结构化 WorkflowDraft，必须经过 DraftValidator、Compiler、PolicyInjector 和 ExecutableValidator。
17. GraphExecutor 只能根据 node_run 状态推进，不允许询问任意 Agent “下一步做什么”。
18. React Flow GUI 是 demo 核心体验，不能砍成纯 CLI 或只读 JSON 查看器。
19. L0 只读省略审批、L1 写入省略额外前置审批都不等于 auto-approve；任何可合并 ChangeSet 仍需真实用户 ChangeSetApproval，系统不得伪造批准记录。
20. workflow_run 必须执行不可变 CompiledGraph snapshot，不得执行可变 AuthorGraph 或前端直接提交的图。
21. 不要在 agent_task 内隐藏执行 PatchGuard/Test/Risk/Approval；每个 node_type 只能有一个 NodeHandler。
22. 不要只依赖普通 git diff；ChangeSet 必须覆盖 staged、untracked、ignored 和文件系统边界。
23. 不要在等待审批时保留 dirty shared repo 或长期占用写锁。
24. 不要允许 Approval 脱离其 subject 生效：ChangeSetApproval 必须绑定 base/change_set/patch/evidence，PrivilegeApproval 必须绑定 request/action/resource hash。
25. 不要把 prompt、环境变量白名单或事后日志扫描当成 CLI Agent 的强制安全边界。
26. OpenCode 每次运行必须注入默认 deny 的 runtime policy；无法约束的能力不能启用。
27. 不要允许用户或 Planner 修改 effective scope、policy_risk_floor、requires_changeset_approval、system_managed 或 runtime policy。
28. 不要将 workflow/node 运行状态写回 WorkflowNode 定义。
29. 不要从公开 API 单独启动 task，所有执行必须归属于 workflow snapshot 和 node_run。
30. 不要在未确认 executable path/hash 的情况下通过 PATH 启动真实 CLI Agent。
31. 所有锁、审批、grant 和 scheduler claim 必须使用数据库原子条件更新。
32. Demo 安全承诺必须遵守第 13.6 节威胁模型，不夸大为恶意本地进程隔离。
33. Demo 只能运行一个持有有效 singleton lease 的 Master；所有 scheduler mutation 必须校验 master fencing token，不能只依赖 `workers=1` 的启动约定。
34. TestRunner 与 CLI Agent 子进程都不得继承 Hub/API/provider/SSH 凭据；CommandGuard 只约束 argv，不得被描述为可隔离敌对仓库代码的安全沙箱。
35. 所有可重试 mutation 的业务状态和 idempotency response 必须同事务提交；不能用进程内 set 或“先写状态、后写幂等记录”代替。
36. 任何资源上限超限都必须 fail closed 并执行 workspace 恢复；不能通过截断 ChangeSet、diff 或证据后继续 Approval/Merge。
37. OpenCode Executor/Planner 永不开放 bash，也永不传 `--auto`；测试命令只能由独立 Master TestRunner 执行，PrivilegeRequest 也不能申请 command argv。
38. 不能假设 inline OpenCode config 会替换其他配置；必须在首次 OpenCode 子进程前做项目启动文件扫描，并在运行前验证 resolved config 安全子集。

---

## 29. 最终目标

最终系统应该是：

> 一个类似 ComfyUI 的多 Coding Agent 可视化工作流平台。

这里的“类似 ComfyUI”强调的是节点图编排、依赖关系可视化、人工可审查的执行路径和可拖拽的 Agent 分配，而不是把 Coding Agent 的所有输出统一成同一种数据结构。

Master 可以根据用户任务自动生成节点图。用户可以拖动节点、连接流程、把 Claude Code / Codex / OpenCode 拖到任务节点上。未分配节点由 Master 自动分配。

执行过程中，Agent Console 展示每个 Agent 的输出、日志、diff、风险和审批请求。所有高危操作必须走提权审批。所有代码修改必须经过安全检查、测试和用户确认。

---

## 30. 优先级

```text
第一优先级：冻结 StrictModel、数据库、AuthorGraph/CompiledGraph、ChangeSet、状态机和 API 契约
第二优先级：RuleBasedPlanner + Compiler/Validator + GraphExecutor + MockAgent 无副作用闭环，同时完成 React Flow fixture 原型
第三优先级：Session 共享集成 repo、WorkspaceTransaction、Path/Patch/Command Guard、Approval 和恢复
第四优先级：通用 CLI Runner、OpenCode runtime policy、OpenCodeAdapter 和只读 OpenCodePlanner
第五优先级：FastAPI/version/idempotency/WebSocket 与 React Flow 数据闭环；可与第四优先级后半并行
第六优先级：React Flow 完整核心体验、ChangeSet/console/risk/approval 审查和 Playwright E2E
第七优先级：后续 Codex / Claude Code / Aider 真实接入、容器沙箱、MCP 和远程 Worker
```

请按阶段实现，不要一开始追求完整复杂系统。先保证 demo 的核心闭环稳定、安全、可测试，并确保 Master 的智能规划能力与确定性调度执行层严格分离。
