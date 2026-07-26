# 当前开发波次

## 基线

- 集成分支：`main`
- 冻结协议：`contracts-frozen-v1`
- 当前已完成：`HUB-000/010/020/030/100/110/120/130`
- 当前任务：`HUB-200` 交接准备
- 阶段 1：CLI/Mock gate 已通过
- Agent 不直接 merge 或 push

## 当前分配

| 任务 | Agent | 状态 | 分支 | Worktree | 简报 |
|---|---|---|---|---|---|
| `HUB-130` | Hermes + MiMo-V2.5-Pro | completed | `agent/hermes-context-artifacts` | `E:\agent_hub_worktrees\hermes-context-artifacts` | [HUB-130-hermes.md](HUB-130-hermes.md) |
| `HUB-110` | Codex + GPT-5.6 xhigh | completed | `codex/hub-110-review-fixes` | `E:\agent_hub_graph` | [HUB-110-codex.md](HUB-110-codex.md) |

HUB-130 的历史简报和交接提示词保留为任务证据，不再用于继续开发。

## 执行顺序

1. 已在独立 worktree 完成 Compiler、Validator、DurableScheduler、GraphExecutor 和 MockAgent 纵向闭环。
2. 已通过阶段 1 CLI/Mock gate、Python 全量测试和前端组合验证。
3. Claude Code 最终复审无阻塞问题；Codex 已集成到 `main`，下一步开始 `HUB-200`。

历史上的 `agent/*` worktree 基于公开历史重写前的提交，只作为旧任务证据，不能直接用于新开发。新任务必须从最新 `main` 创建 task-specific branch/worktree，并在交付报告中记录实际 base commit。
