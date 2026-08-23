# HUB-300：通用 CLI Agent Runner

## 基本信息

- Owner：OpenCode / MiMo-V2.5-Pro
- Reviewer：Codex / GPT-5.6 xhigh
- 状态：ready，资料刷新合入后启动
- Base：从届时最新 `main` 创建；当前已验证基线为 `dc789dc`
- Branch：`agent/opencode-cli-runner`
- Worktree：`E:\agent_hub_worktrees\opencode-hub-300`
- 依赖：HUB-030、HUB-200，均已完成

## 目标

实现与具体 Agent 无关的 `CliAgentSpec` 和 `CliAgentRunner`，提供固定 argv、
binary 身份复核、JSON Lines 增量解析、有界输出、最小环境以及 timeout/cancel
进程树回收。HUB-300 只建立安全传输层，不实现 OpenCode 专用权限配置、
Planner/Executor Adapter 或 Console 持久化。

## 允许修改

- `adapters/cli/**`（新目录）
- `adapters/__init__.py`
- `tests/adapters/**`（新目录，使用本地无网络 helper CLI）
- 本任务交付记录

如发现必须修改其他 Owner 路径，先提交接口问题，不直接跨界修改。

## 禁止修改

- `protocol/**`
- `workspace/**`、`security/**`、`storage/**`、`workflow/**`
- `app/**`、`web/**`、`console/**`
- `adapters/capabilities/opencode-manifest.json` 的实测结论
- HUB-310 的 OpenCode runtime policy/profile/configure-opencode

## 必须实现

1. `CliAgentSpec` 使用严格 Pydantic 模型；可执行文件必须是规范化绝对路径，argv
   以数组保存，禁止 shell 字符串、空 token、NUL 和未声明占位符。
2. 注册或构造 spec 时记录版本与 SHA-256；每次运行前重新核对文件身份、路径和
   hash，任何漂移都 fail closed。HUB-030 manifest 只作为兼容性输入，不能在运行时
   解析 help 文本猜能力。
3. 使用天然不经 shell 的 `asyncio.create_subprocess_exec(...)`，或等价的
   `Popen(..., shell=False)`；prompt 和 argv 有明确长度上限，用户文本不能改变
   argv 结构。
4. 子进程环境由显式白名单构造，默认不继承 Master/API/CI token、SSH agent、代理
   凭据或用户常规 Agent profile。环境键和值都受数量与长度限制。
5. 并发、增量读取 stdout/stderr，限制单行、单事件、总输出和内存；不得因一侧
   管道阻塞而死锁。
6. JSON Lines 从原始 bytes 增量解码；解析与脱敏分离。未知事件可作为已脱敏的
   bounded event 交给调用方，坏行产生明确错误但不得泄漏原始内容或破坏后续行。
7. timeout、显式取消、输出超限和异常退出均返回稳定 error code，并终止整个进程树。
   Windows 使用新 process group 与 psutil 递归回收；POSIX 使用独立 process group，
   先 TERM、后 KILL，并有界 wait。
8. 传输层返回受限的 `CliRunResult`/事件，不伪造 `ChangeSet`；HUB-310 再将结果映射为
   `AgentResult`，文件变更仍只信任 Master 的 `WorkspaceTransaction`。
9. 不执行真实 OpenCode，不读取真实凭据，不产生模型调用费用。真实 OpenCode smoke
   属于 HUB-310/HUB-610。

## 必须测试

- strict spec、固定 argv、prompt/env/output 上限和非法占位符
- executable/path/hash/version 漂移拒绝
- stdout/stderr 并发读取、碎片化 UTF-8/JSONL、坏行后继续和未知事件
- 未脱敏原始行不进入 callback、日志、异常文本或落盘文件
- timeout、取消、输出超限、非零退出和父/子/孙进程回收
- Windows/POSIX 平台分支；不支持的真实 OS 能力使用精确 skip，并有平台无关单测

HUB-320 将在接口稳定后独立补充 fake CLI 和故障注入矩阵；HUB-300 自身仍需覆盖
实现的核心正负路径。

## 验收

```powershell
E:\agent_hub_graph\.venv\Scripts\python.exe -m pytest tests/adapters -q
E:\agent_hub_graph\.venv\Scripts\python.exe -m ruff check adapters tests/adapters
E:\agent_hub_graph\.venv\Scripts\python.exe -m ruff format --check adapters tests/adapters
E:\agent_hub_graph\.venv\Scripts\python.exe -m pytest
```

- 不修改冻结协议或其他 Owner 文件。
- `git diff --check` 通过，工作区只含任务内文件。
- 单次提交：`feat(HUB-300): add generic cli agent runner`。
- 不自行 push/merge；交付时报告 base、commit、文件、测试、skip 和残余风险。

## 给 OpenCode 的启动指令

先读取本任务书、`agent-hub-development-plan.md` 第 20 节、
`agent-hub-task-allocation.md`、HUB-030 manifest/兼容性报告和现有 Adapter 接口。
先回报实际 base、分支、worktree 与 owned-path 检查，再开始实现。若任务书与冻结协议
或生产代码冲突，立即 fail closed 并报告，不通过放宽安全约束解决。
