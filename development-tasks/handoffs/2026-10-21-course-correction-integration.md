# 2026-10-21 遗留修复与纠偏资料集成交接

## 实际基线与范围

- Repository：`E:/agent_hub_graph`。
- Branch：`codex/integrate-course-correction`。
- PR base：`main@1f1715dfec0df7370a09c0a825f62203dc114c2d`，已包含 HUB-300 PR #7。
- 修复提交：`9b4186a`；与 main 的组合提交：`dbdee923a110127f2778740eca23f7d06d47ee9e`。最终文档提交以 `git log`/PR head 为准。
- 交付内容：收拢 2026-09-20 未提交的 TestRunner 修复、回归与纠偏资料，更新 HUB-300 已合并状态和 HUB-400 优先级。
- 未修改 `protocol/**`、`adapters/**` 或业务 API；未实现 HUB-400。
- 原有未跟踪文件 `tmpq8eohys6` 保留，不提交、不删除。测试数据在忽略的 `.tmp/correction-20261021-*` 与 `.tmp/ci-20261021*`。

## 行为变化

- TestRunner 在 Windows 先 suspended spawn，加入 Job 后才 resume；正常、失败、超时、取消、输出超限均统一回收进程树。
- stdout/stderr 共享原始字节预算；超限终止、`passed=False`，测试节点记录 `test_output_limit_exceeded`，不能进入审批。
- 结果输出按配置的 UTF-8 字节预算限制，支持默认 10 MiB 范围内超过 100 万字符的日志；超限输出只保留固定提示。
- spawn/cleanup 边界的重复取消不会取消内部清理任务；清理结束后传播 `CancelledError`，同时显式关闭 asyncio transport。
- Ruff 在 Python 3.11 job 执行一次，完整 Python 兼容矩阵与各环境依赖审计保留。

## 本会话独立验证

- 修补前定向基线：`80 passed, 1 skipped`。
- 修补后安全、Runner、故障注入与 OpenAPI 定向：`82 passed, 1 skipped`。
- 两个新增的重复取消用例分别覆盖 spawn、cleanup 边界；检查根进程已退出及 transport 已关闭。
- Mock 工作流：`4 passed`，覆盖正常审批、输出超限失败、工作区副作用恢复、Git objects 污染拒绝。
- 全仓 Ruff check/format、compileall、`git diff --check`、`pip check`：通过。
- 前端 lint、Vitest `7 passed`、production build：通过。
- 组合提交的全量 Windows pytest：执行中，最终结果在本节追加；本次 PR CI 与独立复审尚待完成，不引用历史绿灯代替。
- 沙箱内工作流首次运行在创建 ArtifactStore 私有目录时出现 `WinError 5`；沙箱外使用新 basetemp/cache_dir 后上述 4 项业务断言全部通过，未修改源码或数据目录 ACL 规则。

## HUB-400 新会话接手

1. 当前主任务优先 HUB-400 最小 API 切片，由用户另开的 Codex 会话实施；本会话只完成本次集成。
2. 优先从包含本 PR 的最新 `main` 创建 `codex/hub-400-api` 和独立 worktree，记录完整 base SHA。若先做准备，集成前须同步本次安全修复，不能把未合并分支写成已发布基线。
3. 先接 Mock：Session、plan、workflow read/update/validate/run、状态查询、diff/artifact、批准/拒绝。复用已有 Service、持久化幂等、CAS、lease/fencing 和 scheduler。
4. 鉴权、请求体限制、同源与 artifact 所有权验证随已开放端点一起交付；状态先轮询，未完成 ticket/Origin 验证前不开放 WebSocket。
5. HUB-310 由 OpenCode 独立推进，HUB-320 由 Hermes 按缺口补测；两者不是 HUB-400 的前置依赖。HUB-410 在实际可用 API 上接最小 GUI。

## 待独立复审及残余范围

- 由独立 Reviewer 核对 Job assignment 失败、父进程先退出、共享输出预算、重复取消和工作流审批阻断；本会话的本地验证不代替独立复审。
- HUB-300 的 `proc.transport.close()` P2 留给 Runner Owner，记录于 HUB-300 简报，本次不跨改 Owner 文件。
- POSIX 主动 `setsid()` 逃逸与真实 Agent 宿主隔离属于 HUB-330；不将 Job/process group 当作 OS sandbox，也未新增 Master 硬崩溃恢复承诺。
