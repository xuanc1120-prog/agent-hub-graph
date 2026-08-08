# 当前开发波次

## 基线

- 集成分支：`main`
- 冻结协议：`contracts-frozen-v1`
- 当前已完成：`HUB-000/010/020/030/100/110/120/130/200`
- 当前任务：`HUB-210` 与 `HUB-220` ready，可并行启动
- 当前任务基线：`main@d9d60ddd`
- 阶段 1：CLI/Mock gate 已通过
- 阶段 2：`1/3`，Approval/Merge/Recovery 和故障注入尚未完成
- Agent 不直接 merge 或 push

## 当前分配

| 任务 | Agent | 状态 | 分支 | Worktree | 简报 |
|---|---|---|---|---|---|
| `HUB-210` | Codex + GPT-5.6 xhigh | ready | 待从最新 `main` 创建 | task-specific | 以总计划与任务看板为准 |
| `HUB-220` | Hermes + MiMo-V2.5-Pro | ready | 待从最新 `main` 创建 | task-specific | 以总计划与任务看板为准 |
| `HUB-200` | Codex + GPT-5.6 xhigh | completed | `codex/hub-200-workspace-security` | historical | [HUB-200-codex.md](HUB-200-codex.md) |
| `HUB-130` | Hermes + MiMo-V2.5-Pro | completed | `agent/hermes-context-artifacts` | historical | [HUB-130-hermes.md](HUB-130-hermes.md) |
| `HUB-110` | Codex + GPT-5.6 xhigh | completed | `codex/hub-110-review-fixes` | historical | [HUB-110-codex.md](HUB-110-codex.md) |

HUB-110/HUB-130/HUB-200 的历史简报和交接提示词保留为任务证据，不再用于继续开发。

## 当前执行顺序

1. 从 `main@d9d60ddd` 分别建立 HUB-210 与 HUB-220 的独立分支/worktree。
2. HUB-210 实现 Approval、Capability、Merge、取消线性化和 RecoveryManager。
3. HUB-220 对 HUB-200/210 的路径、租约、审批冲突和崩溃恢复做故障注入；可先覆盖 HUB-200，随后随 HUB-210 接口补齐。
4. 两项均通过交叉审查后执行阶段 2 gate，再把 HUB-300 提升为当前任务。

HUB-200 最终复审无 P0/P1/P2，源提交 `0e82e694` 已通过 `d9d60ddd` 合入
`main`，Linux gate 通过（`3 skipped`）。合并后的远端 CI 仅被新披露的前端
传递依赖公告拦截；当前状态刷新分支已更新锁文件，需合并后重新运行 CI。

历史上的 `agent/*` worktree 基于旧任务提交，只作为任务证据，不能直接用于新开发。新任务必须从最新 `main` 创建 task-specific branch/worktree，并在交付报告中记录实际 base commit。
