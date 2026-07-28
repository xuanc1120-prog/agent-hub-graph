# HUB-200：共享工作区、ChangeSet 与 Guard

## 基本信息

- Owner：Codex / GPT-5.6 xhigh
- Reviewer：Claude Code / Claude Opus 4.8
- Base：`main@66c6bcc`
- Branch：`codex/hub-200-workspace-security`
- 依赖：HUB-110 已完成并集成
- 状态：ready_for_review

## 目标

建立 demo-only 共享写入区的安全闭环：Session 使用独立本地集成 clone；一个
Session 同时只有一个带 fencing token 的 workspace lease；Agent 产生的 staged、
unstaged、untracked 和 ignored 变更被捕获成唯一 canonical ChangeSet，仓库随后按
精确路径恢复 clean；编译期和运行期分别执行 PathPolicy、CommandGuard、
PatchGuard 和 RiskClassifier。

## 已完成实现

1. `GitManager` 使用固定绝对 Git binary、`shell=False`、隔离 HOME/config、禁用
   prompt/credential/hooks/pager/signing；Session clone 使用 `--no-hardlinks`，
   创建 `agent-hub/session/<session_id>`，删除并验证无 remote。
2. Session validate/run 已改为只读取 `shared_repo_path`，不再操作用户 source
   worktree。
3. `PathPolicy` 已实现 exact existing/new scope、containment、NFC/casefold、
   Windows reserved/ADS/UNC/device、glob、symlink/junction/reparse、hardlink、
   `.git`/runtime/secret 拒绝。
4. `CommandGuard` 已实现 pytest、`python -m pytest`、npm/pnpm test、go test 的
   argv 和参数级白名单；拒绝 shell string、元字符和危险参数。
5. `WorkspaceTransaction` 已实现 clean baseline、完整 inventory/evidence、
   临时 `GIT_INDEX_FILE` canonical patch、真实 index 语义校验、精确 restore、
   ignored preimage、资源上限和 patch 重放验证。
6. `PatchGuard` 已按 modify/delete、create、rename 分别校验 existing/new scope，
   secret 进入 quarantine；`RiskClassifier` 已实现 L0-L4 确定性上调规则。
7. `WorkflowCompiler` 的生产调用已接入 PathPolicy、CommandGuard 和静态风险下限。
8. `ChangeSetRepository` 已实现 canonical 文档、patch/evidence/preimage artifact
   持久化、session/task lineage、Master/workspace fencing、状态 CAS、typed event
   和 security event。
9. 写型 `AgentTask` 已接入 workspace lease 与 `WorkspaceTransaction`；成功、
   失败和取消均执行 capture/restore，并持久化 `CAPTURED` 或
   `ABANDONED_PARTIAL`。
10. PatchGuard、CommandGuard、RiskClassifier、command/docs_static Test handler
    已注册到执行器；写链可确定性运行至 HUB-210 的 Approval gate。
11. TestRunner 使用固定 argv、`shell=False`、最小环境、超时/取消进程树回收、
    有界脱敏输出，并显式解析 npm/pnpm 所需 Node 运行时。
12. Executor 会在接受 handler artifact 前重新读取并校验文件 hash、owner 和
    source task；CompiledGraph 的风险、命令、文件 scope 与 Agent Catalog 漂移
    均 fail closed。

## 复审与集成边界

1. 当前代码交 Claude Code / Claude Opus 4.8 做阻断式交叉复审。
2. 复审通过后由 Codex 合并 `main`，随后从最新 `main` 并行启动 HUB-210 和 HUB-220。
3. Approval、MergePatch、取消线性化、RecoveryManager 和崩溃恢复仍属于
   HUB-210/HUB-220；HUB-200 写链在 Approval 节点 fail closed 是预期行为。
4. 真实 OpenCode CLI 运行与宿主级隔离属于 HUB-300/310/330，不在本任务内。

## 允许修改

- `workspace/**`
- `security/**`
- `storage/change_set_repository.py`
- `workflow/compiler.py`
- `storage/errors.py`、`storage/__init__.py`、`storage/agent_repository.py`、
  `storage/workflow_run_repository.py`
- `workflow/events.py`、`workflow/executor.py`、
  `workflow/executable_validator.py`
- `workflow/handlers/**`
- `app/services.py`
- `adapters/mock.py`
- 对应 `tests/**`
- `.gitignore`、本任务简报、开发计划和任务看板

冻结约束：不得修改 `protocol/**`；不得使用 `git reset --hard`、无 pathspec
restore 或 `git clean`；不得为测试放宽 lease、scope、CAS 或 artifact 校验。

## 当前验证

```text
Python full suite: 491 passed, 7 skipped
Ruff check/format: clean
Frontend Oxlint: passed
Frontend Vitest: 7 passed
Frontend production build: passed
Playwright smoke: 1 passed
```

## 残余风险

- TestRunner 具备 argv、环境、输出和进程生命周期边界，但不是 OS/container
  sandbox；仓库测试代码的宿主文件系统和网络隔离留给 HUB-300/330 硬化。
- Agent 在 ChangeSet 建立前崩溃时由 task/node failure event 记录；专用恢复审计
  和崩溃注入由 HUB-210/220 覆盖。
- 本任务未修改冻结的 `protocol/**`。

复审提示词：
[HUB-200-claude-review-prompt.md](../handoffs/HUB-200-claude-review-prompt.md)
