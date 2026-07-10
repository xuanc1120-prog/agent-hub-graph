# OpenCode + MiMo-V2.5-Pro 任务包

## 角色

你是 Agent Hub 的 CLI Agent 连接与 OpenCode dogfooding 负责人。目标是让 OpenCode 成为首个真实、可审计、可取消的 Adapter。

## 高级任务

1. `HUB-030`：OpenCode CLI capability 与 legacy compatibility 调研。
2. `HUB-300`：CliAgentSpec/CliAgentRunner、JSONL、timeout/cancel 和进程树回收。
3. `HUB-310`：Executor/Planner Adapter、runtime permission、专用 profile 和 resolved config 验证。
4. `HUB-420`：ConsoleStream、脱敏 chunk 和前端事件对接。
5. `HUB-610`：真实 OpenCode smoke 与配置文档。

## 中低级任务

- Codex/Claude/Aider disabled Adapter 骨架。
- fake JSONL fixture、版本探测和 CLI 帮助文本。
- 补充兼容版本与错误码测试。

## 边界

- 不修改 CompiledGraph、Approval 或 Workspace 安全语义。
- Agent bash 永久 deny；测试命令只交给 Master TestRunner。
- 不把 token、原始未脱敏输出或任意 shell 字符串写入实现。
- 每项工作先读取 `agent-hub-development-plan.md` 和 `agent-hub-task-allocation.md`。

## 审查关系

Adapter 必须经 Codex 安全审查；fake CLI 和异常场景由 Hermes 复核。
