# Claude Code 交接提示词：HUB-110 二次复审

你现在负责 Agent Hub 的 `HUB-110` 独立二次复审。只审查，不修改代码、不 merge、不 push。

## Git 范围

- 待审分支：`codex/hub-110-review-fixes`
- 首次交付提交：`35711ee45180cd98620dfb049308ccf75965d4f6`
- 完整 HUB-110 基线：`d11196a06480a441ac7af3d43df146170b4385dd`
- 先确认工作区 clean、待审分支 HEAD 正确，并分别检查：
  - 修复差异：`35711ee..HEAD`
  - 完整任务差异：`d11196a..HEAD`
- `protocol/**` 不应发生变化。

## 必须复核的首次审查问题

1. `ExecutableValidator` 必须把 command test 一一绑定到
   `effective_allowed_commands`，拒绝未知、缺失、重复或改序的测试命令；
   风险下限必须等于 compiler 规则推导值，系统规则 ID 和完整安全链必须封印。
2. Agent 的 `available`、`auto_assignable`、`unavailable_reason` 必须可持久化并
   原样恢复；v1 数据迁移必须使用保守默认值。
3. `IfOperator.IN` 的 operand permutation 和重复成员必须产生相同的 author hash
   与 compiled hash。
4. `show-events` 必须跨越 500 条分页边界完整回放，保证严格顺序、无重复、
   无遗漏并包含最终 terminal event。
5. handler 返回的 artifact 必须绑定当前 AgentTask；同 session 的跨 task
   artifact、planner artifact 和非 AgentTask artifact ref 必须拒绝。
6. 不安全且未使用的 `start_task` 入口必须已删除，task 启动只能走带
   runtime-policy 校验和原子 ownership claim 的路径。
7. bundle 首次清理失败后必须在 `finally` 重试，且不能覆盖原始执行结果。
8. planner lineage 必须从 planner run 持久化到 workflow run record、
   `workflow.run_created` event、`run-workflow` 和 `show-run` CLI 输出。

## 重点回归

- 审查 migration v2 在 fresh DB、v1 DB、重复初始化和 future schema 下的行为。
- 尝试篡改 compiled graph 的 argv、risk floor、rule ID、test chain 和 docs/command
  互斥关系，确认 validator fail closed。
- 注入恶意 handler 返回另一 task 的 artifact，确认得到
  `handler_artifact_invalid`。
- 注入一次 bundle cleanup failure，确认发生重试且最终无 bundle 残留。
- 生成超过 500 条 run/session events，确认 service/CLI 完整回放。
- 从 `plan -> workflow -> run -> show-run/show-events` 重建 planner lineage。

## 验证命令

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m pytest
Set-Location web/frontend
npm.cmd run lint
npm.cmd run test
npm.cmd run build
$env:CI = "true"
npx.cmd playwright test --project=smoke
```

Codex 环境中 Playwright smoke 断言已通过，但 runner 卡在
`Terminating the WebServer`，未能独立确认 teardown 返回码。请在你的环境或 CI
中特别确认测试进程能够正常退出且没有遗留 `vite preview` 子进程。

## 输出格式

先列 findings，按 P0/P1/P2 排序，并附文件与行号、触发步骤、影响和建议修复。
随后给出测试结果、是否存在环境限制、残余风险和最终结论：

- `批准合并`
- `拒绝合并`

没有 P0/P1 时必须明确写出“无阻塞问题”。不要以已有交付报告代替独立复现。
