# HUB-110 Codex 执行简报

## 基线

- Owner：Codex + GPT-5.6 xhigh
- Reviewer：Claude Code + Claude Opus 4.8
- Base commit：`d11196a06480a441ac7af3d43df146170b4385dd`
- Branch：`codex/hub-110-workflow-runtime`
- Worktree：`E:\agent_hub_worktrees\codex-hub-110`

## 目标

完成阶段 1 的确定性纵向切片：把 AuthorGraph 经 DraftValidator、WorkflowCompiler、PolicyInjector 和 ExecutableValidator 固化为不可变 CompiledGraph snapshot，再由数据库驱动的单 Master scheduler 运行只读 MockAgent 工作流。

## 实现范围

1. 草图结构、路径/argv 语法、DAG、条件边和 compiler-only 字段校验。
2. 确定性 Agent 路由、scope 收敛、system node ID、graph canonical hash 和写任务安全链编译。
3. NodeRegistry、每种 node_type 唯一配置模型/Handler、CompiledGraph 执行级校验。
4. workflow_run/node_run/task 的 snapshot、CAS 状态转换、master fencing 和原子运行事件。
5. DurableScheduler/GraphExecutor 的顺序 DAG、条件边、skipped/failure 传播。
6. 只读 MockAgentAdapter、TaskPackage/ContextPack 和脱敏 artifact 闭环。
7. CLI 的 create-session、register-agent、plan、validate、show-workflow、run-workflow、show-events。

## 安全边界

- HUB-110 不实现 Git 写入、ChangeSet、Guard、Approval 或 Merge 的真实副作用，这些属于 HUB-200/210。
- `requires_write=true` 的图可以被确定性编译为安全链预览，但在本阶段执行级校验必须 fail closed。
- GraphExecutor 只读取 `workflow_runs.compiled_snapshot_json`，不读取可变 workflow。
- 所有 claim/状态推进必须在同一事务校验 Master lease/fencing token，并与 typed event 一起提交。
- MockAgent 不得写 repo、运行命令或访问 Master token。

## 验收

- 非法 AuthorGraph/CompiledGraph 被稳定拒绝，hash 与输入顺序无关。
- snapshot 与后续 workflow 编辑隔离。
- 单 Master 能恢复轮询并顺序执行只读 Mock DAG；分支未选路径进入 skipped。
- node_run、task、workflow_run 和 event 可完整回放。
- CLI 从 session 创建到只读 Mock workflow completed 全链路通过。
- 完整 pytest、Ruff、前端既有测试和 CI 入口通过。

## 禁止路径

- 不修改冻结 `protocol/**` 契约。
- 不实现或弱化 HUB-200/210 的 workspace、Guard、Approval、Capability 或 Merge 安全语义。
- 不直接 merge 或 push；完成后先交 Claude Code 复审。

## 交付状态

- 实现状态：已通过 Claude Code 最终交叉复审、集成到 `main`，阶段 1 CLI/Mock gate 已通过。
- 最终复审基线：Python `389 passed, 5 skipped`；Ruff check/format 通过。
- 前端：Oxlint 通过，Vitest `7 passed`，生产构建通过，Playwright smoke `1 passed`。
- 写入型图只生成确定性安全链预览，执行仍以 `write_runtime_unavailable` fail closed。
- HUB-110 未提前实现或弱化写入安全边界；GitManager、WorkspaceTransaction 和真实 Guard 当前由 `HUB-200` 实现，Approval、Merge、取消和 RecoveryManager 仍属于 `HUB-210`。
