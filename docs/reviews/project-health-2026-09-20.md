# 项目接手审阅：方向、交接、验收与测试

审阅日期：2026-09-20。主分支：`main@f274f56`。

**后续状态：** 用户已授权修正。问题 01—03 已在当前工作区修复并完成定向验证，交接状态、交付顺序与验收分层也已调整。实际范围、验证限制及待办以 [修正确认](../../development-tasks/handoffs/2026-09-20-course-correction.md) 为准。下文保留修正前的审阅证据，不作为当前未修复清单。

## 结论

**项目存在中度的投入重心偏移：基础设施、安全门禁和交接程序走在产品验证前面；没有足够证据认定代码已全面失控，或存在大量毫无意义的测试。** 核心架构仍围绕本地 Agent 工作流，未发现偏向远程集群、多租户等无关方向。真实 Adapter 和 GUI 尚未完成是文档承认的开发状态，不能直接算缺陷。

本轮发现更值得修的是：**现有测试能够全部通过，但部分测试与方案要求不一致，部分测试名称超出了实际验证范围。** 不建议再追加一轮泛化的“全面硬化”来解决；应先修少量明确问题，再交付可操作的最小闭环。

## 范围与成本控制

- 阅读项目定位、任务看板、当前/历史任务交接材料、CI、主要模块结构；重点抽查 TestRunner、测试节点、服务装配、存储边界和前端基线。
- 主分支为主要证据；另外只检查本地 `agent/opencode-cli-runner@b8f3158` 的提交、改动规模、接口与结构，不视为已合入或已完整审查。
- 没有启动其他 Agent、调用真实模型、安装依赖、获取远端状态、跑全量 CI，或修改业务代码。
- 这是仓库全局盘点加关键路径抽查，**不是逐行审计，也不是“无漏洞”认证**。不评估当前外部依赖漏洞库。
- 按 Git 跟踪文件统计：生产/工具 Python、TS、TSX 共约 30,600 行；测试约 16,133 行，Python 测试函数 535 个（参数化实例数更多）。数量本身不能证明过度测试。

## 已核实的问题

### 01｜优先修：TestRunner 正常退出后可能遗留子进程

