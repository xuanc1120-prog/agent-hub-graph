# 2026-09-20 修正确认与后续 Agent 交接

## 2026-10-21 集成接手状态

- HUB-300 已通过 PR #7 合入 `main@1f1715dfec0df7370a09c0a825f62203dc114c2d`；源 head 为 `eee5241`，实际 base 为 `f274f56`。不要根据下文历史记录重新启动 HUB-300。
- 遗留修复与本资料由当前会话在 `codex/integrate-course-correction` 集成；接手时核对实际 HEAD、PR 和验证记录，未合并前不能把分支当作已发布 `main`。
- 下一项主任务是 HUB-400 最小 API 切片，由用户另开的 Codex 会话实施。当前会话不实现业务 API；HUB-310 可独立推进，不是 HUB-400 的前置条件。
- 本轮增加 spawn/cleanup 边界重复取消回归，确保取消不会中断进程回收，并显式关闭 TestRunner 的 asyncio transport。
- 下文的 2026-09-20 测试计数仅是历史证据，本次实际复验单独记录。

## 授权和当前状态

用户在全局审阅后明确要求：“修正偏移”“把注释的内容修一下”“给其他的 agent 留下修正的确认信息”。本记录确认本轮实际修改，供后续 Agent 接手；**不代表其他 Agent 已复审或确认**。

- 修正起点：`main@f274f56556b419609b9f084031e65a551261a7e3`。
- 原始交付方式：2026-09-20 为工作区改动，未提交、push 或合并；2026-10-21 接手集成状态见上节。接手时先看 `git status --short`、`git diff` 和实际 HEAD。
- 历史审阅：[原始项目健康报告](../../docs/reviews/project-health-2026-09-20.md)。它描述修正前状态，不再把其中 01—03 当作未修复结论。
- 冻结协议 `protocol/**` 未修改，没有引入依赖，也没有重写存储/调度架构。

## 已修正的行为

| 原问题 | 当前行为 | 对应验证 |
|---|---|---|
| TestRunner 返回后遗留后台进程 | Windows 用 CREATE_SUSPENDED 启动，加入 Job 后 resume，所有退出路径终止 Job；POSIX 保留独立进程组并在所有退出路径清理；有界等待进程和管道结束 | 真实子/孙进程：正常、继承管道、非零退出、超时、取消、超限；Windows assignment 失败时测试代码不得执行 |
| 输出超限仍成功 | stdout/stderr 共用原始字节预算，超限终止，`output_limit_exceeded=True`，`passed=False`；测试节点记录 `test_output_limit_exceeded`，不能进入审批 | 预算边界、两路输出合计、运行中超限终止、完整工作流失败和恢复 |
| 10 MiB 配置与 100 万字符模型冲突 | 移除内部结果模型独立字符上限；由 Runner 按配置限制 UTF-8 字节 | 使用应用默认配置运行大于 100 万字符但未超预算的输出 |

`output_truncated` 仅描述显示/保留内容被截断；`output_limit_exceeded` 才是硬预算失败。stdout/stderr 的展示标签不计入原始输出预算。被超限终止的输出只保留固定提示，避免输出被截断时泄漏半个凭据。新增字段属于内部 TestRunResult/测试报告，默认 false，旧报告可读取；不改变冻结协议。

