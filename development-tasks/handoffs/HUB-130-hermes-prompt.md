# Hermes 交接提示词：HUB-130

把下面整段发送给 Hermes：

```text
你现在负责 Agent Hub 的 HUB-130：Context、Artifact 与 Event 基础设施。

模型与角色：Hermes + MiMo-V2.5-Pro；Owner 为 Hermes，最终 Reviewer/Integrator 为 Codex。

工作区：
- Repository: E:\agent_hub_graph
- Branch: agent/hermes-context-artifacts
- Worktree: E:\agent_hub_worktrees\hermes-context-artifacts
- Integration branch: main
- Frozen contract tag: contracts-frozen-v1

开始前必须执行并在回复中报告：
1. Set-Location E:\agent_hub_worktrees\hermes-context-artifacts
2. git status --short --branch
3. git branch --show-current
4. git rev-parse HEAD
5. git merge-base --is-ancestor main HEAD

如果分支不符、工作区不 clean、HEAD 不是交接时给出的 base commit，立即停止，不要自行 reset/rebase。

先阅读：
- development-tasks/next-wave/HUB-130-hermes.md（本任务唯一执行简报）
- agent-hub-development-plan.md 第 11、15、18.7、19 章
- docs/adr/0001-core-protocol-freeze.md
- protocol/context.py、protocol/event.py、protocol/task.py、protocol/common.py
- storage/db.py、storage/repositories.py、migrations/init.sql、app/config.py

实现顺序：
1. 先写 ArtifactStore/Repository 的失败路径测试，再实现原子文件写入、metadata、quota、containment 和清理。
2. 实现 typed EventRegistry/EventRepository，run_seq 必须在同一 SQLite immediate transaction 中分配并插入。
3. 实现 ContextBuilder，证明不会扩大 TaskPackage 的 effective scope。
4. 实现只读 TaskContextBundle、严格 manifest、canonical hash、归属校验和幂等清理。
5. 运行 focused tests，再运行完整 pytest 与 Ruff。

硬约束：
- 不修改 protocol/**、migrations/**、app/config.py、context/planner_bundle.py、workflow/**、master/**、security/**、workspace/**、前端。
- 不实现 Console、Executor、Guard、API 或 Adapter。
- 不接受 Agent/调用方提供的落盘路径。
- 不用 MAX(seq)+1，不保存任意 dict payload，不静默忽略 ACL、hash、quota 或清理失败。
- 不运行 git reset，不修改其他 worktree，不 merge，不 push。
- 如果冻结契约或 schema 无法满足任务，停止编码并给出最小阻塞证据，不绕过约束。

提交前验证：
- python -m ruff check .
- python -m ruff format --check .
- python -m pytest

只创建一个提交：feat(HUB-130): add context artifact and event infrastructure

最终返回：commit hash、base commit、修改文件、API 摘要、测试输出、Windows/POSIX 权限验证结果、残余风险，以及 HUB-110 应如何调用这些接口。不要 merge 或 push。
```