位置：[security/test_runner.py:117](../../security/test_runner.py#L117)、[workflow/handlers/guards.py:438](../../workflow/handlers/guards.py#L438)。

证据：`_terminate_process_tree` 仅在超时/取消分支调用；普通退出后直接收集输出、返回结果。用本地无网络探针替换 executable 解析，启动一个输出子进程 PID 后正常退出的 Python 父进程；子进程只 sleep，标准流重定向。实际结果：`passed=True`，子进程仍存活。探针随后主动终止并回收了自己创建的子进程。

影响：测试返回以后后台工作仍可能继续，越过测试节点结束、工作区核验与清理的时间边界。即使仓库受信任，普通测试启动的后台服务也可能触发。当前已证明的是进程遗留，不声称已证明任意越权写入。

建议：统一所有退出路径的进程回收，在确认进程树结束后才进入后续工作区核验。补一个真实父子进程生命周期测试即可，暂不扩展成新的通用进程框架。

### 02｜优先统一要求：输出超限仍通过，测试反而固定了这一行为

位置：[security/test_runner.py:35](../../security/test_runner.py#L35)、[security/test_runner.py:48](../../security/test_runner.py#L48)、[workflow/handlers/guards.py:474](../../workflow/handlers/guards.py#L474)、[tests/security/test_test_runner.py:44](../../tests/security/test_test_runner.py#L44)。

`passed` 只检查退出码与超时；collector 满后继续读取并丢弃，测试节点也不检查 `output_truncated`。现有单测同时断言 `passed` 和 `output_truncated` 为真。

本地探针输出 70,000 个字符，配置输出上限 64 字节，实际得到 `passed=True, truncated=True`。这与[方案第 16 节](../../agent-hub-development-plan.md#L1270)要求输出超限终止并阻断不一致。

影响：资源门槛没有按既定规则执行，但这不是无限内存缓存漏洞——collector 已限制保留数据。必须先明确“总输出预算”和“显示截断”的不同语义：如果前者是硬限制，应终止并失败；如果只是预览上限，应修正文档和命名。不要以当前测试绿灯作为两者已一致的证据。

### 03｜普通正确性缺陷：输出上限在跨模块交接中不一致

位置：[app/config.py:54](../../app/config.py#L54)、[app/services.py:588](../../app/services.py#L588)、[security/test_runner.py:30](../../security/test_runner.py#L30)。

应用默认把 `10 * 1024 * 1024` 字节传给 TestRunner，但 `TestRunResult.output` 限制为 1,000,000 个字符。ASCII 输出介于这两个上限之间时，可以通过收集边界，却在构造结果时抛出 ValidationError。直接构造 1,000,001 字符结果已复现 `string_too_long`；未额外运行大型输出的完整工作流。

影响：本应按统一预算处理的日志被转换为测试执行错误。不是攻击者提权漏洞。建议统一上限与计量单位，补一个装配后的配置边界检查。

## 交接与方向问题

### 04｜看板与本地事实不同步，但没有发现当前 Runner 基线混用

[看板](../../agent-hub-task-allocation.md#L32)和[波次说明](../../development-tasks/next-wave/README.md#L8)仍写 HUB-300 待启动、worktree 待创建；实际本地 worktree `E:/agent_hub_worktrees/opencode-hub-300` 已存在，工作区干净，提交为 `b8f3158`，比 main 多一个提交。其 merge-base 正是 `f274f56`，所以本次**未发现该分支基于过期主线开发**。

建议把状态改成符合实际证据的“已有实现、待确认审查/集成状态”，记录 base/head 和下一步，不擅自视为已验收。文件所有权还指向不存在的 `console/approval_manager.py`，实现已在 `workflow/approval_manager.py`，应一并同步。旧 worktree 可保留证据，不必为了整洁立即删除。

### 05｜偏移主要体现在交付顺序，而非所有安全设计都多余

[当前看板](../../agent-hub-task-allocation.md#L33)要求 HUB-400 等待 `300 -> 320 -> 310 -> 330`，尽管 API 依赖已满足；[原方案](../../agent-hub-development-plan.md#L3135)和[优先级](../../agent-hub-development-plan.md#L3728)则允许 GUI 数据闭环与真实 Agent 接入后半段并行。现状是 HTTP 只有 healthz，UI 仍是 HUB-000 静态占位页。

这不证明此前串行排队决定错误：人员、额度和并行协调成本可能正是当时原因。但它让用户价值验证继续后移，容易形成“先完善所有防线，再第一次使用产品”的循环。

建议下一阶段缩成一条可演示链：选择本地测试仓库 → 生成/编辑简单图 → 执行一个任务 → 查看 diff → 批准或拒绝。可先接 Mock API/UI 验证交互，再接一个真实 Adapter；不要求现在同时开发，也不绕过真实执行必要的权限审查。WebSocket 完整续传、全量操作面板、移动端精修可以排在首次闭环之后。

### 06｜已有局部复杂度集中，不值得现在大重构

`WorkflowRunRepository` 3,008 行，`ApprovalRepository` 2,242 行，`ChangeSetRepository` 1,583 行，承担了大量事务和业务状态转换。它们是未来维护热点，但行数不足以证明抽象错误；把必须原子提交的逻辑随意拆散反而可能增加风险。

还存在三处相关的子进程实现：capability 探测、TestRunner、待合入 CliAgentRunner。职责不同，有理由独立；目前生命周期与输出策略已出现差异。建议先对齐少数共同不变量，不急于抽成“统一万能执行框架”。

应保留：源码仓库与集成工作区分离、精确路径恢复、审批绑定实际 patch、单写租约/CAS、命令白名单、凭据隔离。这些直接保护用户文件，不属于因为“开发阶段”就可以取消的过度设计。

## 验收与测试评价

### 07｜门槛有重复成本，但没有“每项标准都过严”的证据

- [CI](../../.github/workflows/ci.yml#L14)在 Python 3.11/3.12/3.13 的每个 job 中重复跑 Ruff 和依赖审计；同时没有 Windows job，而实现包含不少 Windows 专用路径/进程分支。建议常规检查集中一次，主 Python 版本加针对性的 Windows 检查优先；若承诺多个 Python 版本，兼容矩阵仍可保留或安排在合并/发布时。审计的环境标记差异仍需考虑，不能简单认定全部重复。
- [本地 CI 脚本](../../scripts/ci.ps1#L21)每次重建 `.venv`、安装前后端依赖并跑完整链。适合最终复验，不适合每个小改动；增加不重建环境的快速入口更省额度和时间。
- [协作规则](../../agent-hub-task-allocation.md#L21)要求每个任务一个提交，[跨 Owner 修改](../../agent-hub-task-allocation.md#L120)必须拆两个任务。对协议或高风险并发修改有帮助，对小型接口适配和文档修正则偏重。建议按变更风险选择流程，而不是为所有修改统一增加交接次数。
- 历史“无 P0/P1/P2”只能代表当次审阅结果，不是未来每个开发任务都必须零缺陷的通用门槛；本轮没有把它推定成项目已明确规定的统一标准。

### 08｜有少量低价值测试，更关键的是遗漏真实行为

- [Vitest](../../web/frontend/src/App.test.tsx#L7)与[Playwright](../../web/frontend/e2e/smoke.spec.ts#L3)都只断言 Workflow、Mock Agent、HUB-000 三处文字。初建脚手架时有用，后续不应为保留这些文字阻碍产品更新。保留浏览器可启动检查，把组件检查随功能替换成真实交互。
- [OpenAPI 测试](../../tests/test_openapi_draft.py#L51)单独验证 JSON 可解析、固定文件位置，边际价值较低；生成契约一致性和核心 DTO 覆盖测试则应保留。
- `test_runner_times_out_and_reaps_process_tree` 实际只启动 sleep 测试，断言超时和失败，**没有构造子孙进程并检查它们消失**。取消测试只核对父 PID。测试名给出的保证强于实际断言，关联问题见 01。
- 输出超限测试锁定了与方案相反的结果，关联问题见 02。这比单纯测试数量偏多更值得优先处理。
- 路径逃逸、审批 CAS、取消/合并竞争、事务恢复、跨 session artifact 所有权等测试有清晰的失败代价，应保留。当前证据不支持批量删除安全回归测试。

## 本次验证记录

- 四个定向测试文件：`test_command_guard.py`、`test_test_runner.py`、`test_test_runner_cancellation.py`、`test_openapi_draft.py`：共收集 65 项，64 通过、1 个 POSIX symlink 用例在 Windows 跳过。
- 两个无网络 TestRunner 行为探针复现了进程遗留与输出超限仍成功；模型构造探针复现上限不一致。没有执行真实 Agent，也没有遗留探针子进程。
- 额外尝试 `test_hub200_command_runtime.py`：在 `application.initialize()` 创建临时 `artifacts/logs` 目录时遇到 `WinError 5`，尚未进入业务流程。原因未定位，不将其算作业务回归，也不宣称集成复验通过。
- 首次使用 `-p no:cacheprovider` 与项目 strict-config/cache_dir 配置冲突，是本次验证命令问题；恢复插件并指定独立 cache_dir 后通过。
- `git diff --check` 通过。原有未跟踪项 `tmpq8eohys6` 未触碰。新增本报告；探针和测试临时数据位于忽略的 `.tmp/review-20260920-*` 路径。

## 建议的接手顺序

1. 同步 HUB-300 实际交接状态和 Owner 路径，避免重复开工；不重新打开已关闭的历史审查任务。
2. 小步修复问题 01—03，只增加直接复现这些问题的必要回归；明确输出预算语义。
3. 把下一里程碑改成上述最小可操作闭环，其他增强项保留在待办，不以全部实现作为第一次体验产品的前提。
4. 日常验收按改动范围运行；涉及文件写入、审批、凭据、进程生命周期的变更保留专项负向测试，最终集成再跑完整回归。

不建议推倒重写、大规模删测试、继续扩大规范篇幅，或为了证明安全而无边界增加故障注入组合。