Windows 实现依据：[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[psutil 进程 API](https://psutil.io/api/)。Job 所有权用于生命周期管理，不宣称成为文件系统或网络沙箱；本轮未新增 Master 硬崩溃恢复承诺。

## 已确认的验证

1. Runner 专项：**46 passed、1 skipped**，Windows 上跳过 POSIX symlink 用例。覆盖上述真实进程行为和输出限制；用现有 `.venv`，未安装依赖或调用真实 Agent。
2. 工作流：`tests/workflow/test_hub200_command_runtime.py` 和 `test_hub200_test_side_effect.py`，**4 passed**。包括正常进入审批、超限拒绝审批、文件副作用恢复、Git objects 污染拒绝。
3. 集成验证需要脱离 Codex 文件系统沙箱运行，因为 ArtifactStore 的私有目录 ACL 会与沙箱身份冲突。未修改源码目录 ACL；专用数据在 `.tmp/fix-20260920-integration`。已有 cache 目录出现权限警告，不影响 4 项业务断言。后续最好为沙箱外验证使用新的独立 cache_dir。

完整 CI、Linux 真机和 HUB-300 全分支审查不是本轮已完成项；不写成已验证。后续定向回归和静态检查的最终结果见下方收尾记录。

## 方向和验收修正

当前优先级是可操作产品闭环：测试仓库 → 简单图 → 编辑/确认 → 执行 → diff → 批准/拒绝。先 Mock 接通 API/UI，再接一个真实 Adapter。取消“API 必须等整个阶段 3”硬排队；不强制多 Agent 并行。

WebSocket 完整续传、所有操作面板、移动端精修等保留路线图，不阻塞首次闭环。鉴权、工作区隔离、精确恢复、审批绑定、凭据边界、CAS/租约不得删减。未实现的危险入口不开放。

验收按风险分层，详见 [当前看板](../../agent-hub-task-allocation.md)。CI 中 Ruff/format 只在 Python 3.11 跑一次；兼容性测试矩阵和可能受环境标记影响的依赖审计保留。普通开发直接用相关 pytest/lint，不每次重建环境。没有批量删测试或另造一套门禁框架。

## HUB-300 与下一步（2026-09-20 历史记录）

- `agent/opencode-cli-runner@b8f3158`，base `f274f56`；worktree：`E:/agent_hub_worktrees/opencode-hub-300`。本轮未修改它，也未把它标记为已验收。
- 后续 Runner Owner/Reviewer 复用现有工作，核对 Job 绑定时机、父进程先退出、管道继承和共享输出预算；不要把本轮 TestRunner 测试结果当作 CLI Runner 的通过证明。
- HUB-320 按差异补测，不复制一套同义矩阵；HUB-310/330 仍负责真实 Adapter 的权限与凭据边界。
- HUB-400 最小 API 切片 ready，基于包含本轮修正的集成基线启动；HUB-410 随实际 API 接最小图编辑、diff 和审批入口。状态可先轮询，未做好 ticket/Origin 控制时不开放 WebSocket。

## 接手确认方式

下一个 Agent 在交付记录中简要说明：实际 HEAD/base、是否包含本轮修正、承接的下一切片、需要补验证的具体缺口。不要虚构“已获其他 Agent 确认”，不要重新启动已关闭的 HUB-200/210/220 历史审查。

本轮未触碰原有未跟踪项 `tmpq8eohys6`。临时测试数据保留于忽略的 `.tmp/fix-20260920-*`，不属于交付源码。

## 最终收尾记录

- 最终 Runner/CommandGuard/HUB-220 Runner 故障注入/OpenAPI 定向回归：**80 passed、1 skipped，20.27 秒**。与前述 4 项工作流集成合计 **84 passed、1 skipped**；早期 46 项是子集，不重复计数。
- 本轮 7 个修改/新增 Python 文件的 Ruff check、format check 通过；`git diff --check` 通过。
- 最终定向回归使用新的 `.tmp/fix-20260920-final` 和 `.tmp/fix-20260920-final-cache`，没有缓存权限警告。
- 本轮未执行全量 CI，不声称 Linux 真机复验通过。GitHub CI 保留 Linux Python 3.11/3.12/3.13 回归；Windows 进程行为已在当前机器验证。
- 接手入口为仓库根目录 `AGENTS.md`；README、开发方案、任务看板、当前波次和 HUB-300/400 简报已同步，原始审阅报告已标记修正后状态。

最终定向回归命令（复用既有环境）：

```powershell
.venv\Scripts\python.exe -m pytest tests/security/test_command_guard.py tests/security/test_test_runner.py tests/security/test_test_runner_cancellation.py tests/security/test_test_runner_lifecycle.py tests/security/test_hub220_fault_injection.py::TestTestRunnerFaultInjection tests/test_openapi_draft.py -q -o addopts='' --basetemp .tmp/fix-20260920-final -o cache_dir=.tmp/fix-20260920-final-cache
.venv\Scripts\python.exe -m pytest tests/workflow/test_hub200_command_runtime.py tests/workflow/test_hub200_test_side_effect.py -q -o addopts='' --basetemp .tmp/fix-20260920-integration -o cache_dir=.tmp/fix-20260920-integration-cache
```

第二条展示同一组集成复验的推荐命令，使用独立缓存避免本轮遇到的 warning。重跑前选择新的 basetemp/cache_dir，尤其不要跨沙箱身份复用目录。
