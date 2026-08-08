# HUB-200 Claude Code 阻断式复审提示词

你现在是 Agent Hub 的独立安全审查人。请使用 Claude Code + Claude Opus 4.8
（架构和并发问题使用 xhigh）对 HUB-200 做阻断式交叉复审，只输出审查意见，
不要直接修改 Owner 文件。

## 审查基线

- Repository：`E:\agent_hub_graph`
- Base：`66c6bcced4a86792936f9ac9c8b3399348d2e920`
- Review branch：`codex/hub-200-workspace-security`
- Review head：该分支在远端的最新提交
- Owner：Codex
- 冻结约束：`protocol/**` 不得有任何改动

先执行：

```powershell
git fetch origin
git switch codex/hub-200-workspace-security
git merge-base main HEAD
git diff --stat main...HEAD
git diff main...HEAD -- protocol
git status --short
```

若 merge-base 不是上述 Base、工作区不 clean，或 `protocol/**` 有 diff，立即作为
P1 报告并停止批准。

## 复审目标

确认 demo-only 共享工作区安全链满足以下不变量：

1. 一个 Session 同时只有一个带 fencing token 的写 lease；lease 丢失后旧执行者
   不能持久化 ChangeSet 或推进状态。
2. 写型 AgentTask 无论成功、失败、超时或取消，都只捕获并恢复当前 task 的改动，
   不使用 `git reset --hard`、无 pathspec restore 或 `git clean`。
3. staged、unstaged、untracked、ignored 变更及 preimage/evidence 被确定性捕获，
   canonical patch 可重放，仓库最终恢复 clean。
4. ChangeSet 文档、patch、evidence、preimage 的 session/task owner、hash、size、
   lineage 和状态 CAS 在事务边界内校验。
5. PathPolicy、CommandGuard、PatchGuard、Test、RiskClassifier 的编译期和运行期
   scope 不会被扩大；L4 直接拒绝。
6. Executor 在接受 NodeHandler artifact 前重新读取文件并校验 hash、owner 和
   source task，不能只信任数据库元数据或 handler 返回值。
7. TestRunner 不使用 shell，不继承 API token，输出有界且脱敏，超时/取消会回收
   进程树；npm/pnpm 的 Node 依赖只通过已解析目录加入最小 PATH。
8. test 命令即使返回 0，只要污染仓库，也必须标记失败并精确恢复副作用。
9. Mock 写链能执行到 Approval gate；Approval、MergePatch、取消线性化和
   RecoveryManager 未被误实现在 HUB-200 内。

## 重点代码

- `workspace/git_manager.py`
- `workspace/path_policy.py`
- `workspace/transaction.py`
- `workspace/lock_manager.py`
- `storage/change_set_repository.py`
- `storage/workflow_run_repository.py`
- `security/command_guard.py`
- `security/patch_guard.py`
- `security/risk_classifier.py`
- `security/test_runner.py`
- `workflow/handlers/agent_task.py`
- `workflow/handlers/guards.py`
- `workflow/handlers/factory.py`
- `workflow/executor.py`
- `workflow/executable_validator.py`
- `app/services.py`

## 必跑验证

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m pytest
git diff --check

Set-Location web\frontend
npm.cmd run lint
npm.cmd test -- --run
npm.cmd run build
npm.cmd run test:e2e
```

当前 Owner 基线结果为：

- Python：`491 passed, 7 skipped`
- Ruff check/format：通过
- Oxlint：通过
- Vitest：`7 passed`
- 前端 production build：通过
- Playwright smoke：`1 passed`

## 输出格式

1. P0/P1 阻塞问题，按严重程度排序，必须包含文件和行号。
2. P2 非阻塞问题。
3. 独立验证结果和实际 HEAD。
4. 对九项安全不变量逐项给出通过/失败结论。
5. 残余风险。
6. 最终结论只能是“批准合并”或“拒绝合并”。

已知且可接受的范围边界：TestRunner 不是 OS/container sandbox；真实 CLI Agent
宿主隔离属于 HUB-300/330。除此之外，不得以 demo 为由放宽 lease、fencing、
scope、artifact 完整性、状态 CAS 或 clean restore。
