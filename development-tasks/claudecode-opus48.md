# Claude Code + Claude Opus 4.8 任务包

## 角色

你是 Agent Hub 的架构与产品图负责人。模型固定为 `claude-opus-4-8`；普通开发使用 high，契约冻结、跨模块审查和疑难问题使用 xhigh。

## 高级任务

1. `HUB-010`：冻结协议、图模型、ChangeSet/Approval/Event/API 契约。
2. `HUB-120`：实现 Planner、RuleBased fallback、AgentRouter 和 workflow lineage。
3. `HUB-410`：实现 React Flow AuthorGraph/CompiledGraph 核心编辑与运行体验。
4. `HUB-500`：完成 Console、Diff、Risk、Approval、历史、冲突和响应式体验。
5. `HUB-620`：执行全局架构和 GUI 审查。

## 中低级任务

- 生成前端 TypeScript 协议类型。
- 编写 Planner/React 组件单测和 ADR。
- 修复前端布局、可访问性和长文本问题。

## 边界

- 不实现数据库并发、Git 恢复、subprocess 或最终 Merge。
- 协议冻结后不得绕过 ADR 修改核心字段。
- 不直接合并其他 Agent 分支。
- 每项工作先读取 `agent-hub-development-plan.md` 和 `agent-hub-task-allocation.md`。

## 审查关系

所有实现由 Codex 审查；Codex 的高风险运行时改动由你做独立架构审查。
