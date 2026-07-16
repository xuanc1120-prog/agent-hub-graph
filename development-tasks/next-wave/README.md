# 当前开发波次

## 基线

- 集成分支：`main`
- 冻结协议：`contracts-frozen-v1`
- 当前已完成：`HUB-000/010/020/030/100/120`
- 当前任务：`HUB-130`
- 后续任务：`HUB-110`，必须等待 `HUB-130` 审查合并
- Agent 不直接 merge 或 push

## 当前分配

| 任务 | Agent | 状态 | 分支 | Worktree | 简报 |
|---|---|---|---|---|---|
| `HUB-130` | Hermes + MiMo-V2.5-Pro | ready | `agent/hermes-context-artifacts` | `E:\agent_hub_worktrees\hermes-context-artifacts` | [HUB-130-hermes.md](HUB-130-hermes.md) |
| `HUB-110` | Codex + GPT-5.6 xhigh | blocked | 合并 HUB-130 后新建 | 合并 HUB-130 后新建 | 待 HUB-130 API 审查后冻结 |

可直接发送给 Hermes 的提示词见 [HUB-130 handoff](../handoffs/HUB-130-hermes-prompt.md)。

## 执行顺序

1. Hermes 在独立 worktree 完成 `HUB-130`，只提交一次，不 push。
2. Codex 审查 Artifact/Event 原子性、Context 权限不扩张和平台文件权限。
3. Codex 将通过审查的提交集成到 `main` 并运行组合 CI。
4. 依据已集成 API 编写并启动 `HUB-110`，完成阶段 1 CLI/Mock 纵向闭环。

历史上的 `agent/*` worktree 基于公开历史重写前的提交，只作为旧任务证据，不能直接用于新开发。新任务必须从最新 `main` 创建 task-specific branch/worktree，并在交付报告中记录实际 base commit。
