# 当前开发波次

## 基线

- 集成分支：`main`
- 冻结协议：`contracts-frozen-v1`
- 当前已完成：`HUB-000/010/020/030/100/110/120/130`
- 当前任务：`HUB-200` in progress
- 当前任务基线：`main@f356e69`
- 阶段 1：CLI/Mock gate 已通过
- Agent 不直接 merge 或 push

## 当前分配

| 任务 | Agent | 状态 | 分支 | Worktree | 简报 |
|---|---|---|---|---|---|
| `HUB-200` | Codex + GPT-5.6 xhigh | in_progress | `codex/hub-200-workspace-security` | `E:\agent_hub_graph` | [HUB-200-codex.md](HUB-200-codex.md) |
| `HUB-130` | Hermes + MiMo-V2.5-Pro | completed | `agent/hermes-context-artifacts` | historical | [HUB-130-hermes.md](HUB-130-hermes.md) |
| `HUB-110` | Codex + GPT-5.6 xhigh | completed | `codex/hub-110-review-fixes` | historical | [HUB-110-codex.md](HUB-110-codex.md) |

HUB-110/HUB-130 的历史简报和交接提示词保留为任务证据，不再用于继续开发。

## HUB-200 执行顺序

1. 已完成 Session 独立无 remote clone、共享仓库 preflight 和 exact PathPolicy。
2. 已完成 CommandGuard、PatchGuard、RiskClassifier、LockManager facade 和 canonical WorkspaceTransaction 首批实现。
3. 下一批实现 ChangeSet/artifact 持久化及 Guard/Test NodeHandler 接线。
4. HUB-200 全量验证后交 Claude Code 阻断式复审，通过后才合并 `main` 并启动 HUB-210/HUB-220。

历史上的 `agent/*` worktree 基于旧任务提交，只作为任务证据，不能直接用于新开发。新任务必须从最新 `main` 创建 task-specific branch/worktree，并在交付报告中记录实际 base commit。
