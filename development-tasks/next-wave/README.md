# 当前开发波次

## 基线

- 集成分支：`main`
- 冻结协议：`contracts-frozen-v1`
- 当前已完成：`HUB-000/010/020/030/100/110/120/130/200/210/220`
- 当前待启动任务：`HUB-300`
- 当前任务基线：`main@540b57f` 或其更新后继提交
- 阶段 1：CLI/Mock gate 已通过
- 阶段 2：`3/3`，安全链、审批与恢复 gate 已通过
- Agent 不直接 merge 或 push

## 当前分配

| 任务 | Agent | 状态 | 分支 | Worktree | 简报 |
|---|---|---|---|---|---|
| `HUB-210` | Codex + GPT-5.6 xhigh | completed | `codex/hub-210-approval-recovery` | historical | 已通过 PR #3 合入最新 `main` |
| `HUB-220` | Hermes + MiMo-V2.5-Pro | completed | `agent/hermes-security-fault-injection` | historical | 已通过 PR #4 合入 `540b57f`，PR/main CI 通过 |
| `HUB-300` | OpenCode + MiMo-V2.5-Pro | queued | 待创建 | 待创建 | 当前待启动：CliAgentSpec/CliAgentRunner |
| `HUB-200` | Codex + GPT-5.6 xhigh | completed | `codex/hub-200-workspace-security` | historical | [HUB-200-codex.md](HUB-200-codex.md) |
| `HUB-130` | Hermes + MiMo-V2.5-Pro | completed | `agent/hermes-context-artifacts` | historical | [HUB-130-hermes.md](HUB-130-hermes.md) |
| `HUB-110` | Codex + GPT-5.6 xhigh | completed | `codex/hub-110-review-fixes` | historical | [HUB-110-codex.md](HUB-110-codex.md) |

HUB-110/HUB-130/HUB-200 的历史简报和交接提示词保留为任务证据，不再用于继续开发。

## 当前执行顺序

1. 基于 `main@540b57f` 或其更新后继提交，为 HUB-300 创建独立 branch/worktree。
2. 实现并审查 CliAgentSpec/CliAgentRunner、固定 argv、JSONL、timeout/cancel 和进程树回收。
3. HUB-300 接口稳定后启动 HUB-320 fake CLI 与异常注入测试，再进入 HUB-310 OpenCode Adapter。

HUB-200/210/220 均已完成独立复审并合入 `main`。HUB-220 的 Phase A/B、
Linux PR gate 与合并后的 `main` CI 均通过，阶段 2 gate 正式关闭。

历史上的 `agent/*` worktree 基于旧任务提交，只作为任务证据，不能直接用于新开发。新任务必须从最新 `main` 创建 task-specific branch/worktree，并在交付报告中记录实际 base commit。
