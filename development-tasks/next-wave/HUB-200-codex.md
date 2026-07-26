# HUB-200：共享工作区、ChangeSet 与 Guard

## 基本信息

- Owner：Codex / GPT-5.6 xhigh
- Reviewer：Claude Code / Claude Opus 4.8
- Base：`main@f356e69`
- Branch：`codex/hub-200-workspace-security`
- 依赖：HUB-110 已完成并集成
- 状态：in_progress

## 目标

建立 demo-only 共享写入区的安全闭环：Session 使用独立本地集成 clone；一个
Session 同时只有一个带 fencing token 的 workspace lease；Agent 产生的 staged、
unstaged、untracked 和 ignored 变更被捕获成唯一 canonical ChangeSet，仓库随后按
精确路径恢复 clean；编译期和运行期分别执行 PathPolicy、CommandGuard、
PatchGuard 和 RiskClassifier。

## 已启动的实现

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

## 本任务剩余

1. 将 canonical ChangeSet、patch/evidence/preimage artifacts 持久化，并实现
   ChangeSet 状态 CAS。
2. 新增 PatchGuard、CommandGuard、RiskClassifier NodeHandler，并将写型
   AgentTask 接入 WorkspaceTransaction；失败、超时和取消统一 capture/restore。
3. 实现 command/docs_static TestNodeHandler 的最小安全运行链；Approval、Merge、
   cancel 线性化和 RecoveryManager 仍严格留给 HUB-210。
4. 补充安全事件、资源边界、进程失败和跨 Session 回归测试。
5. 完整 CI 后提交 `feat(HUB-200): add shared workspace changeset security chain`，
   交 Claude Code 阻断式复审。

## 允许修改

- `workspace/**`
- `security/**`
- `storage/change_set_repository.py`
- `workflow/compiler.py`
- `workflow/handlers/**`
- `app/services.py`
- 对应 `tests/**`
- 本任务简报、开发计划和任务看板

冻结约束：不得修改 `protocol/**`；不得使用 `git reset --hard`、无 pathspec
restore 或 `git clean`；不得为测试放宽 lease、scope、CAS 或 artifact 校验。

## 当前验证

```text
Python full suite: 481 passed, 6 skipped
Ruff check/format: clean
Frontend Oxlint: passed
Frontend Vitest: 7 passed
Frontend production build: passed
```
