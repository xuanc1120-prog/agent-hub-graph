# Next Wave Execution

## Baseline

- Protocol tag: `contracts-frozen-v1`
- Preparation base: use the pre-created worktree HEAD and record `git rev-parse HEAD`
- Integration branch: `main`
- Agents do not merge or push

## Parallel Start

| Task | Agent | Branch | Worktree |
|---|---|---|---|
| HUB-020 | Hermes + MiMo-V2.5-Pro | `agent/hermes-qa-support` | `E:\agent_hub_worktrees\hermes-qa-support` |
| HUB-030 | OpenCode + MiMo-V2.5-Pro | `agent/opencode-adapter` | `E:\agent_hub_worktrees\opencode-adapter` |
| HUB-100 | Codex + GPT-5.6 xhigh | `agent/codex-runtime-security` | `E:\agent_hub_worktrees\codex-runtime-security` |
| HUB-120 | Claude Code + Claude Opus 4.8 | `agent/claude-architecture-ui` | `E:\agent_hub_worktrees\claude-architecture-ui` |

Each Agent receives only its matching brief. Before editing it must confirm the branch, clean worktree, base commit, owned paths and frozen tag.

## Integration Order

1. Review and merge HUB-020/HUB-030 independently.
2. Claude Code reviews HUB-100 concurrency and lease behavior; Codex merges it.
3. Start HUB-130 from the merged HUB-100 base.
4. Codex reviews and merges HUB-120/HUB-130.
5. Start HUB-110 and run the stage 1 CLI/Mock vertical-slice gate.

Do not start HUB-110 or HUB-130 from an older base. Do not modify `protocol/` after the freeze tag without a new accepted ADR.
